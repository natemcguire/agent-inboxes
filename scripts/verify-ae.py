"""Isolated AE acceptance; downloads a verified broker into temporary storage."""
import json,os,signal,socket,subprocess,sys,tempfile,time,urllib.request,shutil
from pathlib import Path
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root))
python=sys.executable
def freeport():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
with tempfile.TemporaryDirectory(prefix='ae-http-') as tmp:
    tmp=Path(tmp);http,nats,mqtt=[freeport() for _ in range(3)]
    from unittest.mock import patch
    from agent_inbox.ae_bus import provision
    with patch.dict(os.environ, {'AGENT_INBOX_DIR':str(tmp)}):
        provision()
    env=dict(os.environ,AGENT_INBOX_DIR=str(tmp),AGENT_INBOX_DB=str(tmp/'inbox.db'),AGENT_INBOX_CLOUD_CONFIG=str(tmp/'absent-cloud.json'),
             AGENT_INBOX_URL=f'http://127.0.0.1:{http}',AGENT_INBOX_AE_NATS_PORT=str(nats),AGENT_INBOX_AE_MQTT_PORT=str(mqtt),PYTHONPATH=str(root))
    code="from unittest.mock import patch; from agent_inbox.cli import main\nwith patch('agent_inbox.updates.UpdateWorker.start'):\n main(['serve','--port',"+repr(str(http))+"])"
    with (tmp/'server.log').open('w+') as log:
        process=subprocess.Popen([python,'-c',code],cwd=tmp,env=env,stdout=log,stderr=log)
        try:
            ready=False
            for _ in range(100):
                if process.poll() is not None:break
                try:
                    health=json.load(urllib.request.urlopen(env['AGENT_INBOX_URL']+'/healthz',timeout=1))
                    if health['ae_transport']['connected']:ready=True;break
                except Exception:time.sleep(.1)
            if not ready:
                log.seek(0);raise AssertionError(log.read())
            def cli(actor,*args):
                result=subprocess.run([python,str(root/'bin/agent-inbox'),'ae','--actor',actor,'--session','acceptance',*args],cwd=tmp,env=env,capture_output=True,text=True,check=True)
                return json.loads(result.stdout)
            before=cli('alice@demo','context')
            task=cli('alice@demo','task','create','--title','Release handoff','--description','Keep this context after restart','--path','src/')
            events=cli('bob@demo','events','--source',before['source'],'--after',str(before['cursor']))
            assert any(e['ref']==task['id'] for e in events['events'])
            task=cli('bob@demo','task','claim',task['id'],'--version','1')
            context=cli('bob@demo','context')
            assert context['sections']['assignments'][0]['id']==task['id']
            assert context['sections']['attention'][0]['action']=='reserve_paths'
            task=cli('bob@demo','task','handoff',task['id'],'--version','2','--target','alice@demo','--note','Tests passed; review the final patch')
            restored=cli('alice@demo','context')['sections']['ready_queue'][0]
            assert restored['note']=='Tests passed; review the final patch'
            history=cli('alice@demo','task','history',task['id'])
            assert len(history['history'])==3
            document=urllib.request.urlopen(env['AGENT_INBOX_URL']+'/').read().decode()
            assert 'Agent Inbox' in document
            status=cli('alice@demo','status')
            assert status['transport']['connected'] and status['transport']['mqtt_port']==mqtt
            print('PASS: real serve startup, managed NATS/MQTT, CLI/HTTP context, event replay, claim, reservation guidance, handoff/history, and bundled UI route')
        finally:
            process.send_signal(signal.SIGINT)
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait();raise
            for port in (nats,mqtt):
                with socket.socket() as s:
                    assert s.connect_ex(('127.0.0.1',port))!=0, 'Owned broker was left running'
