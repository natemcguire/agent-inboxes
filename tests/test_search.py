"""Search the actual SQLite index through HTTP, including upgrades and scopes."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from agent_inbox.db import get_connection, init_db
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService
from tests.support import isolated_inbox


class TestConversationSearch(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(isolated_inbox(self.directory.name))
        self.conn = get_connection(Path(self.directory.name) / 'search.db')
        self.addCleanup(self.conn.close)
        self.service = InboxService(self.conn)
        for address in ('codex@boats','claude@boats','reviewer@boats','agent@private','other@private'):
            self.service.ensure_inbox(address)
        self.reservation = self.service.send_email('codex@boats',['claude@boats'],[],
            'File reservations', 'An atomic lease protects the hull file.', 'reservation')
        self.important = self.service.reply_email(self.reservation['email_id'],'claude@boats',
            'The clipboard synchronization loop caused the failure. <img src=x onerror=alert(1)>', 'reply')
        self.service.reply_email(self.important['email_id'],'codex@boats','Fixed and ready for review.', 'later')
        self.other = self.service.send_email('reviewer@boats',['codex@boats'],[],
            'Release notes', 'File reservations are part of the release.', 'other')
        self.private = self.service.send_email('agent@private',['other@private'],[],
            'Private launch', 'Zephyrium confidential forecast.', 'private')
        self.server = AgentInboxServer(('127.0.0.1',0),self.conn)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def search(self, q='', **params):
        url = self.base + '/v1/search?' + urllib.parse.urlencode({'q':q,**params})
        request = urllib.request.Request(url,headers={'X-Agent-Address':'codex@boats','X-Agent-Session':'search-session'})
        with urllib.request.urlopen(request,timeout=5) as response:
            return json.load(response)

    def test_ranked_prefix_results_group_threads_and_find_older_replies(self):
        result = self.search('file reserva',project='boats')
        self.assertEqual(result['results'][0]['thread_id'],self.reservation['thread_id'])
        self.assertEqual(result['total'],2)
        result = self.search('"clipboard synchronization"',project='boats')
        hit = result['results'][0]
        self.assertEqual(hit['email_id'],self.important['email_id'])
        self.assertEqual(hit['message_count'],3)
        self.assertEqual(hit['sender'],'claude@boats')
        self.assertTrue(any(p['match'] and 'clipboard synchronization' in p['text'] for p in hit['excerpt']))
        self.assertIn('<img', ''.join(p['text'] for p in hit['excerpt']))
        self.assertNotIn('html',hit)
        self.assertEqual(self.search('"synchronization clipboard"')['total'],0)

    def test_typo_recovery_scopes_filters_and_read_only_observation(self):
        tables=('inboxes','sessions','agent_leases','email_recipients','ae_events')
        snapshot=lambda:{t:[tuple(r) for r in self.conn.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}
        before=snapshot()
        result=self.search('clipbord',project='boats',inbox='claude@boats')
        self.assertEqual(result['correction'],'clipboard')
        self.assertEqual(result['results'][0]['email_id'],self.important['email_id'])
        self.assertEqual(self.search('clipbord',project='private')['total'],0)
        hidden=self.search('zephryium',project='boats')
        self.assertEqual(hidden['total'],0)
        self.assertIsNone(hidden['correction'])
        self.assertEqual(self.search('project:private',project='boats')['total'],0)
        self.assertEqual(self.search('from:claude@boats clipboard')['results'][0]['email_id'],self.important['email_id'])
        self.assertEqual(self.search('from:reviewer@boats clipboard')['total'],0)
        self.assertEqual(self.search('clipboard before:2000-01-01')['total'],0)
        self.assertEqual(before,snapshot())

    def test_upgrade_backfills_and_index_follows_updates_and_deletes(self):
        for name in ('mail_search_insert','mail_search_update','mail_search_delete'):
            self.conn.execute('DROP TRIGGER '+name)
        self.conn.execute('DROP TABLE mail_search_words')
        self.conn.execute('DROP TABLE mail_search')
        init_db(self.conn)
        self.assertEqual(self.search('clipboard')['total'],1)
        self.conn.execute('UPDATE emails SET body_markdown=? WHERE id=?',('Résumé déploiement',self.other['email_id']))
        self.assertEqual(self.search('resume')['results'][0]['email_id'],self.other['email_id'])
        self.conn.execute('DELETE FROM emails WHERE id=?',(self.other['email_id'],))
        self.assertEqual(self.search('resume')['total'],0)
        init_db(self.conn)
        self.assertEqual(self.search('clipboard')['total'],1)

    def test_pagination_and_untrusted_query_syntax(self):
        for index in range(8):
            self.service.send_email('codex@boats',['claude@boats'],[],f'Authentication topic {index}',
                'Unique authentication policy.',f'page-{index}')
        first=self.search('authentcation',project='boats',limit=3)
        second=self.search('authentcation',project='boats',limit=3,offset=first['next_offset'])
        self.assertEqual(first['total'],8)
        self.assertEqual(first['correction'],'authentication')
        self.assertEqual(second['total'],8)
        self.assertFalse({r['thread_id'] for r in first['results']} & {r['thread_id'] for r in second['results']})
        for query in ('" OR 1=1 --','*','NEAR(foo,bar)','"'):
            self.assertIn('results',self.search(query))
        for params in ({'q':'a'*501},{'q':'after:20260101'},{'limit':31},{'offset':-1}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.search(**params)
            self.assertEqual(error.exception.code,400)
            error.exception.close()
