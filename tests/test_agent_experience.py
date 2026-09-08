"""AE acceptance: context continuity, ownership races and loss-aware attention."""
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from agent_inbox.ae import AgentExperience
from agent_inbox.db import get_connection
from agent_inbox.models import ConflictError, NotFoundError
from agent_inbox.service import InboxService


class AgentExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'inbox.db'
        self.env=patch.dict(os.environ,{'AGENT_INBOX_DB':str(self.path),'AGENT_INBOX_CLOUD_CONFIG':str(Path(self.tmp.name)/'no-cloud.json')})
        self.env.start();self.addCleanup(self.env.stop)
        self.conn=get_connection(self.path);self.addCleanup(self.conn.close)
        self.ae=AgentExperience(self.conn)

    def command(self,op,payload,actor='alpha@project',session='session-a',request_id=None):
        return self.ae.command({'actor':actor,'session':session,'operation':op,'payload':payload,'request_id':request_id or str(uuid.uuid4())})

    def test_context_dependency_handoff_and_restart(self):
        first=self.command('task.create',{'title':'Change API','paths':['api/'],'target':'alpha@project'})
        second=self.command('task.create',{'title':'Update client','dependencies':[first['id']],'target':'beta@project'})
        context=self.ae.context('beta@project','b')
        self.assertEqual(context['sections']['ready_queue'],[])
        self.assertEqual(context['sections']['waiting_on_dependencies'][0]['id'],second['id'])
        claimed=self.command('task.claim',{'id':first['id'],'version':1})
        self.command('task.block',{'id':first['id'],'version':claimed['version'],'note':'Need interface decision'})
        self.command('decision.record',{'title':'Use v2','body':'Return a stable ID','source_ref':'design:123'})
        recovered=self.command('task.resume',{'id':first['id'],'version':3,'note':'Recovered after session restart'},session='new-session')
        self.command('task.complete',{'id':first['id'],'version':recovered['version'],'result':'API v2 implemented; commit abc'},session='new-session')
        other=get_connection(self.path)
        try:
            context=AgentExperience(other).context('beta@project','b')
            self.assertEqual(context['sections']['ready_queue'][0]['id'],second['id'])
            self.assertEqual(context['sections']['decisions'][0]['body'],'Return a stable ID')
            self.assertTrue(any(e['kind']=='task.ready' and e['ref']==second['id'] for e in AgentExperience(other).events('beta@project')['events']))
        finally: other.close()
        self.command('task.claim',{'id':second['id'],'version':1},actor='beta@project',session='b')
        self.command('task.handoff',{'id':second['id'],'version':2,'note':'Client types updated; finish integration','target':'gamma@project'},actor='beta@project',session='b')
        handed=self.ae.context('gamma@project','c')['sections']['ready_queue'][0]
        self.assertIn('finish integration',handed['note'])
        self.assertIsNone(handed['owner'])
        claimed=self.command('task.claim',{'id':second['id'],'version':3},actor='gamma@project',session='c')
        self.assertIn('finish integration',claimed['note'])
        self.assertEqual(len(self.ae.history(second['id'],'gamma@project')['history']),4)

    def test_claim_race_and_session_ownership(self):
        task=self.command('task.create',{'title':'Exclusive work'})
        barrier=threading.Barrier(2);results=[]
        def claim(actor):
            conn=get_connection(self.path)
            try:
                barrier.wait()
                try:
                    AgentExperience(conn).command({'actor':actor,'session':actor,'request_id':actor,'operation':'task.claim','payload':{'id':task['id'],'version':1}})
                    results.append(actor)
                except ConflictError: results.append('conflict')
            finally:conn.close()
        workers=[threading.Thread(target=claim,args=(name+'@project',)) for name in ('one','two')]
        for worker in workers:worker.start()
        for worker in workers:worker.join(5)
        self.assertEqual(results.count('conflict'),1)
        owner=next(r for r in results if r!='conflict')
        with self.assertRaises(ConflictError):self.command('task.complete',{'id':task['id'],'version':2,'result':'wrong runtime'},actor=owner,session='different')
        with self.assertRaises(NotFoundError):self.ae.task(task['id'],'outsider@elsewhere')

    def test_journal_transaction_retry_ack_and_project_filter(self):
        envelope={'actor':'alpha@project','session':'a','request_id':'stable','operation':'task.create','payload':{'title':'Keep once'}}
        created=self.ae.command(envelope)
        cursor=self.conn.execute('SELECT MAX(sequence) FROM ae_events').fetchone()[0]
        self.assertEqual(self.ae.command(envelope),created)
        self.assertEqual(cursor,self.conn.execute('SELECT MAX(sequence) FROM ae_events').fetchone()[0])
        with self.assertRaises(ConflictError):self.ae.command(dict(envelope,payload={'title':'Different'}))
        with self.assertRaises(NotFoundError):self.command('task.create',{'title':'Rollback','dependencies':['missing']})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ae_tasks').fetchone()[0],1)
        events=self.ae.events('alpha@project')
        self.command('ack',{'source':events['source'],'sequence':events['events'][0]['sequence']})
        self.assertTrue(self.ae.events('alpha@project')['events'][0]['acknowledged'])
        self.assertEqual(self.ae.task(created['id'],'alpha@project')['state'],'queued')
        hidden=self.ae.events('other@other')
        self.assertEqual(hidden['events'],[]);self.assertEqual(hidden['cursor'],cursor)
        with self.assertRaises(ConflictError):self.ae.events('alpha@project',after=1,source='other-database')

    def test_mail_roles_subscription_and_context_budget(self):
        svc=InboxService(self.conn)
        message=svc.send_email('sender@project',['alpha@project'],['observer@project'],'Decision','<script>untrusted</script>','mail-token')
        task=self.command('task.create',{'title':'Act on decision','thread_id':message['thread_id']})
        context=self.ae.context('observer@project','observer')
        self.assertEqual(context['sections']['unread_threads'][0]['your_roles'],['cc'])
        self.command('subscribe',{'kind':'thread','ref':message['thread_id']},actor='observer@project')
        with self.assertRaises(NotFoundError):self.command('subscribe',{'kind':'thread','ref':message['thread_id']},actor='stranger@project')
        for i in range(30):self.command('decision.record',{'title':f'Decision {i}','body':'long context '*1000,'source_ref':'test'})
        context=self.ae.context('alpha@project','session-a',50)
        self.assertLessEqual(len(json.dumps(context).encode()),24000)
        self.assertTrue(any(context['truncated'].values()))
        self.assertTrue(context['sections']['decisions'][0]['excerpted_fields'])

    def test_queue_invalidation_and_historical_participation(self):
        task=self.command('task.create',{'title':'Public queue'},actor='maker@project')
        original=self.ae.events('observer@project')
        self.command('task.claim',{'id':task['id'],'version':1},actor='worker@project')
        after=self.ae.events('observer@project',original['cursor'],source=original['source'])
        self.assertEqual(after['events'][0]['kind'],'task.changed')
        self.assertEqual(self.ae.events('observer@project')['events'][0]['sequence'],original['events'][0]['sequence'])
        self.command('task.handoff',{'id':task['id'],'version':2,'target':'receiver@project','note':'Continue'},actor='worker@project')
        self.assertEqual(self.ae.events('worker@project')['events'][-1]['detail']['version'],3)

    def test_brief_cursor_is_delivered_not_snapshot_or_ack(self):
        start=self.ae.brief('alpha@project','a')
        self.assertTrue(start['history_omitted'])
        for i in range(6):self.command('task.create',{'title':str(i)})
        page=self.ae.brief('alpha@project','a',start['cursor'],start['source'],limit=2)
        self.assertLess(page['cursor'],page['snapshot_cursor'])
        self.assertTrue(page['has_more'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ae_receipts').fetchone()[0],0)
        seen=[]
        cursor=start['cursor']
        while True:
            page=self.ae.brief('alpha@project','a',cursor,start['source'],limit=2)
            seen.extend(e['sequence'] for e in page['events']);cursor=page['cursor']
            if not page['has_more']:break
        self.assertEqual(len(seen),6);self.assertEqual(len(set(seen)),6)
        with self.assertRaises(ConflictError):self.ae.brief('alpha@project','a',0,'wrong')

    def test_unicode_budget_and_scan_omission(self):
        for i in range(15):self.command('decision.record',{'title':str(i),'body':'航'*800,'source_ref':'test'})
        context=self.ae.context('alpha@project','a',50)
        self.assertLessEqual(len(json.dumps(context).encode()),24000)
        brief=self.ae.brief('alpha@project','a',0,self.ae.source,50)
        self.assertLessEqual(len(json.dumps(brief).encode()),24000)
        self.conn.executemany("INSERT INTO ae_events(kind,project,ref) VALUES('decision.created','other',?)",[(str(i),) for i in range(1001)])
        context=self.ae.context('alpha@project','a')
        self.assertTrue(context['recent_scan_limited']);self.assertTrue(context['truncated']['recent_events'])
        # Hidden pages advance without pretending the backlog is exhausted.
        page=self.ae.events('new@new')
        self.assertFalse(page['events']);self.assertTrue(page['has_more'])

    def test_structured_handoff_and_task_inventory(self):
        ids=[]
        for i in range(6):ids.append(self.command('task.create',{'title':str(i)})['id'])
        page=self.ae.tasks('alpha@project',limit=2)
        self.assertTrue(page['has_more'])
        self.assertEqual(len(self.ae.tasks('alpha@project',after=page['cursor'],limit=10)['tasks']),4)
        self.command('task.claim',{'id':ids[0],'version':1})
        details=dict(next_action='Run verification',workspace='/tmp/fixture',ref='branch:test',acceptance='Expected total 600',evidence='report.md')
        handed=self.command('task.handoff',{'id':ids[0],'version':2,'target':'beta@project','note':'Ready','handoff':details})
        self.assertEqual(handed['handoff']['next_action'],details['next_action'])
        self.command('task.claim',{'id':ids[0],'version':3},actor='beta@project')
        self.assertEqual(self.ae.history(ids[0],'beta@project')['history'][2]['handoff']['ref'],'branch:test')

    def test_watch_batches_routine_events_but_bypasses_for_direct_mail(self):
        start=self.ae.brief('alpha@project','a')
        self.command('decision.record',{'title':'Routine','body':'Routine update','source_ref':'test'})
        before=time.monotonic()
        batch=self.ae.watch('alpha@project','a',start['cursor'],start['source'],timeout=1,coalesce=.2)
        self.assertGreaterEqual(time.monotonic()-before,.18)
        self.assertEqual(batch['wake_reason'],'batch')
        InboxService(self.conn).send_email('sender@project',['alpha@project'],[],'Blocker','Please review','urgent-mail')
        before=time.monotonic()
        urgent=self.ae.watch('alpha@project','a',batch['cursor'],start['source'],timeout=1,coalesce=1,policy='to-me')
        self.assertLess(time.monotonic()-before,.5)
        self.assertEqual(urgent['wake_reason'],'urgent')
        # One session receiving the page does not consume it for another.
        again=self.ae.brief('alpha@project','b',batch['cursor'],start['source'],policy='to-me')
        self.assertEqual([e['sequence'] for e in urgent['events']],[e['sequence'] for e in again['events']])

    def test_hook_cleanup_preserves_quoted_mentions_and_compound_commands(self):
        from agent_inbox.hooks import _strip_hooks_config
        preserve=["echo 'agent-inbox hook-check'", 'agent-inbox hook-check; echo keep', 'other-tool agent-inbox hook-check']
        config={'hooks':{'PostToolUse':[{'hooks':[{'type':'command','command':cmd} for cmd in preserve+['/tmp/agent-inbox hook-check','agent-inbox hook-check --format=json']]}]}}
        self.assertTrue(_strip_hooks_config(config))
        self.assertEqual([h['command'] for h in config['hooks']['PostToolUse'][0]['hooks']],preserve)
        self.assertFalse(_strip_hooks_config(config))
