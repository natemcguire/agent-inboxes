"""Real isolated HTTP/CLI attention acceptance. No downloads or broker required."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


with tempfile.TemporaryDirectory(prefix='ae-http-') as directory:
    tmp = Path(directory)
    port = free_port()
    # Occupied legacy ports prove core startup no longer depends on either listener.
    listeners = [socket.socket(), socket.socket()]
    for sock in listeners:
        sock.bind(('127.0.0.1', 0)); sock.listen()
    env = dict(os.environ, AGENT_INBOX_DIR=str(tmp), AGENT_INBOX_DB=str(tmp/'inbox.db'),
               AGENT_INBOX_CLOUD_CONFIG=str(tmp/'absent-cloud.json'), AGENT_INBOX_URL=f'http://127.0.0.1:{port}',
               AGENT_INBOX_AE_NATS_PORT=str(listeners[0].getsockname()[1]),
               AGENT_INBOX_AE_MQTT_PORT=str(listeners[1].getsockname()[1]), PYTHONPATH=str(ROOT))
    code = ("from unittest.mock import patch; from agent_inbox.cli import main\n"
            "with patch('agent_inbox.updates.UpdateWorker.start'):\n"
            f" main(['serve','--port','{port}'])")
    def cli(actor, *args):
        result = subprocess.run([sys.executable, str(ROOT/'bin/agent-inbox'), 'ae', '--actor', actor,
                                 '--session', 'acceptance', *args], cwd=tmp, env=env,
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    def start(log):
        process = subprocess.Popen([sys.executable, '-c', code], cwd=tmp, env=env, stdout=log, stderr=log)
        try:
            for _ in range(100):
                if process.poll() is not None: break
                try:
                    with urllib.request.urlopen(env['AGENT_INBOX_URL']+'/healthz', timeout=1) as response:
                        if json.load(response)['status']=='ok': return process
                except (OSError, urllib.error.URLError):
                    time.sleep(.05)
            log.seek(0)
            raise AssertionError(log.read())
        except BaseException:
            if process.poll() is None: process.kill(); process.wait()
            raise
    def stop(process, signum):
        process.send_signal(signum)
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(); raise
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', port)) != 0, 'HTTP listener left running'
    with (tmp/'server.log').open('w+') as log:
        process = start(log)
        try:
            before = cli('alice@demo', 'brief')
            task = cli('alice@demo', 'task', 'create', '--title', 'Release handoff', '--path', 'src/')
            page = cli('bob@demo', 'brief', '--source', before['source'], '--after', str(before['cursor']))
            assert any(e['ref']==task['id'] for e in page['events'])
            cli('bob@demo', 'task', 'claim', task['id'], '--version', '1')
            assert cli('bob@demo', 'context')['sections']['attention'][0]['action']=='reserve_paths'
            cli('bob@demo', 'task', 'handoff', task['id'], '--version', '2', '--target', 'alice@demo',
                '--note', 'Tests passed; review', '--next-action', 'Verify the patch', '--workspace', str(tmp),
                '--ref', 'fixture:1', '--acceptance', 'Checks pass', '--evidence', 'server.log')
            handoff = cli('alice@demo', 'brief')['sections']['ready_queue'][0]
            assert handoff['handoff']['next_action']=='Verify the patch'
            wake = cli('alice@demo', 'watch', '--source', before['source'], '--after', str(page['cursor']),
                       '--coalesce', '30', '--timeout', '1')
            assert wake['wake_reason']=='urgent'
            assert len(json.dumps(wake).encode())<=24000
            assert len(cli('alice@demo', 'task', 'history', task['id'])['history'])==3
            assert cli('alice@demo', 'task', 'list')['tasks'][0]['id']==task['id']
            with urllib.request.urlopen(env['AGENT_INBOX_URL']+'/') as response:
                assert 'Agent Inbox' in response.read().decode()
            assert cli('alice@demo', 'status')['transport']['broker_required'] is False
            # Actual SIGTERM, process restart and durable restoration, not reopening one connection.
            stop(process, signal.SIGTERM)
            process = start(log)
            restored = cli('alice@demo', 'brief')
            assert restored['source']==before['source']
            assert restored['sections']['ready_queue'][0]['handoff']==handoff['handoff']
            assert not (tmp/'runtime').exists()
            assert not (tmp/'inbox.db.ae').exists()
            print('PASS: broker-free startup with occupied legacy ports, CLI/HTTP brief, watch, queue, handoff/history, UI, SIGTERM restart and durable recovery')
        finally:
            if process.poll() is None: stop(process, signal.SIGINT)
            for sock in listeners: sock.close()
