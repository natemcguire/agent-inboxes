"""Cloud sync tests use stub HTTP only; never contact a cloud account."""

import contextlib
import io
import json
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_inbox.cli import main
from agent_inbox.cloudsync import (
    CloudClient, CloudSyncError, SyncEngine, SyncWorker, load_config, save_config,
)
from agent_inbox.db import get_connection, init_db
from agent_inbox.service import InboxService


MESSAGE = {
    "message_id": "eml_foreign", "thread_id": "thr_foreign", "seq": 7,
    "project": "remote", "subject": "Across machines", "sender": "alice@remote",
    "sender_session": "s-remote", "recipients_json": {"to": ["bob@local"], "cc": ["eve@third"]},
    "body": "Hello from another machine", "sent_at": "2020-01-01T00:00:00.000Z",
}


class TestSyncEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = get_connection(Path(self.tmp.name) / "inbox.db")
        self.addCleanup(self.conn.close)
        self.service = InboxService(self.conn)
        self.http = mock.Mock()
        self.http.push.return_value = {"accepted": 0, "duplicates": 1}
        self.http.pull.return_value = {"messages": [], "last_seq": 0}
        self.engine = SyncEngine(self.http)

    def send(self, token="local"):
        return self.service.send_email("bob@local", ["alice@remote"], [], "Local", "body", token)

    def cursor(self):
        return self.conn.execute("SELECT last_pulled_seq FROM cloud_state").fetchone()[0]

    def test_push_marks_synced_including_duplicates(self):
        local = self.send()
        self.engine.sync_once(self.conn)
        pushed = self.http.push.call_args.args[0]
        self.assertEqual(pushed[0]["message_id"], local["email_id"])
        self.assertEqual(pushed[0]["sender"], "bob@local")
        self.assertEqual(pushed[0]["recipients_json"], {"to": ["alice@remote"], "cc": []})
        self.assertIsNotNone(self.conn.execute("SELECT cloud_synced_at FROM emails").fetchone()[0])
        self.engine.sync_once(self.conn)
        self.assertEqual(self.http.push.call_count, 1)

    def test_push_batch_limit_and_retry(self):
        for n in range(101):
            self.send(str(n))
        self.http.push.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.engine.sync_once(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM emails WHERE cloud_synced_at IS NULL").fetchone()[0], 101)
        self.http.push.side_effect = None
        self.engine.sync_once(self.conn)
        self.assertEqual(len(self.http.push.call_args.args[0]), 100)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM emails WHERE cloud_synced_at IS NULL").fetchone()[0], 1)

    def test_pull_visible_to_list_read_watch_and_local_reply(self):
        self.http.pull.return_value = {"messages": [MESSAGE], "last_seq": 7}
        before = self.service.watch_state("bob@local")["cursor"]
        self.engine.sync_once(self.conn)
        self.http.pull.assert_called_once_with(0)
        self.assertEqual(self.cursor(), 7)
        self.assertEqual(self.service.list_threads("bob@local", unread_only=True)[0]["thread_id"], "thr_foreign")
        email = self.service.get_thread("bob@local", "thr_foreign")["emails"][0]
        self.assertEqual(email["email_id"], "eml_foreign")
        self.assertEqual(email["body_markdown"], MESSAGE["body"])
        self.assertEqual(email["sender_session"], "s-remote")
        self.assertEqual(email["cc"], ["eve@third"])
        self.assertFalse(email["read"])
        self.assertTrue(self.service.watch_state("bob@local", after=before)["changed"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
        reply = self.service.reply_email("eml_foreign", "bob@local", "Got it", "reply")
        self.assertEqual(reply["thread_id"], "thr_foreign")
        self.assertEqual(reply["to"], ["alice@remote"])

    def test_pull_replay_preserves_read_state_and_rowid(self):
        self.http.pull.return_value = {"messages": [MESSAGE], "last_seq": 7}
        self.engine.sync_once(self.conn)
        self.service.mark_thread_read("bob@local", "thr_foreign")
        before = self.service.watch_state("bob@local")["cursor"]
        # Simulate replay after cursor loss. Existing messages/read state must survive.
        self.conn.execute("UPDATE cloud_state SET last_pulled_seq = 0")
        self.engine.sync_once(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_recipients").fetchone()[0], 2)
        self.assertEqual(self.service.watch_state("bob@local")["cursor"], before)
        self.assertFalse(self.service.watch_state("bob@local")["changed"])
        self.assertEqual(self.service.list_threads("eve@third", unread_only=True)[0]["unread_count"], 1)
        self.http.push.assert_not_called()
        self.assertEqual(self.cursor(), 7)
        self.http.pull.return_value = {"messages": [], "last_seq": 7}
        self.engine.sync_once(self.conn)
        self.http.pull.assert_called_with(7)

    def test_failed_materialization_rolls_back_page_and_cursor(self):
        broken = dict(MESSAGE, message_id="eml_broken", seq=8, recipients_json={"to": ["invalid"]})
        self.http.pull.return_value = {"messages": [MESSAGE, broken], "last_seq": 8}
        with self.assertRaises(Exception):
            self.engine.sync_once(self.conn)
        self.assertEqual(self.cursor(), 0)
        for table in ("emails", "threads", "projects", "inboxes", "thread_inboxes", "email_recipients"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)

    def test_pull_failure_keeps_cursor(self):
        self.http.pull.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.engine.sync_once(self.conn)
        self.assertEqual(self.cursor(), 0)

    def test_schema_upgrade_is_idempotent(self):
        self.send()
        self.conn.execute("ALTER TABLE emails DROP COLUMN cloud_synced_at")
        self.conn.execute("DROP TABLE cloud_state")
        init_db(self.conn)
        init_db(self.conn)
        self.assertIsNone(self.conn.execute("SELECT cloud_synced_at FROM emails").fetchone()[0])
        self.assertEqual(self.cursor(), 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO cloud_state VALUES (2, 0)")


class TestCloudConfigAndHTTP(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config" / "cloud.json"
        patcher = mock.patch("agent_inbox.cloudsync.config_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["cloud"] + args)
        self.assertNotIn("secret-token", out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_login_verifies_before_save_and_off_preserves_token(self):
        with mock.patch.object(CloudClient, "pull") as pull:
            pull.side_effect = CloudSyncError("Cloud request failed (HTTP 401)")
            self.assertEqual(self.run_cli(["login", "--token", "secret-token"])[0], 1)
            self.assertFalse(self.path.exists())
            pull.side_effect = None
            def verified(*args, **kwargs):
                self.assertFalse(self.path.exists())
                return {"messages": [], "last_seq": 0}
            pull.side_effect = verified
            self.assertEqual(self.run_cli(["login", "--token", "secret-token"])[0], 0)
            pull.assert_called_with(0, limit=1)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(load_config()["enabled"])
        self.assertEqual(self.run_cli(["off"])[0], 0)
        self.assertFalse(load_config()["enabled"])
        self.assertEqual(load_config()["token"], "secret-token")

    def test_status_uses_server_counters(self):
        with mock.patch("agent_inbox.client.InboxClient.cloud_status", return_value={
            "enabled": True, "url": "https://example.com", "unsynced_count": 3, "last_pulled_seq": 7,
        }):
            code, out, _ = self.run_cli(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["last_pulled_seq"], 7)

    def test_http_bearer_payload_and_timeout(self):
        client = CloudClient("https://example.com/", "secret-token")
        with mock.patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value = io.StringIO('{"messages": [], "last_seq": 3}')
            client.pull(3)
            args, kwargs = opener.return_value.open.call_args
            req = args[0]
            self.assertEqual(req.full_url, "https://example.com/api/agent-mail")
            self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")
            self.assertEqual(json.loads(req.data), {"action": "pull", "after_seq": 3, "limit": 200})
            self.assertEqual(kwargs["timeout"], 15)

    def test_worker_skips_missing_or_disabled_config(self):
        for config in (None, {"enabled": False}):
            with mock.patch("agent_inbox.cloudsync.load_config", return_value=config), mock.patch("agent_inbox.cloudsync.get_connection") as connect:
                SyncWorker("unused").run()
                connect.assert_not_called()

    def test_worker_retries_and_closes_connection(self):
        worker = SyncWorker("unused")
        worker.wake = mock.Mock()
        attempts = []
        def sync(conn):
            attempts.append(conn)
            if len(attempts) == 1:
                raise OSError("offline")
            worker.stop()
        with mock.patch("agent_inbox.cloudsync.load_config", return_value={"enabled": True, "url": "https://example.com", "token": "secret-token"}), mock.patch("agent_inbox.cloudsync.get_connection") as connect, mock.patch.object(SyncEngine, "sync_once", side_effect=sync):
            worker.run()
            self.assertEqual(len(attempts), 2)
            connect.return_value.close.assert_called_once()


class TestCloudServerIntegration(unittest.TestCase):
    def setUp(self):
        from agent_inbox.client import InboxClient
        from agent_inbox.server import AgentInboxServer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = get_connection(Path(self.tmp.name) / "inbox.db")
        self.addCleanup(self.conn.close)
        patcher = mock.patch("agent_inbox.server.load_config", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = AgentInboxServer(("127.0.0.1", 0), self.conn)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.client = InboxClient(f"http://127.0.0.1:{self.server.server_port}")

    def test_http_send_reply_nudge_and_cloud_status(self):
        with mock.patch.object(self.server, "nudge_cloud_sync") as nudge:
            sent = self.client.send_email("bob@local", ["alice@remote"], subject="Test", body_markdown="hello")
            self.client.reply_email(sent["email_id"], "alice@remote", "reply")
            self.assertEqual(nudge.call_count, 2)
        with mock.patch("agent_inbox.cloudsync.load_config", return_value={
            "enabled": True, "url": "https://example.com", "token": "secret-token",
        }):
            status = self.client.cloud_status()
        self.assertEqual(status, {"enabled": True, "url": "https://example.com", "unsynced_count": 2, "last_pulled_seq": 0})

    def test_pulled_mail_reaches_http_and_hook(self):
        from agent_inbox.hooks import run_hook_check
        http = mock.Mock()
        http.pull.return_value = {"messages": [MESSAGE], "last_seq": 7}
        SyncEngine(http).sync_once(self.conn)
        self.assertEqual(self.client.list_threads("bob@local", unread=True)[0]["thread_id"], "thr_foreign")
        self.assertEqual(self.client.get_thread("bob@local", "thr_foreign")["emails"][0]["email_id"], "eml_foreign")
        self.assertTrue(self.client.watch("bob@local", timeout=0)["changed"])
        output = io.StringIO()
        with mock.patch("agent_inbox.client.InboxClient", return_value=self.client), mock.patch("agent_inbox.identity.derive_identity", return_value=("bob", "local", "bob@local")), mock.patch("agent_inbox.hooks._stamp_path", return_value=Path(self.tmp.name) / "hook.json"), contextlib.redirect_stdout(output):
            self.assertEqual(run_hook_check(), 0)
        self.assertIn("Across machines", output.getvalue())

    def test_lifecycle_skips_disabled_and_starts_enabled_worker(self):
        with mock.patch("agent_inbox.server.SyncWorker") as worker:
            for config in (None, {"enabled": False}):
                with mock.patch("agent_inbox.server.load_config", return_value=config):
                    self.server._cloud_check_at = 0
                    self.server.service_actions()
            worker.assert_not_called()
            with mock.patch("agent_inbox.server.load_config", return_value={"enabled": True}):
                self.server._cloud_check_at = 0
                self.server.service_actions()
                worker.return_value.start.assert_called_once()
                self.server.nudge_cloud_sync()
                worker.return_value.wake.set.assert_called_once()
            with mock.patch("agent_inbox.server.load_config", return_value={"enabled": False}):
                self.server._cloud_check_at = 0
                self.server.service_actions()
                worker.return_value.stop.assert_called_once()
