"""Unit and integration tests for the loopback HTTP Server and endpoints."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.support import isolated_inbox

from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.server import AgentInboxServer


class TestServerAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.enterClassContext(isolated_inbox(cls.tmp_dir.name))
        cls.db_path = Path(cls.tmp_dir.name) / "test_api.db"
        cls.db_conn = get_connection(cls.db_path)

        # Bind to loopback on random free port (port 0)
        cls.server = AgentInboxServer(("127.0.0.1", 0), cls.db_conn, verbose=False)
        cls.host, cls.port = cls.server.server_address
        cls.base_url = f"http://{cls.host}:{cls.port}"
        cls.client = InboxClient(cls.base_url)

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db_conn.close()
        cls.tmp_dir.cleanup()

    def test_healthz_endpoint(self):
        resp = self.client.healthz()
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["db"], "ok")
        self.assertIn("version", resp)

    def test_cors_get_allows_only_trusted_origins(self):
        for origin, allowed in [("https://nates-software.com", True), ("https://evil.example.com", False)]:
            with self.subTest(origin=origin):
                req = urllib.request.Request(f"{self.base_url}/healthz", headers={"Origin": origin})
                with urllib.request.urlopen(req, timeout=5) as response:
                    self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin if allowed else None)
                    if allowed:
                        self.assertEqual(response.headers.get("Vary"), "Origin")

    def test_cors_preflight_options(self):
        # OPTIONS preflight (sent before POST/JSON or Idempotency-Key requests)
        # must return 204 with the allow headers, not 501.
        req = urllib.request.Request(
            f"{self.base_url}/v1/emails",
            method="OPTIONS",
            headers={
                "Origin": "https://nates-software.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Idempotency-Key",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(
                r.headers.get("Access-Control-Allow-Origin"),
                "https://nates-software.com",
            )
            self.assertIn("Idempotency-Key", r.headers.get("Access-Control-Allow-Headers", ""))
            # Chrome Private Network Access: public->loopback needs this opt-in.
            self.assertEqual(
                r.headers.get("Access-Control-Allow-Private-Network"), "true"
            )

    def test_put_and_list_inboxes(self):
        # Register inboxes
        r1 = self.client.put_inbox("worker1@boats", display_name="Worker One")
        self.assertEqual(r1["address"], "worker1@boats")
        self.assertTrue(r1["created"])

        r2 = self.client.put_inbox("worker2@boats", display_name="Worker Two")
        self.assertTrue(r2["created"])

        # Filter by project
        inboxes = self.client.list_inboxes(project="boats")
        self.assertEqual(len(inboxes), 2)
        addrs = [ib["address"] for ib in inboxes]
        self.assertIn("worker1@boats", addrs)
        self.assertIn("worker2@boats", addrs)

    def test_missing_idempotency_key_header_returns_400(self):
        req = urllib.request.Request(
            f"{self.base_url}/v1/emails",
            data=json.dumps({
                "from": "a@p1",
                "to": ["b@p2"],
                "subject": "Hi",
                "body_markdown": "Test",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)
        err_body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(err_body["error"]["code"], "missing_idempotency_key")

if __name__ == "__main__":
    unittest.main()
