"""Optional append-only cloud mail sync; read state and sessions remain local."""

import json
import logging
import os
import socket
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from agent_inbox.db import get_connection
from agent_inbox.models import InboxError, parse_address, utc_now_iso
from agent_inbox.service import InboxService

DEFAULT_URL = "https://nates-software.com"
SYNC_INTERVAL = 30.0
logger = logging.getLogger(__name__)


def config_path() -> Path:
    return Path.home() / ".config" / "agent-inbox" / "cloud.json"


def load_config(path: Optional[Path] = None) -> Optional[dict]:
    try:
        with (path or config_path()).open() as f:
            config = json.load(f)
    except FileNotFoundError:
        return None
    if not isinstance(config, dict):
        raise ValueError("Invalid cloud config")
    return config


def save_config(config: dict, path: Optional[Path] = None) -> None:
    """Atomically replace config; the token is never written with public permissions."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cloud-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(config, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class CloudSyncError(InboxError):
    def __init__(self, message: str):
        super().__init__("cloud_sync_error", message, status_code=502)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Bearer credential to a redirect target.
        return None


class CloudClient:
    def __init__(self, url: str, token: str):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise CloudSyncError("Invalid cloud URL")
        if not isinstance(token, str) or not token.strip() or "\n" in token or "\r" in token:
            raise CloudSyncError("Invalid cloud token")
        self.url = url.rstrip("/")
        self.token = token

    def _request(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url + "/api/agent-mail", method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=15) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except urllib.error.HTTPError as exc:
            raise CloudSyncError(f"Cloud request failed (HTTP {exc.code})") from None
        except Exception:
            # Neither server-controlled response bodies nor credentials enter logs/CLI output.
            raise CloudSyncError("Cloud request failed; check connectivity and credentials") from None

    def push(self, messages: list) -> dict:
        if len(messages) > 100:
            raise CloudSyncError("Cloud push batches may contain at most 100 messages")
        return self._request({"action": "push", "messages": messages})

    def pull(self, after_seq: int, limit: int = 200) -> dict:
        result = self._request({"action": "pull", "after_seq": after_seq, "limit": limit})
        validate_pull(result, after_seq)
        return result


def validate_pull(result: dict, after_seq: int) -> None:
    if (not isinstance(result.get("messages"), list)
            or type(result.get("last_seq")) is not int or result["last_seq"] < after_seq):
        raise CloudSyncError("Invalid cloud pull response")
    previous = after_seq
    for message in result["messages"]:
        seq = message.get("seq")
        if type(seq) is not int or seq <= previous or seq > result["last_seq"]:
            raise CloudSyncError("Invalid cloud message sequence")
        previous = seq


def sync_status(conn: sqlite3.Connection) -> dict:
    config = load_config() or {}
    return {
        "enabled": config.get("enabled", False) is True,
        "url": config.get("url"),
        "unsynced_count": conn.execute(
            "SELECT COUNT(*) FROM emails WHERE cloud_synced_at IS NULL"
        ).fetchone()[0],
        "last_pulled_seq": conn.execute(
            "SELECT last_pulled_seq FROM cloud_state WHERE id = 1"
        ).fetchone()[0],
    }


class SyncEngine:
    def __init__(self, client: CloudClient):
        self.client = client

    def sync_once(self, conn: sqlite3.Connection) -> None:
        """Push one bounded batch, then atomically import one pull page and its cursor."""
        rows = conn.execute("""
            SELECT e.*, p.slug AS project, i.local_part || '@' || sp.slug AS sender
            FROM emails e JOIN threads t ON t.id = e.thread_id
            JOIN projects p ON p.id = t.home_project_id
            JOIN inboxes i ON i.id = e.from_inbox_id
            JOIN projects sp ON sp.id = i.project_id
            WHERE e.cloud_synced_at IS NULL ORDER BY e.rowid LIMIT 100
        """).fetchall()
        messages = []
        for row in rows:
            recipients = {"to": [], "cc": []}
            for rec in conn.execute("""
                SELECT r.kind, i.local_part || '@' || p.slug AS address
                FROM email_recipients r JOIN inboxes i ON i.id = r.inbox_id
                JOIN projects p ON p.id = i.project_id
                WHERE r.email_id = ? ORDER BY r.position
            """, (row["id"],)):
                recipients[rec["kind"]].append(rec["address"])
            messages.append({
                "message_id": row["id"], "thread_id": row["thread_id"],
                "project": row["project"], "subject": row["subject"],
                "sender": row["sender"], "sender_session": row["sender_session"],
                "recipients_json": recipients, "body": row["body_markdown"],
                "sent_at": row["sent_at"], "origin_device": socket.gethostname(),
            })
        if messages:
            self.client.push(messages)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany("UPDATE emails SET cloud_synced_at = ? WHERE id = ?",
                                 [(utc_now_iso(), row["id"]) for row in rows])
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        cursor = conn.execute("SELECT last_pulled_seq FROM cloud_state WHERE id = 1").fetchone()[0]
        result = self.client.pull(cursor)
        validate_pull(result, cursor)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for message in result["messages"]:
                self._materialize(conn, message)
            conn.execute("UPDATE cloud_state SET last_pulled_seq = MAX(last_pulled_seq, ?) WHERE id = 1",
                         (result["last_seq"],))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _materialize(conn: sqlite3.Connection, message: dict) -> None:
        # The wire format has stable email/thread IDs; project/inbox integer IDs
        # are machine-local and resolved by canonical slug/address.
        if conn.execute("SELECT 1 FROM emails WHERE id = ?", (message["message_id"],)).fetchone():
            return
        service = InboxService(conn)
        sent_at = message["sent_at"]
        project_id = service.ensure_project(message["project"])

        def inbox_id(address):
            local, project = parse_address(address)
            pid = service.ensure_project(project)
            conn.execute("INSERT OR IGNORE INTO inboxes (project_id, local_part, created_at) VALUES (?, ?, ?)",
                         (pid, local, sent_at))
            return service._get_inbox_id(address)

        sender_id = inbox_id(message["sender"])
        recipients = message["recipients_json"]
        if isinstance(recipients, str):
            recipients = json.loads(recipients)
        thread_id = message["thread_id"]
        conn.execute("""INSERT OR IGNORE INTO threads
            (id, home_project_id, subject, created_at, last_email_at) VALUES (?, ?, ?, ?, ?)""",
                     (thread_id, project_id, message["subject"], sent_at, sent_at))
        inserted = conn.execute("""INSERT OR IGNORE INTO emails
            (id, thread_id, from_inbox_id, subject, body_markdown, client_token,
             sent_at, sender_session, cloud_synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (message["message_id"], thread_id, sender_id, message["subject"],
                      message["body"], "cloud:" + message["message_id"], sent_at,
                      message.get("sender_session"), utc_now_iso()))
        if inserted.rowcount != 1:
            raise CloudSyncError("Cloud message could not be materialized")
        participants = {sender_id}
        position = 0
        for kind in ("to", "cc"):
            for address in recipients[kind]:
                recipient_id = inbox_id(address)
                participants.add(recipient_id)
                conn.execute("""INSERT OR IGNORE INTO email_recipients
                    (email_id, inbox_id, kind, position) VALUES (?, ?, ?, ?)""",
                             (message["message_id"], recipient_id, kind, position))
                position += 1
        for participant in participants:
            conn.execute("INSERT OR IGNORE INTO thread_inboxes (thread_id, inbox_id, joined_at) VALUES (?, ?, ?)",
                         (thread_id, participant, sent_at))
        conn.execute("""UPDATE threads SET created_at = MIN(created_at, ?),
            last_email_at = MAX(last_email_at, ?) WHERE id = ?""", (sent_at, sent_at, thread_id))


class SyncWorker(threading.Thread):
    """One private DB connection; network errors never escape into serve."""
    def __init__(self, db_path):
        super().__init__(name="agent-inbox-cloud-sync", daemon=True)
        self.db_path = db_path
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def run(self):
        conn = None
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                try:
                    config = load_config()
                    if not config or config.get("enabled") is not True:
                        return
                    if conn is None:
                        conn = get_connection(self.db_path)
                    SyncEngine(CloudClient(config["url"], config["token"])).sync_once(conn)
                except Exception as exc:
                    logger.debug("Cloud sync failed (%s); retrying next tick", type(exc).__name__)
                self.wake.wait(SYNC_INTERVAL)
        finally:
            if conn is not None:
                conn.close()
