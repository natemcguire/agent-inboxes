"""Project observation through the real HTTP/SQLite boundary."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_inbox.ae import AgentExperience
from agent_inbox.db import get_connection, init_db
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService
from tests.support import isolated_inbox


class TestProjectObserver(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(isolated_inbox(self.directory.name))
        self.conn = get_connection(Path(self.directory.name) / 'observer.db')
        self.addCleanup(self.conn.close)
        self.service = InboxService(self.conn)
        for address in ('codex@boats', 'claude@boats', 'reviewer@boats', 'agent@elsewhere', 'other@elsewhere'):
            self.service.ensure_inbox(address)
        self.service.ensure_project('empty')
        self.first = self.service.send_email('codex@boats', ['claude@boats'], ['reviewer@boats'],
                                             'Design: Hull', 'Inspect the hull.', 'first')
        self.reply = self.service.reply_email(self.first['email_id'], 'claude@boats', '100% checked.', 'reply')
        self.second = self.service.send_email('reviewer@boats', ['codex@boats'], [], 'Release: Hull', 'Ready.', 'second')
        self.foreign = self.service.send_email('agent@elsewhere', ['other@elsewhere'], [], 'Unrelated', '100% checked.', 'foreign')
        self.service.claim_agent('codex', 'boats', 'session-a')
        self.service.post_announcement('codex@boats', 'Preview', 'Ready to inspect.', 'announcement')
        task = AgentExperience(self.conn).command({'actor':'codex@boats', 'session':'session-a', 'request_id':'task',
            'operation':'task.create', 'payload':{'title':'Inspect hull','target':'claude@boats','thread_id':self.first['thread_id']}})
        self.task_id = task['id']
        self.service.acquire_reservations('boats', ['hull.py'], 'codex@boats', 'session-a', reason='Inspect hull', client_token='lease')
        self.server = AgentInboxServer(('127.0.0.1', 0), self.conn)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def request(self, path, body=None, headers=None):
        headers = dict(headers or {})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            error.read()
            error.close()
            raise

    def snapshot(self):
        tables = ('inboxes', 'sessions', 'agent_leases', 'email_recipients', 'announcement_receipts',
                  'ae_tasks', 'ae_receipts', 'reservations', 'ae_events')
        return {table:[tuple(r) for r in self.conn.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}

    def test_observer_unifies_threads_without_agent_side_effects(self):
        # Even agent headers cannot turn a human observation into a heartbeat.
        self.conn.execute("UPDATE agent_leases SET last_seen='2000-01-01T00:00:00.000Z'")
        # A remote sender's clock can precede its parent; ancestry still wins.
        self.conn.execute("UPDATE emails SET sent_at='1999-01-01T00:00:00.000Z' WHERE id=?", (self.reply['email_id'],))
        before = self.snapshot()
        headers = {'X-Agent-Address':'codex@boats', 'X-Agent-Session':'session-a'}
        listing = self.request('/v1/projects/boats/threads', headers=headers)
        detail = self.request('/v1/projects/boats/threads/' + self.first['thread_id'], headers=headers)
        overview = self.request('/v1/projects/boats/overview', headers=headers)
        self.request('/v1/projects/boats/announcements', headers=headers)
        self.request('/v1/projects', headers=headers)
        agent_list = self.request('/v1/inboxes/claude@boats/threads?observe=true', headers=headers)
        agent_detail = self.request('/v1/inboxes/claude@boats/threads/'+self.first['thread_id']+'?observe=true', headers=headers)
        self.request('/v1/announcements?inbox=claude@boats&observe=true', headers=headers)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(agent_list['threads'][0]['unread_count'], 1)
        self.assertEqual(agent_detail['reading_as'], 'claude@boats')
        self.assertEqual(agent_detail['emails'][0]['your_role'], 'to')
        self.assertIs(agent_detail['emails'][0]['read'], False)
        self.assertEqual([r['thread_id'] for r in listing['threads']], [self.second['thread_id'], self.first['thread_id']])
        self.assertEqual(listing['total'], 2)
        self.assertEqual([m['email_id'] for m in detail['emails']], [self.first['email_id'], self.reply['email_id']])
        self.assertEqual(detail['emails'][1]['reply_to_email_id'], self.first['email_id'])
        self.assertIsNone(detail['reading_as'])
        self.assertTrue(all(m['read'] is None and m['your_role']=='observer' for m in detail['emails']))
        self.assertEqual(overview['thread_count'], 2)
        self.assertEqual(overview['message_count'], 3)
        self.assertEqual(overview['tasks'][0]['id'], self.task_id)
        self.assertEqual(overview['reservation_count'], 1)
        self.assertEqual({a['address']:a['task_count'] for a in overview['agents']},
                         {'claude@boats':1, 'codex@boats':0, 'reviewer@boats':0})

    def test_scope_rejection_cross_project_membership_and_empty_project(self):
        count = self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
        for path in ('/v1/projects/missing/overview', '/v1/projects/boats/threads/'+self.foreign['thread_id']):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], count)
        self.assertEqual(self.request('/v1/projects/empty/threads')['threads'], [])
        cross = self.service.send_email('agent@elsewhere', ['claude@boats'], [], 'Shared work', 'A scoped cross-project thread.', 'cross')
        detail = self.request('/v1/projects/boats/threads/'+cross['thread_id'])
        self.assertEqual(detail['emails'][0]['from'], 'agent@elsewhere')
        self.assertEqual(self.request('/v1/projects/boats/threads')['total'], 3)

    def test_pagination_body_search_and_receipt_refresh(self):
        first = self.request('/v1/projects/boats/threads?limit=1')
        from urllib.parse import quote
        second = self.request('/v1/projects/boats/threads?limit=1&cursor='+quote(first['next_cursor']))
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual([first['threads'][0]['thread_id'], second['threads'][0]['thread_id']],
                         [self.second['thread_id'], self.first['thread_id']])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/v1/projects/elsewhere/threads?cursor='+quote(first['next_cursor']))
        self.assertEqual(caught.exception.code, 400)
        for query in ('100%25', 'checked'):
            self.assertEqual([t['thread_id'] for t in self.request('/v1/projects/boats/threads?q='+query)['threads']], [self.first['thread_id']])
        old = second['threads'][0]['receipt_activity']
        self.service.mark_thread_read('claude@boats', self.first['thread_id'])
        row = self.request('/v1/projects/boats/threads?q=checked')['threads'][0]
        self.assertNotEqual(row['receipt_activity'], old)
        receipts = self.request('/v1/projects/boats/threads/'+self.first['thread_id'])['emails'][0]['receipts']
        self.assertIsNotNone(next(r['read_at'] for r in receipts if r['address']=='claude@boats'))

    def test_actual_peer_and_session_are_persisted_on_send_and_reply(self):
        headers = {'Idempotency-Key':'http-message', 'X-Agent-Session':'http-session', 'X-Forwarded-For':'203.0.113.123'}
        body = {'from':'codex@boats','to':['claude@boats'],'subject':'HTTP provenance','body_markdown':'Recorded at the boundary.','api_peer_ip':'203.0.113.123'}
        sent = self.request('/v1/emails', body, headers)
        self.request('/v1/emails', body, headers)
        self.request('/v1/emails/'+sent['email_id']+'/reply', {'from':'claude@boats','body_markdown':'Reply.'},
                     {'Idempotency-Key':'http-reply','X-Agent-Session':'reply-session'})
        messages = self.request('/v1/projects/boats/threads/'+sent['thread_id'])['emails']
        self.assertEqual(len(messages), 2)
        self.assertEqual([m['sender_session'] for m in messages], ['http-session','reply-session'])
        for message in messages:
            self.assertEqual(message['api_peer_ip'], '127.0.0.1')
            self.assertEqual(message['api_received_at'], message['sent_at'])
        historical = self.request('/v1/projects/boats/threads/'+self.first['thread_id'])['emails'][0]
        self.assertIsNone(historical['api_peer_ip'])
        self.assertIsNone(historical['api_received_at'])
        health = self.request('/healthz')
        self.assertEqual((health['listen_address'],health['client_ip'],health['port']), ('127.0.0.1','127.0.0.1',self.server.server_port))
        self.assertGreaterEqual(health['uptime_seconds'], 0)

    def test_upgrade_preserves_mail_and_does_not_invent_provenance(self):
        self.conn.execute('ALTER TABLE emails DROP COLUMN api_peer_ip')
        self.conn.execute('ALTER TABLE emails DROP COLUMN api_received_at')
        before = self.snapshot()
        for _ in range(2):
            init_db(self.conn)
        self.assertEqual(self.snapshot(), before)
        row = self.conn.execute('SELECT body_markdown,api_peer_ip,api_received_at FROM emails WHERE id=?', (self.first['email_id'],)).fetchone()
        self.assertEqual(tuple(row), ('Inspect the hull.', None, None))


if __name__ == '__main__':
    unittest.main()
