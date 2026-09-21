"""Credential transport tests use real HTTP servers, including redirect targets."""
import contextlib
import io
import json
import os
import tempfile
import threading
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
