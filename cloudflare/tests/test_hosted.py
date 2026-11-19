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
import sys
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
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            self.hub_port = sock.getsockname()[1]
        self.hub_url = f'http://127.0.0.1:{self.hub_port}'
        self.hub_name = 'auth-test-' + uuid.uuid4().hex[:8]
        self.hub_config = ROOT/('wrangler.'+self.hub_name+'.local.json')
        self.hub_config.write_text(json.dumps({'name':self.hub_name,'main':'tests/auth_hub.js','compatibility_date':'2026-09-15','vars':{'JWKS':json.dumps(self.identity.jwks)}}))
        self.hub_process = None
        variables = dict(config['vars'], SSO_ORIGIN=self.hub_url, MEMBERS='nate@example.com,josh@example.com', WORKSPACE='integration-'+uuid.uuid4().hex)
        self.environment='integration-'+uuid.uuid4().hex[:8]
        config['env'] = {self.environment: {'vars': variables, 'durable_objects': config['durable_objects'], 'services':[{'binding':'AUTH_HUB','service':self.hub_name}]}}
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
        if not self.hub_process:
            self.hub_process = subprocess.Popen(['npx','wrangler','dev','--config',str(self.hub_config),'--port',str(self.hub_port)],cwd=ROOT,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                try:
                    urllib.request.urlopen(self.hub_url, timeout=1).close()
                    break
                except OSError:
                    time.sleep(.2)
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
        if self.hub_process and self.hub_process.poll() is None:
            os.killpg(self.hub_process.pid,signal.SIGTERM)
            self.hub_process.wait(timeout=10)
        self.hub_config.unlink(missing_ok=True)
        self.config.unlink(missing_ok=True)
        if self.previous is None:self.dotenv.unlink(missing_ok=True)
        else:self.dotenv.write_bytes(self.previous)
        self.log.close()

    def call(self, method, path, body=None, human=None, agent=None, session='terminal-1', headers=None, key=None):
        h = {'Content-Type':'application/json'}
        if human:h['Cookie'] = '__Host-ain_session=' + self.identity.token(human)
        if agent:h.update({'Authorization':'Bearer '+agent['token'],'X-Agent-Session':session,'X-Agent-Address':agent['address']})
        if key:h['Idempotency-Key'] = key
        if headers:
            prior_cookie = h.get('Cookie')
            h.update(headers)
            if prior_cookie and headers.get('Cookie'):
                h['Cookie'] = prior_cookie + '; ' + headers['Cookie']
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
        r.expect(401,'GET','/v1/hosted/me',headers={'Cookie':'__Host-ain_session='+identity})
    good = r.identity.token(nate)
    parts=good.split('.');parts[-1]=parts[-1][:-8]+'AAAAAAAA'
    r.expect(401,'GET','/v1/hosted/me',headers={'Cookie':'__Host-ain_session='+'.'.join(parts)})
    r.expect(401,'GET','/v1/hosted/me',headers={'X-Hosted-Member':nate,'Authorization':'Bearer ain_forged'})
    r.expect(200,'GET','/v1/hosted/me',headers={'Cookie':'__Host-ain_session='+good})
    assert 'One place to follow' in r.expect(200,'GET','/welcome',human=nate)
    r.expect(200,'GET','/',human=nate)
    assert 'One place to follow' in r.expect(200,'GET','/welcome',human=nate)
    r.expect(403,'POST','/v1/hosted/tokens',{'project':'harbor','family':'forged'},human=nate,headers={'Origin':'https://evil.example'})
    a = r.expect(201,'POST','/v1/hosted/tokens',{'project':'harbor','family':'codex-nate'},human=nate)
    b = r.expect(201,'POST','/v1/hosted/tokens',{'project':'harbor','family':'claude-josh'},human=josh)
    private = r.expect(201,'POST','/v1/hosted/tokens',{'project':'private','family':'reviewer'},human=josh)
    # A valid stored agent key does not bypass revocation of its human owner.
    request = urllib.request.Request(r.hub_url+'/test/revoke', method='POST',
        headers={'Content-Type':'application/json'}, data=json.dumps({'email':josh}).encode())
    urllib.request.urlopen(request).close()
    r.expect(401,'GET','/v1/hosted/me',human=josh)
    r.expect(401,'GET','/v1/projects',agent=b)
    request = urllib.request.Request(r.hub_url+'/test/restore', method='POST',
        headers={'Content-Type':'application/json'}, data=json.dumps({'email':josh}).encode())
    urllib.request.urlopen(request).close()
    r.expect(200,'GET','/v1/projects',agent=b)
    r.expect(401,'GET','/v1/hosted/me',headers={'Cf-Access-Jwt-Assertion':good})
    r.expect(401,'POST','/auth/session',{'token':good,'state':'forged'})
    print('PASS live product revocation disables browser and scoped agent access; legacy headers and forged login state rejected',flush=True)
    status,_,headers = r.call('GET','/welcome',human=nate)
    assert status==303 and headers['Location']=='/'
    print('PASS signed logins, forged/expired JWTs, CSRF, and automatic welcome retirement',flush=True)

    assert r.expect(200,'GET','/v1/hosted/me',human=nate)['self_registration'] is True
    status, automatic, setup_headers = r.call('POST','/v1/hosted/registration/setup',{},human=nate)
    assert status==201
    cookie_header=next(v for k,v in setup_headers.items() if k.lower()=='set-cookie')
    assert all(flag in cookie_header for flag in ('HttpOnly','Secure','SameSite=Strict'))
    cookie=cookie_header.split(';')[0]
    again=r.expect(200,'POST','/v1/hosted/registration/setup',{},human=nate,headers={'Cookie':cookie})
    assert again==automatic
    separate=r.expect(201,'POST','/v1/hosted/registration/setup',{},human=josh,headers={'Cookie':cookie})
    assert separate['owner']==josh and separate['token']!=automatic['token']
    r.expect(401,'POST','/v1/hosted/registration/setup',{})
    r.expect(403,'POST','/v1/hosted/registration/setup',{},human=nate,headers={'Origin':'https://evil.example'})
    print('PASS automatic registration on sign-in, HttpOnly credential persistence, owner isolation and CSRF protection',flush=True)

    # An owner-issued admin view key reads every inbox and sends as itself in any project.
    admin = r.expect(201,'POST','/v1/hosted/tokens',{'project':'*','family':'claude','label':'Admin view'},human=nate)
    assert admin['address']=='claude@*' and admin['project']=='*'
    admin_agent = {**admin,'address':'claude@harbor'}
    everything = r.expect(200,'GET','/v1/projects',agent=admin_agent)['projects']
    assert {p['slug'] for p in everything} >= {'harbor','private'}
    assert r.expect(200,'GET','/v1/inboxes/codex-nate%40harbor/threads',agent=admin_agent)['threads'] is not None
    assert r.expect(200,'GET','/v1/inboxes/reviewer%40private/threads',agent=admin_agent)['threads'] is not None
    assert r.expect(200,'GET','/v1/projects/private/threads',agent=admin_agent)['threads'] is not None
    assert len(r.expect(200,'GET','/v1/inboxes',agent=admin_agent)['inboxes']) >= 4
    note = r.expect(201,'POST','/v1/emails',{'from':'claude@harbor','to':['codex-nate@harbor'],'subject':'Design: admin hello','body_markdown':'From the admin view key.'},agent=admin_agent,key='admin-hello-1')
    assert note['thread_id']
    r.expect(403,'POST','/v1/emails',{'from':'codex-nate@harbor','to':['claude@harbor'],'subject':'forged','body_markdown':'no'},agent=admin_agent,key='admin-forge-1')
    r.expect(403,'POST','/v1/emails',{'from':'claude@nowhere','to':['codex-nate@nowhere'],'subject':'lost','body_markdown':'no'},agent=admin_agent,key='admin-lost-1')
    r.expect(403,'POST','/v1/projects/harbor/reservations',{'holder':'claude@harbor','paths':['x']},agent=admin_agent,key='admin-res-1')
    r.expect(403,'GET','/v1/inboxes/codex-nate%40harbor/watch',agent=admin_agent)
    r.expect(403,'POST','/v1/leases/claim',{'family':'claude','project':'harbor'},agent=admin_agent)
    print('PASS admin view key: every project and inbox readable, sends only as itself, no leases, reservations or watches',flush=True)

    # One human login authorizes unattended enrollment across projects.
    registrar = r.expect(201,'POST','/v1/hosted/registrars',{'label':'Trusted machine'},human=nate)
    rh = {'Authorization':'Bearer '+registrar['token']}
    r.expect(401,'GET','/v1/hosted/registration',headers={'Authorization':'Bearer ainr_forged'})
    assert r.expect(200,'GET','/v1/hosted/registration',headers=rh)['owner']==nate
    assert 'token' not in r.expect(200,'GET','/v1/hosted/registrars',human=nate)['registrars'][0]
    for method,path,body in [('GET','/v1/projects',None),('GET','/v1/hosted/tokens',None),('POST','/v1/hosted/registrars',{})]:
        r.expect(403,method,path,body,headers=rh)
    r.expect(403,'POST','/v1/hosted/register',{'project':'auto','family':'nate'},headers=rh)
    r.expect(403,'POST','/v1/hosted/register',{'project':'auto','family':'new'},agent=a)
    r.expect(403,'POST','/v1/hosted/registrars/'+registrar['id']+'/revoke',{},human=josh)
    auto = r.expect(201,'POST','/v1/hosted/register',{'project':'auto','family':'robot','owner':josh},headers=rh)
    assert auto['owner']==nate
    retried_body={'project':'auto','family':'robot','registration_id':uuid.uuid4().hex}
    extra=r.expect(201,'POST','/v1/hosted/register',retried_body,headers=rh)
    assert extra==r.expect(201,'POST','/v1/hosted/register',retried_body,headers=rh)
    assert extra['token']!=auto['token']
    r.expect(409,'POST','/v1/hosted/register',{**retried_body,'label':'changed'},headers=rh)
    other_owner={'Authorization':'Bearer '+separate['token']}
    r.expect(400,'POST','/v1/hosted/register',{'project':'auto','family':'robot'},headers=other_owner)
    r.expect(200,'POST','/v1/hosted/tokens/'+extra['id']+'/revoke',{},human=nate)
    r.expect(403,'POST','/v1/hosted/register',retried_body,headers=rh)
    r.expect(200,'GET','/v1/hosted/health',agent=auto)

    r.expect(200,'POST','/v1/leases/claim',{'project':'auto','family':'robot'},agent=auto)
    r.expect(403,'GET','/v1/projects/private/threads',agent=auto)
    # Exercise the actual CLI, private files, repeated registration and two
    # simultaneous terminals without inheriting the developer's hosted settings.
    with tempfile.TemporaryDirectory() as directory:
        environment={k:v for k,v in os.environ.items() if not k.startswith('AGENT_INBOX_')}
        environment.update(AGENT_INBOX_DIR=directory, PYTHONPATH=str(ROOT.parent))
        base=[sys.executable,'-m','agent_inbox.cli','hosted']
        def cli(arguments, credential=None):
            result=subprocess.run(base+arguments, input=credential, text=True, capture_output=True, env=environment, cwd=ROOT.parent)
            assert result.returncode==0, result.stderr
            assert registrar['token'] not in result.stdout
            return result.stdout
        cli(['login','--url',r.url,'--credential-stdin'],registrar['token'])
        args=['register','--url',r.url,'--project','cli-project','--agent','robot-cli','--no-instructions']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outputs=list(pool.map(lambda _:cli(args),range(2)))
        assert outputs[0]==outputs[1] and 'TOKEN_FILE=' in outputs[0]
        assert 'ain_' not in outputs[0]
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in Path(directory).rglob('*') if path.is_file())
        cli(['status','--url',r.url])
        cli(['register','--url',r.url,'--project','cli-other','--agent','robot-cli'])
        cli(['register','--url',r.url,'--project','cli-unrelated','--agent','unrelated-agent'])
        watch=base[:-1]+['watch','--all','--url',r.url,'--agent','robot-cli','--timeout','3','--json']
        quiet=subprocess.run(watch+['--timeout','0.5','--quiet'],capture_output=True,text=True,env=environment,cwd=ROOT.parent,timeout=5)
        assert quiet.returncode==3 and quiet.stdout=='', (quiet.returncode,quiet.stdout,quiet.stderr)
        started=time.monotonic()
        waiting=subprocess.Popen(watch,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment,cwd=ROOT.parent)
        time.sleep(.4)
        mail=r.expect(201,'POST','/v1/emails',{'from':'nate@cli-other','to':['robot-cli@cli-other'],'subject':'Multi-project wakeup','body_markdown':'Watch both projects.'},human=nate,key='multi-watch')
        stdout,stderr=waiting.communicate(timeout=5)
        result=json.loads(stdout)
        assert waiting.returncode==0 and time.monotonic()-started<2.8, (stdout,stderr)
        assert result['watched']==2 and [p['project'] for p in result['projects']]==['cli-other'], result
        assert result['projects'][0]['latest']['thread_id']==mail['thread_id']
        saved=[json.loads(p.read_text()) for p in Path(directory).rglob('*.json')]
        watched_agent=next(d for d in saved if d.get('project')=='cli-other')
        assert r.expect(200,'GET','/v1/inboxes/robot-cli@cli-other/threads?unread=true',agent=watched_agent)['threads']
        r.expect(200,'POST','/v1/inboxes/robot-cli@cli-other/threads/'+mail['thread_id']+'/read',{},agent=watched_agent)
        r.expect(200,'POST','/v1/hosted/tokens/'+watched_agent['id']+'/revoke',{},human=nate)
        failed=subprocess.run(watch+['--quiet'],capture_output=True,text=True,env=environment,cwd=ROOT.parent,timeout=5)
        assert failed.returncode==1 and json.loads(failed.stdout)['errors'][0]['project']=='cli-other', (failed.stdout,failed.stderr)
        print('PASS multi-project CLI watch, global deadline, quiet timeout, identity filtering, unchanged receipts and visible authentication failures',flush=True)

    r.expect(200,'POST','/v1/hosted/registrars/'+registrar['id']+'/revoke',{},human=nate)
    r.expect(401,'GET','/v1/hosted/registration',headers=rh)
    r.expect(401,'GET','/v1/projects',agent=auto)
    persistent_registrar=r.expect(201,'POST','/v1/hosted/registrars',{},human=nate)
    persistent_headers={'Authorization':'Bearer '+persistent_registrar['token']}
    print('PASS autonomous enrollment, owner binding, registrar isolation, private CLI credentials, concurrent registration, and cascading revocation',flush=True)

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
    hit=r.expect(200,'GET','/v1/search?q=checkot',human=nate)
    assert hit['correction']=='checkout' and hit['results'][0]['thread_id']==first['thread_id']
    hit=r.expect(200,'GET','/v1/search?q=%22one%20request%22',human=nate)
    assert hit['results'][0]['email_id']==reply['email_id']
    assert r.expect(200,'GET','/v1/search?q=checkout',agent=private)['total']==0
    assert r.expect(200,'GET','/v1/search?q=checkot',agent=private)['correction'] is None
    assert r.expect(200,'GET','/v1/search?q=project%3Aprivate',agent=a)['total']==0
    r.expect(403,'GET','/v1/search?project=private',agent=a)
    r.expect(401,'GET','/v1/search?q=checkout')
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

    # Open sign-up. The front door is public and starts account creation at the hub.
    maker,second='maker@example.com','second@example.com'
    code,page,_=r.call('GET','/')
    assert code==200 and 'action="/auth/signup"' in page and 'Create free account' in page, (code,page[:200])
    assert r.call('GET','/signup')[0]==200 and 'box-sizing' in r.expect(200,'GET','/signup.css')
    code,_,headers=r.call('GET','/auth/signup?email=maker%40example.com')
    query=urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)
    assert code==302 and headers['Location'].startswith(r.hub_url+'/authorize?'), headers
    assert query['intent']==['signup'] and query['email']==[maker] and query['app']==['agent-inboxes'], query
    assert query['return']==[r.url+'/auth/callback'] and '__Host-ain_state='+query['state'][0] in headers.get('Set-Cookie', headers.get('set-cookie',''))
    code,_,headers=r.call('GET','/auth/login')
    assert code==302 and 'intent' not in urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)
    code,_,headers=r.call('GET','/signup',human=maker)
    assert code==302 and headers['Location']=='/'

    # A person outside the shared workspace starts in an empty workspace of their own.
    assert r.expect(200,'GET','/v1/hosted/me',human=maker)['members']==[maker]
    assert r.expect(200,'GET','/v1/projects',human=maker)['projects']==[]
    assert r.call('GET',f"/v1/projects/harbor/threads/{first['thread_id']}",human=maker)[0]!=200
    assert r.expect(200,'GET','/v1/search?q=checkout',human=maker)['total']==0
    # The names already used in the shared workspace are free here: nothing is global to take.
    mine=r.expect(201,'POST','/v1/hosted/tokens',{'project':'harbor','family':'codex-nate'},human=maker)
    r.expect(200,'POST','/v1/leases/claim',{'family':'codex-nate','project':'harbor'},agent=mine)
    assert r.call('GET',f"/v1/inboxes/codex-nate%40harbor/threads/{first['thread_id']}",agent=mine)[0]!=200
    assert r.expect(200,'GET','/v1/search?q=checkout',agent=mine)['total']==0
    note=r.expect(201,'POST','/v1/emails',{'from':'maker@harbor','to':['codex-nate@harbor'],'subject':'Design: private plan','body_markdown':'Only this workspace reads about the zebra.'},human=maker,key='maker-note')
    assert r.expect(200,'GET','/v1/search?q=zebra',agent=mine)['results'][0]['thread_id']==note['thread_id']
    for viewer in ({'human':nate},{'human':josh},{'agent':a},{'agent':admin_agent},{'human':second}):
        assert r.expect(200,'GET','/v1/search?q=zebra',**viewer)['total']==0, viewer
        assert r.call('GET',f"/v1/projects/harbor/threads/{note['thread_id']}",**viewer)[0]!=200, viewer
    assert r.expect(200,'GET','/v1/projects',human=second)['projects']==[]
    assert r.expect(200,'GET','/v1/hosted/tokens',human=second)['tokens']==[]
    assert all(key['owner']==nate or key['owner']==josh for key in r.expect(200,'GET','/v1/hosted/tokens',human=nate)['tokens'])
    r.expect(403,'POST','/v1/hosted/tokens/'+mine['id']+'/revoke',{},human=nate)
    r.expect(403,'POST','/v1/hosted/tokens/'+a['id']+'/revoke',{},human=maker)
    # Unattended enrollment works inside the personal workspace and stays inside it.
    enrollment=r.expect(201,'POST','/v1/hosted/registration/setup',{},human=maker)
    enrolled=r.expect(201,'POST','/v1/hosted/register',{'project':'harbor','family':'robot-maker'},headers={'Authorization':'Bearer '+enrollment['token']})
    assert [p['slug'] for p in r.expect(200,'GET','/v1/projects',agent=enrolled)['projects']]==['harbor']
    assert r.expect(200,'GET','/v1/search?q=checkout',agent=enrolled)['total']==0
    # A key alone answers "who am I": address, project, owner and workspace, never the secret.
    identity=r.expect(200,'GET','/v1/hosted/whoami',headers={'Authorization':'Bearer '+mine['token']})
    assert (identity['address'],identity['project'],identity['agent'],identity['owner'])==('codex-nate@harbor','harbor','codex-nate',maker), identity
    shared_identity=r.expect(200,'GET','/v1/hosted/whoami',headers={'Authorization':'Bearer '+a['token']})
    assert shared_identity['address']=='codex-nate@harbor' and shared_identity['owner']==nate
    assert shared_identity['workspace']!=identity['workspace'] and mine['token'] not in json.dumps(identity)
    assert r.expect(200,'GET','/v1/hosted/whoami',human=maker)['email']==maker
    r.expect(401,'GET','/v1/hosted/whoami')
    # The same answer is a directory: everyone in the project, what each is for, and which entry is the caller.
    people={entry['address']:entry for entry in identity['directory']}
    assert set(people)=={'codex-nate@harbor','robot-maker@harbor','maker@harbor'}, people
    assert people['codex-nate@harbor']['you'] is True and people['robot-maker@harbor']['you'] is False
    assert people['maker@harbor']['kind']=='human' and people['robot-maker@harbor']['owner']==maker
    labelled=r.expect(201,'POST','/v1/hosted/tokens',{'project':'garden','family':'codex-nate','label':'Greenhouse Mac · Codex'},human=maker)
    r.expect(200,'POST','/v1/leases/claim',{'family':'codex-nate','project':'garden'},agent=labelled)
    other=r.expect(200,'GET','/v1/hosted/whoami',headers={'Authorization':'Bearer '+labelled['token']})
    assert next(e for e in other['directory'] if e['you'])['description']=='Greenhouse Mac · Codex'
    assert all(entry['project']=='garden' for entry in other['directory'])
    # Two agents with one name in one account are told about each other, and cannot write to each other.
    again=r.expect(200,'GET','/v1/hosted/whoami',headers={'Authorization':'Bearer '+mine['token']})
    assert again['same_name_elsewhere']==['codex-nate@garden'] and other['same_name_elsewhere']==['codex-nate@harbor']
    assert shared_identity['same_name_elsewhere']==[] and len(again['rules'])>=4 and any('different agent' in rule for rule in again['rules'])
    crossing={'from':'codex-nate@harbor','to':['codex-nate@garden'],'subject':'Design: wrong project','body_markdown':'This must not arrive.'}
    r.expect(403,'POST','/v1/emails',crossing,agent=mine,key='crossing')
    assert r.expect(200,'GET','/v1/inboxes/codex-nate%40garden/threads',agent=labelled)['threads']==[]
    # An agent stuck acknowledging is stopped; reading, retries and other agents carry on.
    chatty=r.expect(201,'POST','/v1/hosted/tokens',{'project':'harbor','family':'chatty'},human=maker)
    r.expect(200,'POST','/v1/leases/claim',{'family':'chatty','project':'harbor'},agent=chatty)
    ack={'from':'chatty@harbor','to':['codex-nate@harbor'],'subject':'Design: acknowledgement','body_markdown':'Got it, thanks.'}
    sent=[r.call('POST','/v1/emails',{**ack,'subject':f'Design: acknowledgement {n}'},agent=chatty,key=f'ack-{n}')[0] for n in range(31)]
    assert sent==[201]*30+[429], sent
    refused=r.expect(429,'POST','/v1/emails',{**ack,'subject':'Design: one more'},agent=chatty,key='ack-more')
    assert refused['error']['code']=='slow_down' and 'human' in refused['error']['message']
    r.expect(201,'POST','/v1/emails',{**ack,'subject':'Design: acknowledgement 0'},agent=chatty,key='ack-0')
    assert r.expect(200,'GET','/v1/inboxes/chatty%40harbor/threads',agent=chatty)['threads'] is not None
    r.expect(201,'POST','/v1/emails',{'from':'codex-nate@harbor','to':['chatty@harbor'],'subject':'Design: please stop','body_markdown':'Summarize for the human instead.'},agent=mine,key='please-stop')
    r.expect(201,'POST','/v1/emails',{'from':'maker@harbor','to':['chatty@harbor'],'subject':'Design: from a person','body_markdown':'People are not limited.'},human=maker,key='person-1')
    print('PASS directory with self-identification, same-name agents told apart and walled off, and the send limit',flush=True)
    # Access removed at the hub ends the person's browser and agent access at once.
    for action,expected in (('revoke',401),('restore',200)):
        request = urllib.request.Request(r.hub_url+'/test/'+action, method='POST',
            headers={'Content-Type':'application/json'}, data=json.dumps({'email':maker}).encode())
        urllib.request.urlopen(request).close()
        r.expect(expected,'GET','/v1/hosted/me',human=maker)
        r.expect(expected,'GET','/v1/projects',agent=mine)
    print('PASS public sign-up entry, private workspace per person, reusable names, key-only identity, and hub revocation',flush=True)

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

    # Browser sign-out must reach the hub, invalidate the prior token, and leave
    # durable agent credentials usable. A hub rejection must not claim success.
    logout_token = r.identity.token(nate, jti='logout-contract')
    logout_headers = {'Cookie':'__Host-ain_session='+logout_token}
    r.expect(200,'GET','/v1/hosted/me',headers=logout_headers)
    code,body,headers = r.call('POST','/auth/logout',{},headers=logout_headers)
    assert code == 200 and body['ok'] is True
    assert 'Max-Age=0' in headers.get('Set-Cookie', headers.get('set-cookie',''))
    r.expect(401,'GET','/v1/hosted/me',headers=logout_headers)
    r.expect(200,'GET','/v1/projects',agent=a)
    r.expect(503,'POST','/auth/logout',{},headers={'Cookie':'__Host-ain_session=invalid'})
    r.expect(403,'POST','/auth/logout',{},headers={**logout_headers,'Origin':'https://evil.example'})
    code,_,headers=r.call('GET','/auth/signed-out')
    assert code == 302 and headers.get('Location',headers.get('location')) == r.hub_url+'/signed-out'
    print('PASS global browser logout, agent credential independence, and logout failure handling',flush=True)

    r.stop();r.start()
    persisted=r.expect(200,'GET',f"/v1/projects/harbor/threads/{first['thread_id']}",human=nate)
    assert len(persisted['emails'])==3
    r.expect(303,'GET','/welcome',human=josh)
    r.expect(401,'GET','/v1/projects',agent=b)
    r.stop()
    request = urllib.request.Request(r.hub_url+'/test/revoke', method='POST',
        headers={'Content-Type':'application/json'}, data=json.dumps({'email':nate}).encode())
    urllib.request.urlopen(request).close()
    r.start()
    r.expect(401,'GET','/v1/projects',agent=a)
    r.expect(401,'GET','/v1/hosted/me',human=nate)
    r.expect(401,'GET','/v1/hosted/registration',headers=persistent_headers)
    r.expect(200,'GET','/v1/hosted/me',human=josh)
    r.stop()
    inspected=False
    workspaces=[]
    for path in (r.directory/'state').rglob('*.sqlite'):
        with sqlite3.connect(path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='hosted_tokens'").fetchone():continue
            inspected=True
            assert db.execute('SELECT complete,body FROM hosted_welcome').fetchone()==(1,None)
            workspaces.append(sorted(row[0] for row in db.execute('SELECT email FROM hosted_members')))
            assert all(len(row[0])==64 and not row[0].startswith('ain_') for row in db.execute('SELECT digest FROM hosted_tokens'))
            assert all(len(row[0])==64 for row in db.execute('SELECT digest FROM hosted_registrars'))
            assert db.execute("SELECT COUNT(*) FROM reservations WHERE path='src/free.py'").fetchone()[0]==0
    assert inspected,'The real Durable Object SQLite database was not found'
    # Separate storage: the shared workspace holds its two members, each newcomer is alone.
    assert sorted(workspaces)==[['josh@example.com','nate@example.com'],['maker@example.com'],['second@example.com']], workspaces
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
