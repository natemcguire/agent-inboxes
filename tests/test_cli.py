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
from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService


class TestCLI(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.db_path = Path(self.tmp_dir.name) / "cli_test.db"
        self.env_patch = mock.patch.dict(os.environ, {
            "AGENT_INBOX_DIR": self.tmp_dir.name,
            "AGENT_INBOX_DB": str(self.db_path),
            "AGENT_INBOX_CLOUD_CONFIG": str(Path(self.tmp_dir.name) / "cloud.json"),
            "AGENT_INBOX_AGENT": "test-agent",
            "AGENT_INBOX_PROJECT": "test-project",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.db_conn = get_connection(self.db_path)
        self.addCleanup(self.db_conn.close)
        self.server = AgentInboxServer(("127.0.0.1", 0), self.db_conn, verbose=False)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["AGENT_INBOX_URL"] = self.base_url
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for address in ('target@other-project', 'filetest@other-project'):
            InboxService(self.db_conn).ensure_inbox(address)

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
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "test-agent@test-project")
        # Line 2 announces this agent session's short id.
        self.assertRegex(lines[1], r"^session: [a-z0-9][a-z0-9-]*$")

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
        body = "First paragraph: café.\n\n- Preserve this line\n"
        body_path = Path(self.tmp_dir.name) / "message.md"
        body_path.write_text(body, encoding="utf-8")
        reader = InboxClient(self.base_url)
        for source in (str(body_path), "-"):
            with self.subTest(source=source), mock.patch("sys.stdin", io.StringIO(body)):
                code, out, err = self._run_cli([
                    "send", "--to", "filetest@other-project",
                    "--subject", "File Body Test", "--body-file", source, "--json",
                ])
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                delivered = reader.get_thread("filetest@other-project", data["thread_id"])["emails"]
                self.assertEqual(len(delivered), 1)
                self.assertEqual(delivered[0]["email_id"], data["email_id"])
                self.assertEqual(delivered[0]["body_markdown"], body)
                self.assertEqual(delivered[0]["subject"], "File Body Test")
                self.assertEqual(delivered[0]["from"], "test-agent@test-project")
                self.assertEqual(delivered[0]["to"], ["filetest@other-project"])

    def test_inboxes_command(self):
        InboxService(self.db_conn).ensure_inbox("known@test-project", display_name="Known agent")
        code, out, err = self._run_cli(["inboxes", "--project", "test-project", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([inbox["address"] for inbox in data["inboxes"]], ["known@test-project"])
        self.assertEqual(data["inboxes"][0]["display_name"], "Known agent")

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
        self.assertEqual(res.stdout.strip().splitlines()[0], "bin-agent@bin-project")

    def test_honest_not_running_error(self):
        """When server is down, CLI emits an honest error and non-zero exit code."""
        with mock.patch.dict(os.environ, {"AGENT_INBOX_URL": "http://127.0.0.1:59999"}):
            code, out, err = self._run_cli(["whoami"])
            self.assertEqual(code, 1)
            self.assertIn("not reachable", err)


if __name__ == "__main__":
    unittest.main()
