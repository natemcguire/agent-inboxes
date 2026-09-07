"""Tests for Layer-2 delivery hooks: emit-dedup logic and config wiring."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_inbox import hooks


def _thread(subject, last):
    return {"thread_id": "thr_x", "subject": subject, "last_email_at": last}


class TestBuildNotice(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = mock.patch("agent_inbox.hooks.get_data_dir", return_value=Path(self.tmp.name))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_no_threads_is_silent(self):
        self.assertIsNone(hooks.build_notice("claude@p", []))

    def test_emits_once_then_dedups(self):
        threads = [_thread("Release: coordinate", "2026-09-07T14:32:00.000Z")]
        first = hooks.build_notice("claude@p", threads, now=1000.0)
        self.assertIsNotNone(first)
        self.assertIn("claude@p", first)
        self.assertIn("Release: coordinate", first)
        # Same activity, seconds later -> silent (dedup).
        self.assertIsNone(hooks.build_notice("claude@p", threads, now=1030.0))

    def test_renags_after_five_minutes_when_still_unread(self):
        threads = [_thread("Design: review", "2026-09-07T14:32:00.000Z")]
        self.assertIsNotNone(hooks.build_notice("claude@p", threads, now=1000.0))
        self.assertIsNone(hooks.build_notice("claude@p", threads, now=1200.0))  # <5min
        self.assertIsNotNone(hooks.build_notice("claude@p", threads, now=1000.0 + 301))  # >5min

    def test_new_activity_emits_immediately(self):
        t1 = [_thread("Claim: a", "2026-09-07T14:32:00.000Z")]
        self.assertIsNotNone(hooks.build_notice("claude@p", t1, now=1000.0))
        t2 = [_thread("Claim: a", "2026-09-07T14:40:00.000Z")]  # newer activity
        self.assertIsNotNone(hooks.build_notice("claude@p", t2, now=1010.0))

    def test_drained_inbox_clears_stamp(self):
        threads = [_thread("Release: x", "2026-09-07T14:32:00.000Z")]
        hooks.build_notice("claude@p", threads, now=1000.0)
        self.assertIsNone(hooks.build_notice("claude@p", [], now=1001.0))  # drained
        # New mail right after a drain emits at once (stamp was cleared).
        self.assertIsNotNone(hooks.build_notice("claude@p", threads, now=1002.0))


class TestHooksWiring(unittest.TestCase):

    def _run(self, home):
        with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
            return {
                "install": dict(hooks.install_hooks()),
                "status": dict(hooks.hooks_status()),
            }

    def test_install_is_idempotent_and_preserves_existing_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir()
            settings = home / ".claude" / "settings.json"
            settings.write_text(json.dumps({
                "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo keep"}]}]}
            }), encoding="utf-8")

            with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
                self.assertEqual(dict(hooks.install_hooks())["Claude Code"], "installed")
                self.assertEqual(dict(hooks.install_hooks())["Claude Code"], "already installed")
                cfg = json.loads(settings.read_text())
                ups = cfg["hooks"]["UserPromptSubmit"]
                self.assertEqual(len(ups), 2)  # existing + ours
                self.assertIn("PostToolUse", cfg["hooks"])
                # Uninstall removes only ours, keeps the pre-existing hook.
                hooks.uninstall_hooks()
                cfg2 = json.loads(settings.read_text())
                self.assertEqual(
                    cfg2["hooks"]["UserPromptSubmit"],
                    [{"hooks": [{"type": "command", "command": "echo keep"}]}],
                )

    def test_absent_runtime_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)  # no runtime config dirs
            with mock.patch.object(Path, "home", classmethod(lambda cls: home)):
                res = dict(hooks.install_hooks())
            self.assertIn("skipped", res["Claude Code"])


if __name__ == "__main__":
    unittest.main()
