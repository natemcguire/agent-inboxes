"""Core domain and transactional database operations for Agent Inboxes."""

import datetime
import re
import sqlite3
import uuid
from typing import List, Optional, Tuple

from agent_inbox.models import (
    ConflictError,
    NotFoundError,
    ValidationError,
    generate_email_id,
    generate_thread_id,
    normalize_address,
    normalize_slug,
    parse_address,
    utc_now_iso,
)

# A session counts as "active" if it interacted within this window.
SESSION_ACTIVE_WINDOW_SECONDS = 30 * 60

# File reservation (NB-7) TTL bounds: default 15 minutes, range 1m–2h.
# The 2h cap bounds a single lease's horizon, not total hold time — renewal
# extends by the lease's own TTL, so the horizon can never exceed the cap.
RESERVATION_DEFAULT_TTL_SECONDS = 15 * 60
RESERVATION_MIN_TTL_SECONDS = 60
RESERVATION_MAX_TTL_SECONDS = 2 * 60 * 60

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _iso_add_seconds(iso_ts: str, seconds: int) -> str:
    """Return the RFC 3339 timestamp `seconds` after `iso_ts` (same format)."""
    dt = datetime.datetime.strptime(iso_ts, _ISO_FMT).replace(tzinfo=datetime.timezone.utc)
    out = dt + datetime.timedelta(seconds=seconds)
    return out.strftime("%Y-%m-%dT%H:%M:%S.") + f"{out.microsecond // 1000:03d}Z"


def _iso_diff_seconds(from_ts: str, to_ts: str) -> int:
    """Whole seconds from `from_ts` to `to_ts` (may be negative)."""
    a = datetime.datetime.strptime(from_ts, _ISO_FMT)
    b = datetime.datetime.strptime(to_ts, _ISO_FMT)
    return int((b - a).total_seconds())


# Named (non-file) resource leases live in the same reservations table under
# an internal key prefix that a normalized file path can never produce:
# normalization collapses duplicate slashes, so 'res://<name>' is unreachable
# from any path input. Resources are project-wide, exact-match only.
RESOURCE_PREFIX = "res://"
_RESOURCE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:_-]*$")


def normalize_resource_name(raw) -> str:
    """Validate and canonicalize a resource lease name (e.g. 'release:pages')."""
    if not raw or not isinstance(raw, str) or not raw.strip():
        raise ValidationError("validation_error", "Resource name must be a non-empty string")
    name = raw.strip().lower()
    if not _RESOURCE_NAME_PATTERN.match(name):
        raise ValidationError(
            "validation_error",
            f"Invalid resource name '{raw}': must match [a-z0-9][a-z0-9:_-]*",
        )
    return name


def resource_key(name: str) -> str:
    """Internal storage key for a resource lease."""
    return RESOURCE_PREFIX + normalize_resource_name(name)


def is_resource_key(key: str) -> bool:
    return isinstance(key, str) and key.startswith(RESOURCE_PREFIX)


def reservation_kind(key: str) -> str:
    return "resource" if is_resource_key(key) else "file"


def reservation_display(key: str) -> str:
    """Outward-facing form: resource keys are shown as their bare name."""
    return key[len(RESOURCE_PREFIX):] if is_resource_key(key) else key


def normalize_reservation_key(raw) -> str:
    """Normalize either a file path or an internal 'res://<name>' resource key."""
    if isinstance(raw, str) and raw.startswith(RESOURCE_PREFIX):
        return resource_key(raw[len(RESOURCE_PREFIX):])
    return normalize_reservation_path(raw)


def normalize_reservation_path(raw) -> str:
    """Normalize a repo-relative POSIX reservation path.

    Rejects absolute paths, empty paths, and any `..` segment. Strips `./`
    segments and duplicate slashes. A trailing `/` is preserved: it marks a
    directory reservation, semantically distinct from a file path. Storage is
    case-preserving; comparisons are case-insensitive.
    """
    if not raw or not isinstance(raw, str) or not raw.strip():
        raise ValidationError("validation_error", "Reservation path must be a non-empty string")
    p = raw.strip().replace("\\", "/")
    if p.startswith("/"):
        raise ValidationError("validation_error", f"Reservation path must be repo-relative, not absolute: '{raw}'")
    is_dir = p.endswith("/")
    segments = [s for s in p.split("/") if s not in ("", ".")]
    if not segments:
        raise ValidationError("validation_error", f"Reservation path is empty after normalization: '{raw}'")
    if any(s == ".." for s in segments):
        raise ValidationError("validation_error", f"Reservation path may not contain '..': '{raw}'")
    return "/".join(segments) + ("/" if is_dir else "")


def reservation_paths_conflict(a: str, b: str) -> bool:
    """True when two normalized reservation keys overlap (case-insensitive).

    File paths overlap on the same path, an ancestor directory reservation, or
    a descendant of a requested directory. Resource keys ('res://…') never
    prefix-conflict: they match exactly or not at all, and never against files.
    """
    la, lb = a.lower(), b.lower()
    if is_resource_key(la) or is_resource_key(lb):
        return la == lb
    if la == lb:
        return True
    if lb.endswith("/") and la.startswith(lb):
        return True
    if la.endswith("/") and lb.startswith(la):
        return True
    return False


def _session_active_cutoff_iso() -> str:
    """RFC 3339 UTC cutoff; timestamps at/after it are 'active'. Same format as
    utc_now_iso(), so lexicographic string comparison in SQL is correct."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=SESSION_ACTIVE_WINDOW_SECONDS
    )
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cutoff.microsecond // 1000:03d}Z"


class InboxService:
    """Encapsulates transactional operations on the SQLite database."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn


    # ------------------------------------------------------------------
    # Agent leases: lowest-free-slot claiming for concurrent same-family
    # agents (claude, claude-2, claude-3, ...). A lease is free when it has
    # never been claimed or its last_seen is older than LEASE_EXPIRY_SECONDS.
    # ------------------------------------------------------------------

    LEASE_EXPIRY_SECONDS = 2 * 60 * 60

    def _lease_expiry_cutoff(self) -> str:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.LEASE_EXPIRY_SECONDS)
        return cutoff.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cutoff.microsecond // 1000:03d}Z"

    def claim_agent(self, family: str, project_slug: str) -> dict:
        """Atomically claim the lowest free slot for a runtime family in a project."""
        family = normalize_slug(family)
        project_slug = normalize_slug(project_slug)
        if not family or not project_slug:
            raise ValidationError("invalid_lease", "family and project are required")
        cutoff = self._lease_expiry_cutoff()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            slot = None
            for n in range(1, 100):
                candidate = family if n == 1 else f"{family}-{n}"
                row = self.conn.execute(
                    "SELECT last_seen FROM agent_leases WHERE agent_slug = ? COLLATE NOCASE AND project_slug = ? COLLATE NOCASE",
                    (candidate, project_slug),
                ).fetchone()
                if row is None or row["last_seen"] < cutoff:
                    slot = candidate
                    break
            if slot is None:
                raise ConflictError("lease_exhausted", f"No free slot for {family} in {project_slug} (99 concurrent leases)")
            self.conn.execute(
                """
                INSERT INTO agent_leases (agent_slug, project_slug)
                VALUES (?, ?)
                ON CONFLICT(agent_slug, project_slug) DO UPDATE SET
                  claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                  last_seen  = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (slot, project_slug),
            )
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return {"agent": slot, "project": project_slug, "address": f"{slot}@{project_slug}"}

    def release_agent(self, agent_slug: str, project_slug: str) -> dict:
        """Free a lease so the slot can be reclaimed immediately."""
        agent_slug = normalize_slug(agent_slug)
        project_slug = normalize_slug(project_slug)
        cur = self.conn.execute(
            "DELETE FROM agent_leases WHERE agent_slug = ? COLLATE NOCASE AND project_slug = ? COLLATE NOCASE",
            (agent_slug, project_slug),
        )
        return {"released": cur.rowcount > 0, "agent": agent_slug, "project": project_slug}

    def touch_lease(self, agent_slug: str, project_slug: str) -> None:
        """Refresh last_seen for an EXISTING lease; never creates one."""
        try:
            self.conn.execute(
                """
                UPDATE agent_leases
                SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE agent_slug = ? COLLATE NOCASE AND project_slug = ? COLLATE NOCASE
                """,
                (normalize_slug(agent_slug), normalize_slug(project_slug)),
            )
        except Exception:
            pass

    def _get_inbox_id(self, address: str) -> Optional[int]:
        """Look up inbox ID by address."""
        local_part, project_slug = parse_address(address)
        row = self.conn.execute(
            """
            SELECT i.id FROM inboxes i
            JOIN projects p ON i.project_id = p.id
            WHERE i.local_part = ? COLLATE NOCASE AND p.slug = ? COLLATE NOCASE
            """,
            (local_part, project_slug),
        ).fetchone()
        return row[0] if row else None

    def _is_thread_member(self, thread_id: str, inbox_id: int) -> bool:
        """True if the inbox is a member of the thread (present in thread_inboxes)."""
        row = self.conn.execute(
            "SELECT 1 FROM thread_inboxes WHERE thread_id = ? AND inbox_id = ?",
            (thread_id, inbox_id),
        ).fetchone()
        return row is not None

    def ensure_project(self, slug: str) -> int:
        """Idempotently ensure a project exists and return its ID."""
        canonical_slug = normalize_slug(slug)
        now = utc_now_iso()
        self.conn.execute(
            "INSERT OR IGNORE INTO projects (slug, created_at) VALUES (?, ?)",
            (canonical_slug, now),
        )
        row = self.conn.execute(
            "SELECT id FROM projects WHERE slug = ? COLLATE NOCASE",
            (canonical_slug,),
        ).fetchone()
        if not row:
            raise NotFoundError("project_error", f"Could not create or find project '{slug}'")
        return row[0]

    def ensure_inbox(self, address: str, display_name: Optional[str] = None) -> dict:
        """Idempotently ensure an inbox exists. Updates last_seen_at.

        When called outside an existing transaction (e.g. the standalone
        ``PUT /v1/inboxes/{address}`` path) this owns a single ``BEGIN IMMEDIATE``
        span so the project row and the inbox row commit atomically. When called
        from inside send/reply — which already hold a transaction — it detects
        the open span via ``conn.in_transaction`` and stays inert, letting the
        caller's transaction wrap it. Autocommit mode (isolation_level=None) makes
        ``in_transaction`` a reliable signal here.
        """
        owns_txn = not self.conn.in_transaction
        if owns_txn:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            local_part, project_slug = parse_address(address)
            project_id = self.ensure_project(project_slug)
            now = utc_now_iso()

            row = self.conn.execute(
                """
                SELECT id, display_name, created_at FROM inboxes
                WHERE project_id = ? AND local_part = ? COLLATE NOCASE
                """,
                (project_id, local_part),
            ).fetchone()

            if row:
                inbox_id = row["id"]
                created_at = row["created_at"]
                new_display = display_name if display_name is not None else row["display_name"]
                self.conn.execute(
                    "UPDATE inboxes SET last_seen_at = ?, display_name = ? WHERE id = ?",
                    (now, new_display, inbox_id),
                )
                created = False
            else:
                self.conn.execute(
                    """
                    INSERT INTO inboxes (project_id, local_part, display_name, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, local_part, display_name, now, now),
                )
                created_at = now
                created = True

            if owns_txn:
                self.conn.execute("COMMIT")
        except BaseException:
            if owns_txn:
                self.conn.execute("ROLLBACK")
            raise

        return {
            "address": f"{local_part}@{project_slug}",
            "created": created,
            "created_at": created_at,
            "last_seen_at": now,
        }

    def touch_session(self, address: str, session_id: str, pid: Optional[int] = None) -> None:
        """Record activity for one agent session on an inbox (upsert last_seen_at).

        Called alongside the existing inbox last_seen touch whenever an
        identified session interacts. A no-op for missing/blank session ids.
        """
        if not session_id or not isinstance(session_id, str) or not session_id.strip():
            return
        session_id = session_id.strip()[:64]
        norm_addr = normalize_address(address)
        self.ensure_inbox(norm_addr)
        inbox_id = self._get_inbox_id(norm_addr)
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO sessions (inbox_id, session_id, pid, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(inbox_id, session_id) DO UPDATE SET
              last_seen_at = excluded.last_seen_at,
              pid = COALESCE(excluded.pid, sessions.pid)
            """,
            (inbox_id, session_id, pid, now, now),
        )

    def active_sessions(self, address: str) -> List[dict]:
        """List sessions seen on this inbox within the active window."""
        norm_addr = normalize_address(address)
        inbox_id = self._get_inbox_id(norm_addr)
        if inbox_id is None:
            return []
        cutoff = _session_active_cutoff_iso()
        rows = self.conn.execute(
            """
            SELECT session_id, pid, last_seen_at FROM sessions
            WHERE inbox_id = ? AND last_seen_at >= ?
            ORDER BY last_seen_at DESC
            """,
            (inbox_id, cutoff),
        ).fetchall()
        return [
            {"session_id": r["session_id"], "pid": r["pid"], "last_seen_at": r["last_seen_at"]}
            for r in rows
        ]

    def list_inboxes(self, project_slug: Optional[str] = None) -> List[dict]:
        """List inboxes, optionally filtered by project slug."""
        cutoff = _session_active_cutoff_iso()
        base_query = """
            SELECT i.id AS inbox_id, i.local_part, p.slug as project_slug, i.display_name,
                   i.created_at, i.last_seen_at,
                   (SELECT COUNT(*) FROM sessions s
                    WHERE s.inbox_id = i.id AND s.last_seen_at >= ?) AS active_sessions
            FROM inboxes i
            JOIN projects p ON i.project_id = p.id
        """
        if project_slug:
            canonical_project = normalize_slug(project_slug)
            rows = self.conn.execute(
                base_query + " WHERE p.slug = ? COLLATE NOCASE ORDER BY i.local_part ASC",
                (cutoff, canonical_project),
            ).fetchall()
        else:
            rows = self.conn.execute(
                base_query + " ORDER BY p.slug ASC, i.local_part ASC",
                (cutoff,),
            ).fetchall()

        return [
            {
                "address": f"{r['local_part']}@{r['project_slug']}",
                "project": r["project_slug"],
                "local_part": r["local_part"],
                "display_name": r["display_name"],
                "created_at": r["created_at"],
                "last_seen_at": r["last_seen_at"],
                "active_sessions": r["active_sessions"],
            }
            for r in rows
        ]

    def send_email(
        self,
        from_addr: str,
        to_addrs: List[str],
        cc_addrs: List[str],
        subject: str,
        body_markdown: str,
        client_token: str,
        sender_session: Optional[str] = None,
    ) -> dict:
        """
        Start a thread and send its first email atomically.
        Enforces idempotency via client_token.
        """
        if not client_token or not isinstance(client_token, str) or not client_token.strip():
            raise ValidationError("missing_idempotency_key", "Idempotency-Key header is required")

        client_token = client_token.strip()
        if client_token.startswith("cloud-import:v1:"):
            raise ValidationError("reserved_client_token", "cloud-import:v1: is reserved for cloud imports")

        # Check existing idempotency
        existing = self.conn.execute(
            "SELECT id, thread_id, sent_at FROM emails WHERE client_token = ?",
            (client_token,),
        ).fetchone()
        if existing:
            return {
                "email_id": existing["id"],
                "thread_id": existing["thread_id"],
                "sent_at": existing["sent_at"],
            }

        # Validate inputs
        norm_from = normalize_address(from_addr)
        if not to_addrs or not isinstance(to_addrs, list):
            raise ValidationError("missing_recipients", "'to' recipient list cannot be empty")
        
        norm_to = [normalize_address(a) for a in to_addrs]
        norm_cc = [normalize_address(a) for a in (cc_addrs or [])]

        if not subject or not isinstance(subject, str) or not subject.strip():
            raise ValidationError("missing_subject", "Subject is required and cannot be empty")
        if not body_markdown or not isinstance(body_markdown, str):
            raise ValidationError("missing_body", "body_markdown is required")

        # Invariant: No duplicate recipient across to and cc
        if len(norm_to) != len(set(norm_to)):
            raise ValidationError("duplicate_recipient", "Duplicate recipient in 'to' list")
        if len(norm_cc) != len(set(norm_cc)):
            raise ValidationError("duplicate_recipient", "Duplicate recipient in 'cc' list")
        
        for addr in norm_to:
            if addr in norm_cc:
                raise ValidationError("duplicate_recipient", f"Recipient '{addr}' cannot appear in both 'to' and 'cc'")

        # Begin immediate transaction
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check idempotency inside lock
            existing = self.conn.execute(
                "SELECT id, thread_id, sent_at FROM emails WHERE client_token = ?",
                (client_token,),
            ).fetchone()
            if existing:
                self.conn.execute("COMMIT")
                return {
                    "email_id": existing["id"],
                    "thread_id": existing["thread_id"],
                    "sent_at": existing["sent_at"],
                }

            # Auto-provision from, to, cc
            self.ensure_inbox(norm_from)
            from_inbox_id = self._get_inbox_id(norm_from)
            from_local, from_proj = parse_address(norm_from)
            home_project_id = self.ensure_project(from_proj)

            to_inbox_ids = []
            for addr in norm_to:
                self.ensure_inbox(addr)
                inbox_id = self._get_inbox_id(addr)
                to_inbox_ids.append((addr, inbox_id))

            cc_inbox_ids = []
            for addr in norm_cc:
                self.ensure_inbox(addr)
                inbox_id = self._get_inbox_id(addr)
                cc_inbox_ids.append((addr, inbox_id))

            thread_id = generate_thread_id()
            email_id = generate_email_id()
            sent_at = utc_now_iso()

            # Insert thread
            self.conn.execute(
                """
                INSERT INTO threads (id, home_project_id, subject, created_at, last_email_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, home_project_id, subject.strip(), sent_at, sent_at),
            )

            # Insert email
            self.conn.execute(
                """
                INSERT INTO emails (id, thread_id, from_inbox_id, subject, body_markdown, reply_to_email_id, client_token, sent_at, sender_session)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (email_id, thread_id, from_inbox_id, subject.strip(), body_markdown, client_token, sent_at, sender_session),
            )

            # Insert recipients
            pos = 0
            for _, i_id in to_inbox_ids:
                self.conn.execute(
                    "INSERT INTO email_recipients (email_id, inbox_id, kind, position, read_at) VALUES (?, ?, 'to', ?, NULL)",
                    (email_id, i_id, pos),
                )
                pos += 1

            for _, i_id in cc_inbox_ids:
                self.conn.execute(
                    "INSERT INTO email_recipients (email_id, inbox_id, kind, position, read_at) VALUES (?, ?, 'cc', ?, NULL)",
                    (email_id, i_id, pos),
                )
                pos += 1

            # Insert thread_inboxes
            all_participant_ids = {from_inbox_id} | {i_id for _, i_id in to_inbox_ids} | {i_id for _, i_id in cc_inbox_ids}
            for p_id in all_participant_ids:
                self.conn.execute(
                    "INSERT INTO thread_inboxes (thread_id, inbox_id, joined_at) VALUES (?, ?, ?) ON CONFLICT(thread_id,inbox_id) DO UPDATE SET joined_at=MIN(joined_at,excluded.joined_at)",
                    (thread_id, p_id, sent_at),
                )

            self.conn.execute("COMMIT")
            return {
                "email_id": email_id,
                "thread_id": thread_id,
                "sent_at": sent_at,
            }
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def reply_email(
        self,
        reply_to_email_id: str,
        from_addr: str,
        body_markdown: str,
        client_token: str,
        to_addrs: Optional[List[str]] = None,
        cc_addrs: Optional[List[str]] = None,
        sender_session: Optional[str] = None,
    ) -> dict:
        """
        Reply to an existing email in a thread.
        Default recipients: reply-all excluding the sender.
        Enforces idempotency and reference chaining.
        """
        if not client_token or not isinstance(client_token, str) or not client_token.strip():
            raise ValidationError("missing_idempotency_key", "Idempotency-Key header is required")

        client_token = client_token.strip()
        if client_token.startswith("cloud-import:v1:"):
            raise ValidationError("reserved_client_token", "cloud-import:v1: is reserved for cloud imports")

        # Check existing idempotency
        existing = self.conn.execute(
            "SELECT id, thread_id, reply_to_email_id, sent_at FROM emails WHERE client_token = ?",
            (client_token,),
        ).fetchone()
        if existing:
            # Reconstruct response shape
            rec_rows = self.conn.execute(
                """
                SELECT r.kind, i.local_part, p.slug
                FROM email_recipients r
                JOIN inboxes i ON r.inbox_id = i.id
                JOIN projects p ON i.project_id = p.id
                WHERE r.email_id = ?
                ORDER BY r.position ASC
                """,
                (existing["id"],),
            ).fetchall()
            resp_to = [f"{r['local_part']}@{r['slug']}" for r in rec_rows if r["kind"] == "to"]
            resp_cc = [f"{r['local_part']}@{r['slug']}" for r in rec_rows if r["kind"] == "cc"]
            ref_rows = self.conn.execute(
                "SELECT referenced_email_id FROM email_references WHERE email_id = ? ORDER BY position ASC",
                (existing["id"],),
            ).fetchall()
            resp_refs = [r["referenced_email_id"] for r in ref_rows]

            return {
                "email_id": existing["id"],
                "thread_id": existing["thread_id"],
                "to": resp_to,
                "cc": resp_cc,
                "reply_to_email_id": existing["reply_to_email_id"],
                "references": resp_refs,
                "sent_at": existing["sent_at"],
            }

        norm_from = normalize_address(from_addr)
        if not body_markdown or not isinstance(body_markdown, str):
            raise ValidationError("missing_body", "body_markdown is required")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check idempotency inside lock
            existing = self.conn.execute(
                "SELECT id, thread_id, reply_to_email_id, sent_at FROM emails WHERE client_token = ?",
                (client_token,),
            ).fetchone()
            if existing:
                self.conn.execute("COMMIT")
                rec_rows = self.conn.execute(
                    """
                    SELECT r.kind, i.local_part, p.slug
                    FROM email_recipients r
                    JOIN inboxes i ON r.inbox_id = i.id
                    JOIN projects p ON i.project_id = p.id
                    WHERE r.email_id = ?
                    ORDER BY r.position ASC
                    """,
                    (existing["id"],),
                ).fetchall()
                resp_to = [f"{r['local_part']}@{r['slug']}" for r in rec_rows if r["kind"] == "to"]
                resp_cc = [f"{r['local_part']}@{r['slug']}" for r in rec_rows if r["kind"] == "cc"]
                ref_rows = self.conn.execute(
                    "SELECT referenced_email_id FROM email_references WHERE email_id = ? ORDER BY position ASC",
                    (existing["id"],),
                ).fetchall()
                return {
                    "email_id": existing["id"],
                    "thread_id": existing["thread_id"],
                    "to": resp_to,
                    "cc": resp_cc,
                    "reply_to_email_id": existing["reply_to_email_id"],
                    "references": [r["referenced_email_id"] for r in ref_rows],
                    "sent_at": existing["sent_at"],
                }

            # Find parent email
            parent = self.conn.execute(
                """
                SELECT e.id, e.thread_id, e.subject, i.local_part, p.slug
                FROM emails e
                JOIN inboxes i ON e.from_inbox_id = i.id
                JOIN projects p ON i.project_id = p.id
                WHERE e.id = ?
                """,
                (reply_to_email_id,),
            ).fetchone()
            if not parent:
                raise NotFoundError("email_not_found", f"Email '{reply_to_email_id}' not found")

            thread_id = parent["thread_id"]
            parent_from_addr = f"{parent['local_part']}@{parent['slug']}"
            subject = parent["subject"]

            # Derive recipients if not explicitly provided
            if to_addrs is None and cc_addrs is None:
                # Reply-all excluding the new sender
                parent_recipients = self.conn.execute(
                    """
                    SELECT r.kind, i.local_part, p.slug
                    FROM email_recipients r
                    JOIN inboxes i ON r.inbox_id = i.id
                    JOIN projects p ON i.project_id = p.id
                    WHERE r.email_id = ?
                    ORDER BY r.position ASC
                    """,
                    (reply_to_email_id,),
                ).fetchall()

                if norm_from != parent_from_addr:
                    norm_to = [parent_from_addr]
                    norm_cc = []
                    for pr in parent_recipients:
                        rec_addr = f"{pr['local_part']}@{pr['slug']}"
                        if rec_addr != norm_from and rec_addr != parent_from_addr and rec_addr not in norm_cc:
                            norm_cc.append(rec_addr)
                else:
                    # Replying to own email
                    norm_to = []
                    norm_cc = []
                    for pr in parent_recipients:
                        rec_addr = f"{pr['local_part']}@{pr['slug']}"
                        if rec_addr != norm_from:
                            if pr["kind"] == "to" and rec_addr not in norm_to:
                                norm_to.append(rec_addr)
                            elif pr["kind"] == "cc" and rec_addr not in norm_cc and rec_addr not in norm_to:
                                norm_cc.append(rec_addr)
                    if not norm_to and not norm_cc:
                        norm_to = [parent_from_addr]
            else:
                norm_to = [normalize_address(a) for a in (to_addrs or [])]
                norm_cc = [normalize_address(a) for a in (cc_addrs or [])]
                if not norm_to:
                    raise ValidationError("missing_recipients", "'to' recipient list cannot be empty")

            # Validate no duplicate recipient across to and cc
            if len(norm_to) != len(set(norm_to)):
                raise ValidationError("duplicate_recipient", "Duplicate recipient in 'to' list")
            if len(norm_cc) != len(set(norm_cc)):
                raise ValidationError("duplicate_recipient", "Duplicate recipient in 'cc' list")
            for addr in norm_to:
                if addr in norm_cc:
                    raise ValidationError("duplicate_recipient", f"Recipient '{addr}' cannot appear in both 'to' and 'cc'")

            # Auto-provision
            self.ensure_inbox(norm_from)
            from_inbox_id = self._get_inbox_id(norm_from)

            to_inbox_ids = []
            for addr in norm_to:
                self.ensure_inbox(addr)
                inbox_id = self._get_inbox_id(addr)
                to_inbox_ids.append((addr, inbox_id))

            cc_inbox_ids = []
            for addr in norm_cc:
                self.ensure_inbox(addr)
                inbox_id = self._get_inbox_id(addr)
                cc_inbox_ids.append((addr, inbox_id))

            # Build references chain: parent's references + parent_id
            parent_ref_rows = self.conn.execute(
                "SELECT referenced_email_id FROM email_references WHERE email_id = ? ORDER BY position ASC",
                (reply_to_email_id,),
            ).fetchall()
            references = [r["referenced_email_id"] for r in parent_ref_rows]
            if reply_to_email_id not in references:
                references.append(reply_to_email_id)

            email_id = generate_email_id()
            sent_at = utc_now_iso()

            # Insert email
            self.conn.execute(
                """
                INSERT INTO emails (id, thread_id, from_inbox_id, subject, body_markdown, reply_to_email_id, client_token, sent_at, sender_session)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email_id, thread_id, from_inbox_id, subject, body_markdown, reply_to_email_id, client_token, sent_at, sender_session),
            )

            # Insert recipients
            pos = 0
            for _, i_id in to_inbox_ids:
                self.conn.execute(
                    "INSERT INTO email_recipients (email_id, inbox_id, kind, position, read_at) VALUES (?, ?, 'to', ?, NULL)",
                    (email_id, i_id, pos),
                )
                pos += 1

            for _, i_id in cc_inbox_ids:
                self.conn.execute(
                    "INSERT INTO email_recipients (email_id, inbox_id, kind, position, read_at) VALUES (?, ?, 'cc', ?, NULL)",
                    (email_id, i_id, pos),
                )
                pos += 1

            # Insert references
            for ref_pos, ref_id in enumerate(references):
                self.conn.execute(
                    "INSERT INTO email_references (email_id, referenced_email_id, position) VALUES (?, ?, ?)",
                    (email_id, ref_id, ref_pos),
                )

            # Update thread participants and activity
            all_participant_ids = {from_inbox_id} | {i_id for _, i_id in to_inbox_ids} | {i_id for _, i_id in cc_inbox_ids}
            for p_id in all_participant_ids:
                self.conn.execute(
                    "INSERT INTO thread_inboxes (thread_id, inbox_id, joined_at) VALUES (?, ?, ?) ON CONFLICT(thread_id,inbox_id) DO UPDATE SET joined_at=MIN(joined_at,excluded.joined_at)",
                    (thread_id, p_id, sent_at),
                )

            self.conn.execute("UPDATE threads SET last_email_at = MAX(last_email_at, ?) WHERE id = ?", (sent_at, thread_id))

            self.conn.commit()
            return {
                "email_id": email_id,
                "thread_id": thread_id,
                "to": norm_to,
                "cc": norm_cc,
                "reply_to_email_id": reply_to_email_id,
                "references": references,
                "sent_at": sent_at,
            }
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def list_threads(self, address: str, unread_only: bool = False, limit: int = 50) -> List[dict]:
        """List newest-active threads visible to an inbox."""
        norm_addr = normalize_address(address)
        self.ensure_inbox(norm_addr)
        inbox_id = self._get_inbox_id(norm_addr)

        limit = max(1, min(limit, 200))

        # Find threads joined by this inbox
        thread_rows = self.conn.execute(
            """
            SELECT t.id, t.subject, t.created_at, t.last_email_at, t.activity_id
            FROM threads t
            JOIN thread_inboxes ti ON t.id = ti.thread_id
            WHERE ti.inbox_id = ?
            ORDER BY t.activity_id DESC, t.id DESC
            """,
            (inbox_id,),
        ).fetchall()

        results = []
        for t in thread_rows:
            thread_id = t["id"]
            # Calculate unread count for this inbox
            unread_row = self.conn.execute(
                """
                SELECT COUNT(*) as unread_count
                FROM email_recipients er
                JOIN emails e ON er.email_id = e.id
                WHERE e.thread_id = ? AND er.inbox_id = ? AND er.read_at IS NULL
                """,
                (thread_id, inbox_id),
            ).fetchone()
            unread_count = unread_row["unread_count"] if unread_row else 0

            if unread_only and unread_count == 0:
                continue

            # Fetch participants in order of appearance in the thread
            participant_rows = self.conn.execute(
                """
                SELECT DISTINCT i.local_part, p.slug, act.activity_at, act.prio
                FROM (
                    SELECT e.from_inbox_id AS inbox_id, e.sent_at AS activity_at, 0 AS prio
                    FROM emails e WHERE e.thread_id = ?
                    UNION ALL
                    SELECT er.inbox_id AS inbox_id, e.sent_at AS activity_at, er.position + 1 AS prio
                    FROM email_recipients er
                    JOIN emails e ON er.email_id = e.id
                    WHERE e.thread_id = ?
                ) act
                JOIN inboxes i ON act.inbox_id = i.id
                JOIN projects p ON i.project_id = p.id
                ORDER BY act.activity_at ASC, act.prio ASC
                """,
                (thread_id, thread_id),
            ).fetchall()

            participants = []
            for pr in participant_rows:
                addr = f"{pr['local_part']}@{pr['slug']}"
                if addr not in participants:
                    participants.append(addr)

            results.append({
                "thread_id": thread_id,
                "subject": t["subject"],
                "participants": participants,
                "last_email_at": t["last_email_at"],
                "activity_id": t["activity_id"],
                "unread_count": unread_count,
            })

            if len(results) >= limit:
                break

        return results

    def get_thread(self, address: str, thread_id: str) -> dict:
        """
        Return the complete thread without changing read state.
        Read state is calculated relative to the requesting inbox.
        """
        norm_addr = normalize_address(address)
        self.ensure_inbox(norm_addr)
        inbox_id = self._get_inbox_id(norm_addr)

        thread_row = self.conn.execute(
            "SELECT id, subject FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if not thread_row:
            raise NotFoundError("thread_not_found", f"Thread '{thread_id}' not found")

        # A thread is only visible to its member inboxes. Raise the SAME
        # not-found error on a non-member so we never leak that the thread
        # exists to an unrelated (or freshly auto-provisioned) inbox.
        if not self._is_thread_member(thread_id, inbox_id):
            raise NotFoundError("thread_not_found", f"Thread '{thread_id}' not found")

        email_rows = self.conn.execute(
            """
            SELECT e.id, e.from_inbox_id, e.subject, e.body_markdown, e.reply_to_email_id, e.sent_at,
                   e.sender_session,
                   i.local_part AS from_local_part, p.slug AS from_project_slug
            FROM emails e
            JOIN inboxes i ON e.from_inbox_id = i.id
            JOIN projects p ON i.project_id = p.id
            WHERE e.thread_id = ?
            ORDER BY e.sent_at ASC, e.id ASC
            """,
            (thread_id,),
        ).fetchall()

        email_dicts = []
        for e in email_rows:
            email_id = e["id"]

            # Recipients
            rec_rows = self.conn.execute(
                """
                SELECT r.kind, r.read_at, r.inbox_id, i.local_part, p.slug
                FROM email_recipients r
                JOIN inboxes i ON r.inbox_id = i.id
                JOIN projects p ON i.project_id = p.id
                WHERE r.email_id = ?
                ORDER BY r.position ASC
                """,
                (email_id,),
            ).fetchall()

            to_addrs = [f"{r['local_part']}@{r['slug']}" for r in rec_rows if r["kind"] == "to"]
            cc_addrs = [f"{r['local_part']}@{r['slug']}" for r in rec_rows if r["kind"] == "cc"]

            # References
            ref_rows = self.conn.execute(
                "SELECT referenced_email_id FROM email_references WHERE email_id = ? ORDER BY position ASC",
                (email_id,),
            ).fetchall()
            references = [r["referenced_email_id"] for r in ref_rows]

            # Read state relative to requesting inbox:
            # If inbox is recipient: read if read_at is set.
            # If inbox is sender (and not recipient): sender state is not tracked as unread -> read: true.
            # If neither (viewer observing thread): read: true.
            rec_entry = next((r for r in rec_rows if r["inbox_id"] == inbox_id), None)
            if rec_entry is not None:
                is_read = rec_entry["read_at"] is not None
            else:
                is_read = True

            email_dicts.append({
                "email_id": email_id,
                "from": f"{e['from_local_part']}@{e['from_project_slug']}",
                "to": to_addrs,
                "cc": cc_addrs,
                "subject": e["subject"],
                "body_markdown": e["body_markdown"],
                "sent_at": e["sent_at"],
                "reply_to_email_id": e["reply_to_email_id"],
                "references": references,
                "read": is_read,
                "sender_session": e["sender_session"],
            })

        return {
            "thread_id": thread_id,
            "subject": thread_row["subject"],
            "emails": email_dicts,
        }

    def watch_state(self, address: str, after: int = 0) -> dict:
        """One non-blocking check of the long-poll watch condition.

        The cursor is the max SQLite rowid over emails delivered to this inbox.
        ``changed`` is True when an UNREAD delivered email exists with rowid
        greater than ``after``; ``latest`` then describes the newest such email.
        """
        norm_addr = normalize_address(address)
        self.ensure_inbox(norm_addr)
        inbox_id = self._get_inbox_id(norm_addr)
        after = max(0, int(after or 0))

        cursor_row = self.conn.execute(
            """
            SELECT COALESCE(MAX(e.rowid), 0) AS cursor
            FROM emails e JOIN email_recipients er ON er.email_id = e.id
            WHERE er.inbox_id = ?
            """,
            (inbox_id,),
        ).fetchone()
        cursor = cursor_row["cursor"]

        unread_row = self.conn.execute(
            """
            SELECT COUNT(*) AS unread_count
            FROM email_recipients er
            WHERE er.inbox_id = ? AND er.read_at IS NULL
            """,
            (inbox_id,),
        ).fetchone()
        unread_count = unread_row["unread_count"]

        latest_row = self.conn.execute(
            """
            SELECT e.thread_id, e.subject, e.sent_at,
                   i.local_part AS from_local_part, p.slug AS from_project_slug
            FROM emails e
            JOIN email_recipients er ON er.email_id = e.id
            JOIN inboxes i ON e.from_inbox_id = i.id
            JOIN projects p ON i.project_id = p.id
            WHERE er.inbox_id = ? AND er.read_at IS NULL AND e.rowid > ?
            ORDER BY e.rowid DESC
            LIMIT 1
            """,
            (inbox_id, after),
        ).fetchone()

        latest = None
        if latest_row:
            latest = {
                "thread_id": latest_row["thread_id"],
                "subject": latest_row["subject"],
                "from": f"{latest_row['from_local_part']}@{latest_row['from_project_slug']}",
                "sent_at": latest_row["sent_at"],
            }

        return {
            "changed": latest is not None,
            "unread_count": unread_count,
            "cursor": cursor,
            "latest": latest,
        }

    # ------------------------------------------------------------------
    # File reservations (NB-7): advisory write-coordination leases.
    # A reservation is a lease, not a lock — nothing is enforced on the
    # filesystem and expired rows are swept lazily on every touch.
    # ------------------------------------------------------------------

    def _sweep_expired_reservations(self, project_id: Optional[int], now: str) -> None:
        """Lazily close expired-but-unreleased rows (audit: released_by='expired').
        Sweeps one project, or every project when project_id is None."""
        if project_id is None:
            self.conn.execute(
                """
                UPDATE reservations
                SET released_at = expires_at, released_by = 'expired'
                WHERE released_at IS NULL AND expires_at <= ?
                """,
                (now,),
            )
            return
        self.conn.execute(
            """
            UPDATE reservations
            SET released_at = expires_at, released_by = 'expired'
            WHERE project_id = ? AND released_at IS NULL AND expires_at <= ?
            """,
            (project_id, now),
        )

    _RESERVATION_SELECT = """
        SELECT r.id, r.path, r.holder_inbox_id, r.holder_session, r.reason,
               r.ttl_seconds, r.created_at, r.expires_at, r.released_at,
               r.released_by, r.repo_key,
               i.local_part, p.slug AS project_slug,
               rp.slug AS reservation_project
        FROM reservations r
        JOIN inboxes i ON r.holder_inbox_id = i.id
        JOIN projects p ON i.project_id = p.id
        JOIN projects rp ON r.project_id = rp.id
    """

    def _active_reservations(self, project_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Active rows for one project, or machine-wide when project_id is None."""
        if project_id is None:
            return self.conn.execute(
                self._RESERVATION_SELECT
                + " WHERE r.released_at IS NULL ORDER BY rp.slug ASC, r.created_at ASC, r.id ASC"
            ).fetchall()
        return self.conn.execute(
            self._RESERVATION_SELECT
            + " WHERE r.project_id = ? AND r.released_at IS NULL ORDER BY r.created_at ASC, r.id ASC",
            (project_id,),
        ).fetchall()

    def _finished_reservations(self, project_id: Optional[int], limit: int) -> List[sqlite3.Row]:
        """Finished (released/expired/forced) audit rows, newest first."""
        limit = max(1, min(int(limit or 50), 200))
        if project_id is None:
            return self.conn.execute(
                self._RESERVATION_SELECT
                + " WHERE r.released_at IS NOT NULL ORDER BY r.released_at DESC, r.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return self.conn.execute(
            self._RESERVATION_SELECT
            + " WHERE r.project_id = ? AND r.released_at IS NOT NULL"
            + " ORDER BY r.released_at DESC, r.id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()

    @staticmethod
    def _is_own(row: sqlite3.Row, inbox_id: int, session: Optional[str]) -> bool:
        return row["holder_inbox_id"] == inbox_id and (row["holder_session"] or None) == (session or None)

    def _conflict_dict(self, requested_key: str, row: sqlite3.Row, now: str) -> dict:
        return {
            "path": reservation_display(requested_key),
            "kind": reservation_kind(requested_key),
            "reserved_path": reservation_display(row["path"]),
            "holder": f"{row['local_part']}@{row['project_slug']}",
            "session": row["holder_session"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "expires_in_seconds": _iso_diff_seconds(now, row["expires_at"]),
        }

    def _reservation_dict(self, row: sqlite3.Row, now: str, include_project: bool = False) -> dict:
        d = {
            "path": reservation_display(row["path"]),
            "kind": reservation_kind(row["path"]),
            "holder": f"{row['local_part']}@{row['project_slug']}",
            "session": row["holder_session"],
            "reason": row["reason"],
            "ttl_seconds": row["ttl_seconds"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "expires_in_seconds": _iso_diff_seconds(now, row["expires_at"]),
        }
        if row["repo_key"]:
            d["repo_key"] = row["repo_key"]
        if include_project:
            d["project"] = row["reservation_project"]
        return d

    def _history_dict(self, row: sqlite3.Row, include_project: bool = False) -> dict:
        d = {
            "path": reservation_display(row["path"]),
            "kind": reservation_kind(row["path"]),
            "holder": f"{row['local_part']}@{row['project_slug']}",
            "session": row["holder_session"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "released_at": row["released_at"],
            "released_by": row["released_by"],
        }
        if row["repo_key"]:
            d["repo_key"] = row["repo_key"]
        if include_project:
            d["project"] = row["reservation_project"]
        return d

    @staticmethod
    def _repo_compatible_conflict(req_key: str, req_repo_key: Optional[str], row: sqlite3.Row) -> bool:
        """Whether an overlapping active row actually conflicts, given repo keys.

        Resources are project-wide — repo identity never exempts them. For file
        paths, two leases conflict only when either side lacks a repo key
        (NULL stays conservative so legacy/keyless clients still conflict
        rather than silently bypass) or the keys are equal. Different keys mean
        different repositories that merely share a directory basename.
        """
        if is_resource_key(req_key) or is_resource_key(row["path"]):
            return True
        row_key = row["repo_key"]
        if req_repo_key is None or row_key is None:
            return True
        return req_repo_key == row_key

    def _find_conflicts(
        self,
        project_id: int,
        paths: List[str],
        inbox_id: int,
        session: Optional[str],
        now: str,
        repo_key: Optional[str] = None,
    ) -> Tuple[List[dict], List[sqlite3.Row], dict]:
        """Return (conflicts, conflicting_rows, own_active_by_path) for the
        requested keys against the project's active reservations."""
        active = self._active_reservations(project_id)
        conflicts: List[dict] = []
        conflict_rows: List[sqlite3.Row] = []
        own_by_path: dict = {}
        seen_row_ids = set()
        for req in paths:
            for row in active:
                if self._is_own(row, inbox_id, session):
                    if row["path"].lower() == req.lower():
                        own_by_path[req] = row
                    continue
                if reservation_paths_conflict(req, row["path"]) and self._repo_compatible_conflict(
                    req, repo_key, row
                ):
                    conflicts.append(self._conflict_dict(req, row, now))
                    if row["id"] not in seen_row_ids:
                        seen_row_ids.add(row["id"])
                        conflict_rows.append(row)
        return conflicts, conflict_rows, own_by_path

    def acquire_reservations(
        self,
        project_slug: str,
        paths: List[str],
        holder_addr: str,
        session: Optional[str],
        reason: str = "",
        ttl_seconds: Optional[int] = None,
        force: bool = False,
        client_token: Optional[str] = None,
        repo_key: Optional[str] = None,
        resources: Optional[List[str]] = None,
    ) -> dict:
        """All-or-nothing multi-key acquire inside one BEGIN IMMEDIATE span.

        A call reserves either file ``paths`` or named ``resources`` — not both.
        Returns {"reservations": [...]} on success or {"conflicts": [...]} when
        any key is actively held by a different (address, session) and force is
        False. Re-acquiring an own active key renews it (idempotent). A forced
        takeover auto-mails each displaced holder after commit; mail failure
        never rolls back or errors the acquire.
        """
        if not client_token or not isinstance(client_token, str) or not client_token.strip():
            raise ValidationError("missing_idempotency_key", "Idempotency-Key header is required")
        client_token = client_token.strip()
        if paths and resources:
            raise ValidationError("validation_error", "A call reserves either 'paths' or 'resources', not both")
        if resources:
            if not isinstance(resources, list):
                raise ValidationError("validation_error", "'resources' must be a list")
            requested = [resource_key(r) for r in resources]
            # Resource leases are project-wide: repo identity never applies.
            repo_key = None
        else:
            if not paths or not isinstance(paths, list):
                raise ValidationError("validation_error", "'paths' list cannot be empty")
            requested = [normalize_reservation_key(p) for p in paths]

        norm_paths: List[str] = []
        for np in requested:
            if np.lower() not in {x.lower() for x in norm_paths}:
                norm_paths.append(np)

        ttl = RESERVATION_DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        ttl = max(RESERVATION_MIN_TTL_SECONDS, min(ttl, RESERVATION_MAX_TTL_SECONDS))
        norm_holder = normalize_address(holder_addr)
        reason = (reason or "").strip()

        # Idempotent replay of a retried acquire call.
        existing = self.conn.execute(
            self._RESERVATION_SELECT + " WHERE r.client_token = ?",
            (client_token,),
        ).fetchall()
        if existing:
            now = utc_now_iso()
            return {"reservations": [self._reservation_dict(r, now) for r in existing]}

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT id FROM reservations WHERE client_token = ?", (client_token,)
            ).fetchall()
            if existing:
                self.conn.execute("COMMIT")
                return self.acquire_reservations(  # replay path above (no txn)
                    project_slug, paths, holder_addr, session, reason, ttl_seconds, force, client_token
                )

            self.ensure_inbox(norm_holder)
            inbox_id = self._get_inbox_id(norm_holder)
            project_id = self.ensure_project(project_slug)
            now = utc_now_iso()
            self._sweep_expired_reservations(project_id, now)

            conflicts, conflict_rows, own_by_path = self._find_conflicts(
                project_id, norm_paths, inbox_id, session, now, repo_key=repo_key
            )

            if conflicts and not force:
                self.conn.execute("ROLLBACK")
                return {"conflicts": conflicts}

            displaced: dict = {}
            if conflicts and force:
                for row in conflict_rows:
                    self.conn.execute(
                        "UPDATE reservations SET released_at = ?, released_by = ? WHERE id = ?",
                        (now, f"forced:{norm_holder}", row["id"]),
                    )
                    victim = f"{row['local_part']}@{row['project_slug']}"
                    displaced.setdefault(victim, []).append(reservation_display(row["path"]))

            expires_at = _iso_add_seconds(now, ttl)
            for np in norm_paths:
                own = own_by_path.get(np)
                if own is not None:
                    # Idempotent re-acquire renews the existing lease (and
                    # refreshes its repo identity to the caller's current one).
                    self.conn.execute(
                        """
                        UPDATE reservations
                        SET expires_at = ?, ttl_seconds = ?, reason = ?, client_token = ?, repo_key = ?
                        WHERE id = ?
                        """,
                        (expires_at, ttl, reason or own["reason"], client_token,
                         None if is_resource_key(np) else repo_key, own["id"]),
                    )
                else:
                    self.conn.execute(
                        """
                        INSERT INTO reservations
                          (project_id, path, holder_inbox_id, holder_session, reason,
                           ttl_seconds, created_at, expires_at, client_token, repo_key)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (project_id, np, inbox_id, session, reason, ttl, now, expires_at,
                         client_token, None if is_resource_key(np) else repo_key),
                    )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

        rows = self.conn.execute(
            self._RESERVATION_SELECT + " WHERE r.client_token = ?",
            (client_token,),
        ).fetchall()
        now = utc_now_iso()
        result = {"reservations": [self._reservation_dict(r, now) for r in rows]}
        if displaced:
            notified = self._notify_takeover(norm_holder, session, reason, displaced)
            if notified:
                result["notified"] = notified
        return result

    def _notify_takeover(
        self,
        taker_addr: str,
        taker_session: Optional[str],
        reason: str,
        displaced: dict,
    ) -> List[str]:
        """Auto-mail each displaced holder about a forced takeover.

        Runs AFTER the takeover transaction has committed: a mail failure must
        neither roll back the takeover nor error the acquire, so every send is
        individually failure-isolated. One mail per displaced holder per
        takeover event, listing all of their displaced paths.
        """
        notified: List[str] = []
        for victim, victim_paths in displaced.items():
            subject = f"Reservation takeover: {victim_paths[0]}"
            if len(victim_paths) > 1:
                subject += f" (+{len(victim_paths) - 1} more)"
            path_lines = "\n".join(f"- `{p}`" for p in victim_paths)
            session_note = f" (session {taker_session})" if taker_session else ""
            reason_note = reason or "(no reason given)"
            body = (
                f"Your active reservation(s) were taken over with `--force`:\n\n"
                f"{path_lines}\n\n"
                f"Taken by: {taker_addr}{session_note}\n"
                f"Reason: {reason_note}\n\n"
                f"Audit: the displaced lease rows are recorded with "
                f"`released_by=forced:{taker_addr}`.\n"
                f"Reply to this mail to coordinate if you were still working on them."
            )
            try:
                self.send_email(
                    from_addr=taker_addr,
                    to_addrs=[victim],
                    cc_addrs=[],
                    subject=subject,
                    body_markdown=body,
                    client_token=str(uuid.uuid4()),
                    sender_session=taker_session,
                )
                notified.append(victim)
            except Exception:
                # Advisory courtesy mail only — never fail the acquire.
                pass
        return notified

    def _own_active_rows(
        self, project_id: int, inbox_id: int, session: Optional[str]
    ) -> List[sqlite3.Row]:
        return [
            r for r in self._active_reservations(project_id)
            if self._is_own(r, inbox_id, session)
        ]

    def _renew_or_release(
        self,
        project_slug: str,
        holder_addr: str,
        session: Optional[str],
        paths: Optional[List[str]],
        release_all: bool,
        release: bool,
    ) -> dict:
        """Shared implementation for renew (release=False) and release (=True)."""
        norm_holder = normalize_address(holder_addr)
        self.ensure_inbox(norm_holder)
        inbox_id = self._get_inbox_id(norm_holder)
        project_id = self.ensure_project(project_slug)

        norm_paths = [normalize_reservation_key(p) for p in (paths or [])]
        if not release_all and not norm_paths:
            raise ValidationError("validation_error", "Provide paths or all=true")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now_iso()
            self._sweep_expired_reservations(project_id, now)
            own = self._own_active_rows(project_id, inbox_id, session)
            own_by_lower = {r["path"].lower(): r for r in own}

            if release_all:
                targets = list(own)
                missed: List[str] = []
            else:
                targets = []
                missed = []
                for np in norm_paths:
                    row = own_by_lower.get(np.lower())
                    if row is not None:
                        targets.append(row)
                    else:
                        missed.append(reservation_display(np))

            done: List[str] = []
            for row in targets:
                if release:
                    self.conn.execute(
                        "UPDATE reservations SET released_at = ?, released_by = 'holder' WHERE id = ?",
                        (now, row["id"]),
                    )
                else:
                    # Renew extends to now + the lease's own TTL (horizon <= 2h).
                    self.conn.execute(
                        "UPDATE reservations SET expires_at = ? WHERE id = ?",
                        (_iso_add_seconds(now, row["ttl_seconds"]), row["id"]),
                    )
                done.append(reservation_display(row["path"]))
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

        key = "released" if release else "renewed"
        return {key: done, "missed": missed}

    def renew_reservations(
        self, project_slug: str, holder_addr: str, session: Optional[str],
        paths: Optional[List[str]] = None, renew_all: bool = False,
    ) -> dict:
        return self._renew_or_release(project_slug, holder_addr, session, paths, renew_all, release=False)

    def release_reservations(
        self, project_slug: str, holder_addr: str, session: Optional[str],
        paths: Optional[List[str]] = None, release_all: bool = False,
    ) -> dict:
        return self._renew_or_release(project_slug, holder_addr, session, paths, release_all, release=True)

    def list_reservations(self, project_slug: str, holder_addr: Optional[str] = None) -> List[dict]:
        """List active reservations for a project (after the lazy expiry sweep)."""
        project_id = self.ensure_project(project_slug)
        now = utc_now_iso()
        self._sweep_expired_reservations(project_id, now)
        rows = self._active_reservations(project_id)
        result = [self._reservation_dict(r, now) for r in rows]
        if holder_addr:
            norm = normalize_address(holder_addr)
            result = [r for r in result if r["holder"] == norm]
        return result

    def reservation_history(self, project_slug: Optional[str] = None, limit: int = 50) -> List[dict]:
        """Finished (released/expired/forced) audit rows, newest first.

        Per-project when a slug is given, machine-wide otherwise. The lazy
        expiry sweep runs first so just-expired leases appear in history rather
        than lingering as stale actives."""
        now = utc_now_iso()
        project_id = self.ensure_project(project_slug) if project_slug else None
        self._sweep_expired_reservations(project_id, now)
        rows = self._finished_reservations(project_id, limit)
        return [self._history_dict(r, include_project=project_id is None) for r in rows]

    def list_reservations_all(self) -> List[dict]:
        """Machine-wide active reservations across all projects (observer view)."""
        now = utc_now_iso()
        self._sweep_expired_reservations(None, now)
        rows = self._active_reservations(None)
        return [self._reservation_dict(r, now, include_project=True) for r in rows]

    def reservation_conflicts(
        self, project_slug: str, paths: List[str], holder_addr: str, session: Optional[str],
        repo_key: Optional[str] = None,
    ) -> List[dict]:
        """Non-mutating conflict check used by the wait long-poll."""
        norm_holder = normalize_address(holder_addr)
        self.ensure_inbox(norm_holder)
        inbox_id = self._get_inbox_id(norm_holder)
        project_id = self.ensure_project(project_slug)
        now = utc_now_iso()
        self._sweep_expired_reservations(project_id, now)
        norm_paths = [normalize_reservation_key(p) for p in paths]
        conflicts, _, _ = self._find_conflicts(
            project_id, norm_paths, inbox_id, session, now, repo_key=repo_key
        )
        return conflicts

    def mark_thread_read(self, address: str, thread_id: str) -> int:
        """Mark every delivered email in the thread read for the given inbox."""
        norm_addr = normalize_address(address)
        self.ensure_inbox(norm_addr)
        inbox_id = self._get_inbox_id(norm_addr)

        # Check thread exists
        thread_row = self.conn.execute("SELECT id FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if not thread_row:
            raise NotFoundError("thread_not_found", f"Thread '{thread_id}' not found")

        # Same membership invariant as get_thread: a non-member (or freshly
        # auto-provisioned) inbox must not be able to mark an unrelated thread
        # read, and must not learn the thread exists. Same not-found error.
        if not self._is_thread_member(thread_id, inbox_id):
            raise NotFoundError("thread_not_found", f"Thread '{thread_id}' not found")

        now = utc_now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                """
                UPDATE email_recipients
                SET read_at = ?
                WHERE inbox_id = ?
                  AND read_at IS NULL
                  AND email_id IN (SELECT id FROM emails WHERE thread_id = ?)
                """,
                (now, inbox_id, thread_id),
            )
            count = cur.rowcount
            self.conn.commit()
            return count
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
