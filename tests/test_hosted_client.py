"""Credential transport tests use real HTTP servers, including redirect targets."""
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_inbox.client import InboxClient, hosted_origin as origin, save_hosted_credentials as save
from agent_inbox.cli import main
from agent_inbox.models import InboxError


class HostedClientTests(unittest.TestCase):
    def test_credentials_health_routing_and_redirects(self):
        received=[]
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path,self.headers.get('Authorization')))
                if not self.headers.get('User-Agent','').startswith('agent-inboxes/'):
                    self.send_response(403);self.end_headers();return
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{other.server_port}/stolen');self.end_headers();return
                data=json.dumps({'ok':True}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        servers=[ThreadingHTTPServer(('127.0.0.1',0),Endpoint) for _ in range(2)]
        server,other=servers
        for item in servers:threading.Thread(target=item.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                token=Path(directory)/'key';token.write_text('ain_test_secret\n')
                with patch.dict(os.environ,{'AGENT_INBOX_TOKEN':'','AGENT_INBOX_TOKEN_FILE':str(token)}):
                    client=InboxClient(f'http://127.0.0.1:{server.server_port}')
                    self.assertTrue(client.healthz()['ok'])
                    self.assertEqual(received,[('/v1/hosted/health','Bearer ain_test_secret')])
                    with self.assertRaises(InboxError):client._request('GET','/redirect')
                    self.assertNotIn('/stolen',[path for path,_ in received])
            with patch.dict(os.environ,{'AGENT_INBOX_TOKEN':'ain_secret'}):
                with self.assertRaises(ValueError):InboxClient('http://example.com')
                with self.assertRaises(ValueError):InboxClient('https://user:password@example.com')
        finally:
            for item in servers:item.shutdown();item.server_close()


class HostedRegistrationTests(unittest.TestCase):
    def test_reject_unsafe_origins(self):
        for value in ['http://example.com', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com?token=x', 'https://example.com#x']:
            with self.assertRaises(ValueError):
                origin(value)
        self.assertEqual(origin('https://inbox.example.com/'), 'https://inbox.example.com')

    def test_atomic_private_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owner' / 'registration.json'
            save(path, {'token': 'secret'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            save(path, {'token': 'replacement'})
            self.assertEqual(json.loads(path.read_text())['token'], 'replacement')

    def test_missing_login_fails_without_shell_output(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AGENT_INBOX_DIR': directory}):
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                self.assertEqual(main(['hosted', 'register', '--project', 'harbor', '--agent', 'codex']), 1)
            self.assertEqual(output.getvalue(), '')
            self.assertIn('One-time setup', error.getvalue())

    def test_hosted_scope_does_not_remap_local_relay(self):
        from agent_inbox.identity import derive_project
        with patch.dict(os.environ, {'AGENT_INBOX_PROJECT': 'shared', 'AGENT_INBOX_TOKEN_FILE': '/private/key'}):
            with patch('agent_inbox.db.get_connection', side_effect=AssertionError('Hosted identity must not open the local database')):
                self.assertEqual(derive_project(), 'shared')


class RepositoryIdentityTests(unittest.TestCase):
    """A repository connected once is that project's agent in every shell; a real HTTP service records who called."""

    def setUp(self):
        import subprocess
        seen = self.seen = []
        keys = self.keys = {}

        class Service(BaseHTTPRequestHandler):
            def reply(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status);self.send_header('Content-Type', 'application/json');self.end_headers();self.wfile.write(raw)

            def do_GET(self):
                bearer = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
                seen.append((keys.get(bearer, bearer), self.path))
                if self.path == '/v1/hosted/registration':
                    return self.reply(200, {'owner': 'owner@example.com', 'workspace': 'w', 'id': 'reg_1'})
                if self.path == '/v1/hosted/whoami' and bearer in keys:
                    agent, project = keys[bearer].split('@')
                    return self.reply(200, {'kind': 'agent', 'address': keys[bearer], 'agent': agent, 'project': project,
                        'owner': 'owner@example.com', 'directory': [], 'same_name_elsewhere': [], 'rules': []})
                self.reply(200, {'inboxes': [], 'status': 'ok'})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                address = body['family'] + '@' + body['project']
                token = 'ain_key_for_' + address.replace('@', '_at_')
                keys[token] = address
                self.reply(201, {'id': 'key_' + address, 'token': token, 'address': address, 'project': body['project'],
                    'family': body['family'], 'owner': 'owner@example.com', 'expires_at': time.time() + 3600})

            def do_PUT(self):
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                self.reply(200, {})

            def log_message(self, *args): pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        for name in ('boats', 'homeassistant'):
            (self.root / name).mkdir()
            subprocess.run(['git', 'init', '-q', str(self.root / name)], check=True)
        # A Claude session with no inbox variables of its own.
        self.shell = {'PATH': os.environ['PATH'], 'HOME': str(self.root), 'CLAUDE_CODE_SESSION_ID': 'session-one',
            'AGENT_INBOX_DIR': str(self.root / 'data'), 'AGENT_INBOX_DB': str(self.root / 'data' / 'inbox.db'),
            'AGENT_INBOX_CLOUD_CONFIG': str(self.root / 'no-cloud.json')}
        self.start = Path.cwd()
        self.run_in('boats', ['hosted', 'login', '--url', self.url, '--credential-stdin'], stdin='ainr_owner_credential')

    def tearDown(self):
        os.chdir(self.start)
        self.server.shutdown();self.server.server_close()
        self.directory.cleanup()

    def run_in(self, repository, arguments, shell=None, stdin=''):
        """One CLI invocation in a repository, as a fresh process would see it."""
        os.chdir(self.root / repository)
        output, error = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {**self.shell, **(shell or {})}, clear=True), patch('sys.stdin', io.StringIO(stdin)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            status = main(arguments)
        return status, output.getvalue(), error.getvalue()

    def callers(self):
        """Which agent's key the service saw on inbox requests since the last check."""
        found = [who for who, path in self.seen if path.startswith('/v1/inboxes')]
        self.seen.clear()
        return found

    def connect(self, repository, *extra):
        status, _, error = self.run_in(repository, ['hosted', 'register', '--url', self.url, *extra])
        self.assertEqual(status, 0, error)
        return error

    def test_each_repository_acts_as_its_own_agent_without_exports(self):
        self.assertIn('claude@boats', self.connect('boats'))
        self.assertIn('claude@homeassistant', self.connect('homeassistant'))
        self.seen.clear()
        for repository in ('boats', 'homeassistant', 'boats'):
            self.assertEqual(self.run_in(repository, ['inboxes'])[0], 0)
            self.assertEqual(self.callers(), ['claude@' + repository])
        status, output, _ = self.run_in('homeassistant', ['whoami'])
        self.assertEqual(status, 0)
        self.assertEqual(output.splitlines()[0], 'claude@homeassistant')

    def test_a_worktree_is_the_same_agent_as_its_repository(self):
        import subprocess
        self.connect('boats')
        git = ['git', '-C', str(self.root / 'boats'), '-c', 'user.name=t', '-c', 'user.email=t@example.com']
        subprocess.run(git + ['commit', '-q', '--allow-empty', '-m', 'start'], check=True)
        subprocess.run(git + ['worktree', 'add', '-q', str(self.root / 'boats-wt-task49')], check=True, capture_output=True)
        self.seen.clear()
        self.assertEqual(self.run_in('boats-wt-task49', ['inboxes'])[0], 0)
        self.assertEqual(self.callers(), ['claude@boats'])

    def test_exports_from_another_project_stop_the_command(self):
        self.connect('boats');self.connect('homeassistant')
        _, exports, _ = self.run_in('boats', ['hosted', 'register', '--url', self.url])
        leaked = dict(line.removeprefix('export ').split('=', 1) for line in exports.splitlines() if line.startswith('export '))
        leaked = {name: value.strip("'") for name, value in leaked.items()}
        self.assertEqual(leaked['AGENT_INBOX_PROJECT'], 'boats')
        self.seen.clear()
        # The boats exports are still in a shell that has moved to the homeassistant repository.
        status, output, error = self.run_in('homeassistant', ['inboxes'], shell=leaked)
        self.assertEqual(status, 2)
        self.assertEqual(self.seen, [])
        self.assertIn('boats', error);self.assertIn('claude@homeassistant', error);self.assertIn('unset AGENT_INBOX_URL', error)
        # The same exports in the repository they belong to are fine.
        self.assertEqual(self.run_in('boats', ['inboxes'], shell=leaked)[0], 0)
        self.assertEqual(self.callers(), ['claude@boats'])

    def test_a_key_used_from_the_wrong_folder_is_explained_not_guessed(self):
        self.connect('boats')
        _, exports, _ = self.run_in('boats', ['hosted', 'register', '--url', self.url])
        token_file = next(line.split('=', 1)[1].strip("'") for line in exports.splitlines() if 'TOKEN_FILE' in line)
        (self.root / 'elsewhere').mkdir()
        # Only the key travelled: no project variable, and a folder that is not connected.
        status, output, error = self.run_in('elsewhere', ['whoami'], shell={'AGENT_INBOX_URL': self.url, 'AGENT_INBOX_TOKEN_FILE': token_file})
        self.assertEqual(status, 2)
        self.assertEqual(output, '')
        self.assertIn('claude@boats', error);self.assertIn('elsewhere', error);self.assertIn('different agents', error)

    def test_registering_another_project_does_not_silently_repoint_a_repository(self):
        self.connect('boats')
        note = self.connect('boats', '--project', 'homeassistant')
        self.assertIn('stays connected as claude@boats', note)
        self.seen.clear()
        self.run_in('boats', ['inboxes'])
        self.assertEqual(self.callers(), ['claude@boats'])
        self.connect('boats', '--project', 'homeassistant', '--rebind')
        self.run_in('boats', ['inboxes'])
        self.assertEqual(self.callers(), ['claude@homeassistant'])

    def test_an_explicit_endpoint_is_left_alone(self):
        self.connect('boats')
        self.seen.clear()
        # A local or test service chosen on purpose is not replaced by the repository's hosted identity.
        self.run_in('boats', ['inboxes'], shell={'AGENT_INBOX_URL': self.url})
        self.assertEqual(self.callers(), [''])

    def test_connecting_puts_the_identity_where_the_agent_reads_it_every_session(self):
        notes = self.root / 'boats' / 'CLAUDE.md'
        notes.write_text('# Boats\n\nKeep the deck dry.\n')
        said = self.connect('boats')
        text = notes.read_text()
        self.assertIn('CLAUDE.md', said)
        self.assertTrue(text.startswith('# Boats\n\nKeep the deck dry.\n'))
        self.assertIn('this repository is project `boats`', text)
        self.assertIn('Claude is `claude@boats`', text)
        self.assertIn('is a different agent, not you', text)
        self.assertIn('Never send an acknowledgement', text)
        # Connecting again, or connecting the other repository, leaves one block naming this project.
        self.connect('boats');self.connect('homeassistant')
        self.assertEqual(notes.read_text(), text)
        self.assertEqual(text.count('<!-- agent-inboxes:start -->'), 1)
        other = (self.root / 'homeassistant' / 'CLAUDE.md').read_text()
        self.assertIn('Claude is `claude@homeassistant`', other)
        self.assertNotIn('boats', other)

    def test_claude_code_is_told_who_it_is_before_every_turn(self):
        settings = self.root / 'boats' / '.claude' / 'settings.local.json'
        settings.parent.mkdir()
        mine = {'permissions': {'allow': ['Bash(ls:*)']}, 'hooks': {'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'echo mine'}]}]}}
        settings.write_text(json.dumps(mine))
        self.connect('boats');self.connect('boats');self.connect('homeassistant')
        config = json.loads(settings.read_text())
        self.assertEqual(config['permissions'], mine['permissions'])
        commands = [hook['command'] for group in config['hooks']['UserPromptSubmit'] for hook in group['hooks']]
        self.assertEqual(commands[0], 'echo mine')
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[1].endswith('agent-inbox identity'), commands)
        # What that command prints, in each repository and outside any.
        self.seen.clear()
        for repository in ('boats', 'homeassistant'):
            status, output, _ = self.run_in(repository, ['identity'])
            self.assertEqual(status, 0)
            self.assertEqual(len(output.strip().splitlines()), 1)
            self.assertIn(f'you are claude@{repository}', output)
            self.assertIn('different agent', output);self.assertIn('never an acknowledgement', output)
        (self.root / 'elsewhere').mkdir()
        self.assertEqual(self.run_in('elsewhere', ['identity']), (0, '', ''))
        self.assertEqual(self.seen, [], 'the reminder must not use the network')
        # A Codex session in the boats repository is not told it is Claude.
        self.assertEqual(self.run_in('boats', ['identity'], shell={'CLAUDE_CODE_SESSION_ID': '', 'CODEX_SESSION_ID': 'c'})[1], '')

    def test_instructions_can_be_declined(self):
        self.connect('boats', '--no-instructions')
        self.assertFalse((self.root / 'boats' / 'CLAUDE.md').exists())
        self.assertFalse((self.root / 'boats' / 'AGENTS.md').exists())
        self.assertFalse((self.root / 'boats' / '.claude').exists())
        self.seen.clear()
        self.run_in('boats', ['inboxes'])
        self.assertEqual(self.callers(), ['claude@boats'])
