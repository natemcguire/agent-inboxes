"""Unit and integration tests for the loopback HTTP Server and endpoints."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.models import ValidationError
from agent_inbox.server import AgentInboxServer


class TestServerAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
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

    def test_loopback_binding(self):
        """Verify the server binds strictly to IPv4 loopback."""
        self.assertEqual(self.host, "127.0.0.1")

    def test_healthz_endpoint(self):
        resp = self.client.healthz()
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["db"], "ok")
        self.assertIn("version", resp)

    def test_cors_allowed_origin_gets_header(self):
        # A GET from an allowed web-suite origin must carry the ACAO header,
        # or the browser blocks the response.
        req = urllib.request.Request(
            f"{self.base_url}/healthz",
            headers={"Origin": "https://nates-software.com"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(
                r.headers.get("Access-Control-Allow-Origin"),
                "https://nates-software.com",
            )
            self.assertEqual(r.headers.get("Vary"), "Origin")

    def test_cors_disallowed_origin_gets_no_header(self):
        # An untrusted origin must NOT receive an ACAO header (unauthenticated
        # local service — no reflecting arbitrary origins).
        req = urllib.request.Request(
            f"{self.base_url}/healthz",
            headers={"Origin": "https://evil.example.com"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

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

    def test_send_list_read_reply_flow_via_http(self):
        # Step 1: Send email
        send_res = self.client.send_email(
            from_addr="sender@project-a",
            to_addrs=["receiver@project-b"],
            cc_addrs=["cc@project-b"],
            subject="HTTP Integration Test",
            body_markdown="Hello over HTTP API",
            idempotency_key="key-http-001",
        )
        self.assertIn("email_id", send_res)
        self.assertIn("thread_id", send_res)
        thread_id = send_res["thread_id"]
        email_id = send_res["email_id"]

        # Test idempotency: send again with same key
        send_res_repeat = self.client.send_email(
            from_addr="sender@project-a",
            to_addrs=["receiver@project-b"],
            subject="HTTP Integration Test",
            body_markdown="Hello over HTTP API",
            idempotency_key="key-http-001",
        )
        self.assertEqual(send_res_repeat["email_id"], email_id)

        # Step 2: List unread threads for receiver
        threads = self.client.list_threads("receiver@project-b", unread=True)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["thread_id"], thread_id)
        self.assertEqual(threads[0]["unread_count"], 1)

        # Step 3: Get thread detail
        thread_data = self.client.get_thread("receiver@project-b", thread_id)
        self.assertEqual(thread_data["thread_id"], thread_id)
        self.assertEqual(len(thread_data["emails"]), 1)
        self.assertFalse(thread_data["emails"][0]["read"])

        # Step 4: Mark thread read
        read_res = self.client.mark_thread_read("receiver@project-b", thread_id)
        self.assertEqual(read_res["thread_id"], thread_id)
        self.assertEqual(read_res["marked_read"], 1)

        # Confirm unread is now 0
        threads_after = self.client.list_threads("receiver@project-b", unread=True)
        self.assertEqual(len(threads_after), 0)

        # Step 5: Reply to email
        reply_res = self.client.reply_email(
            email_id=email_id,
            from_addr="receiver@project-b",
            body_markdown="Acknowledged over HTTP",
            idempotency_key="key-http-reply-001",
        )
        self.assertEqual(reply_res["thread_id"], thread_id)
        self.assertEqual(reply_res["reply_to_email_id"], email_id)
        self.assertEqual(reply_res["references"], [email_id])
        self.assertIn("sender@project-a", reply_res["to"])
        self.assertIn("cc@project-b", reply_res["cc"])


if __name__ == "__main__":
    unittest.main()
