"""Unit tests for identity derivation from Git and environment variables."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_inbox.identity import (
    _extract_repo_name_from_url,
    derive_agent,
    derive_identity,
    derive_project,
)


class TestIdentity(unittest.TestCase):

    def test_extract_repo_name_from_urls(self):
        cases = [
            ("git@github.com:nate/boats.git", "boats"),
            ("https://github.com/nate/boats.git", "boats"),
            ("https://github.com/nate/boats", "boats"),
            ("git@gitlab.com:team/subgroup/nate-bot.git", "nate-bot"),
            ("ssh://git@host:2222/path/to/my-repo.git", "my-repo"),
            ("file:///path/to/local-repo.git", "local-repo"),
        ]
        for url, expected in cases:
            self.assertEqual(_extract_repo_name_from_url(url), expected)

    def test_derive_project_from_env(self):
        with mock.patch.dict(os.environ, {"AGENT_INBOX_PROJECT": "custom-project"}):
            self.assertEqual(derive_project(), "custom-project")

    def test_derive_agent_from_env(self):
        with mock.patch.dict(os.environ, {"AGENT_INBOX_AGENT": "worker-42"}):
            self.assertEqual(derive_agent(), "worker-42")

    def test_derive_agent_runtime_detection(self):
        # Claude detection
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/some/path"}, clear=True):
            self.assertEqual(derive_agent(), "claude")

        # Codex detection
        with mock.patch.dict(os.environ, {"CODEX_SANDBOX": "true"}, clear=True):
            self.assertEqual(derive_agent(), "codex")

        # Orca detection
        with mock.patch.dict(os.environ, {"ORCA_TASK_ID": "task-99"}, clear=True):
            self.assertEqual(derive_agent(), "orca")

        # Fallback to agent
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(derive_agent(), "agent")

    def test_derive_identity_full(self):
        with mock.patch.dict(os.environ, {
            "AGENT_INBOX_AGENT": "claude",
            "AGENT_INBOX_PROJECT": "nate-bot",
        }):
            agent, project, address = derive_identity()
            self.assertEqual(agent, "claude")
            self.assertEqual(project, "nate-bot")
            self.assertEqual(address, "claude@nate-bot")


if __name__ == "__main__":
    unittest.main()
