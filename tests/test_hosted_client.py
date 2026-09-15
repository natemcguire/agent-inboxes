"""Credential transport tests use real HTTP servers, including redirect targets."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_inbox.client import InboxClient
from agent_inbox.models import InboxError


class HostedClientTests(unittest.TestCase):
    def test_credentials_health_routing_and_redirects(self):
        received=[]
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path,self.headers.get('Authorization')))
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
