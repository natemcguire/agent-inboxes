"""Explicit, transactional inbox maintenance with read-only previews."""

from agent_inbox.models import ConflictError, ValidationError, normalize_address, utc_now_iso


class InboxMaintenance:
    def inbox_summary(self, address):
        inbox_id = self.require_inbox(address, sender=True)
        row = self.conn.execute('''SELECT COUNT(DISTINCT e.thread_id) AS threads,COUNT(*) AS emails,
            MIN(e.sent_at) AS first_activity,MAX(e.sent_at) AS last_activity FROM emails e
            WHERE e.from_inbox_id=? OR EXISTS
            (SELECT 1 FROM email_recipients r WHERE r.email_id=e.id AND r.inbox_id=?)''', (inbox_id, inbox_id)).fetchone()
        result = dict(row, address=address)
        result['sent_emails'] = self.conn.execute('SELECT COUNT(*) FROM emails WHERE from_inbox_id=?', (inbox_id,)).fetchone()[0]
        result['unread'] = self.conn.execute('SELECT COUNT(*) FROM email_recipients WHERE inbox_id=? AND read_at IS NULL', (inbox_id,)).fetchone()[0]
        result['reservations'] = self.conn.execute('SELECT COUNT(*) FROM reservations WHERE holder_inbox_id=?', (inbox_id,)).fetchone()[0]
        return result

    def _maintenance_ready(self, address):
        inbox_id = self.require_inbox(address, sender=True)
        if self.conn.execute('SELECT 1 FROM cloud_state WHERE endpoint IS NOT NULL').fetchone():
            raise ConflictError('cloud_bound', 'Inbox maintenance requires local-only data; cloud history cannot be rewritten safely')
        if self.conn.execute('SELECT 1 FROM reservations WHERE holder_inbox_id=? AND released_at IS NULL AND expires_at>?',
                             (inbox_id, utc_now_iso())).fetchone():
            raise ConflictError('active_reservations', 'Release active file/resource reservations before inbox maintenance')
        if self.conn.execute("SELECT 1 FROM ae_tasks WHERE state!='completed' AND (owner=? OR target=?)", (address, address)).fetchone():
            raise ConflictError('active_tasks', 'Hand off active/queued AE work explicitly before inbox maintenance')
        return inbox_id

    def merge_inboxes(self, source, target, dry_run=False):
        source, target = normalize_address(source), normalize_address(target)
        if type(dry_run) is not bool:
            raise ValidationError('invalid_dry_run', 'dry_run must be boolean')
        if source == target or source.split('@')[1] != target.split('@')[1]:
            raise ValidationError('invalid_merge', 'Merge requires two different inboxes in the same project')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            summary = {'source': self.inbox_summary(source), 'target': self.inbox_summary(target), 'dry_run': dry_run}
            if dry_run:
                self.conn.rollback()
                return summary
            source_id = self._maintenance_ready(source)
            target_id = self._maintenance_ready(target)
            roles = {r[0] for r in self.conn.execute('SELECT role FROM inboxes WHERE id IN (?,?)', (source_id, target_id))}
            if len(roles) != 1:
                raise ValidationError('role_conflict', 'Cannot merge an agent inbox with a sender-only service')
            self.conn.execute('UPDATE emails SET from_inbox_id=? WHERE from_inbox_id=?', (target_id, source_id))
            for row in self.conn.execute('SELECT * FROM email_recipients WHERE inbox_id=?', (source_id,)).fetchall():
                old = self.conn.execute('SELECT * FROM email_recipients WHERE email_id=? AND inbox_id=?', (row['email_id'], target_id)).fetchone()
                if old:
                    # One physical person read either copy: preserve that receipt.
                    read = min((v for v in (row['read_at'], old['read_at']) if v is not None), default=None)
                    kind = 'to' if 'to' in (row['kind'], old['kind']) else 'cc'
                    self.conn.execute('UPDATE email_recipients SET kind=?,read_at=? WHERE email_id=? AND inbox_id=?',
                                      (kind, read, row['email_id'], target_id))
                    self.conn.execute('DELETE FROM email_recipients WHERE email_id=? AND inbox_id=?', (row['email_id'], source_id))
                else:
                    # A newly visible receipt needs a fresh delivery cursor and
                    # AE event so the target's existing watchers see it.
                    self.conn.execute('DELETE FROM email_recipients WHERE email_id=? AND inbox_id=?', (row['email_id'], source_id))
                    self.conn.execute('INSERT INTO email_recipients (email_id,inbox_id,kind,position,read_at) VALUES (?,?,?,?,?)',
                                      (row['email_id'], target_id, row['kind'], row['position'], row['read_at']))
            for row in self.conn.execute('SELECT * FROM thread_inboxes WHERE inbox_id=?', (source_id,)).fetchall():
                self.conn.execute('''INSERT INTO thread_inboxes VALUES (?,?,?) ON CONFLICT(thread_id,inbox_id)
                    DO UPDATE SET joined_at=MIN(joined_at,excluded.joined_at)''', (row['thread_id'], target_id, row['joined_at']))
            for row in self.conn.execute('SELECT * FROM announcement_receipts WHERE inbox_id=?', (source_id,)).fetchall():
                self.conn.execute('''INSERT INTO announcement_receipts VALUES (?,?,?) ON CONFLICT(announcement_id,inbox_id)
                    DO UPDATE SET read_at=MIN(read_at,excluded.read_at)''', (row['announcement_id'], target_id, row['read_at']))
            self.conn.execute('UPDATE reservations SET holder_inbox_id=? WHERE holder_inbox_id=?', (target_id, source_id))
            for row in self.conn.execute('SELECT * FROM sessions WHERE inbox_id=?', (source_id,)).fetchall():
                self.conn.execute('''INSERT INTO sessions VALUES (?,?,?,?,?) ON CONFLICT(inbox_id,session_id)
                    DO UPDATE SET first_seen_at=MIN(first_seen_at,excluded.first_seen_at),
                    pid=CASE WHEN excluded.last_seen_at>last_seen_at THEN excluded.pid ELSE pid END,
                    last_seen_at=MAX(last_seen_at,excluded.last_seen_at)''',
                    (target_id, row['session_id'], row['pid'], row['first_seen_at'], row['last_seen_at']))
            self.conn.execute('INSERT OR IGNORE INTO ae_subscriptions SELECT ?,kind,ref FROM ae_subscriptions WHERE actor=?', (target, source))
            self._remove_inbox(source, source_id)
            self.conn.commit()
            return dict(summary, merged=True)
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _remove_inbox(self, address, inbox_id):
        self.conn.execute('DELETE FROM announcement_receipts WHERE inbox_id=?', (inbox_id,))
        self.conn.execute('DELETE FROM ae_subscriptions WHERE actor=?', (address,))
        self.conn.execute('DELETE FROM hook_stamps WHERE address IN (?,?)', (address, address + '#announcements'))
        agent, project = address.split('@')
        self.conn.execute('DELETE FROM agent_leases WHERE agent_slug=? AND project_slug=?', (agent, project))
        self.conn.execute('DELETE FROM inboxes WHERE id=?', (inbox_id,))

    def delete_inbox(self, address, force=False, dry_run=False):
        address = normalize_address(address)
        if type(force) is not bool or type(dry_run) is not bool:
            raise ValidationError('invalid_delete', 'force and dry_run must be boolean')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            summary = dict(self.inbox_summary(address), dry_run=dry_run, force=force)
            summary['effect'] = 'Deletes this inbox, its sent emails, its recipient copies and reservation history. Other recipients lose emails sent by this inbox.'
            if dry_run:
                self.conn.rollback()
                return summary
            inbox_id = self._maintenance_ready(address)
            if (summary['emails'] or summary['reservations']) and not force:
                raise ConflictError('inbox_not_empty', 'Inbox has history; use merge to preserve it or --force to destroy it')
            # Preserve replies and threads while removing the deleted sender's mail.
            self.conn.execute('UPDATE emails SET reply_to_email_id=NULL WHERE reply_to_email_id IN (SELECT id FROM emails WHERE from_inbox_id=?)', (inbox_id,))
            self.conn.execute('DELETE FROM email_references WHERE referenced_email_id IN (SELECT id FROM emails WHERE from_inbox_id=?)', (inbox_id,))
            self.conn.execute('DELETE FROM emails WHERE from_inbox_id=?', (inbox_id,))
            self.conn.execute('DELETE FROM email_recipients WHERE inbox_id=?', (inbox_id,))
            self.conn.execute('DELETE FROM reservations WHERE holder_inbox_id=?', (inbox_id,))
            self._remove_inbox(address, inbox_id)
            self.conn.execute('''UPDATE threads SET last_email_at=COALESCE((SELECT MAX(sent_at) FROM emails WHERE thread_id=threads.id),created_at),
                activity_id=COALESCE((SELECT MAX(delivery_id) FROM emails WHERE thread_id=threads.id),0)''')
            self.conn.commit()
            return dict(summary, deleted=True)
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
