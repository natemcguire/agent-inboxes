"""Unit tests for SQLite database schema, WAL mode, pragmas, and permissions."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_inbox.db import get_connection, init_db


class TestDatabase(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "subdir" / "test_inbox.db"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_connection_pragmas_and_permissions(self):
        conn = get_connection(self.db_path)
        try:
            # Check directory permissions (0700)
            dir_mode = stat.S_IMODE(os.stat(self.db_path.parent).st_mode)
            self.assertEqual(dir_mode, 0o700)

            # Check file permissions (0600)
            file_mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
            self.assertEqual(file_mode, 0o600)

            # Check foreign keys pragma
            fk_res = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            self.assertEqual(fk_res, 1)

            # Check WAL journal mode
            journal_res = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(journal_res.lower(), "wal")

            # Check busy timeout (5000ms)
            timeout_res = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
            self.assertEqual(timeout_res, 5000)

            # Verify tables exist
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
                ).fetchall()
            ]
            for required_table in [
                "projects",
                "inboxes",
                "threads",
                "thread_inboxes",
                "emails",
                "email_recipients",
                "email_references",
            ]:
                self.assertIn(required_table, tables)

            # Verify indexes exist
            indexes = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name;"
                ).fetchall()
            ]
            self.assertIn("idx_emails_thread_sent", indexes)
            self.assertIn("idx_recipients_unread", indexes)
            self.assertIn("idx_threads_activity", indexes)

        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
