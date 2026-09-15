"""Session-bound name leases. Inactivity is not a change of identity."""

from datetime import datetime, timedelta, timezone

from agent_inbox.identity import process_started_at
from agent_inbox.models import ConflictError, ValidationError, normalize_slug, utc_now_iso


class AgentLeases:
    LEASE_EXPIRY_SECONDS = 12 * 60 * 60

    def _lease_expiry_cutoff(self):
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.LEASE_EXPIRY_SECONDS)
        return cutoff.isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    @staticmethod
    def _holder_alive(row):
        return bool(row['holder_pid_started'] and
                    process_started_at(row['holder_pid']) == row['holder_pid_started'])

    def lookup_agent_by_session(self, project_slug, session_id, recover=False):
        if not session_id:
            return None
        # A taken-over name no longer has this session_id. Expiry alone must
        # never hide the session's own name. Prefer its most recent claim.
        owns_txn = recover and not self.conn.in_transaction
        if owns_txn:
            self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('''SELECT * FROM agent_leases
                WHERE project_slug=? AND session_id=? ORDER BY last_seen DESC, claimed_at DESC, agent_slug LIMIT 1''',
                (normalize_slug(project_slug), session_id)).fetchone()
            if row and recover:
                # Restore liveness atomically with lookup, so a competing claim
                # cannot take the expired name between resolution and use.
                self.conn.execute('UPDATE agent_leases SET last_seen=? WHERE agent_slug=? AND project_slug=? AND session_id=?',
                                  (utc_now_iso(), row['agent_slug'], row['project_slug'], session_id))
            if owns_txn:
                self.conn.commit()
            return ({'agent': row['agent_slug'], 'project': row['project_slug'],
                     'address': f"{row['agent_slug']}@{row['project_slug']}", 'session': session_id} if row else None)
        except BaseException:
            if owns_txn:
                self.conn.rollback()
            raise

    def claim_agent(self, family, project_slug, session_id=None, holder_pid=None):
        if not isinstance(family, str) or not family.strip() or not isinstance(project_slug, str) or not project_slug.strip():
            raise ValidationError('invalid_lease', 'family and project are required')
        family, project_slug = normalize_slug(family), normalize_slug(project_slug)
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 64):
            raise ValidationError('invalid_session', 'session must be a nonempty string of at most 64 characters')
        started = process_started_at(holder_pid)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            historical = self.lookup_agent_by_session(project_slug, session_id)
            slot = None
            if historical:
                previous = historical['agent']
                if previous == family or (previous.startswith(family + '-') and previous[len(family)+1:].isdigit()):
                    slot = previous
            if slot is None:
                cutoff = self._lease_expiry_cutoff()
                for number in range(1, 100):
                    candidate = family if number == 1 else f'{family}-{number}'
                    inbox = self.conn.execute('''SELECT i.role FROM inboxes i JOIN projects p ON p.id=i.project_id
                        WHERE i.local_part=? AND p.slug=?''', (candidate, project_slug)).fetchone()
                    if inbox and inbox['role'] == 'service':
                        continue
                    row = self.conn.execute('SELECT * FROM agent_leases WHERE agent_slug=? AND project_slug=?',
                                            (candidate, project_slug)).fetchone()
                    if row is None or (row['last_seen'] < cutoff and not self._holder_alive(row)):
                        slot = candidate
                        break
            if slot is None:
                raise ConflictError('lease_exhausted', f'No free slot for {family} in {project_slug} (99 concurrent leases)')
            now = utc_now_iso()
            self.conn.execute('''INSERT INTO agent_leases
                (agent_slug,project_slug,session_id,holder_pid,holder_pid_started,claimed_at,last_seen)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(agent_slug,project_slug) DO UPDATE SET
                session_id=excluded.session_id, holder_pid=excluded.holder_pid,
                holder_pid_started=excluded.holder_pid_started, claimed_at=excluded.claimed_at, last_seen=excluded.last_seen''',
                (slot, project_slug, session_id, holder_pid if started else None, started, now, now))
            self.ensure_inbox(f'{slot}@{project_slug}', role='agent')
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return {'agent': slot, 'project': project_slug, 'address': f'{slot}@{project_slug}',
                'session': session_id, 'recovered': bool(historical and historical['agent'] == slot)}

    def release_agent(self, agent_slug, project_slug, session_id=None):
        agent_slug, project_slug = normalize_slug(agent_slug), normalize_slug(project_slug)
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row = self.conn.execute('SELECT session_id FROM agent_leases WHERE agent_slug=? AND project_slug=?',
                                    (agent_slug, project_slug)).fetchone()
            if row and row['session_id'] and row['session_id'] != session_id:
                raise ConflictError('lease_owner_mismatch', 'This name belongs to a different session')
            cur = self.conn.execute('DELETE FROM agent_leases WHERE agent_slug=? AND project_slug=?',
                                    (agent_slug, project_slug))
            self.conn.commit()
            return {'released': cur.rowcount > 0, 'agent': agent_slug, 'project': project_slug}
        except BaseException:
            self.conn.rollback()
            raise

    def touch_lease(self, agent_slug, project_slug, session_id=None, holder_pid=None):
        # Anonymous callers may refresh legacy unbound leases only. A header
        # naming someone else cannot keep their lease alive or steal its binding.
        started = process_started_at(holder_pid)
        self.conn.execute('''UPDATE agent_leases SET last_seen=?,
            holder_pid=CASE WHEN ? IS NOT NULL THEN ? ELSE holder_pid END,
            holder_pid_started=COALESCE(?,holder_pid_started)
            WHERE agent_slug=? AND project_slug=? AND session_id IS ?''',
            (utc_now_iso(), started, holder_pid, started,
             normalize_slug(agent_slug), normalize_slug(project_slug), session_id))
