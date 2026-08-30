"""End-to-end multi-agent cross-project round-trip integration test."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.server import AgentInboxServer


class TestE2EMultiAgentFlow(unittest.TestCase):

    def test_cross_project_flow_with_server_restart(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "e2e_inbox.db"

            # ----------------------------------------------------
            # Phase 1: Start server instance 1
            # ----------------------------------------------------
            conn1 = get_connection(db_path)
            server1 = AgentInboxServer(("127.0.0.1", 0), conn1, verbose=False)
            host1, port1 = server1.server_address
            client1 = InboxClient(f"http://{host1}:{port1}")

            t1 = threading.Thread(target=server1.serve_forever, daemon=True)
            t1.start()

            try:
                # 1. Health check
                health = client1.healthz()
                self.assertEqual(health["status"], "ok")

                # 2. Whoami / ensure inboxes for two agents in different projects
                ib_codex = client1.put_inbox("codex-worker1@boats", display_name="Codex worker 1")
                self.assertEqual(ib_codex["address"], "codex-worker1@boats")
                self.assertTrue(ib_codex["created"])

                ib_claude = client1.put_inbox("claude@nate-bot", display_name="Claude")
                self.assertEqual(ib_claude["address"], "claude@nate-bot")
                self.assertTrue(ib_claude["created"])

                # 3. codex-worker1@boats starts a thread and sends to claude@nate-bot
                idempotency_token = "74d48667-62a5-4fd1-89d4-c51b59f58e64"
                send_res = client1.send_email(
                    from_addr="codex-worker1@boats",
                    to_addrs=["claude@nate-bot"],
                    cc_addrs=[],
                    subject="Sail API response shape",
                    body_markdown="I added `draft_id`. Can you check the consumer?",
                    idempotency_key=idempotency_token,
                )
                eml_01_id = send_res["email_id"]
                thr_01_id = send_res["thread_id"]

                # 4. Repeat with same idempotency token returns exact same email
                send_repeat = client1.send_email(
                    from_addr="codex-worker1@boats",
                    to_addrs=["claude@nate-bot"],
                    subject="Sail API response shape",
                    body_markdown="I added `draft_id`. Can you check the consumer?",
                    idempotency_key=idempotency_token,
                )
                self.assertEqual(send_repeat["email_id"], eml_01_id)
                self.assertEqual(send_repeat["thread_id"], thr_01_id)

                # 5. claude@nate-bot lists unread threads -> sees 1 unread thread
                claude_unread = client1.list_threads("claude@nate-bot", unread=True)
                self.assertEqual(len(claude_unread), 1)
                self.assertEqual(claude_unread[0]["thread_id"], thr_01_id)
                self.assertEqual(claude_unread[0]["unread_count"], 1)
                self.assertEqual(claude_unread[0]["participants"], ["codex-worker1@boats", "claude@nate-bot"])

                # 6. claude@nate-bot reads thread detail -> read state is unread
                thread_detail = client1.get_thread("claude@nate-bot", thr_01_id)
                self.assertEqual(thread_detail["thread_id"], thr_01_id)
                self.assertEqual(len(thread_detail["emails"]), 1)
                self.assertFalse(thread_detail["emails"][0]["read"])

                # 7. claude@nate-bot marks thread read
                read_res = client1.mark_thread_read("claude@nate-bot", thr_01_id)
                self.assertEqual(read_res["marked_read"], 1)

                # Confirm claude has 0 unread now
                self.assertEqual(len(client1.list_threads("claude@nate-bot", unread=True)), 0)

            finally:
                # Stop Server 1
                server1.shutdown()
                server1.server_close()
                conn1.close()

            # ----------------------------------------------------
            # Phase 2: Restart server with identical database file
            # ----------------------------------------------------
            conn2 = get_connection(db_path)
            server2 = AgentInboxServer(("127.0.0.1", 0), conn2, verbose=False)
            host2, port2 = server2.server_address
            client2 = InboxClient(f"http://{host2}:{port2}")

            t2 = threading.Thread(target=server2.serve_forever, daemon=True)
            t2.start()

            try:
                # 8. Confirm state persisted across server restart
                detail_after_restart = client2.get_thread("claude@nate-bot", thr_01_id)
                self.assertEqual(len(detail_after_restart["emails"]), 1)
                self.assertTrue(detail_after_restart["emails"][0]["read"])  # Persisted as read for Claude

                # 9. claude@nate-bot replies to eml_01
                reply_token = "29952737-5a3f-4a30-95e3-846e77155d7a"
                reply_res = client2.reply_email(
                    email_id=eml_01_id,
                    from_addr="claude@nate-bot",
                    body_markdown="Checked; the consumer now accepts it.",
                    idempotency_key=reply_token,
                )
                eml_02_id = reply_res["email_id"]
                self.assertEqual(reply_res["thread_id"], thr_01_id)
                self.assertEqual(reply_res["reply_to_email_id"], eml_01_id)
                self.assertEqual(reply_res["references"], [eml_01_id])
                self.assertEqual(reply_res["to"], ["codex-worker1@boats"])
                self.assertEqual(reply_res["cc"], [])

                # 10. Repeat reply idempotency check
                repeat_reply = client2.reply_email(
                    email_id=eml_01_id,
                    from_addr="claude@nate-bot",
                    body_markdown="Checked; the consumer now accepts it.",
                    idempotency_key=reply_token,
                )
                self.assertEqual(repeat_reply["email_id"], eml_02_id)

                # 11. codex-worker1@boats checks unread threads -> sees reply unread!
                codex_unread = client2.list_threads("codex-worker1@boats", unread=True)
                self.assertEqual(len(codex_unread), 1)
                self.assertEqual(codex_unread[0]["unread_count"], 1)

                # 12. codex-worker1@boats fetches thread detail -> sees 2 emails in order
                codex_thread = client2.get_thread("codex-worker1@boats", thr_01_id)
                self.assertEqual(len(codex_thread["emails"]), 2)
                # First email (sent by codex) -> read: True
                self.assertTrue(codex_thread["emails"][0]["read"])
                # Second email (reply sent by claude to codex) -> read: False
                self.assertFalse(codex_thread["emails"][1]["read"])
                self.assertEqual(codex_thread["emails"][1]["body_markdown"], "Checked; the consumer now accepts it.")

                # 13. codex marks thread read
                client2.mark_thread_read("codex-worker1@boats", thr_01_id)
                self.assertEqual(len(client2.list_threads("codex-worker1@boats", unread=True)), 0)

            finally:
                server2.shutdown()
                server2.server_close()
                conn2.close()


if __name__ == "__main__":
    unittest.main()
