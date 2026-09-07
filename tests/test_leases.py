"""Tests for agent lease claiming and the global instruction-file setup."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_inbox.db import get_connection
from agent_inbox.project_setup import (
    END_MARKER,
    START_MARKER,
    inject_into_file,
    setup_global,
)
from agent_inbox.service import InboxService


class TestAgentLeases(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.db"
        self.conn = get_connection(self.db_path)
        self.service = InboxService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_claim_assigns_lowest_free_slots_in_order(self):
        first = self.service.claim_agent("claude", "proj")
        second = self.service.claim_agent("claude", "proj")
        third = self.service.claim_agent("claude", "proj")
        self.assertEqual(first["agent"], "claude")
        self.assertEqual(second["agent"], "claude-2")
        self.assertEqual(third["agent"], "claude-3")
        self.assertEqual(third["address"], "claude-3@proj")

    def test_release_frees_slot_for_immediate_reclaim(self):
        self.service.claim_agent("codex", "proj")
        self.service.claim_agent("codex", "proj")  # codex-2
        released = self.service.release_agent("codex", "proj")
        self.assertTrue(released["released"])
        reclaimed = self.service.claim_agent("codex", "proj")
        self.assertEqual(reclaimed["agent"], "codex")

    def test_expired_lease_is_reclaimed_and_touch_keeps_it_alive(self):
        self.service.claim_agent("claude", "proj")
        # Age the lease past expiry, then confirm a fresh claim takes the base slot.
        self.conn.execute(
            "UPDATE agent_leases SET last_seen = '2000-01-01T00:00:00.000Z' WHERE agent_slug = 'claude'"
        )
        reclaimed = self.service.claim_agent("claude", "proj")
        self.assertEqual(reclaimed["agent"], "claude")
        # touch_lease refreshes last_seen so the next claim must take claude-2.
        self.conn.execute(
            "UPDATE agent_leases SET last_seen = '2000-01-01T00:00:00.000Z' WHERE agent_slug = 'claude'"
        )
        self.service.touch_lease("claude", "proj")
        nxt = self.service.claim_agent("claude", "proj")
        self.assertEqual(nxt["agent"], "claude-2")

    def test_leases_are_scoped_per_project(self):
        a = self.service.claim_agent("claude", "proj-a")
        b = self.service.claim_agent("claude", "proj-b")
        self.assertEqual(a["agent"], "claude")
        self.assertEqual(b["agent"], "claude")


class TestGlobalSetup(unittest.TestCase):

    def test_inject_into_file_is_idempotent_and_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "CLAUDE.md"
            target.write_text("# Mine\n\nkeep this\n", encoding="utf-8")
            self.assertTrue(inject_into_file(target))
            first = target.read_text(encoding="utf-8")
            self.assertIn("keep this", first)
            self.assertIn(START_MARKER, first)
            self.assertIn(END_MARKER, first)
            # Second run: no change, no duplicate block.
            self.assertFalse(inject_into_file(target))
            self.assertEqual(first.count(START_MARKER), 1)

    def test_setup_global_only_touches_runtimes_with_config_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir()
            targets = (
                ("Claude Code", home / ".claude", "CLAUDE.md"),
                ("Codex CLI", home / ".codex", "AGENTS.md"),
            )
            with mock.patch("agent_inbox.project_setup.GLOBAL_INSTRUCTION_TARGETS", targets):
                results = {r[0]: r[2] for r in setup_global()}
            self.assertEqual(results["Claude Code"], "updated")
            self.assertEqual(results["Codex CLI"], "skipped")
            self.assertTrue((home / ".claude" / "CLAUDE.md").exists())
            self.assertFalse((home / ".codex").exists())


if __name__ == "__main__":
    unittest.main()
