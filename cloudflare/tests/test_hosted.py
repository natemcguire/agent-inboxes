"""Real HTTP → Python Worker → Durable Object SQLite integration tests.

Run from the repo: python3 cloudflare/tests/test_hosted.py
Requires npm ci in cloudflare/, uv, and Python cryptography.
No database, authorization, or service methods are mocked.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from identity import Identity

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Runtime:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.identity = Identity()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        config = json.loads((ROOT/'wrangler.jsonc').read_text())
        config.pop('routes',None)
        variables = dict(config['vars'], ACCESS_DOMAIN='integration.cloudflareaccess.com', ACCESS_AUD='integration-audience', MEMBERS='nate@example.com,josh@example.com', ACCESS_JWKS=json.dumps(self.identity.jwks), WORKSPACE='integration-'+uuid.uuid4().hex)
        self.environment='integration-'+uuid.uuid4().hex[:8]
        config['env'] = {self.environment: {'vars': variables, 'durable_objects': config['durable_objects']}}
        self.config = ROOT/('wrangler.'+self.environment+'.local.json')
        self.config.write_text(json.dumps(config))
        # An environment-specific empty dotenv prevents the developer's .dev.vars
        # from overriding the isolated, freshly signed integration configuration.
        self.dotenv = ROOT/('.dev.vars.'+self.environment)
        self.previous = self.dotenv.read_bytes() if self.dotenv.exists() else None
        self.dotenv.write_text('')
        self.process = None
        self.log = (self.directory/'worker.log').open('w+')
        self.opener = urllib.request.build_opener(NoRedirect())

    def start(self):
        self.process = subprocess.Popen(['npm','run','dev','--','--config',str(self.config),'--env',self.environment,'--port',str(self.port),'--persist-to',str(self.directory/'state')],cwd=ROOT,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline = time.monotonic()+45
        while time.monotonic() < deadline:
            try:
                if self.call('GET','/v1/projects')[0] == 401:
                    return
            except OSError:
                pass
            if self.process.poll() is not None:
                break
            time.sleep(.2)
        self.log.flush()
        raise RuntimeError((self.directory/'worker.log').read_text()[-6000:])

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGTERM)
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid,signal.SIGKILL);self.process.wait()
        self.process = None

    def close(self):
        self.stop()
        self.config.unlink(missing_ok=True)
        if self.previous is None:self.dotenv.unlink(missing_ok=True)
        else:self.dotenv.write_bytes(self.previous)
        self.log.close()

    def call(self, method, path, body=None, human=None, agent=None, session='terminal-1', headers=None, key=None):
        h = {'Content-Type':'application/json'}
        if human:h['Cf-Access-Jwt-Assertion'] = self.identity.token(human)
        if agent:h.update({'Authorization':'Bearer '+agent['token'],'X-Agent-Session':session,'X-Agent-Address':agent['address']})
        if key:h['Idempotency-Key'] = key
        if headers:h.update(headers)
        request=urllib.request.Request(self.url+path,method=method,headers=h,data=json.dumps(body).encode() if body is not None else None)
        try:result=self.opener.open(request,timeout=10)
        except urllib.error.HTTPError as exc:result=exc
        with result:
            raw=result.read().decode()
            try:data=json.loads(raw)
            except ValueError:data=raw
            return result.status,data,dict(result.headers)

    def expect(self, status, *args, **kwargs):
        actual,data,_=self.call(*args, **kwargs)
        assert actual == status, (args, status, actual, data)
        return data


def run(runtime):
    r=runtime
    nate,josh='nate@example.com','josh@example.com'
    for identity in [r.identity.token('stranger@example.com'),r.identity.token(nate,exp=0),r.identity.token(nate,aud=['wrong']),r.identity.token(nate,iss='https://evil.example')]:
        r.expect(401,'GET','/v1/hosted/me',headers={'Cf-Access-Jwt-Assertion':identity})
    good = r.identity.token(nate)
    parts=good.split('.');parts[-1]=parts[-1][:-8]+'AAAAAAAA'
    r.expect(401,'GET','/v1/hosted/me',headers={'Cf-Access-Jwt-Assertion':'.'.join(parts)})
    r.expect(401,'GET','/v1/hosted/me',headers={'X-Hosted-Member':nate,'Authorization':'Bearer ain_forged'})
    r.expect(200,'GET','/v1/hosted/me',headers={'Cookie':'CF_Authorization='+good})
    assert 'One place to follow' in r.expect(200,'GET','/welcome',human=nate)
    r.expect(200,'GET','/',human=nate)
    assert 'One place to follow' in r.expect(200,'GET','/welcome',human=nate)
    r.expect(403,'POST','/v1/hosted/tokens',{'project':'harbor','family':'forged'},human=nate,headers={'Origin':'https://evil.example'})
    a = r.expect(201,'POST','/v1/hosted/tokens',{'project':'harbor','family':'codex-nate'},human=nate)
    b = r.expect(201,'POST','/v1/hosted/tokens',{'project':'harbor','family':'claude-josh'},human=josh)
    private = r.expect(201,'POST','/v1/hosted/tokens',{'project':'private','family':'reviewer'},human=josh)
    status,_,headers = r.call('GET','/welcome',human=nate)
    assert status==303 and headers['Location']=='/'
    print('PASS signed logins, forged/expired JWTs, CSRF, and automatic welcome retirement',flush=True)

    assert [p['slug'] for p in r.expect(200,'GET','/v1/projects',agent=a)['projects']]==['harbor']
    for path in ['/v1/projects/private/threads','/v1/inboxes/claude-josh@harbor/threads']:
        r.expect(403,'GET',path,agent=a)
    r.expect(403,'POST','/v1/leases/claim',{'project':'harbor','family':'claude-josh'},agent=a)
    r.expect(403,'POST','/v1/hosted/tokens/'+b['id']+'/revoke',{},human=nate)
    for agent in (a,b):
        r.expect(200,'POST','/v1/leases/claim',{'family':agent['family'],'project':'harbor'},agent=agent)
    r.expect(403,'PUT','/v1/inboxes/nate@harbor',{},agent=a)
    message={'from':a['address'],'to':[b['address']],'subject':'Design: shared checkout','body_markdown':'Review the retry path.'}
    r.expect(403,'POST','/v1/emails',{**message,'from':b['address']},agent=a,key='spoof')
    r.expect(403,'POST','/v1/emails',{**message,'to':[private['address']]},agent=a,key='cross-project')
    first=r.expect(201,'POST','/v1/emails',message,agent=a,key='message-1')
    assert first==r.expect(201,'POST','/v1/emails',message,agent=a,key='message-1')
    r.expect(409,'POST','/v1/emails',{**message,'body_markdown':'Changed'},agent=a,key='message-1')
    reply=r.expect(201,'POST',f"/v1/emails/{first['email_id']}/reply",{'from':b['address'],'body_markdown':'One request, one result.'},agent=b,key='reply-1')
    assert reply['thread_id']==first['thread_id'] and reply['reply_to_email_id']==first['email_id']
    r.expect(403,'GET',f"/v1/emails/{first['email_id']}/status",agent=private)
    # Identical human and agent perspectives do not mutate receipts or liveness.
    before=r.expect(200,'GET','/v1/projects/harbor/overview',human=nate)['agents']
    thread=r.expect(200,'GET',f"/v1/inboxes/{b['address']}/threads/{first['thread_id']}",human=nate)
    assert len(thread['emails'])==2
    r.expect(200,'GET','/v1/announcements?inbox='+b['address'],human=nate)
    after=r.expect(200,'GET','/v1/projects/harbor/overview',human=nate)['agents']
    assert before==after
    r.expect(403,'POST',f"/v1/inboxes/{b['address']}/threads/{first['thread_id']}/read",{},human=nate)
    r.expect(200,'POST',f"/v1/inboxes/{b['address']}/threads/{first['thread_id']}/read",{},agent=b)
    # Humans send/reply as themselves, regardless of the observed inbox.
    human_reply=r.expect(201,'POST',f"/v1/emails/{reply['email_id']}/reply",{'from':'nate@harbor','body_markdown':'Ready for review.'},human=nate,key='human-reply')
    assert human_reply['thread_id']==first['thread_id']
    r.expect(403,'POST','/v1/emails',{**message,'from':b['address']},human=nate,key='human-spoof')
    r.expect(201,'POST','/v1/announcements',{'from':a['address'],'subject':'Preview ready','body_markdown':'Review the shared preview.'},agent=a,key='announcement')
    print('PASS project/agent isolation, send/reply/idempotency, announcements, read-only observation',flush=True)

    def reserve(agent,paths,key,session='terminal-1'):
        return r.call('POST','/v1/projects/harbor/reservations',{'holder':agent['address'],'paths':paths,'ttl_seconds':60},agent=agent,key=key,session=session)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(reserve,agent,['src/check.py'],'race-'+agent['id']) for agent in (a,b)]
        results=[f.result() for f in futures]
    assert sorted(result[0] for result in results)==[201,409],results
    winner,loser=(a,b) if results[0][0]==201 else (b,a)
    assert reserve(loser,['src/free.py','src/check.py'],'atomic')[0]==409
    leases=r.expect(200,'GET','/v1/projects/harbor/reservations',human=nate)['reservations']
    assert [x['path'] for x in leases]==['src/check.py']
    # A different terminal cannot release the first terminal's reservation.
    r.expect(200,'POST','/v1/projects/harbor/reservations/release',{'holder':winner['address'],'all':True},agent=winner,session='terminal-2')
    assert len(r.expect(200,'GET','/v1/projects/harbor/reservations',human=nate)['reservations'])==1
    r.expect(200,'POST','/v1/projects/harbor/reservations/renew',{'holder':winner['address'],'all':True},agent=winner)
    r.expect(200,'POST','/v1/projects/harbor/reservations/release',{'holder':winner['address'],'all':True},agent=winner)
    assert reserve(loser,['src/check.py'],'after-release')[0]==201
    def command(agent,op,payload):
        return r.expect(200,'POST','/v1/ae/command',{'actor':agent['address'],'session':'terminal-1','request_id':uuid.uuid4().hex,'operation':op,'payload':payload},agent=agent)
    task=command(a,'task.create',{'title':'Review checkout','description':'PRD: retry safely','target':b['address']})
    active=command(b,'task.claim',{'id':task['id'],'version':task['version']})
    assert active['owner']==b['address'] and active['state']=='active'
    completed=command(b,'task.complete',{'id':task['id'],'version':active['version'],'result':'Verified the real HTTP retry and reservation conflict.'})
    assert completed['state']=='completed'
    print('PASS concurrent reservations, atomic conflict rollback, session ownership, renew/release, task lifecycle',flush=True)

    initial=r.expect(200,'GET',f"/v1/inboxes/{b['address']}/watch?timeout=0",agent=b)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        started=time.monotonic()
        waiting=pool.submit(r.expect,200,'GET',f"/v1/inboxes/{b['address']}/watch?timeout=4&after={initial['cursor']}",agent=b)
        time.sleep(.3)
        r.expect(201,'POST','/v1/emails',{**message,'subject':'Design: watch wakeup'},agent=a,key='watch-message')
        assert waiting.result()['changed'] and time.monotonic()-started<3
    brief=r.expect(200,'GET',f"/v1/ae/brief?actor={a['address']}&session=terminal-1",agent=a)
    query=urllib.parse.urlencode({'actor':a['address'],'session':'terminal-1','source':brief['source'],'after':brief['cursor'],'timeout':0,'coalesce':0})
    r.expect(200,'GET','/v1/ae/watch?'+query,agent=a)
    r.expect(200,'POST','/v1/hosted/tokens/'+b['id']+'/revoke',{},human=josh)
    r.expect(401,'GET','/v1/projects',agent=b)
    print('PASS long polling while another client writes, AE cursor contract, immediate key revocation',flush=True)

    r.stop();r.start()
    persisted=r.expect(200,'GET',f"/v1/projects/harbor/threads/{first['thread_id']}",human=nate)
    assert len(persisted['emails'])==3
    r.expect(303,'GET','/welcome',human=josh)
    r.expect(401,'GET','/v1/projects',agent=b)
    r.stop()
    config=json.loads(r.config.read_text())
    config['env'][r.environment]['vars']['MEMBERS']=josh
    r.config.write_text(json.dumps(config))
    r.start()
    r.expect(401,'GET','/v1/projects',agent=a)
    r.expect(401,'GET','/v1/hosted/me',human=nate)
    r.expect(200,'GET','/v1/hosted/me',human=josh)
    r.stop()
    inspected=False
    for path in (r.directory/'state').rglob('*.sqlite'):
        with sqlite3.connect(path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='hosted_tokens'").fetchone():continue
            inspected=True
            assert db.execute('SELECT complete,body FROM hosted_welcome').fetchone()==(1,None)
            assert db.execute('SELECT count(*) FROM hosted_members').fetchone()[0]==2
            assert all(len(row[0])==64 and not row[0].startswith('ain_') for row in db.execute('SELECT digest FROM hosted_tokens'))
            assert db.execute("SELECT COUNT(*) FROM reservations WHERE path='src/free.py'").fetchone()[0]==0
    assert inspected,'The real Durable Object SQLite database was not found'
    print('PASS restart durability, welcome content deletion, hashed keys, and persisted rollback evidence',flush=True)


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='agent-inboxes-worker-test-') as directory:
        runtime=Runtime(directory)
        try:
            runtime.start();run(runtime)
        except Exception:
            runtime.log.flush()
            print((Path(directory)/'worker.log').read_text()[-4500:])
            raise
        finally:runtime.close()
