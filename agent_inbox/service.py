"""Core domain and transactional database operations for Agent Inboxes."""

import sqlite3
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


class InboxService:
    """Encapsulates transactional operations on the SQLite database."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

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

    def list_inboxes(self, project_slug: Optional[str] = None) -> List[dict]:
        """List inboxes, optionally filtered by project slug."""
        if project_slug:
            canonical_project = normalize_slug(project_slug)
            rows = self.conn.execute(
                """
                SELECT i.local_part, p.slug as project_slug, i.display_name, i.created_at, i.last_seen_at
                FROM inboxes i
                JOIN projects p ON i.project_id = p.id
                WHERE p.slug = ? COLLATE NOCASE
                ORDER BY i.local_part ASC
                """,
                (canonical_project,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT i.local_part, p.slug as project_slug, i.display_name, i.created_at, i.last_seen_at
                FROM inboxes i
                JOIN projects p ON i.project_id = p.id
                ORDER BY p.slug ASC, i.local_part ASC
                """
            ).fetchall()

        return [
            {
                "address": f"{r['local_part']}@{r['project_slug']}",
                "project": r["project_slug"],
                "local_part": r["local_part"],
                "display_name": r["display_name"],
                "created_at": r["created_at"],
                "last_seen_at": r["last_seen_at"],
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
    ) -> dict:
        """
        Start a thread and send its first email atomically.
        Enforces idempotency via client_token.
        """
        if not client_token or not isinstance(client_token, str) or not client_token.strip():
            raise ValidationError("missing_idempotency_key", "Idempotency-Key header is required")

        client_token = client_token.strip()

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
                INSERT INTO emails (id, thread_id, from_inbox_id, subject, body_markdown, reply_to_email_id, client_token, sent_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (email_id, thread_id, from_inbox_id, subject.strip(), body_markdown, client_token, sent_at),
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
                    "INSERT OR IGNORE INTO thread_inboxes (thread_id, inbox_id, joined_at) VALUES (?, ?, ?)",
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
    ) -> dict:
        """
        Reply to an existing email in a thread.
        Default recipients: reply-all excluding the sender.
        Enforces idempotency and reference chaining.
        """
        if not client_token or not isinstance(client_token, str) or not client_token.strip():
            raise ValidationError("missing_idempotency_key", "Idempotency-Key header is required")

        client_token = client_token.strip()

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
                INSERT INTO emails (id, thread_id, from_inbox_id, subject, body_markdown, reply_to_email_id, client_token, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email_id, thread_id, from_inbox_id, subject, body_markdown, reply_to_email_id, client_token, sent_at),
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
                    "INSERT OR IGNORE INTO thread_inboxes (thread_id, inbox_id, joined_at) VALUES (?, ?, ?)",
                    (thread_id, p_id, sent_at),
                )

            self.conn.execute("UPDATE threads SET last_email_at = ? WHERE id = ?", (sent_at, thread_id))

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
            SELECT t.id, t.subject, t.created_at, t.last_email_at
            FROM threads t
            JOIN thread_inboxes ti ON t.id = ti.thread_id
            WHERE ti.inbox_id = ?
            ORDER BY t.last_email_at DESC, t.id DESC
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
            })

        return {
            "thread_id": thread_id,
            "subject": thread_row["subject"],
            "emails": email_dicts,
        }

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
