"""Reuse the local HTTP contract without opening sockets or blocking the isolate."""
import io
import json
import time
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit

from agent_inbox.server import InboxRequestHandler


class Handler(InboxRequestHandler):
    def __init__(self, db, method, path, headers, body, peer):
        self.command = method
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        # Long polling is performed asynchronously by the Worker, outside each
        # short storage transaction. The engine returns one current snapshot.
        for key in ('wait', 'timeout', 'coalesce'):
            query[key] = ['0']
        self.path = urlunsplit(('', '', parsed.path, urlencode(query, doseq=True), ''))
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value
        raw = json.dumps(body).encode() if body is not None else b''
        self.headers['Content-Length'] = str(len(raw))
        self.rfile, self.wfile = io.BytesIO(raw), io.BytesIO()
        self.client_address = (peer, 0)
        self.server = SimpleNamespace(get_thread_connection=lambda: db,
            nudge_cloud_sync=lambda: None, started_at=time.monotonic(), server_address=('Cloudflare', 443))
        self.status = 200
        self.response_headers = {}

    def send_response(self, status, message=None):
        self.status = int(status)

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def _send_cors_headers(self):
        pass  # The hosted UI and API use the same origin.

    def _get_harness_pid(self):
        return None  # Process IDs on another computer cannot prove liveness.

    def _get_session_pid(self):
        return None

    def run(self):
        getattr(self, 'do_' + self.command)()
        data = json.loads(self.wfile.getvalue())
        if 'delivery_status' in data:
            data['delivery_status'] = 'stored in shared workspace'
        return self.status, data
