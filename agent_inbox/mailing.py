"""Recipient validation and durable project broadcasts shared by every client."""

from datetime import datetime, timedelta
from difflib import get_close_matches

from agent_inbox.models import NotFoundError, ValidationError, normalize_address


class ProjectMail:
    BROADCAST_RETENTION_DAYS = 7

    def require_inbox(self, address, sender=False, create_missing=False):
        address = normalize_address(address)
        row = self.conn.execute('''SELECT i.id,i.role FROM inboxes i JOIN projects p ON p.id=i.project_id
            WHERE i.local_part=? AND p.slug=?''', tuple(address.split('@'))).fetchone()
        if row is None and create_missing:
            self.ensure_inbox(address)
            return self._get_inbox_id(address)
        if row is None:
            candidates = [item['address'] for item in self.list_inboxes(address.split('@')[1])]
            suggestions = get_close_matches(address, candidates, n=3, cutoff=.5)
            hint = ' Did you mean ' + ', '.join(suggestions) + '?' if suggestions else ''
            raise NotFoundError('unknown_sender' if sender else 'unknown_recipient',
                f"Unknown {'sender' if sender else 'recipient'} '{address}'. Register it explicitly first." + hint)
        if not sender and row['role'] == 'service':
            raise ValidationError('service_recipient', f"'{address}' is a service that does not receive mail")
        return row['id']

    def expand_recipients(self, addresses, sender, kind, create_missing=False):
        if not isinstance(addresses, list):
            raise ValidationError('invalid_recipients', f"'{kind}' must be a list")
        result, broadcasts, explicit = [], [], set()
        for raw in addresses:
            if isinstance(raw, str) and raw.strip().startswith('*@'):
                project = normalize_address('broadcast@' + raw.strip()[2:]).split('@')[1]
                row = self.conn.execute('SELECT id FROM projects WHERE slug=?', (project,)).fetchone()
                if row is None:
                    raise NotFoundError('unknown_project', f"Unknown project '{project}'; register it first")
                if row['id'] not in broadcasts:
                    broadcasts.append(row['id'])
                recipients = [i['address'] for i in self.list_inboxes(project)
                              if i['role'] == 'agent' and i['address'] != sender]
            else:
                address = normalize_address(raw)
                if address in explicit:
                    raise ValidationError('duplicate_recipient', f"Duplicate recipient in '{kind}' list")
                explicit.add(address)
                self.require_inbox(address, create_missing=create_missing)
                recipients = [address]
            for address in recipients:
                if address not in result:
                    result.append(address)
        return result, broadcasts

    def store_broadcasts(self, email_id, to_projects, cc_projects, sent_at):
        expires = (datetime.fromisoformat(sent_at.replace('Z', '+00:00')) +
                   timedelta(days=self.BROADCAST_RETENTION_DAYS)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        for kind, projects in [('to', to_projects), ('cc', cc_projects)]:
            for project_id in projects:
                self.conn.execute('INSERT OR IGNORE INTO email_broadcasts VALUES (?,?,?,?)',
                                  (email_id, project_id, kind, expires))

    def backfill_broadcasts(self, inbox_id, project_id, now):
        rows = self.conn.execute('''SELECT b.email_id,b.kind,e.thread_id FROM email_broadcasts b
            JOIN emails e ON e.id=b.email_id WHERE b.project_id=? AND b.expires_at>?
            AND e.from_inbox_id!=? ORDER BY e.sent_at,e.id''', (project_id, now, inbox_id)).fetchall()
        for row in rows:
            self.conn.execute('''INSERT OR IGNORE INTO email_recipients (email_id,inbox_id,kind,position)
                SELECT ?,?,?,COALESCE(MAX(position),-1)+1 FROM email_recipients WHERE email_id=?''',
                (row['email_id'], inbox_id, row['kind'], row['email_id']))
            self.conn.execute('INSERT OR IGNORE INTO thread_inboxes VALUES (?,?,?)',
                              (row['thread_id'], inbox_id, now))

    def email_status(self, email_id):
        email = self.conn.execute('SELECT id,thread_id,sent_at FROM emails WHERE id=?', (email_id,)).fetchone()
        if email is None:
            raise NotFoundError('email_not_found', f"Email '{email_id}' not found")
        recipients = self.conn.execute('''SELECT i.local_part || '@' || p.slug AS address,r.kind,r.read_at
            FROM email_recipients r JOIN inboxes i ON i.id=r.inbox_id JOIN projects p ON p.id=i.project_id
            WHERE r.email_id=? ORDER BY r.position''', (email_id,)).fetchall()
        broadcasts = self.conn.execute('''SELECT p.slug AS project,b.kind,b.expires_at FROM email_broadcasts b
            JOIN projects p ON p.id=b.project_id WHERE b.email_id=? ORDER BY p.slug''', (email_id,)).fetchall()
        return {'email_id': email_id, 'thread_id': email['thread_id'], 'sent_at': email['sent_at'],
                'recipients': [dict(r) for r in recipients], 'broadcasts': [dict(r) for r in broadcasts],
                'read_count': sum(r['read_at'] is not None for r in recipients),
                'unread_count': sum(r['read_at'] is None for r in recipients)}
