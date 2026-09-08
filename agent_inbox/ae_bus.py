"""Built-in authenticated NATS + MQTT runtime; SQLite remains the authority."""
import collections
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import select
import socket
import sqlite3
import subprocess
import tarfile
import threading
import time
import urllib.request

from agent_inbox.config import get_data_dir

VERSION = '2.14.6'
CHECKSUMS = {
 'darwin-amd64':'239d4f3314334cc86cb12d7c252cf9b3461364451cc8ec5a39fb4915acd717c1',
 'darwin-arm64':'291b9aa8c342b3cdcc53d872585bd467de4b5c9acf375066d882eb5412a09d55',
 'linux-amd64':'61c3d55f69f61ec616b75782250936445f2819e9e5f2ae6159b10a31abd2200c',
 'linux-arm64':'3ff6e463762db64186a36cf0276dae8320509e995151ad0153ba9c9f67eee3f9',
}


def binary_path():
    return get_data_dir() / 'runtime' / f'nats-server-{VERSION}'


def provision():
    """Install the pinned broker. Never called as an implicit background download."""
    target=binary_path()
    if target.is_file():
        return target
    machine={'aarch64':'arm64','x86_64':'amd64'}.get(platform.machine().lower(),platform.machine().lower())
    key=f'{platform.system().lower()}-{machine}'
    if key not in CHECKSUMS:
        raise RuntimeError('Bundled AE broker supports macOS/Linux on arm64/amd64')
    name=f'nats-server-v{VERSION}-{key}'
    url=f'https://github.com/nats-io/nats-server/releases/download/v{VERSION}/{name}.tar.gz'
    with urllib.request.urlopen(url,timeout=30) as response:
        payload=response.read(32*1024*1024+1)
    if len(payload)>32*1024*1024 or hashlib.sha256(payload).hexdigest()!=CHECKSUMS[key]:
        raise RuntimeError('AE broker archive failed pinned checksum verification')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:
        member=archive.getmember(f'{name}/nats-server')
        if not member.isfile() or member.size>64*1024*1024:
            raise RuntimeError('Invalid AE broker archive')
        body=archive.extractfile(member).read()
        temp=target.with_name(target.name+'.'+secrets.token_hex(6))
        try:
            with temp.open('xb') as stream:
                os.chmod(temp,0o700);stream.write(body);stream.flush();os.fsync(stream.fileno())
            os.replace(temp,target)
        finally:
            temp.unlink(missing_ok=True)
        for item in ('LICENSE','NOTICE'):
            try:
                member=archive.getmember(f'{name}/{item}')
            except KeyError:
                continue
            if member.isfile() and member.size<100000:
                (target.parent/f'nats-{item}').write_bytes(archive.extractfile(member).read())
    return target


class NatsConnection:
    """Small internal Core NATS client: bounded frames, server PING and flush.

    Application events are recoverable from SQLite. No claim of JetStream publish
    acknowledgment is made: flush confirms broker processing, not disk persistence.
    """
    def __init__(self,port,user,password):
        self.sock=socket.create_connection(('127.0.0.1',port),timeout=3)
        self.buffer=bytearray();self.pending=collections.deque()
        try:
            if not self.line().startswith(b'INFO '): raise RuntimeError('Not a NATS server')
            config=json.dumps({'verbose':False,'pedantic':True,'user':user,'pass':password,'name':'agent-inbox-ae'})
            self.sock.sendall(b'CONNECT '+config.encode()+b'\r\n')
            self.flush()
        except BaseException:
            self.close();raise

    def read(self,n):
        while len(self.buffer)<n:
            chunk=self.sock.recv(min(65536,n-len(self.buffer)))
            if not chunk: raise ConnectionError('NATS disconnected')
            self.buffer.extend(chunk)
        value=bytes(self.buffer[:n]);del self.buffer[:n];return value

    def line(self):
        while b'\r\n' not in self.buffer:
            if len(self.buffer)>8192: raise RuntimeError('NATS control line too large')
            chunk=self.sock.recv(4096)
            if not chunk: raise ConnectionError('NATS disconnected')
            self.buffer.extend(chunk)
        n=self.buffer.index(b'\r\n');return self.read(n+2)[:-2]

    def frame(self):
        line=self.line()
        if line==b'PING': self.sock.sendall(b'PONG\r\n');return None
        if line.startswith(b'-ERR'): raise ConnectionError('NATS rejected operation')
        if line.startswith(b'MSG '):
            parts=line.decode('ascii').split()
            if len(parts) not in (4,5): raise RuntimeError('Invalid NATS frame')
            size=int(parts[-1])
            if size<0 or size>1024*1024: raise RuntimeError('NATS payload too large')
            payload=self.read(size)
            if self.read(2)!=b'\r\n': raise RuntimeError('Invalid NATS payload boundary')
            return (parts[1],parts[3] if len(parts)==5 else None,payload)
        return line

    def publish(self,subject,payload):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',subject): raise ValueError('Invalid subject')
        self.sock.sendall(f'PUB {subject} {len(payload)}\r\n'.encode()+payload+b'\r\n')

    def subscribe(self,subject):
        if not re.fullmatch(r'[A-Za-z0-9_.>*-]+',subject): raise ValueError('Invalid subscription')
        self.sock.sendall(f'SUB {subject} 1\r\n'.encode());self.flush()

    def flush(self):
        self.sock.sendall(b'PING\r\n')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            frame=self.frame()
            if frame==b'PONG': return
            if isinstance(frame,tuple): self.pending.append(frame)
        raise TimeoutError('NATS flush timed out')

    def receive(self,timeout=.1):
        if self.pending: return self.pending.popleft()
        if self.buffer or select.select([self.sock],[],[],timeout)[0]:
            frame=self.frame()
            if isinstance(frame,tuple): return frame
        return None

    def close(self):
        self.sock.close()


class AgentBus(threading.Thread):
    def __init__(self,db_path,nats_port=8792,mqtt_port=8793):
        super().__init__(name='agent-experience',daemon=True)
        self.db_path=Path(db_path);self.nats_port=nats_port;self.mqtt_port=mqtt_port
        self.stopped=threading.Event();self.ready=threading.Event();self.process=None
        self.error=None;self.connected=False;self.client=None
        self.directory=self.db_path.parent/(self.db_path.name+'.ae')
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.passwords=None;self.lock=None;self.log=None

    def launch(self):
        # A per-database OS lock prevents two supervisors sharing broker storage.
        import fcntl
        self.lock=(self.directory/'owner.lock').open('a')
        try: fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.lock.close();self.lock=None;raise RuntimeError('AE broker already owned by another service')
        target=binary_path()
        if not target.is_file(): raise RuntimeError('AE broker missing; run agent-inbox ae setup')
        credentials=self.directory/'credentials.json'
        if not credentials.exists():
            with credentials.open('x') as stream:
                os.chmod(credentials,0o600)
                json.dump({'service':secrets.token_urlsafe(32),'agent':secrets.token_urlsafe(32)},stream)
        self.passwords=json.loads(credentials.read_text())
        # Broker itself is loopback-only. Application agents cannot forge event publications.
        config={
          'host':'127.0.0.1','port':self.nats_port,
          'jetstream':{'store_dir':str(self.directory/'jetstream'),'max_file_store':512*1024*1024},
          'mqtt':{'host':'127.0.0.1','port':self.mqtt_port},
          'authorization':{'users':[
            {'user':'ae-service','password':self.passwords['service']},
            {'user':'ae-agent','password':self.passwords['agent'],'permissions':{
              'publish':['ae.commands.>'],'subscribe':['ae.events.>','ae.replies.>','_INBOX.>']}}
          ]}}
        config_path=self.directory/'server.json'
        fd=os.open(config_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream: json.dump(config,stream)
        self.log=(self.directory/'broker.log').open('ab')
        self.process=subprocess.Popen([str(target),'-c',str(config_path)],stdout=self.log,stderr=self.log)
        # Startup readiness is checked by authenticated connection, not port availability alone.
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if self.process.poll() is not None: raise RuntimeError('AE broker exited; inspect broker.log')
            try:
                client=NatsConnection(self.nats_port,'ae-service',self.passwords['service']);client.close();return
            except (OSError,RuntimeError): time.sleep(.1)
        raise RuntimeError('AE broker failed readiness')

    def run(self):
        from agent_inbox.ae import AgentExperience
        from agent_inbox.db import get_connection
        from agent_inbox.models import InboxError
        conn=None
        try:
            self.launch()
            conn=get_connection(self.db_path);ae=AgentExperience(conn)
            command_subject=f'ae.commands.{ae.source}'
            while not self.stopped.is_set():
                try:
                    if self.process.poll() is not None:
                        raise RuntimeError('Managed AE broker exited')
                    self.client=NatsConnection(self.nats_port,'ae-service',self.passwords['service'])
                    self.client.subscribe(command_subject);self.connected=True;self.error=None;self.ready.set()
                    while not self.stopped.is_set():
                        message=self.client.receive(.05)
                        if message:
                            _,reply,payload=message
                            request_id=None
                            try:
                                envelope=json.loads(payload)
                                request_id=envelope.get('request_id') if isinstance(envelope,dict) else None
                                result={'ok':True,'result':ae.command(envelope)}
                            except (InboxError,ValueError,TypeError) as exc:
                                result={'ok':False,'error':exc.to_dict()['error'] if isinstance(exc,InboxError) else {'code':'invalid_command','message':'Invalid AE command'}}
                            if isinstance(request_id,str):
                                response_subject=f'ae.replies.{ae.source}.{hashlib.sha256(request_id.encode()).hexdigest()}'
                                packet=json.dumps(dict(result,request_id=request_id)).encode()
                                if len(packet)>900000:
                                    packet=json.dumps({'ok':False,'request_id':request_id,'error':{'code':'response_too_large','message':'Use a smaller page limit'}}).encode()
                                self.client.publish(response_subject,packet)
                                if reply and re.fullmatch(r'_INBOX\.[A-Za-z0-9_.-]{1,200}',reply):
                                    self.client.publish(reply,packet)
                                self.client.flush()
                        cursor=conn.execute('SELECT sequence FROM ae_delivery WHERE id=1').fetchone()[0]
                        rows=conn.execute('SELECT * FROM ae_events WHERE sequence>? ORDER BY sequence LIMIT 100',(cursor,)).fetchall()
                        for row in rows:
                            event=dict(row);event['source']=ae.source;event['detail']=json.loads(event['detail'])
                            token=(event['project'] or '').encode().hex() or 'global'
                            self.client.publish(f'ae.events.{ae.source}.{token}',json.dumps(event).encode())
                            self.client.flush()
                            conn.execute('UPDATE ae_delivery SET sequence=? WHERE id=1',(event['sequence'],))
                except (OSError,RuntimeError,ValueError,sqlite3.OperationalError):
                    self.connected=False;self.error='Broker connection interrupted; durable events will retry'
                    if self.process.poll() is not None and not self.stopped.is_set():
                        self.stopped.wait(.5)
                        if not self.stopped.is_set():
                            self.process=subprocess.Popen([str(binary_path()),'-c',str(self.directory/'server.json')],stdout=self.log,stderr=self.log)
                    self.stopped.wait(.5)
                finally:
                    if self.client: self.client.close();self.client=None
        except Exception as exc:
            self.error=str(exc)
        finally:
            self.connected=False;self.ready.set()
            if conn: conn.close()
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired: self.process.kill();self.process.wait(timeout=5)
            if self.log: self.log.close()
            if self.lock: self.lock.close()

    def status(self):
        return {'connected':self.connected,'error':self.error,'nats_port':self.nats_port,'mqtt_port':self.mqtt_port,
                'mqtt_version':'3.1.1','credentials_file':str(self.directory/'credentials.json')}

    def stop(self):
        self.stopped.set();self.join(timeout=12)
