"""Tests for session tracking, long-poll watch, and in-place schema upgrade."""

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.support import isolated_inbox

from agent_inbox.client import InboxClient
from agent_inbox.db import get_connection
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService


# Frozen v1.0 schema (before sender_session / sessions): used to prove that an
# existing database upgrades in place when opened by the current code.
V1_SCHEMA_SQL = """
CREATE TABLE projects (
  id         INTEGER PRIMARY KEY,
  slug       TEXT NOT NULL COLLATE NOCASE UNIQUE,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE inboxes (
  id           INTEGER PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES projects(id),
  local_part   TEXT NOT NULL COLLATE NOCASE,
  display_name TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT,
  UNIQUE (project_id, local_part)
);
CREATE TABLE threads (
  id              TEXT PRIMARY KEY,
  home_project_id INTEGER NOT NULL REFERENCES projects(id),
  subject         TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  last_email_at   TEXT NOT NULL
);
CREATE TABLE emails (
  id                TEXT PRIMARY KEY,
  thread_id         TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  from_inbox_id     INTEGER NOT NULL REFERENCES inboxes(id),
  subject           TEXT NOT NULL,
  body_markdown     TEXT NOT NULL,
  reply_to_email_id TEXT REFERENCES emails(id),
  client_token      TEXT NOT NULL UNIQUE,
  sent_at           TEXT NOT NULL
);
CREATE TABLE thread_inboxes (
  thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  inbox_id INTEGER NOT NULL REFERENCES inboxes(id) ON DELETE CASCADE,
  joined_at TEXT NOT NULL,
  PRIMARY KEY (thread_id, inbox_id)
);
CREATE TABLE email_recipients (
  email_id TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  inbox_id INTEGER NOT NULL REFERENCES inboxes(id),
  kind TEXT NOT NULL CHECK (kind IN ('to', 'cc')),
  position INTEGER NOT NULL,
  read_at TEXT,
  PRIMARY KEY (email_id, inbox_id)
);
CREATE TABLE email_references (
  email_id TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  referenced_email_id TEXT NOT NULL REFERENCES emails(id),
  position INTEGER NOT NULL,
  PRIMARY KEY (email_id, position),
  UNIQUE (email_id, referenced_email_id)
);
"""


class TestSchemaUpgrade(unittest.TestCase):

    def test_existing_v1_database_upgrades_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "v1_inbox.db"
            raw = sqlite3.connect(str(db_path))
            raw.executescript(V1_SCHEMA_SQL)
            # An empty schema cannot reveal an upgrade that discards old data.
            raw.executescript("""
                INSERT INTO projects (id,slug) VALUES (1,'legacy');
                INSERT INTO inboxes (id,project_id,local_part,display_name) VALUES
                    (1,1,'alice','Alice'), (2,1,'bob','Bob'), (3,1,'observer','Observer');
                INSERT INTO threads VALUES
                    ('thr_legacy',1,'Existing work','2020-01-01T00:00:00.000Z','2020-01-02T00:00:00.000Z');
                INSERT INTO emails VALUES
                    ('eml_original','thr_legacy',1,'Existing work','Original body',NULL,'legacy-1','2020-01-01T00:00:00.000Z'),
                    ('eml_reply','thr_legacy',2,'Re: Existing work','Reply body','eml_original','legacy-2','2020-01-02T00:00:00.000Z');
                INSERT INTO thread_inboxes VALUES
                    ('thr_legacy',1,'2020-01-01T00:00:00.000Z'),
                    ('thr_legacy',2,'2020-01-01T00:00:00.000Z'),
                    ('thr_legacy',3,'2020-01-01T00:00:00.000Z');
                INSERT INTO email_recipients VALUES
                    ('eml_original',2,'to',0,'2020-01-01T01:00:00.000Z'),
                    ('eml_original',3,'cc',1,NULL),
                    ('eml_reply',1,'to',0,NULL);
                INSERT INTO email_references VALUES ('eml_reply','eml_original',0);
            """)
            raw.close()

            conn = get_connection(db_path)
            try:
                email_cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
                self.assertIn("sender_session", email_cols)
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                self.assertIn("sessions", tables)
                # NB-7: the reservations table is also created on upgrade.
                self.assertIn("reservations", tables)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM emails').fetchone()[0], 2)
                svc = InboxService(conn)
                detail = svc.get_thread('alice@legacy', 'thr_legacy')
                self.assertEqual(detail['subject'], 'Existing work')
                self.assertEqual([e['email_id'] for e in detail['emails']], ['eml_original', 'eml_reply'])
                self.assertEqual([e['body_markdown'] for e in detail['emails']], ['Original body', 'Reply body'])
                self.assertEqual(detail['emails'][1]['from'], 'bob@legacy')
                self.assertEqual(detail['emails'][1]['reply_to_email_id'], 'eml_original')
                self.assertEqual(detail['emails'][1]['references'], ['eml_original'])
                self.assertFalse(detail['emails'][1]['read'])
                self.assertIsNone(detail['emails'][1]['sender_session'])
                status = svc.email_status('eml_original')
                self.assertEqual(status['recipients'], [
                    {'address': 'bob@legacy', 'kind': 'to', 'read_at': '2020-01-01T01:00:00.000Z'},
                    {'address': 'observer@legacy', 'kind': 'cc', 'read_at': None},
                ])
                cursor = svc.watch_state('observer@legacy')['cursor']
                self.assertGreater(cursor, 0)
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            finally:
                conn.close()
            # Reopening runs initialization again; neither data nor cursors may change.
            reopened = get_connection(db_path)
            try:
                svc = InboxService(reopened)
                self.assertEqual(svc.get_thread('alice@legacy', 'thr_legacy'), detail)
                self.assertEqual(svc.email_status('eml_original'), status)
                self.assertEqual(svc.watch_state('observer@legacy')['cursor'], cursor)
            finally:
                reopened.close()


class TestWatchAndSessions(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.enterContext(isolated_inbox(self.tmp_dir.name))
        self.db_path = Path(self.tmp_dir.name) / "watch_test.db"
        self.db_conn = get_connection(self.db_path)
        self.server = AgentInboxServer(("127.0.0.1", 0), self.db_conn, verbose=False)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db_conn.close()
        self.tmp_dir.cleanup()

    def test_watch_times_out_with_no_mail(self):
        client = InboxClient(self.base_url)
        started = time.monotonic()
        res = client.watch("quiet@watch-proj", timeout=1)
        elapsed = time.monotonic() - started
        self.assertFalse(res["changed"])
        self.assertEqual(res["unread_count"], 0)
        self.assertEqual(res["cursor"], 0)
        self.assertIsNone(res["latest"])
        self.assertGreaterEqual(elapsed, 0.9)

    def test_watch_unblocks_when_mail_arrives(self):
        sender = InboxClient(self.base_url, session_id="s-aaaa1111")
        watcher = InboxClient(self.base_url, session_id="s-bbbb2222")

        def send_later():
            time.sleep(0.6)
            sender.send_email(
                from_addr="sender@watch-proj",
                to_addrs=["receiver@watch-proj"],
                subject="Wake up",
                body_markdown="New mail while you were blocked.",
                create_missing=True,
            )

        t = threading.Thread(target=send_later, daemon=True)
        started = time.monotonic()
        t.start()
        res = watcher.watch("receiver@watch-proj", timeout=10)
        elapsed = time.monotonic() - started
        t.join()

        self.assertTrue(res["changed"])
        self.assertEqual(res["unread_count"], 1)
        self.assertGreater(res["cursor"], 0)
        self.assertEqual(res["latest"]["subject"], "Wake up")
        self.assertEqual(res["latest"]["from"], "sender@watch-proj")
        # Unblocked promptly (well before the 10s timeout), not at the deadline.
        self.assertLess(elapsed, 5.0)

        # A second watch past the returned cursor sees no NEW mail even though
        # the first email is still unread.
        res2 = watcher.watch("receiver@watch-proj", timeout=1, after=res["cursor"])
        self.assertFalse(res2["changed"])
        self.assertEqual(res2["unread_count"], 1)

    def test_sender_session_stamped_and_active_sessions_counted(self):
        s1 = InboxClient(self.base_url, session_id="s-11111111")
        s2 = InboxClient(self.base_url, session_id="s-22222222")

        # Both sessions act as the SAME address.
        s1.put_inbox("claude@watch-proj")
        res2 = s2.put_inbox("claude@watch-proj")
        self.assertEqual(res2["session_id"], "s-22222222")
        self.assertEqual(res2["active_sessions"], 2)

        send_res = s1.send_email(
            from_addr="claude@watch-proj",
            to_addrs=["peer@watch-proj"],
            subject="Session stamp",
            body_markdown="Which claude sent this?",
            create_missing=True,
        )
        thread = s2.get_thread("peer@watch-proj", send_res["thread_id"])
        self.assertEqual(thread["emails"][0]["sender_session"], "s-11111111")

        inboxes = s1.list_inboxes(project="watch-proj")
        by_addr = {ib["address"]: ib for ib in inboxes}
        self.assertEqual(by_addr["claude@watch-proj"]["active_sessions"], 2)


if __name__ == "__main__":
    unittest.main()
