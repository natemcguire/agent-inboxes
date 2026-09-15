"""Access and permission regressions; transaction coverage lives in test_service."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_inbox.db import get_connection
from agent_inbox.models import NotFoundError
from agent_inbox.server import run_server
from agent_inbox.service import InboxService


class TestRejectNonLoopbackBind(unittest.TestCase):
    """P1a: the unauthenticated service must refuse non-loopback hosts."""

    def test_non_loopback_host_rejected_from_argument_or_environment(self):
        for source, kwargs in [("argument", {"host": "0.0.0.0"}), ("environment", {})]:
            with self.subTest(source=source), mock.patch.dict(os.environ, {"AGENT_INBOX_HOST": "192.168.1.50"}):
                with self.assertRaisesRegex(ValueError, "non-loopback"):
                    run_server(port=0, **kwargs)


class TestCustomParentPermissions(unittest.TestCase):
    """P1b: a db in a pre-existing shared directory must not have that
    directory's permissions rewritten to 0700."""

    def test_shared_parent_not_chmodded(self):
        for source in ("argument", "environment"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as shared:
                shared_path = Path(shared)
                os.chmod(shared_path, 0o755)
                db_path = shared_path / "inbox.db"
                # Both explicit paths and AGENT_INBOX_DB previously confused
                # shared directories with directories owned by the service.
                with mock.patch.dict(os.environ, {"AGENT_INBOX_DB": str(db_path)}):
                    conn = get_connection(db_path) if source == "argument" else get_connection()
                    conn.close()
                self.assertEqual(stat.S_IMODE(shared_path.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)


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
            create_missing=True,
        )
        self.thread_id = sent["thread_id"]

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_nonmember_cannot_read_or_mark_thread(self):
        for operation in (self.service.get_thread, self.service.mark_thread_read):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(NotFoundError) as ctx:
                    operation("eve@proj", self.thread_id)
                self.assertEqual(ctx.exception.code, "thread_not_found")


if __name__ == "__main__":
    unittest.main()
