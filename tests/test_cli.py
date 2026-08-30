"""Unit tests for the agent-inbox command-line interface."""

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_inbox.cli import main
from agent_inbox.db import get_connection
from agent_inbox.project_setup import setup_project
from agent_inbox.server import AgentInboxServer


class TestCLI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp_dir.name) / "cli_test.db"
        cls.db_conn = get_connection(cls.db_path)

        cls.server = AgentInboxServer(("127.0.0.1", 0), cls.db_conn, verbose=False)
        cls.host, cls.port = cls.server.server_address
        cls.base_url = f"http://{cls.host}:{cls.port}"

        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db_conn.close()
        cls.tmp_dir.cleanup()

    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {
            "AGENT_INBOX_URL": self.base_url,
            "AGENT_INBOX_AGENT": "test-agent",
            "AGENT_INBOX_PROJECT": "test-project",
        })
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def _run_cli(self, args: list) -> tuple[int, str, str]:
        """Run CLI main function and capture stdout/stderr."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("sys.stderr", err_buf):
            code = main(args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_whoami_command(self):
        code, out, err = self._run_cli(["whoami"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "test-agent@test-project")

        # With --json
        code, out, err = self._run_cli(["whoami", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["address"], "test-agent@test-project")

    def test_send_and_list_and_read_and_reply_cli(self):
        # 1. Send
        code, out, err = self._run_cli([
            "send",
            "--to", "target@other-project",
            "--subject", "CLI Feature Test",
            "--body", "Message body from CLI test",
            "--json",
        ])
        self.assertEqual(code, 0)
        send_data = json.loads(out)
        email_id = send_data["email_id"]
        thread_id = send_data["thread_id"]

        # 2. List threads for recipient
        code, out, err = self._run_cli([
            "list",
            "--inbox", "target@other-project",
            "--unread",
            "--json",
        ])
        self.assertEqual(code, 0)
        list_data = json.loads(out)
        self.assertEqual(len(list_data["threads"]), 1)
        self.assertEqual(list_data["threads"][0]["thread_id"], thread_id)

        # 3. Read thread (marks read)
        code, out, err = self._run_cli([
            "read",
            thread_id,
            "--inbox", "target@other-project",
        ])
        self.assertEqual(code, 0)
        self.assertIn("CLI Feature Test", out)
        self.assertIn("Message body from CLI test", out)

        # Confirm unread is now 0
        code, out, err = self._run_cli([
            "list",
            "--inbox", "target@other-project",
            "--unread",
            "--json",
        ])
        self.assertEqual(code, 0)
        list_data_after = json.loads(out)
        self.assertEqual(len(list_data_after["threads"]), 0)

        # 4. Reply
        code, out, err = self._run_cli([
            "reply",
            email_id,
            "--from", "target@other-project",
            "--body", "Reply from CLI target",
            "--json",
        ])
        self.assertEqual(code, 0)
        reply_data = json.loads(out)
        self.assertEqual(reply_data["thread_id"], thread_id)
        self.assertEqual(reply_data["reply_to_email_id"], email_id)

    def test_setup_project_command(self):
        with tempfile.TemporaryDirectory() as td:
            p_dir = Path(td)
            code, out, err = self._run_cli(["setup-project", "--dir", str(p_dir)])
            self.assertEqual(code, 0)
            agents_file = p_dir / "AGENTS.md"
            self.assertTrue(agents_file.exists())
            content = agents_file.read_text(encoding="utf-8")
            self.assertIn("<!-- agent-inboxes:start -->", content)
            self.assertIn("<!-- agent-inboxes:end -->", content)

    def test_send_body_file(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("Content from temporary file")
            f_path = f.name
        try:
            code, out, err = self._run_cli([
                "send",
                "--to", "filetest@other-project",
                "--subject", "File Body Test",
                "--body-file", f_path,
                "--json",
            ])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertIn("email_id", data)
        finally:
            Path(f_path).unlink()

    def test_inboxes_command(self):
        code, out, err = self._run_cli(["inboxes", "--project", "test-project", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("inboxes", data)

    def test_binary_executable_execution(self):
        bin_path = Path(__file__).resolve().parent.parent / "bin" / "agent-inbox"
        import subprocess
        env = dict(os.environ)
        env["AGENT_INBOX_URL"] = self.base_url
        env["AGENT_INBOX_AGENT"] = "bin-agent"
        env["AGENT_INBOX_PROJECT"] = "bin-project"

        res = subprocess.run(
            [str(bin_path), "whoami"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "bin-agent@bin-project")

    def test_honest_not_running_error(self):
        """When server is down, CLI emits an honest error and non-zero exit code."""
        with mock.patch.dict(os.environ, {"AGENT_INBOX_URL": "http://127.0.0.1:59999"}):
            code, out, err = self._run_cli(["whoami"])
            self.assertEqual(code, 1)
            self.assertIn("not reachable", err)


if __name__ == "__main__":
    unittest.main()
