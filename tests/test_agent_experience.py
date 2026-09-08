"""AE acceptance: context continuity, ownership races and real broker protocols."""
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
        self.assertLessEqual(len(json.dumps(context,ensure_ascii=False).encode()),24100)
        self.assertTrue(any(context['truncated'].values()))
        self.assertTrue(context['sections']['decisions'][0]['excerpted_fields'])

    def test_actual_mqtt_and_nats_commands_share_work_and_events(self):
        # Optional test dependency only; the application itself is stdlib + its broker binary.
        try: import paho.mqtt.client as mqtt
        except ImportError:self.skipTest('Install paho-mqtt to run wire interoperability acceptance')
        from agent_inbox.ae_bus import AgentBus,NatsConnection
        def freeport():
            with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
        nats_port,mqtt_port=freeport(),freeport()
        bus=AgentBus(self.path,nats_port,mqtt_port);bus.start()
        self.addCleanup(bus.stop)
        self.assertTrue(bus.ready.wait(12));self.assertTrue(bus.connected,bus.error)
        credentials=json.loads((bus.directory/'credentials.json').read_text())
        source=self.ae.source
        native=NatsConnection(nats_port,'ae-agent',credentials['agent']);self.addCleanup(native.close)
        native.subscribe(f'ae.events.{source}.>')
        packets=[];connected=threading.Event();subscribed=threading.Event()
        client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,protocol=mqtt.MQTTv311)
        client.username_pw_set('ae-agent',credentials['agent'])
        def on_connect(c,u,flags,rc,props):
            if rc==0:c.subscribe([(f'ae/replies/{source}/#',1),(f'ae/events/{source}/#',1)]);connected.set()
        client.on_connect=on_connect
        client.on_subscribe=lambda *args:subscribed.set()
        client.on_message=lambda c,u,m:packets.append((m.topic,json.loads(m.payload)))
        client.connect('127.0.0.1',mqtt_port);client.loop_start()
        def close_mqtt():client.disconnect();client.loop_stop()
        self.addCleanup(close_mqtt)
        self.assertTrue(connected.wait(5));self.assertTrue(subscribed.wait(5))
        request={'request_id':'mqtt-task','actor':'alpha@project','session':'session-a','operation':'task.create','payload':{'title':'Across both protocols'}}
        info=client.publish(f'ae/commands/{source}',json.dumps(request),qos=1);info.wait_for_publish(5)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and not any(p.get('request_id')=='mqtt-task' for _,p in packets):time.sleep(.02)
        reply=next(p for _,p in packets if p.get('request_id')=='mqtt-task')
        self.assertTrue(reply['ok'],reply)
        task_id=reply['result']['id']
        received=[]
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not received:
            frame=native.receive(.1)
            if frame:received.append(json.loads(frame[2]))
        self.assertEqual(received[0]['ref'],task_id)
        request.update(request_id='nats-claim',operation='task.claim',payload={'id':task_id,'version':1})
        native.publish(f'ae.commands.{source}',json.dumps(request).encode());native.flush()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and self.ae.task(task_id,'alpha@project')['state']!='active':time.sleep(.02)
        self.assertEqual(self.ae.task(task_id,'alpha@project')['state'],'active')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not any(p.get('kind')=='task.changed' for _,p in packets):time.sleep(.02)
        self.assertTrue(any(p.get('kind')=='task.changed' for _,p in packets))
        # MQTT replay uses the same application request ID; never creates another task.
        original=dict(request,request_id='mqtt-task',operation='task.create',payload={'title':'Across both protocols'})
        client.publish(f'ae/commands/{source}',json.dumps(original),qos=1).wait_for_publish(5)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ae_tasks').fetchone()[0],1)
        bus.process.terminate()
        bus.process.wait(timeout=5)
        offline=self.command('task.create',{'title':'Committed during broker restart'})
        deadline=time.monotonic()+12
        while time.monotonic()<deadline and not any(p.get('ref')==offline['id'] for _,p in packets):time.sleep(.05)
        self.assertTrue(bus.connected,bus.error)
        # MQTT reconnects/resubscribes; if live notification predates resubscription,
        # authoritative replay must still recover the event with the same source.
        replay=self.ae.events('alpha@project',source=source)
        self.assertTrue(any(e['ref']==offline['id'] for e in replay['events']))
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            pending=self.conn.execute('SELECT MAX(sequence) FROM ae_events').fetchone()[0]
            published=self.conn.execute('SELECT sequence FROM ae_delivery WHERE id=1').fetchone()[0]
            if published==pending:break
            time.sleep(.05)
        self.assertEqual(published,pending)
