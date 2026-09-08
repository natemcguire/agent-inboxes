"""Bounded briefs and policy watches over the authoritative SQLite journal."""
import json
import math
import time

from agent_inbox.models import ValidationError, normalize_address


def wire_size(value):
    # Matches the HTTP serializer and compact CLI output, including Unicode escapes.
    return len(json.dumps(value).encode('utf-8'))


class AttentionMixin:
    def validate_policy(self, policy):
        if policy not in ('all', 'to-me', 'my-tasks', 'my-files', 'my-work'):
            raise ValidationError('invalid_policy', 'Choose all, to-me, my-tasks, my-files or my-work')

    def matches_policy(self, event, actor, policy):
        self.validate_policy(policy)
        if policy == 'all':
            return True
        detail = json.loads(event['detail']) if isinstance(event['detail'], str) else event['detail']
        mail = event['kind'] == 'mail.received' and event['audience'] == actor and detail.get('role') == 'to'
        task = False
        if event['kind'].startswith('task.'):
            task = bool(self.conn.execute('''SELECT 1 FROM ae_tasks t WHERE id=? AND
                (creator=? OR EXISTS(SELECT 1 FROM ae_task_history h WHERE h.task_id=t.id
                  AND h.version<=? AND (h.owner=? OR h.target=?))
                 OR EXISTS(SELECT 1 FROM ae_subscriptions s WHERE s.actor=? AND s.kind='task' AND s.ref=t.id))''',
                (event['ref'], actor, detail.get('version', 1), actor, actor, actor)).fetchone())
        files = False
        if event['kind'] == 'reservation.changed':
            from agent_inbox.service import reservation_paths_conflict
            rows = self.conn.execute("SELECT paths FROM ae_tasks WHERE project=? AND owner=? AND state IN ('active','blocked')",
                                     (actor.split('@')[1], actor))
            path = detail.get('path', '')
            files = any(reservation_paths_conflict(path, p) for row in rows for p in json.loads(row['paths']))
            if not files:
                files = bool(self.conn.execute('''SELECT 1 FROM reservations r JOIN inboxes i ON i.id=r.holder_inbox_id
                    JOIN projects p ON p.id=i.project_id WHERE CAST(r.id AS TEXT)=? AND i.local_part||'@'||p.slug=?''',
                    (event['ref'], actor)).fetchone())
        return {'to-me': mail, 'my-tasks': task, 'my-files': files, 'my-work': mail or task or files}[policy]

    def urgent(self, event, actor):
        if event['kind'] == 'mail.received':
            return event['audience'] == actor and event['detail'].get('role') == 'to'
        if event['kind'].startswith('task.'):
            return (event['kind'] == 'task.ready' or event['detail'].get('state') in ('blocked', 'queued')) and self.matches_policy(event, actor, 'my-tasks')
        if event['kind'] == 'reservation.changed':
            return self.matches_policy(event, actor, 'my-files')
        return False

    def tasks(self, actor, state=None, after='', limit=50):
        from agent_inbox.ae import integer, text
        actor = normalize_address(actor)
        integer(limit, 'limit', 1, 200)
        if state not in (None, 'queued', 'active', 'blocked', 'completed'):
            raise ValidationError('invalid_state', 'Unknown task state')
        text(after, 'after', 100, False)
        # Immutable insertion order; a deleted/cross-project cursor cannot silently reset a scan.
        cursor_row = None
        if after:
            self.task(after, actor)
            cursor_row = self.conn.execute('SELECT created_at,id FROM ae_tasks WHERE id=?', (after,)).fetchone()
        params = [actor.split('@')[1]]
        where = 'project=?'
        if state:
            where += ' AND state=?'; params.append(state)
        if cursor_row:
            where += ' AND (created_at,id)>(?,?)'; params.extend(cursor_row)
        rows = self.conn.execute('SELECT id FROM ae_tasks WHERE '+where+' ORDER BY created_at,id LIMIT ?', (*params, limit+1)).fetchall()
        items = [self.task(r['id'], actor) for r in rows[:limit]]
        return {'tasks': items, 'cursor': items[-1]['id'] if items else after, 'has_more': len(rows)>limit,
                'consistency': 'Current state; filtering may change between pages.'}

    def brief(self, actor, session, after=None, source=None, limit=20, policy='all'):
        actor = normalize_address(actor)
        self.validate_policy(policy)
        if source is not None or after is not None:
            self.check_source(source)
        # Snapshot and delivered-event cursors are intentionally separate. The snapshot is
        # contextual, not a receipt for changes omitted from the bounded event page.
        context = self.context(actor, session, limit, budget=12000)
        snapshot = context.pop('snapshot_cursor')
        context.pop('cursor')
        context.pop('context_budget_bytes')
        context['sections'].pop('recent_events')
        context['truncated'].pop('recent_events')
        context['sections'].pop('following_work')
        context['truncated'].pop('following_work')
        if after is None:
            page = {'source': self.source, 'events': [], 'cursor': snapshot, 'has_more': False}
        else:
            page = self.events(actor, after, limit, source, policy=policy, max_bytes=10000)
        result = dict(context, **{k:v for k,v in page.items() if k!='source'}, snapshot_cursor=snapshot,
                      mode='bootstrap' if after is None else 'incremental', history_omitted=after is None,
                      policy=policy, budget_bytes=24000, acknowledged=False,
                      replay_hint='For earlier history, use the same source and after=0. Drain has_more even on empty pages.')
        # 12 KB context plus 10 KB events leaves room for the envelope and watch metadata.
        if wire_size(result)>24000:
            raise RuntimeError('Brief exceeded its serialization budget')
        return result

    def watch(self, actor, session, after=None, source=None, limit=20, policy='all', timeout=60, coalesce=30):
        from agent_inbox.ae import integer, text
        actor = normalize_address(actor)
        text(session, 'session', 128)
        integer(limit, 'limit', 1, 50)
        if after is None or source is None:
            raise ValidationError('cursor_required', 'Start with brief; watch requires source and after from the last delivered page')
        self.validate_policy(policy)
        for key, value, maximum in [('timeout', timeout, 60), ('coalesce', coalesce, 30)]:
            if not isinstance(value, (int,float)) or not math.isfinite(value) or not 0<=value<=maximum:
                raise ValidationError('invalid_wait', f'{key} must be between 0 and {maximum} seconds')
        deadline = time.monotonic()+timeout
        batch_started = None
        reason = 'timeout'
        while True:
            page = self.events(actor, after, limit, source, policy=policy, max_bytes=10000)
            now = time.monotonic()
            if page['events'] and batch_started is None:
                batch_started = now
            if any(self.urgent(event, actor) for event in page['events']):
                reason = 'urgent'; break
            if page['has_more']:
                reason = 'page_full' if page['events'] else 'scan_limit'; break
            if batch_started is not None and now-batch_started>=coalesce:
                reason = 'batch'; break
            if now>=deadline:
                break
            time.sleep(min(.1, deadline-now))
        result = self.brief(actor, session, after, source, limit, policy)
        result['wake_reason'] = reason
        result['changed'] = bool(result['events'])
        # A wake is a helper returning data. It does not imply the harness schedules a turn.
        return result
