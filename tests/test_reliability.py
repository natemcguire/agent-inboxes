"""Independent regressions for coordination failures and safe recovery."""
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.identity import derive_session
from agent_inbox.models import ConflictError, NotFoundError, InboxError
from agent_inbox.recovery import merge_databases, RecoveryError
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService


class ReliabilityTests(unittest.TestCase):
    def test_session_survives_tool_shells(self):
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'stable-runtime'}, clear=True), patch('os.getppid', side_effect=[100,200]):
            self.assertEqual(derive_session(), derive_session())
        with patch.dict(os.environ, {'CLAUDE_PID': str(os.getpid())}, clear=True), patch('os.getppid', side_effect=[100,200]):
            self.assertEqual(derive_session(), derive_session())

    def test_announcements_future_inbox_ack_scope_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = get_connection(Path(tmp)/'db')
            try:
                svc = InboxService(conn)
                result = svc.post_announcement('sender@alpha','Topic','Body','key')
                self.assertEqual(result, svc.post_announcement('sender@alpha','Topic','Body','key'))
                with self.assertRaises(ConflictError):
                    svc.post_announcement('sender@alpha','Topic','Changed','key')
                self.assertEqual(svc.list_announcements('late@alpha',True)[0]['id'],result['id'])
                self.assertEqual(svc.list_announcements('late@beta'),[])
                with self.assertRaises(NotFoundError):
                    svc.acknowledge_announcement(result['id'],'late@beta')
                svc.acknowledge_announcement(result['id'],'late@alpha')
                self.assertEqual(svc.list_announcements('late@alpha',True),[])
                self.assertEqual(len(svc.list_announcements('other@alpha',True)),1)
                svc.post_announcement('sender@alpha','Global','Body','global',True)
                self.assertEqual(len(svc.list_announcements('future@beta',True)),1)
            finally:
                conn.close()

    def test_http_closes_connections_without_gc_and_preserves_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = get_connection(Path(tmp)/'db')
            server = AgentInboxServer(('127.0.0.1',0),conn)
            opened = []
            real_connect = get_connection
            def capture(path):
                db = real_connect(path)
                opened.append(db)  # Strong references defeat GC-based cleanup.
                return db
            thread = threading.Thread(target=server.serve_forever)
            client = InboxClient(f'http://127.0.0.1:{server.server_port}')
            thread.start()
            try:
                with patch('agent_inbox.server.get_connection', side_effect=capture):
                    for _ in range(30):
                        self.assertEqual(client.healthz()['db'],'ok')
                    ann = client.post_announcement('sender@p','Topic','Body',client_token='http-key')
                    self.assertEqual(client.list_announcements('late@p')[0]['id'],ann['id'])
                    client.acknowledge_announcement(ann['id'],'late@p')
                    self.assertEqual(client.list_announcements('late@p',unread=True),[])
                    with patch('agent_inbox.service.InboxService.list_announcements', side_effect=RuntimeError('injected failure')):
                        with self.assertRaises(InboxError):
                            client.list_announcements('late@p')
                    client.acquire_reservations('p', [f'file{i}.py' for i in range(60)], 'owner@p')
                    client.release_reservations('p', 'owner@p', release_all=True)
                    client.acquire_reservations('p', ['active.py'], 'owner@p')
                    entries = client.list_reservations_everything()
                    self.assertEqual(len(entries), 61)
                    self.assertEqual(sum(row['active'] for row in entries), 1)
                    sent = client.send_email('sender@p',['owner@p'],['observer@p'],'Work','You are the owner')
                    view = client.get_thread('observer@p',sent['thread_id'])
                    self.assertEqual(view['reading_as'],'observer@p')
                    self.assertEqual(view['emails'][0]['your_role'],'cc')
                    self.assertEqual(client.list_threads('observer@p',unread=True)[0]['your_roles'],['cc'])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
                conn.close()
            # Request threads close immediately; join them explicitly for assertions.
            import time
            deadline = time.monotonic()+2
            for db in opened:
                while True:
                    try:
                        db.execute('SELECT 1')
                    except sqlite3.ProgrammingError:
                        break
                    if time.monotonic()>deadline:
                        self.fail('Request connection was left open')
                    time.sleep(.01)

    def test_service_control_targets_owned_job_and_checks_pid(self):
        import plistlib
        from types import SimpleNamespace
        from agent_inbox import operations
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp)/'service.plist'
            plist.write_bytes(plistlib.dumps({'Label': 'com.nate.agent-inbox',
                'ProgramArguments': ['/usr/bin/python3','-m','agent_inbox.cli','serve']}))
            calls = []
            def launchctl(args, **kwargs):
                calls.append(args)
                # First observation is unloaded; readiness observes the new owned PID.
                if args[1] == 'print':
                    count = sum(c[1] == 'print' for c in calls)
                    return SimpleNamespace(returncode=1 if count == 1 else 0, stdout=' pid = 123\n', stderr='')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            with patch.object(operations,'LAUNCH_AGENT_PLIST',plist), patch.object(operations.sys,'platform','darwin'), \
                    patch.object(operations.subprocess,'run',side_effect=launchctl), \
                    patch.object(InboxClient,'healthz',return_value={'service':'agent-inboxes','pid':123,'db':'ok'}):
                self.assertEqual(operations.control('start'),0)
                self.assertEqual(calls[1][1], 'bootstrap')
                self.assertTrue(all(c[0] == 'launchctl' for c in calls))
                with patch.object(InboxClient,'healthz',return_value={'service':'agent-inboxes','pid':999,'db':'ok'}):
                    self.assertEqual(operations.control('status'),1)

    def test_recovery_remaps_ids_and_rejects_conflicts_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,out = [Path(tmp)/name for name in ('a.db','b.db','out.db')]
            left,right = get_connection(a),get_connection(b)
            try:
                InboxService(left).send_email('alice@one',['bob@one'],[],'One','Left','left-token')
                msg = InboxService(right).send_email('alice@two',['bob@two'],[],'Two','Right','right-token')
                InboxService(right).acquire_reservations('two',['src/'],'alice@two','runtime',client_token='reservation-token')
                merge_databases([a,b],out)
                merged = get_connection(out)
                try:
                    view = InboxService(merged).get_thread('bob@two',msg['thread_id'])
                    self.assertEqual(view['emails'][0]['from'],'alice@two')
                    self.assertEqual(merged.execute('SELECT COUNT(*) FROM emails').fetchone()[0],2)
                    self.assertEqual(merged.execute('PRAGMA foreign_key_check').fetchall(),[])
                    history = InboxService(merged).reservation_history()
                    self.assertEqual(history[0]['holder'], 'alice@two')
                    self.assertFalse(history[0]['active'])
                    self.assertEqual(merged.execute('SELECT released_by FROM reservations').fetchone()[0], 'recovery')
                finally:
                    merged.close()
                with self.assertRaises(RecoveryError):
                    merge_databases([a,b],out)
                # Same opaque ID with changed content must abort publication.
                changed = Path(tmp)/'changed.db'
                clone = sqlite3.connect(changed)
                right.backup(clone)
                clone.execute("UPDATE emails SET body_markdown='tampered'")
                clone.commit()
                clone.close()
                rejected = Path(tmp)/'rejected.db'
                with self.assertRaises(RecoveryError):
                    merge_databases([b,changed],rejected)
                self.assertFalse(rejected.exists())
                self.assertEqual(right.execute('SELECT body_markdown FROM emails').fetchone()[0],'Right')
                right.execute("UPDATE cloud_state SET endpoint='https://example.invalid',user_id='user' WHERE id=1")
                with self.assertRaises(RecoveryError):
                    merge_databases([a,b],rejected)
                self.assertFalse(rejected.exists())
            finally:
                left.close()
                right.close()
