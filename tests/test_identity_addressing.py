"""Regression scenarios from the identity/project-mail review notes."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from agent_inbox.cli import main
from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.identity import derive_project, derive_session
from agent_inbox.models import ConflictError, NotFoundError, ValidationError
from agent_inbox.project_registry import lookup_project, register_project
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService


class NotesRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            'AGENT_INBOX_DIR': str(self.root), 'AGENT_INBOX_DB': str(self.root / 'test.db'),
            'AGENT_INBOX_CLOUD_CONFIG': str(self.root / 'cloud.json'),
            'AGENT_INBOX_PROJECT': 'demo', 'AGENT_INBOX_AGENT': '',
            'AGENT_INBOX_SESSION': 'session-one'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = get_connection()
        self.addCleanup(self.conn.close)
        self.svc = InboxService(self.conn)

    def register(self, *addresses):
        for address in addresses:
            self.svc.ensure_inbox(address)

    def start_http(self):
        server = AgentInboxServer(('127.0.0.1', 0), self.conn)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = 'http://127.0.0.1:' + str(server.server_port)
        os.environ['AGENT_INBOX_URL'] = self.url
        return InboxClient(self.url, session_id='session-one')

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def age_leases(self):
        self.conn.execute("UPDATE agent_leases SET last_seen='2000-01-01T00:00:00.000Z'")

    def test_session_recovers_own_name_instead_of_lowest_free_slot(self):
        self.svc.claim_agent('claude', 'demo', 'someone-else')
        mine = self.svc.claim_agent('claude', 'demo', 'session-one')
        self.age_leases()
        self.assertEqual(self.svc.lookup_agent_by_session('demo', 'session-one')['address'], mine['address'])
        self.assertEqual(self.svc.claim_agent('claude', 'demo', 'session-one')['agent'], 'claude-2')

    def test_live_process_protects_expired_lease_and_pid_reuse_does_not(self):
        with mock.patch('agent_inbox.leases.process_started_at', return_value='birth-one'):
            self.svc.claim_agent('claude', 'demo', 'live', 123)
            self.age_leases()
            self.assertEqual(self.svc.claim_agent('claude', 'demo', 'next')['agent'], 'claude-2')
        self.age_leases()
        with mock.patch('agent_inbox.leases.process_started_at', return_value='birth-two'):
            self.assertEqual(self.svc.claim_agent('claude', 'demo', 'third')['agent'], 'claude')
        self.assertIsNone(self.svc.lookup_agent_by_session('demo', 'live'))

    def test_stale_session_cannot_refresh_or_release_reassigned_name(self):
        self.svc.claim_agent('claude', 'demo', 'old')
        self.age_leases()
        self.svc.claim_agent('claude', 'demo', 'new')
        self.age_leases()
        self.svc.touch_lease('claude', 'demo', 'old')
        self.assertTrue(self.conn.execute('SELECT last_seen FROM agent_leases').fetchone()[0].startswith('2000'))
        with self.assertRaises(ConflictError):
            self.svc.release_agent('claude', 'demo', 'old')
        self.assertEqual(self.svc.lookup_agent_by_session('demo', 'new')['agent'], 'claude')
        self.assertTrue(self.svc.release_agent('claude', 'demo', 'new')['released'])

    def test_name_claim_skips_sender_only_service(self):
        self.svc.ensure_inbox('claude@demo', role='service')
        claim = self.svc.claim_agent('claude', 'demo', 'session-one')
        self.assertEqual(claim['agent'], 'claude-2')
        self.assertEqual(self.svc.claim_agent('claude', 'demo', 'session-one')['agent'], 'claude-2')

    def test_unbound_cli_refuses_mail_reservations_and_ae_before_creating_inbox(self):
        self.start_http()
        for args in [('list',), ('read', 'thr_missing'), ('send', '--to', 'a@demo', '--subject', 'X', '--body', 'Y'),
                     ('watch', '--timeout', '1'), ('reserve', 'a.py'), ('renew', '--all'),
                     ('release', '--all'), ('announce', '--subject', 'X', '--body', 'Y'), ('brief',)]:
            with self.subTest(args=args):
                code, out, err = self.cli(*args)
                self.assertEqual(code, 2, (out, err))
                self.assertIn('claim', err)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM inboxes').fetchone()[0], 0)
        self.assertEqual(self.cli('claim', 'agent')[0], 0)
        self.assertEqual(self.cli('list')[0], 0)  # A fresh shell needs no exported name.
        self.age_leases()
        self.assertEqual(self.cli('list')[0], 0)
        self.assertEqual(self.svc.claim_agent('agent', 'demo', 'competing-session')['agent'], 'agent-2')

    def test_resume_session_id_stays_stable_and_explicit_child_ids_stay_distinct(self):
        with mock.patch.dict(os.environ, {'AGENT_INBOX_SESSION': '', 'CLAUDE_SESSION_ID': 'conversation', 'CLAUDE_PID': '100'}):
            first = derive_session()
            os.environ['CLAUDE_PID'] = '101'
            self.assertEqual(first, derive_session())
            os.environ['AGENT_INBOX_SESSION'] = 'child-one'
            child = derive_session()
            os.environ['AGENT_INBOX_SESSION'] = 'child-two'
            self.assertNotEqual(child, derive_session())

    def test_recipient_typo_is_atomic_and_suggests_known_address(self):
        self.register('claude@demo')
        for kind in ('to', 'cc'):
            with self.subTest(kind=kind), self.assertRaises(NotFoundError) as error:
                self.svc.send_email('sender@demo', ['cluade@demo'] if kind == 'to' else ['claude@demo'],
                                    [] if kind == 'to' else ['cluade@demo'], 'Topic', 'Body', kind)
            self.assertEqual(error.exception.code, 'unknown_recipient')
            self.assertIn('claude@demo', str(error.exception))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM emails').fetchone()[0], 0)
        self.assertEqual(len(self.svc.list_inboxes()), 1)

    def test_explicit_sender_must_exist_and_create_missing_is_opt_in(self):
        client = self.start_http()
        self.register('recipient@demo')
        with self.assertRaises(NotFoundError) as error:
            client.send_email('typo@demo', ['recipient@demo'], subject='X', body_markdown='Y')
        self.assertEqual(error.exception.code, 'unknown_sender')
        result = client.send_email('import@demo', ['new@demo'], subject='X', body_markdown='Y', create_missing=True)
        self.assertEqual(len(client.email_status(result['email_id'])['recipients']), 1)

    def test_broadcast_waits_for_future_agents_skips_services_and_expires(self):
        self.svc.ensure_project('demo')
        self.svc.ensure_inbox('ci@automation', role='service')
        sent = self.svc.send_email('ci@automation', ['*@demo'], [], 'Checks red', 'Investigate', 'broadcast')
        self.assertEqual(self.svc.email_status(sent['email_id'])['recipients'], [])
        self.svc.ensure_inbox('daemon@demo', role='service')
        self.register('agent@demo')
        status = self.svc.email_status(sent['email_id'])
        self.assertEqual([r['address'] for r in status['recipients']], ['agent@demo'])
        self.assertEqual(len(self.svc.get_thread('agent@demo', sent['thread_id'])['emails']), 1)
        self.conn.execute("UPDATE email_broadcasts SET expires_at='2000-01-01T00:00:00.000Z'")
        self.register('late@demo')
        self.assertEqual(self.svc.list_threads('late@demo'), [])
        self.assertFalse(any(i['local_part'] == '*' for i in self.svc.list_inboxes()))

    def test_broadcast_fanout_and_service_receiving_are_consistent(self):
        self.register('sender@demo', 'a@demo', 'b@demo')
        self.svc.ensure_inbox('ci@demo', role='service')
        result = self.svc.send_email('sender@demo', ['*@demo'], [], 'X', 'Y', 'fanout')
        self.assertEqual([r['address'] for r in self.svc.email_status(result['email_id'])['recipients']], ['a@demo', 'b@demo'])
        with self.assertRaises(ValidationError):
            self.svc.send_email('sender@demo', ['ci@demo'], [], 'X', 'Y', 'bad-service')
        self.svc.touch_session('ci@demo', 'daemon')
        self.assertEqual(self.svc.active_sessions('ci@demo'), [])
        with self.assertRaises(NotFoundError) as error:
            self.svc.send_email('sender@demo', ['*@missing'], [], 'X', 'Y', 'bad-project')
        self.assertEqual(error.exception.code, 'unknown_project')

    def test_status_is_read_only_and_receipts_are_independent(self):
        self.register('sender@demo', 'a@demo', 'b@demo')
        sent = self.svc.send_email('sender@demo', ['a@demo'], ['b@demo'], 'X', 'Y', 'status')
        before = '\n'.join(self.conn.iterdump())
        self.assertEqual(self.svc.email_status(sent['email_id'])['unread_count'], 2)
        self.assertEqual('\n'.join(self.conn.iterdump()), before)
        self.svc.mark_thread_read('a@demo', sent['thread_id'])
        before = '\n'.join(self.conn.iterdump())
        status = self.svc.email_status(sent['email_id'])
        self.assertEqual(status['read_count'], 1)
        self.assertEqual(status['unread_count'], 1)
        self.assertEqual([(r['address'], r['kind']) for r in status['recipients']],
                         [('a@demo', 'to'), ('b@demo', 'cc')])
        self.assertIsNotNone(status['recipients'][0]['read_at'])
        self.assertIsNone(status['recipients'][1]['read_at'])
        self.assertEqual('\n'.join(self.conn.iterdump()), before)

    def test_delivery_cursor_survives_deleted_mail_and_late_broadcasts(self):
        self.register('old@demo', 'peer@demo')
        sent = self.svc.send_email('old@demo', ['peer@demo'], [], 'Old', 'Body', 'old')
        cursor = self.svc.watch_state('peer@demo')['cursor']
        self.svc.delete_inbox('old@demo', force=True)
        self.svc.ensure_inbox('ci@automation', role='service')
        broadcast = self.svc.send_email('ci@automation', ['*@demo'], [], 'New', 'Body', 'new')
        self.assertTrue(self.svc.watch_state('peer@demo', after=cursor)['changed'])
        self.register('late@demo')
        late = self.svc.watch_state('late@demo')
        self.assertEqual(late['latest']['thread_id'], broadcast['thread_id'])
        self.assertGreater(late['cursor'], cursor)

    def test_broadcast_and_session_binding_survive_connection_restart(self):
        self.svc.ensure_project('demo')
        sent = self.svc.send_email('ci@automation', ['*@demo'], [], 'Topic', 'Body', 'durable')
        self.svc.claim_agent('agent', 'other', 'stable')
        restarted = get_connection(self.root / 'test.db')
        try:
            svc = InboxService(restarted)
            self.assertEqual(svc.lookup_agent_by_session('other', 'stable')['agent'], 'agent')
            svc.ensure_inbox('new@demo')
            self.assertEqual(svc.email_status(sent['email_id'])['unread_count'], 1)
            self.assertEqual(restarted.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            restarted.close()

    def test_concurrent_session_claims_are_unique_and_same_session_is_idempotent(self):
        barrier = threading.Barrier(2)
        claims, errors = [], []
        def claim(session):
            conn = get_connection(self.root / 'test.db')
            try:
                barrier.wait(timeout=5)
                claims.append(InboxService(conn).claim_agent('agent', 'demo', session))
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()
        workers = [threading.Thread(target=claim, args=(sid,)) for sid in ('one', 'two')]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len({r['address'] for r in claims}), 2)
        before = self.svc.lookup_agent_by_session('demo', 'one')
        self.assertEqual(before['address'], self.svc.claim_agent('agent', 'demo', 'one')['address'])

    def test_maintenance_refuses_unfinished_assignment(self):
        from agent_inbox.ae import AgentExperience
        self.register('old@demo', 'new@demo')
        ae = AgentExperience(self.conn)
        ae.command({'actor': 'new@demo', 'session': 's', 'request_id': 'task', 'operation': 'task.create',
                    'payload': {'title': 'Important work', 'target': 'old@demo'}})
        with self.assertRaises(ConflictError) as error:
            self.svc.merge_inboxes('old@demo', 'new@demo')
        self.assertEqual(error.exception.code, 'active_tasks')

    def test_reply_typo_rolls_back_and_invalid_recipient_shapes_are_validation_errors(self):
        self.register('sender@demo', 'peer@demo')
        sent = self.svc.send_email('sender@demo', ['peer@demo'], [], 'Topic', 'Body', 'reply-base')
        with self.assertRaises(NotFoundError):
            self.svc.reply_email(sent['email_id'], 'peer@demo', 'Reply', 'reply-typo', to_addrs=['sendre@demo'])
        for recipients in ([{}], [42]):
            with self.assertRaises(ValidationError):
                self.svc.send_email('sender@demo', recipients, [], 'Bad', 'Body', 'invalid')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM emails').fetchone()[0], 1)

    def test_heartbeat_only_refreshes_existing_session(self):
        self.start_http()
        self.assertEqual(self.cli('heartbeat'), (0, '', ''))
        self.assertEqual(len(self.svc.list_inboxes()), 0)
        self.svc.claim_agent('agent', 'demo', 'session-one')
        self.age_leases()
        self.assertEqual(self.cli('heartbeat'), (0, '', ''))
        self.assertFalse(self.conn.execute('SELECT last_seen FROM agent_leases').fetchone()[0].startswith('2000'))

    def test_registry_is_offline_case_insensitive_for_hosts_and_case_sensitive_for_paths(self):
        register_project(self.conn, 'https://github.com/Acme/Widget.git', 'widgets')
        for repo in ('acme/widget', 'Acme/Widget', 'git@github.com:Acme/Widget.git', 'github.com/acme/widget'):
            self.assertEqual(lookup_project(self.conn, repo)['slug'], 'widgets')
        register_project(self.conn, '/tmp/Widget', 'upper')
        register_project(self.conn, '/tmp/widget', 'lower')
        self.assertEqual(lookup_project(self.conn, '/tmp/Widget')['slug'], 'upper')
        with mock.patch.dict(os.environ, {'AGENT_INBOX_PROJECT': ''}), mock.patch(
                'agent_inbox.identity.subprocess.run', return_value=mock.Mock(returncode=0, stdout='git@github.com:Acme/Widget.git')):
            self.assertEqual(derive_project(), 'widgets')

    def test_registry_cli_exit_codes_and_ambiguous_legacy_data(self):
        self.assertEqual(self.cli('project', 'lookup', '--repo', 'acme/missing', '--json')[0], 4)
        self.assertEqual(self.cli('project', 'register', '--repo', 'acme/widget', '--slug', 'demo')[0], 0)
        self.conn.execute('INSERT INTO project_mappings VALUES (?,?)', ('github.com/Acme/Widget', 'another'))
        self.assertEqual(self.cli('project', 'lookup', '--repo', 'acme/widget', '--json')[0], 5)

    def test_merge_preview_and_preservation_of_thread_and_receipts(self):
        self.register('sender@demo', 'old@demo', 'new@demo')
        sent = self.svc.send_email('sender@demo', ['old@demo'], ['new@demo'], 'Topic', 'Body', 'merge')
        self.svc.mark_thread_read('old@demo', sent['thread_id'])
        before = '\n'.join(self.conn.iterdump())
        summary = self.svc.merge_inboxes('old@demo', 'new@demo', dry_run=True)
        self.assertEqual(summary['source']['threads'], 1)
        self.assertEqual(before, '\n'.join(self.conn.iterdump()))
        self.svc.merge_inboxes('old@demo', 'new@demo')
        status = self.svc.email_status(sent['email_id'])
        self.assertEqual(status['recipients'][0]['kind'], 'to')
        self.assertEqual(status['read_count'], 1)
        self.assertEqual(len(self.svc.get_thread('new@demo', sent['thread_id'])['emails']), 1)
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_delete_requires_force_for_mail_and_retains_other_peoples_replies(self):
        self.register('old@demo', 'peer@demo')
        sent = self.svc.send_email('old@demo', ['peer@demo'], [], 'Topic', 'Body', 'delete')
        reply = self.svc.reply_email(sent['email_id'], 'peer@demo', 'Reply', 'reply')
        old_id = self.conn.execute("SELECT id FROM inboxes WHERE local_part='old'").fetchone()[0]
        self.svc.acquire_reservations('demo', ['old.py'], 'old@demo', 'old-session', client_token='old-file')
        self.svc.release_reservations('demo', 'old@demo', 'old-session', release_all=True)
        before = '\n'.join(self.conn.iterdump())
        with self.assertRaises(ConflictError) as error:
            self.svc.delete_inbox('old@demo')
        self.assertEqual(error.exception.code, 'inbox_not_empty')
        self.assertEqual('\n'.join(self.conn.iterdump()), before)
        preview = self.svc.delete_inbox('old@demo', force=True, dry_run=True)
        self.assertEqual(preview['sent_emails'], 1)
        self.assertEqual(preview['reservations'], 1)
        self.assertEqual('\n'.join(self.conn.iterdump()), before)
        self.svc.delete_inbox('old@demo', force=True)
        self.assertIsNone(self.conn.execute('SELECT id FROM inboxes WHERE id=?', (old_id,)).fetchone())
        self.assertIsNone(self.conn.execute('SELECT id FROM emails WHERE id=?', (sent['email_id'],)).fetchone())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM email_recipients WHERE inbox_id=?', (old_id,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM reservations WHERE holder_inbox_id=?', (old_id,)).fetchone()[0], 0)
        remaining = self.svc.get_thread('peer@demo', sent['thread_id'])['emails']
        self.assertEqual([e['email_id'] for e in remaining], [reply['email_id']])
        self.assertEqual(remaining[0]['body_markdown'], 'Reply')
        self.assertIsNone(remaining[0]['reply_to_email_id'])
        self.assertEqual(remaining[0]['references'], [])
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_merge_delivers_newly_visible_mail_to_existing_watchers(self):
        self.register('sender@demo', 'old@demo', 'new@demo')
        moved = self.svc.send_email('sender@demo', ['old@demo'], [], 'Move', 'Body', 'move')
        seen = self.svc.send_email('sender@demo', ['new@demo'], [], 'Seen', 'Body', 'seen')
        cursor = self.svc.watch_state('new@demo')['cursor']
        self.svc.mark_thread_read('new@demo', seen['thread_id'])
        event_cursor = self.conn.execute('SELECT MAX(sequence) FROM ae_events').fetchone()[0]
        self.svc.merge_inboxes('old@demo', 'new@demo')
        watch = self.svc.watch_state('new@demo', after=cursor)
        self.assertTrue(watch['changed'])
        self.assertEqual(watch['latest']['thread_id'], moved['thread_id'])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM ae_events WHERE sequence>? AND audience='new@demo' AND kind='mail.received' AND ref=?",
                                              (event_cursor, moved['thread_id'])).fetchone())

    def test_reservation_retries_cannot_change_holder_paths_or_reacquire_expired_work(self):
        original = self.svc.acquire_reservations('demo', ['a.py'], 'a@demo', 's1', client_token='one')
        with self.assertRaises(ConflictError):
            self.svc.acquire_reservations('demo', ['b.py'], 'a@demo', 's1', client_token='one')
        with self.assertRaises(ConflictError):
            self.svc.acquire_reservations('demo', ['a.py'], 'a@demo', 's2', client_token='one')
        # Reacquisition with a new token must not erase the first request's memory.
        self.svc.acquire_reservations('demo', ['a.py'], 'a@demo', 's1', client_token='two')
        self.svc.release_reservations('demo', 'a@demo', 's1', release_all=True)
        replay = self.svc.acquire_reservations('demo', ['a.py'], 'a@demo', 's1', client_token='one')
        self.assertTrue(replay['replayed'])
        self.assertFalse(replay['reservations'][0]['active'])

    def test_cross_project_reservation_is_rejected_without_provisioning(self):
        with self.assertRaises(ValidationError):
            self.svc.acquire_reservations('other', ['a.py'], 'a@demo', 's1', client_token='scope')
        self.assertEqual(len(self.svc.list_inboxes()), 0)

    def test_mit_license_is_present_in_runtime_and_cli(self):
        from agent_inbox.license_data import TEXT
        self.assertEqual(TEXT, (Path(__file__).resolve().parents[1] / 'LICENSE').read_text())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as result:
            main(['--license'])
        self.assertEqual(result.exception.code, 0)
        # argparse may wrap whitespace; all license terms must still be present.
        self.assertEqual(' '.join(out.getvalue().split()), ' '.join(TEXT.split()))


if __name__ == '__main__':
    unittest.main()
