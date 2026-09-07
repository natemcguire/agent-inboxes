"""SQLite database initialization, pragmas, connection factory, and schema migrations."""

import os
import sqlite3
from pathlib import Path
from typing import Union

from agent_inbox.config import DIR_MODE, FILE_MODE, get_data_dir, get_db_path

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS projects (
  id         INTEGER PRIMARY KEY,
  slug       TEXT NOT NULL COLLATE NOCASE UNIQUE,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS inboxes (
  id           INTEGER PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES projects(id),
  local_part   TEXT NOT NULL COLLATE NOCASE,
  display_name TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT,
  UNIQUE (project_id, local_part)
);

CREATE TABLE IF NOT EXISTS threads (
  id              TEXT PRIMARY KEY,
  home_project_id INTEGER NOT NULL REFERENCES projects(id),
  subject         TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  last_email_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_inboxes (
  thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  inbox_id  INTEGER NOT NULL REFERENCES inboxes(id) ON DELETE CASCADE,
  joined_at TEXT NOT NULL,
  PRIMARY KEY (thread_id, inbox_id)
);

CREATE TABLE IF NOT EXISTS emails (
  id                TEXT PRIMARY KEY,
  thread_id         TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  from_inbox_id     INTEGER NOT NULL REFERENCES inboxes(id),
  subject           TEXT NOT NULL,
  body_markdown     TEXT NOT NULL,
  reply_to_email_id TEXT REFERENCES emails(id),
  client_token      TEXT NOT NULL UNIQUE,
  sent_at           TEXT NOT NULL,
  sender_session    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  inbox_id      INTEGER NOT NULL REFERENCES inboxes(id) ON DELETE CASCADE,
  session_id    TEXT NOT NULL,
  pid           INTEGER,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  PRIMARY KEY (inbox_id, session_id)
);

CREATE TABLE IF NOT EXISTS email_recipients (
  email_id TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  inbox_id INTEGER NOT NULL REFERENCES inboxes(id),
  kind     TEXT NOT NULL CHECK (kind IN ('to', 'cc')),
  position INTEGER NOT NULL,
  read_at  TEXT,
  PRIMARY KEY (email_id, inbox_id)
);

CREATE TABLE IF NOT EXISTS email_references (
  email_id            TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  referenced_email_id TEXT NOT NULL REFERENCES emails(id),
  position            INTEGER NOT NULL,
  PRIMARY KEY (email_id, position),
  UNIQUE (email_id, referenced_email_id)
);

CREATE TABLE IF NOT EXISTS agent_leases (
  agent_slug   TEXT NOT NULL COLLATE NOCASE,
  project_slug TEXT NOT NULL COLLATE NOCASE,
  claimed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (agent_slug, project_slug)
);

CREATE TABLE IF NOT EXISTS reservations (
  id              INTEGER PRIMARY KEY,
  project_id      INTEGER NOT NULL REFERENCES projects(id),
  path            TEXT    NOT NULL,             -- normalized, repo-relative (case-preserving)
  holder_inbox_id INTEGER NOT NULL REFERENCES inboxes(id),
  holder_session  TEXT,                         -- session slug (nullable for old clients)
  reason          TEXT    NOT NULL DEFAULT '',
  ttl_seconds     INTEGER NOT NULL,
  created_at      TEXT    NOT NULL,
  expires_at      TEXT    NOT NULL,
  released_at     TEXT,                         -- NULL while active
  released_by     TEXT,                         -- 'holder' | 'expired' | 'forced:<address>'
  client_token    TEXT,                         -- acquire idempotency (shared per call)
  repo_key        TEXT                          -- worktree-safe repo identity (NULL = conservative)
);
CREATE INDEX IF NOT EXISTS idx_reservations_active
  ON reservations(project_id, path) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_reservations_token ON reservations(client_token);

CREATE INDEX IF NOT EXISTS idx_emails_thread_sent ON emails(thread_id, sent_at, id);
CREATE INDEX IF NOT EXISTS idx_recipients_unread ON email_recipients(inbox_id, read_at, email_id);
CREATE INDEX IF NOT EXISTS idx_threads_activity ON threads(last_email_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen_at);
"""


def ensure_permissions(path: Path, chmod_parent: bool = True) -> None:
    """Ensure the db file is 0600, and (only when we own it) the parent is 0700.

    ``chmod_parent`` is False for a caller-supplied ``--db``/``AGENT_INBOX_DB``
    file living in a pre-existing shared directory (e.g. ``/tmp``): rewriting
    that directory's permissions to 0700 would lock out other users of an
    unrelated directory. We only tighten a parent the service itself created or
    the default service-owned data dir. The db file's own 0600 is always safe.
    """
    try:
        if chmod_parent:
            parent = path.parent
            if parent.exists():
                os.chmod(parent, DIR_MODE)
        if path.exists():
            os.chmod(path, FILE_MODE)
    except OSError:
        pass


def get_connection(db_path: Union[str, Path, None] = None) -> sqlite3.Connection:
    """
    Open a connection to SQLite database with required pragmas and permissions.
    """
    if db_path is None:
        target_path = get_db_path()
    elif isinstance(db_path, str):
        target_path = Path(db_path).expanduser().resolve()
    else:
        target_path = db_path.expanduser().resolve()

    # Decide whether we're allowed to tighten the parent directory. We only own
    # (and may chmod) the parent if we just created it, or it is the service's
    # own data directory (~/.agent-inboxes, or AGENT_INBOX_DIR — a dir the
    # service manages). We deliberately compare against get_data_dir(), NOT
    # get_db_path().parent: AGENT_INBOX_DB can point at an arbitrary file inside
    # a pre-existing SHARED directory (e.g. /tmp), and that directory must never
    # have its permissions rewritten out from under other users.
    service_data_dir = get_data_dir()
    parent_existed = target_path.parent.exists()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    own_parent = (not parent_existed) or (target_path.parent == service_data_dir)

    ensure_permissions(target_path, chmod_parent=own_parent)

    conn = sqlite3.connect(
        str(target_path),
        timeout=5.0,
        check_same_thread=False,
        isolation_level=None,  # Autocommit mode by default; manual transactions via BEGIN/COMMIT
    )
    conn.row_factory = sqlite3.Row

    # Enforce pragmas
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")

    # Ensure permissions after creation
    ensure_permissions(target_path, chmod_parent=own_parent)

    # Initialize schema
    init_db(conn)

    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize database tables and indexes, then apply in-place upgrades.

    ``SCHEMA_SQL`` uses ``CREATE ... IF NOT EXISTS`` throughout, so a database
    created by an older version keeps its existing tables. Columns added after
    v1 are applied via guarded ``ALTER TABLE`` so existing databases upgrade in
    place; fresh databases already contain them from the schema above.
    """
    conn.executescript(SCHEMA_SQL)

    # v1.1: emails.sender_session (nullable) — stamps which agent session sent
    # an email when several same-family agents share one inbox address.
    email_columns = {row[1] for row in conn.execute("PRAGMA table_info(emails)")}
    if "sender_session" not in email_columns:
        conn.execute("ALTER TABLE emails ADD COLUMN sender_session TEXT")

    # v1.3: reservations.repo_key (nullable) — worktree-safe repository identity.
    # NULL is conservative: keyless leases conflict with everything in-project.
    reservation_columns = {row[1] for row in conn.execute("PRAGMA table_info(reservations)")}
    if "repo_key" not in reservation_columns:
        conn.execute("ALTER TABLE reservations ADD COLUMN repo_key TEXT")
