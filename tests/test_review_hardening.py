"""Regression tests closing the four Codex review findings (2026-08-30).

One test per finding:
  P1a - run_server rejects non-loopback bind hosts.
  P1b - a custom --db parent in a shared dir is NOT chmodded to 0700.
  P2a - standalone ensure_inbox is atomic (project + inbox commit together).
  P2b - get_thread / mark_thread_read enforce inbox membership (no leak).
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_inbox.db import get_connection
from agent_inbox.models import NotFoundError
from agent_inbox.server import run_server
from agent_inbox.service import InboxService


class TestRejectNonLoopbackBind(unittest.TestCase):
    """P1a: the unauthenticated service must refuse non-loopback hosts."""

    def test_explicit_lan_host_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            run_server(host="0.0.0.0", port=0)
        self.assertIn("non-loopback", str(ctx.exception))

    def test_env_host_rejected(self):
        prev = os.environ.get("AGENT_INBOX_HOST")
        os.environ["AGENT_INBOX_HOST"] = "192.168.1.50"
        try:
            with self.assertRaises(ValueError):
                run_server(port=0)
        finally:
            if prev is None:
                os.environ.pop("AGENT_INBOX_HOST", None)
            else:
                os.environ["AGENT_INBOX_HOST"] = prev


class TestCustomParentPermissions(unittest.TestCase):
    """P1b: a db in a pre-existing shared directory must not have that
    directory's permissions rewritten to 0700."""

    def test_shared_parent_not_chmodded(self):
        # A pre-existing directory standing in for a shared location (/tmp-like):
        # give it group/other-readable perms and assert we leave them alone.
        shared = tempfile.TemporaryDirectory()
        shared_path = Path(shared.name)
        os.chmod(shared_path, 0o755)
        try:
            db_path = shared_path / "agent-inbox.db"
            conn = get_connection(db_path)
            conn.close()
            mode = stat.S_IMODE(os.stat(shared_path).st_mode)
            # Parent perms untouched (still group/other-accessible), NOT 0700.
            self.assertEqual(mode, 0o755)
            # The db file itself is still locked down to 0600.
            self.assertEqual(stat.S_IMODE(os.stat(db_path).st_mode), 0o600)
        finally:
            shared.cleanup()

    def test_env_db_in_shared_parent_not_chmodded(self):
        # Regression for the re-review P1: AGENT_INBOX_DB points at a file in a
        # pre-existing shared dir AND get_connection() is called with no
        # explicit path. The first fix compared against get_db_path().parent,
        # which equals that shared dir here -> wrongly "owned". Must stay 0755.
        shared = tempfile.TemporaryDirectory()
        shared_path = Path(shared.name)
        os.chmod(shared_path, 0o755)
        prev = os.environ.get("AGENT_INBOX_DB")
        os.environ["AGENT_INBOX_DB"] = str(shared_path / "inbox.db")
        try:
            conn = get_connection()  # no explicit path -> resolves via env
            conn.close()
            mode = stat.S_IMODE(os.stat(shared_path).st_mode)
            self.assertEqual(mode, 0o755)
        finally:
            if prev is None:
                os.environ.pop("AGENT_INBOX_DB", None)
            else:
                os.environ["AGENT_INBOX_DB"] = prev
            shared.cleanup()

    def test_created_parent_is_locked_down(self):
        # A parent the service itself creates SHOULD be tightened to 0700.
        base = tempfile.TemporaryDirectory()
        try:
            db_path = Path(base.name) / "fresh-subdir" / "agent-inbox.db"
            conn = get_connection(db_path)
            conn.close()
            mode = stat.S_IMODE(os.stat(db_path.parent).st_mode)
            self.assertEqual(mode, 0o700)
        finally:
            base.cleanup()


class TestStandaloneEnsureInboxAtomic(unittest.TestCase):
    """P2a: ensure_inbox outside a caller transaction commits project + inbox
    as one unit and does not leave an open transaction behind."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.conn = get_connection(Path(self.tmp_dir.name) / "test.db")
        self.service = InboxService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_standalone_ensure_inbox_commits_and_closes_txn(self):
        self.assertFalse(self.conn.in_transaction)
        self.service.ensure_inbox("agent@newproj", display_name="Agent")
        # No dangling transaction after a standalone call.
        self.assertFalse(self.conn.in_transaction)
        # Both the project and the inbox are durably present.
        proj = self.conn.execute(
            "SELECT id FROM projects WHERE slug = ? COLLATE NOCASE", ("newproj",)
        ).fetchone()
        self.assertIsNotNone(proj)
        inbox = self.conn.execute(
            "SELECT id FROM inboxes WHERE project_id = ? AND local_part = ? COLLATE NOCASE",
            (proj[0], "agent"),
        ).fetchone()
        self.assertIsNotNone(inbox)

    def test_ensure_inbox_inert_inside_open_transaction(self):
        # When a caller already holds a transaction, ensure_inbox must not
        # commit it out from under them (send/reply rely on this).
        self.conn.execute("BEGIN IMMEDIATE")
        self.service.ensure_inbox("agent@proj2")
        self.assertTrue(self.conn.in_transaction)  # still the caller's txn
        self.conn.execute("ROLLBACK")
        # Rolled back -> the inbox should NOT persist.
        row = self.conn.execute(
            "SELECT 1 FROM projects WHERE slug = ? COLLATE NOCASE", ("proj2",)
        ).fetchone()
        self.assertIsNone(row)


class TestThreadMembershipEnforced(unittest.TestCase):
    """P2b: a non-member inbox cannot read or mark-read another thread, and
    gets the same not-found error (no existence leak)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.conn = get_connection(Path(self.tmp_dir.name) / "test.db")
        self.service = InboxService(self.conn)
        sent = self.service.send_email(
            from_addr="alice@proj",
            to_addrs=["bob@proj"],
            cc_addrs=[],
            subject="Private",
            body_markdown="members only",
            client_token="tok-membership-1",
        )
        self.thread_id = sent["thread_id"]

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_member_can_read_thread(self):
        thread = self.service.get_thread("bob@proj", self.thread_id)
        self.assertEqual(thread["thread_id"], self.thread_id)

    def test_nonmember_get_thread_raises_not_found(self):
        with self.assertRaises(NotFoundError) as ctx:
            self.service.get_thread("eve@proj", self.thread_id)
        self.assertEqual(ctx.exception.code, "thread_not_found")

    def test_nonmember_mark_read_raises_not_found(self):
        with self.assertRaises(NotFoundError) as ctx:
            self.service.mark_thread_read("eve@proj", self.thread_id)
        self.assertEqual(ctx.exception.code, "thread_not_found")

    def test_member_mark_read_succeeds(self):
        count = self.service.mark_thread_read("bob@proj", self.thread_id)
        self.assertGreaterEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
