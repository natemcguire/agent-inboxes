"""Unit tests for InboxService transactional domain logic."""

import tempfile
import unittest
from pathlib import Path

from agent_inbox.db import get_connection
from agent_inbox.models import NotFoundError, ValidationError
from agent_inbox.service import InboxService


class TestInboxService(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.conn = get_connection(self.db_path)
        self.service = InboxService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_ensure_inbox_idempotency(self):
        # Create inbox
        r1 = self.service.ensure_inbox("codex-worker1@boats", display_name="Codex 1")
        self.assertEqual(r1["address"], "codex-worker1@boats")
        self.assertTrue(r1["created"])
        self.assertIsNotNone(r1["created_at"])
        self.assertIsNotNone(r1["last_seen_at"])

        # Update inbox
        r2 = self.service.ensure_inbox("codex-worker1@boats", display_name="Codex Worker One")
        self.assertEqual(r2["address"], "codex-worker1@boats")
        self.assertFalse(r2["created"])
        self.assertEqual(r2["created_at"], r1["created_at"])

        # Check list
        inboxes = self.service.list_inboxes("boats")
        self.assertEqual(len(inboxes), 1)
        self.assertEqual(inboxes[0]["display_name"], "Codex Worker One")

    def test_send_email_and_idempotency(self):
        res1 = self.service.send_email(
            from_addr="codex-worker1@boats",
            to_addrs=["claude@nate-bot"],
            cc_addrs=["observer@nate-bot"],
            subject="Sail API response shape",
            body_markdown="I added `draft_id`. Can you check the consumer?",
            client_token="token-001",
        )
        self.assertTrue(res1["email_id"].startswith("eml_"))
        self.assertTrue(res1["thread_id"].startswith("thr_"))
        self.assertIsNotNone(res1["sent_at"])

        # Repeat with same idempotency token
        res2 = self.service.send_email(
            from_addr="codex-worker1@boats",
            to_addrs=["claude@nate-bot"],
            cc_addrs=["observer@nate-bot"],
            subject="Sail API response shape",
            body_markdown="I added `draft_id`. Can you check the consumer?",
            client_token="token-001",
        )
        self.assertEqual(res1["email_id"], res2["email_id"])
        self.assertEqual(res1["thread_id"], res2["thread_id"])
        self.assertEqual(res1["sent_at"], res2["sent_at"])

        # Verify only 1 email and 1 thread in DB
        email_count = self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        thread_count = self.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        self.assertEqual(email_count, 1)
        self.assertEqual(thread_count, 1)

    def test_send_email_duplicate_recipients_rejected(self):
        # Duplicate in to
        with self.assertRaises(ValidationError):
            self.service.send_email(
                from_addr="codex@boats",
                to_addrs=["claude@nate-bot", "claude@nate-bot"],
                cc_addrs=[],
                subject="Test",
                body_markdown="Body",
                client_token="tok-dup1",
            )

        # Recipient across to and cc
        with self.assertRaises(ValidationError):
            self.service.send_email(
                from_addr="codex@boats",
                to_addrs=["claude@nate-bot"],
                cc_addrs=["claude@nate-bot"],
                subject="Test",
                body_markdown="Body",
                client_token="tok-dup2",
            )

    def test_send_email_missing_to_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.send_email(
                from_addr="codex@boats",
                to_addrs=[],
                cc_addrs=[],
                subject="Test",
                body_markdown="Body",
                client_token="tok-empty-to",
            )

    def test_reply_all_default_and_reference_chaining(self):
        # Step 1: Initial email from codex to claude and cc observer
        initial = self.service.send_email(
            from_addr="codex-worker1@boats",
            to_addrs=["claude@nate-bot"],
            cc_addrs=["observer@nate-bot"],
            subject="Initial Topic",
            body_markdown="Message 1",
            client_token="tok-init",
        )
        eml_1_id = initial["email_id"]
        thread_id = initial["thread_id"]

        # Step 2: Claude replies (reply-all default)
        reply1 = self.service.reply_email(
            reply_to_email_id=eml_1_id,
            from_addr="claude@nate-bot",
            body_markdown="Message 2 from Claude",
            client_token="tok-reply-1",
        )
        self.assertEqual(reply1["thread_id"], thread_id)
        self.assertEqual(reply1["reply_to_email_id"], eml_1_id)
        self.assertEqual(reply1["references"], [eml_1_id])
        self.assertEqual(reply1["to"], ["codex-worker1@boats"])
        self.assertEqual(reply1["cc"], ["observer@nate-bot"])
        eml_2_id = reply1["email_id"]

        # Step 3: Observer replies to email 2
        reply2 = self.service.reply_email(
            reply_to_email_id=eml_2_id,
            from_addr="observer@nate-bot",
            body_markdown="Message 3 from Observer",
            client_token="tok-reply-2",
        )
        self.assertEqual(reply2["thread_id"], thread_id)
        self.assertEqual(reply2["reply_to_email_id"], eml_2_id)
        self.assertEqual(reply2["references"], [eml_1_id, eml_2_id])
        self.assertEqual(reply2["to"], ["claude@nate-bot"])
        self.assertEqual(reply2["cc"], ["codex-worker1@boats"])

        # Repeat reply with same token (idempotency)
        repeat_reply = self.service.reply_email(
            reply_to_email_id=eml_2_id,
            from_addr="observer@nate-bot",
            body_markdown="Message 3 from Observer",
            client_token="tok-reply-2",
        )
        self.assertEqual(repeat_reply["email_id"], reply2["email_id"])
        self.assertEqual(repeat_reply["references"], [eml_1_id, eml_2_id])

    def test_reply_nonexistent_email_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.reply_email(
                reply_to_email_id="eml_nonexistent",
                from_addr="claude@nate-bot",
                body_markdown="Test",
                client_token="tok-404",
            )

    def test_thread_visibility_and_independent_read_state(self):
        # 1. Codex sends to Claude and Observer
        initial = self.service.send_email(
            from_addr="codex@boats",
            to_addrs=["claude@nate-bot"],
            cc_addrs=["observer@nate-bot"],
            subject="Shared Task",
            body_markdown="Please review",
            client_token="tok-shared",
        )
        thread_id = initial["thread_id"]

        # Check unread count for Claude -> 1 unread
        claude_threads = self.service.list_threads("claude@nate-bot", unread_only=True)
        self.assertEqual(len(claude_threads), 1)
        self.assertEqual(claude_threads[0]["unread_count"], 1)

        # Check unread count for Observer -> 1 unread
        observer_threads = self.service.list_threads("observer@nate-bot", unread_only=True)
        self.assertEqual(len(observer_threads), 1)
        self.assertEqual(observer_threads[0]["unread_count"], 1)

        # Check unread count for Sender (Codex) -> 0 unread (sender is not unread)
        codex_threads_unread = self.service.list_threads("codex@boats", unread_only=True)
        self.assertEqual(len(codex_threads_unread), 0)

        # Sender listing all threads sees thread with unread_count=0
        codex_threads_all = self.service.list_threads("codex@boats", unread_only=False)
        self.assertEqual(len(codex_threads_all), 1)
        self.assertEqual(codex_threads_all[0]["unread_count"], 0)

        # Claude fetches thread detail (GET thread should NOT mark read)
        detail = self.service.get_thread("claude@nate-bot", thread_id)
        self.assertEqual(len(detail["emails"]), 1)
        self.assertFalse(detail["emails"][0]["read"])  # Still unread

        # Still unread in list
        self.assertEqual(self.service.list_threads("claude@nate-bot", unread_only=True)[0]["unread_count"], 1)

        # Claude marks thread read
        marked = self.service.mark_thread_read("claude@nate-bot", thread_id)
        self.assertEqual(marked, 1)

        # Claude unread count is now 0
        self.assertEqual(len(self.service.list_threads("claude@nate-bot", unread_only=True)), 0)

        # Claude detail now shows read=True
        detail_after = self.service.get_thread("claude@nate-bot", thread_id)
        self.assertTrue(detail_after["emails"][0]["read"])

        # Observer unread count is STILL 1 (independent per-recipient read state)
        obs_unread = self.service.list_threads("observer@nate-bot", unread_only=True)
        self.assertEqual(len(obs_unread), 1)
    def test_self_reply_default_recipients(self):
        # Initial email from codex to claude
        initial = self.service.send_email(
            from_addr="codex@boats",
            to_addrs=["claude@nate-bot"],
            cc_addrs=[],
            subject="Self follow-up topic",
            body_markdown="First thought",
            client_token="tok-self-1",
        )
        eml_1_id = initial["email_id"]

        # Codex replies to own email
        reply = self.service.reply_email(
            reply_to_email_id=eml_1_id,
            from_addr="codex@boats",
            body_markdown="Second thought follow-up",
            client_token="tok-self-2",
        )
        # Should address the original recipient (claude)
        self.assertEqual(reply["to"], ["claude@nate-bot"])
        self.assertEqual(reply["references"], [eml_1_id])

    def test_thread_sorting_and_limit(self):
        import time
        # Create 3 threads for user
        self.service.send_email(
            from_addr="sender@a",
            to_addrs=["user@b"],
            cc_addrs=[],
            subject="Thread 1",
            body_markdown="Body 1",
            client_token="tok-t1",
        )
        time.sleep(0.01)
        t2 = self.service.send_email(
            from_addr="sender@a",
            to_addrs=["user@b"],
            cc_addrs=[],
            subject="Thread 2",
            body_markdown="Body 2",
            client_token="tok-t2",
        )
        time.sleep(0.01)
        t3 = self.service.send_email(
            from_addr="sender@a",
            to_addrs=["user@b"],
            cc_addrs=[],
            subject="Thread 3",
            body_markdown="Body 3",
            client_token="tok-t3",
        )

        # Thread 3 was created last, so it's top
        threads = self.service.list_threads("user@b", limit=2)
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0]["thread_id"], t3["thread_id"])
        self.assertEqual(threads[1]["thread_id"], t2["thread_id"])


if __name__ == "__main__":
    unittest.main()
