"""Durable, account-bound append-only relay client (cloud-sync-spec v1.1)."""
import http.client
import json
import logging
import os
import random
import sqlite3
import socket
import tempfile
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path

from agent_inbox.cloud_protocol import (MAX_REQUEST, MAX_SEQ, canonical, keys, normalize_time,
    require, strict_loads, timestamp, validate_ancestry, validate_envelope)
from agent_inbox.db import get_connection
from agent_inbox.models import InboxError, utc_now_iso
from agent_inbox.service import InboxService

DEFAULT_URL = 'https://nates-software.com'
SYNC_INTERVAL = 30.0
LOCAL_WARNING = 'LOCAL SCOPE: reservations are machine-local; other machines may hold the same file or res:// resource.'
logger = logging.getLogger(__name__)


def config_path():
    custom = os.environ.get('AGENT_INBOX_CLOUD_CONFIG')
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / '.config' / 'agent-inbox' / 'cloud.json'


def load_config(path=None):
    try:
        config = strict_loads((path or config_path()).read_text())
    except FileNotFoundError:
        return None
    require(type(config) is dict, 'Invalid cloud config')
    return config


def enabled():
    try:
        return (load_config() or {}).get('enabled') is True
    except Exception:
        return False


def save_config(config, path=None):
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix='.cloud-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(config, f)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class CloudSyncError(InboxError):
    def __init__(self, message, *, code='cloud_sync_error', ids=(), retry_after=0, generation=None):
        super().__init__(code, message, status_code=502)
        self.ids = ids
        self.retry_after = retry_after
        self.generation = generation


def endpoint_origin(url):
    try:
        p = urllib.parse.urlsplit(url)
        require(not any(c.isspace() or ord(c) < 32 for c in url))
        require(p.scheme == 'https' and p.hostname and not p.username and not p.password
                and not p.query and not p.fragment and p.path in ('', '/') and '?' not in url and '#' not in url)
        host = p.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        return 'https://' + host + ((':' + str(p.port)) if p.port not in (None, 443) else '')
    except Exception:
        raise CloudSyncError('Cloud endpoint must be an HTTPS origin') from None


class CloudClient:
    def __init__(self, url, token):
        self.url = endpoint_origin(url)
        require(type(token) is str and token.strip() and '\n' not in token and '\r' not in token, 'Invalid cloud credential')
        self.token = token

    def _request(self, payload, path='/api/agent-mail', expected=200):
        p = urllib.parse.urlsplit(self.url)
        connection = http.client.HTTPSConnection(p.hostname, p.port, timeout=5)
        deadline = time.monotonic() + 30
        timer = None
        try:
            connection.connect()
            transport_socket = connection.sock
            def expire():
                try:
                    transport_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            connection.request('POST', path, body=canonical(payload).encode('utf-8'), headers={
                'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            if response.status == 401:
                raise CloudSyncError('Reauthentication required', code='reauth_required')
            chunks, size = [], 0
            while True:
                require(time.monotonic() < deadline, 'Request timeout')
                # read1 prevents a slow peer extending the total deadline per byte.
                transport_socket.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = response.read1(min(65536, MAX_REQUEST + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                require(size <= MAX_REQUEST, 'Response too large')
            if response.status != expected:
                code, ids, generation, retry = 'transport_error', [], None, 0
                try:
                    error_response = strict_loads(b''.join(chunks))
                    keys(error_response, {'protocol_version', 'error'})
                    require(type(error_response['protocol_version']) is int and error_response['protocol_version'] == 1)
                    error = error_response['error']
                    keys(error, {'code', 'message', 'message_ids', 'retry_after_seconds', 'generation'})
                    require(type(error['code']) is str and type(error['message']) is str)
                    require(error['generation'] is None or type(error['generation']) is str)
                    code = error['code']
                    ids = error['message_ids']
                    generation = error['generation']
                    require(error['retry_after_seconds'] is None or type(error['retry_after_seconds']) is int)
                    require((code == 'generation_mismatch') == (generation is not None))
                    retry = error['retry_after_seconds'] or 0
                    require(type(ids) is list and all(type(i) is str for i in ids))
                    require(type(retry) is int and retry >= 0)
                except Exception:
                    code, ids, generation, retry = 'transport_error', [], None, 0
                try:
                    retry = max(retry, int(response.getheader('Retry-After', '0')))
                except ValueError:
                    pass
                # A proxy or unexpected status must never quarantine mail.
                allowed = {400: {'invalid_request'}, 409: {'payload_conflict', 'thread_conflict', 'missing_ancestor', 'generation_mismatch'},
                           413: {'request_too_large'}, 422: {'invalid_message', 'unsupported_envelope_version'},
                           429: {'user_quota_exceeded', 'global_capacity', 'rate_limited'}, 503: {'temporarily_unavailable'}}
                if code not in allowed.get(response.status, set()):
                    code, ids = 'transport_error', []
                raise CloudSyncError(f'Cloud HTTP {response.status}: {code}', code=code,
                                     ids=ids, retry_after=retry, generation=generation)
            result = strict_loads(b''.join(chunks))
            require(type(result) is dict)
            return result
        except CloudSyncError:
            raise
        except Exception:
            raise CloudSyncError('Cloud transport or response error') from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def issue_device(self, device_id):
        return self._request({'protocol_version': 1, 'device_id': device_id}, '/api/agent-mail/devices', 201)

    def push(self, messages, generation):
        return self._request({'protocol_version': 1, 'action': 'push', 'generation': generation, 'messages': messages})

    def pull(self, after_seq, generation=None, limit=200):
        return self._request({'protocol_version': 1, 'action': 'pull', 'generation': generation, 'after_seq': after_seq, 'limit': limit})


@contextmanager
def transaction(conn):
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def state(conn):
    return dict(conn.execute('SELECT * FROM cloud_state WHERE id=1').fetchone())


def identity(result, user_id, generation=None):
    require(type(result.get('protocol_version')) is int and result['protocol_version'] == 1, 'Invalid protocol')
    require(type(result.get('user_id')) is str and bool(result['user_id']) and result['user_id'] == user_id, 'Cloud identity mismatch')
    require(type(result.get('generation')) is str and bool(result['generation']), 'Invalid generation')
    if generation is not None:
        require(result['generation'] == generation, 'Cloud generation mismatch')


def validate_pull(result, after_seq, user_id=None, generation=None, limit=200):
    keys(result, {'protocol_version', 'user_id', 'generation', 'messages', 'last_seq', 'has_more'})
    identity(result, user_id if user_id is not None else result['user_id'], generation)
    require(type(result['messages']) is list and len(result['messages']) <= limit)
    require(type(result['last_seq']) is int and after_seq <= result['last_seq'] <= MAX_SEQ)
    require(type(result['has_more']) is bool)
    require(len(canonical(result).encode('utf-8')) <= MAX_REQUEST)
    previous, seen = after_seq, set()
    for item in result['messages']:
        keys(item, {'seq', 'received_at', 'envelope'})
        require(type(item['seq']) is int and previous < item['seq'] <= MAX_SEQ, 'Invalid sequence')
        previous = item['seq']
        timestamp(item['received_at'])
        validate_envelope(item['envelope'])
        require(item['envelope']['message_id'] not in seen, 'Duplicate page ID')
        seen.add(item['envelope']['message_id'])
    require(result['last_seq'] == previous and (result['messages'] or not result['has_more']), 'Invalid cursor')


def reset_generation(conn, generation):
    require(type(generation) is str and bool(generation), 'Invalid generation')
    with transaction(conn):
        conn.execute('UPDATE cloud_state SET generation=?,last_pulled_seq=0,replaying=1,push_retry_at=0,pull_retry_at=0 WHERE id=1', (generation,))
        conn.execute("UPDATE cloud_envelopes SET state=CASE WHEN state='permanently_rejected' THEN state ELSE 'pending' END, ack_generation=NULL,ack_seq=NULL,ack_at=NULL,retry_at=0")
        conn.execute('UPDATE emails SET cloud_synced_at=NULL')


def login(conn, endpoint, secret):
    endpoint = endpoint_origin(endpoint)
    old = state(conn)
    if old['endpoint'] is not None and old['endpoint'] != endpoint:
        raise CloudSyncError('Database is bound to another endpoint; use a separate database')
    config = load_config() or {}
    device = config.get('device_id') or 'dev_' + uuid.uuid4().hex
    issued = CloudClient(endpoint, secret).issue_device(device)
    keys(issued, {'protocol_version', 'user_id', 'device_id', 'device_token', 'expires_at'})
    require(type(issued['protocol_version']) is int and issued['protocol_version'] == 1 and issued['device_id'] == device)
    timestamp(issued['expires_at'])
    cloud = CloudClient(endpoint, issued['device_token'])
    page = cloud.pull(0, limit=1)
    validate_pull(page, 0, issued['user_id'], limit=1)
    with transaction(conn):
        bound = state(conn)
        require(bound['endpoint'] is None or (bound['endpoint'], bound['user_id']) == (endpoint, issued['user_id']),
                'Database is bound to another account; use a separate database')
        conn.execute("UPDATE cloud_state SET endpoint=?,user_id=?,auth_state='authenticated',push_retry_at=0,pull_retry_at=0 WHERE id=1", (endpoint, issued['user_id']))
        conn.execute("UPDATE cloud_envelopes SET retry_at=0 WHERE reason='reauth_required'")
    if old['generation'] != page['generation']:
        reset_generation(conn, page['generation'])
    save_config({'endpoint': endpoint, 'device_id': device, 'device_token': issued['device_token'], 'enabled': True})


def sync_status(conn):
    s = state(conn)
    counts = {k: 0 for k in ('pending', 'retryable', 'acknowledged', 'permanently_rejected')}
    for row in conn.execute("SELECT COALESCE(c.state,'pending') AS state,COUNT(*) AS n FROM emails e LEFT JOIN cloud_envelopes c ON c.message_id=e.id GROUP BY COALESCE(c.state,'pending')"):
        counts[row['state']] = row['n']
    return {**s, 'enabled': enabled(), 'counts': counts,
            'messages': [dict(r) for r in conn.execute("SELECT message_id,state,reason,attempts,retry_at,ack_generation,ack_seq,ack_at FROM cloud_envelopes WHERE state!='acknowledged' ORDER BY message_id")],
            'unsynced_count': sum(v for k, v in counts.items() if k != 'acknowledged')}


def replay(conn):
    with transaction(conn):
        require(state(conn)['endpoint'] is not None, 'Login required')
        conn.execute('UPDATE cloud_state SET last_pulled_seq=0,replaying=1,pull_retry_at=0 WHERE id=1')


def retry_message(conn, message_id):
    conn.execute("UPDATE cloud_envelopes SET state='pending',reason=NULL,retry_at=0 WHERE message_id=? AND state!='acknowledged'", (message_id,))
    conn.execute('UPDATE cloud_state SET push_retry_at=0 WHERE id=1')


def export_envelope(conn, message_id, device):
    frozen = conn.execute('SELECT envelope_json FROM cloud_envelopes WHERE message_id=?', (message_id,)).fetchone()
    if frozen and frozen[0] is not None:
        return strict_loads(frozen[0])
    row = conn.execute('''SELECT e.*, i.local_part || '@' || p.slug AS sender FROM emails e
        JOIN inboxes i ON i.id=e.from_inbox_id JOIN projects p ON p.id=i.project_id WHERE e.id=?''', (message_id,)).fetchone()
    require(row is not None, 'blocked_by_ancestor')
    metadata = conn.execute('SELECT metadata_json FROM cloud_threads WHERE thread_id=?', (row['thread_id'],)).fetchone()
    if metadata:
        thread = strict_loads(metadata[0])
    else:
        root = conn.execute('''SELECT e.*,p.slug FROM emails e JOIN inboxes i ON i.id=e.from_inbox_id
            JOIN projects p ON p.id=i.project_id WHERE e.thread_id=? AND reply_to_email_id IS NULL''', (row['thread_id'],)).fetchall()
        require(len(root) == 1, 'missing thread root')
        root = root[0]
        thread = dict(thread_id=row['thread_id'], root_message_id=root['id'], home_project=root['slug'].lower(), subject=root['subject'], created_at=normalize_time(root['sent_at']))
    recipients = {'to': [], 'cc': []}
    for r in conn.execute('''SELECT r.kind,i.local_part || '@' || p.slug AS address FROM email_recipients r
        JOIN inboxes i ON i.id=r.inbox_id JOIN projects p ON p.id=i.project_id WHERE email_id=? ORDER BY position''', (message_id,)):
        recipients[r['kind']].append(r['address'].lower())
    return dict(envelope_version=1, message_id=message_id, thread=thread, sender=row['sender'].lower(),
                sender_session=row['sender_session'], recipients=recipients, subject=row['subject'],
                body_markdown=row['body_markdown'], reply_to_email_id=row['reply_to_email_id'],
                references=[r[0] for r in conn.execute('SELECT referenced_email_id FROM email_references WHERE email_id=? ORDER BY position', (message_id,))],
                sent_at=normalize_time(row['sent_at']), origin_device=device)


def freeze(conn, message_id, device):
    e = export_envelope(conn, message_id, device)
    payload = canonical(e)
    payload.encode('utf-8')
    conn.execute('INSERT INTO cloud_envelopes(message_id,envelope_json) VALUES (?,?) ON CONFLICT(message_id) DO UPDATE SET envelope_json=COALESCE(cloud_envelopes.envelope_json,excluded.envelope_json)', (message_id, payload))
    validate_envelope(e)
    # Normalize legacy projections once, before frozen canonical identity is used.
    conn.execute('UPDATE emails SET sent_at=? WHERE id=?', (e['sent_at'], message_id))
    thread_row = conn.execute('SELECT * FROM threads WHERE id=?', (e['thread']['thread_id'],)).fetchone()
    require(conn.execute('SELECT slug FROM projects WHERE id=?', (thread_row['home_project_id'],)).fetchone()[0].lower() == e['thread']['home_project'], 'thread_conflict')
    require(thread_row['subject'] == e['thread']['subject'] and normalize_time(thread_row['created_at']) == e['thread']['created_at'], 'thread_conflict')
    for member in conn.execute('SELECT inbox_id,joined_at FROM thread_inboxes WHERE thread_id=?', (e['thread']['thread_id'],)).fetchall():
        conn.execute('UPDATE thread_inboxes SET joined_at=? WHERE thread_id=? AND inbox_id=?', (normalize_time(member['joined_at']),e['thread']['thread_id'],member['inbox_id']))
    for member in conn.execute('SELECT id,sent_at FROM emails WHERE thread_id=?', (e['thread']['thread_id'],)).fetchall():
        try:
            normalized = normalize_time(member['sent_at'])
        except (ValueError, TypeError):
            continue
        conn.execute('UPDATE emails SET sent_at=? WHERE id=?', (normalized,member['id']))
    conn.execute('UPDATE threads SET created_at=?,last_email_at=(SELECT MAX(sent_at) FROM emails WHERE thread_id=threads.id) WHERE id=?', (e['thread']['created_at'],e['thread']['thread_id']))
    metadata = canonical(e['thread'])
    old = conn.execute('SELECT metadata_json FROM cloud_threads WHERE thread_id=?', (e['thread']['thread_id'],)).fetchone()
    require(old is None or old[0] == metadata, 'thread_conflict')
    if old is None:
        conn.execute('INSERT INTO cloud_threads VALUES (?,?)', (e['thread']['thread_id'], metadata))
    return e, payload


class SyncEngine:
    def __init__(self, client, device_id=None, cancelled=None):
        self.client = client
        self.device_id = device_id
        self.cancelled = cancelled or (lambda: False)

    def sync_once(self, conn):
        s = state(conn)
        require(s['endpoint'] is not None and s['user_id'] is not None, 'Cloud login required before sync')
        require(endpoint_origin(self.client.url) == s['endpoint'], 'Cloud endpoint mismatch')
        if s['auth_state'] != 'authenticated':
            return
        # Pull first also ensures reset replay completes before recovery uploads.
        for direction in ('pull', 'push'):
            if self.cancelled():
                return
            s = state(conn)
            if s['auth_state'] != 'authenticated' or s[direction + '_retry_at'] > time.time():
                continue
            if direction == 'push' and (s['replaying'] or not s['generation']):
                continue
            try:
                getattr(self, '_' + direction)(conn)
                conn.execute(f'UPDATE cloud_state SET {direction}_failures=0,{direction}_retry_at=0 WHERE id=1')
            except Exception as exc:
                self._failure(conn, direction, exc)

    def _failure(self, conn, direction, exc):
        code = exc.code if isinstance(exc, CloudSyncError) else str(exc) if isinstance(exc, ValueError) else 'transport_or_import_error'
        if code == 'generation_mismatch':
            reset_generation(conn, exc.generation)
            return
        s = state(conn)
        n = s[direction + '_failures']
        delay = min(900, 30 * 2 ** min(n, 5) * random.uniform(1, 1.2))
        deadline = time.time() + max(delay, getattr(exc, 'retry_after', 0))
        with transaction(conn):
            conn.execute(f'UPDATE cloud_state SET {direction}_retry_at=?,{direction}_failures={direction}_failures+1,last_error=? WHERE id=1', (deadline, code))
            if code == 'reauth_required':
                conn.execute("UPDATE cloud_state SET auth_state='reauth-required' WHERE id=1")
            if direction == 'push' or code == 'reauth_required':
                conn.execute("UPDATE cloud_envelopes SET state='retryable',reason=?,retry_at=? WHERE state IN ('pending','retryable') AND COALESCE(reason,'')!='blocked_by_ancestor'", (code, deadline))

    def _pull(self, conn):
        # Bounded below 120 attempts/min; remaining pages drain next tick.
        for _ in range(50):
            if self.cancelled():
                return
            s = state(conn)
            result = self.client.pull(s['last_pulled_seq'], generation=s['generation'])
            # A changed 200 generation is replayed from zero, never imported at an old cursor.
            identity(result, s['user_id'])
            if result['generation'] != s['generation']:
                reset_generation(conn, result['generation'])
                continue
            validate_pull(result, s['last_pulled_seq'], s['user_id'], s['generation'])
            with transaction(conn):
                require(state(conn)['generation'] == s['generation'] and state(conn)['last_pulled_seq'] == s['last_pulled_seq'], 'Concurrent replay')
                for item in result['messages']:
                    self._materialize(conn, item['envelope'], item['seq'], s['generation'])
                conn.execute('UPDATE cloud_state SET last_pulled_seq=?,last_pull_at=?,replaying=? WHERE id=1', (result['last_seq'], utc_now_iso(), int(result['has_more'])))
            if not result['has_more']:
                return

    def _ack(self, conn, message_id, payload, generation, seq):
        now = utc_now_iso()
        changed = conn.execute("UPDATE cloud_envelopes SET state='acknowledged',reason=NULL,retry_at=0,ack_generation=?,ack_seq=?,ack_at=? WHERE message_id=? AND envelope_json=?", (generation, seq, now, message_id, payload)).rowcount
        require(changed == 1, 'Acknowledgment snapshot conflict')
        conn.execute('UPDATE emails SET cloud_synced_at=? WHERE id=?', (now, message_id))

    @staticmethod
    def _dependency_stamp(conn, e):
        dependencies = []
        for ref in e['references']:
            row = conn.execute('SELECT state,ack_generation,ack_seq FROM cloud_envelopes WHERE message_id=?', (ref,)).fetchone()
            dependencies.append([ref, list(row) if row else None])
        return canonical(dependencies)

    def _block(self, conn, mid, e):
        conn.execute("UPDATE cloud_envelopes SET state='retryable',reason='blocked_by_ancestor',retry_at=0,dependency_stamp=? WHERE message_id=?", (self._dependency_stamp(conn,e),mid))
        conn.execute("UPDATE cloud_state SET last_error='blocked_by_ancestor' WHERE id=1")

    def _push(self, conn):
        s = state(conn)
        snapshots = {}
        with transaction(conn):
            rows = conn.execute("SELECT e.id FROM emails e LEFT JOIN cloud_envelopes c ON c.message_id=e.id WHERE c.state IS NULL OR c.state IN ('pending','retryable') ORDER BY e.delivery_id").fetchall()
            for row in rows:
                mid = row['id']
                record = conn.execute('SELECT * FROM cloud_envelopes WHERE message_id=?', (mid,)).fetchone()
                if record and record['retry_at'] > time.time():
                    continue
                try:
                    candidate = export_envelope(conn, mid, self.device_id)
                    if record and record['reason'] == 'blocked_by_ancestor' and record['dependency_stamp'] == self._dependency_stamp(conn,candidate):
                        continue
                    if not (record and record['envelope_json']) and not conn.execute('SELECT 1 FROM cloud_threads WHERE thread_id=?', (candidate['thread']['thread_id'],)).fetchone() and not conn.execute('SELECT 1 FROM project_mappings WHERE slug=?', (candidate['thread']['home_project'],)).fetchone():
                        conn.execute("INSERT INTO cloud_envelopes(message_id,state,reason) VALUES (?,'retryable','unresolved_project_mapping') ON CONFLICT(message_id) DO UPDATE SET state='retryable',reason='unresolved_project_mapping'", (mid,))
                        conn.execute("UPDATE cloud_state SET last_error='unresolved_project_mapping' WHERE id=1")
                        continue
                    e, payload = freeze(conn, mid, self.device_id)
                    snapshots[mid] = (e, payload)
                except Exception as exc:
                    conn.execute("INSERT INTO cloud_envelopes(message_id,state,reason) VALUES (?,'permanently_rejected',?) ON CONFLICT(message_id) DO UPDATE SET state='permanently_rejected',reason=excluded.reason", (mid, str(exc) if isinstance(exc, ValueError) else 'invalid_message'))
                    conn.execute("UPDATE cloud_state SET last_error='invalid_message' WHERE id=1")
        ordered, visiting, visited = [], set(), set()
        def visit(mid):
            if mid in visited:
                return
            if mid in visiting:
                raise ValueError('blocked_by_ancestor')
            visiting.add(mid)
            e, payload = snapshots[mid]
            try:
                for ref in e['references']:
                    if ref in snapshots:
                        visit(ref)
                    r = conn.execute('SELECT state FROM cloud_envelopes WHERE message_id=?', (ref,)).fetchone()
                    require(r is not None and (r[0] == 'acknowledged' or ref in visited), 'blocked_by_ancestor')
                validate_ancestry(e, lambda ref: self._lookup(conn, ref))
                ordered.append((mid, e, payload))
                visited.add(mid)
            except ValueError:
                self._block(conn, mid, e)
            finally:
                visiting.discard(mid)
        for mid in snapshots:
            visit(mid)
        # Rejection/rebatch retries are bounded; valid neighbors progress this tick.
        max_count, attempts = 100, 0
        while ordered and attempts < 50:
            if self.cancelled():
                return
            batch = []
            for item in ordered[:max_count]:
                candidate = batch + [item]
                request = dict(protocol_version=1, action='push', generation=s['generation'], messages=[x[1] for x in candidate])
                if len(canonical(request).encode('utf-8')) > MAX_REQUEST:
                    break
                batch = candidate
            require(bool(batch), 'Request cannot be packed')
            attempts += 1
            ids = [x[0] for x in batch]
            conn.executemany('UPDATE cloud_envelopes SET attempts=attempts+1 WHERE message_id=?', [(i,) for i in ids])
            try:
                result = self.client.push([x[1] for x in batch], generation=s['generation'])
                keys(result, {'protocol_version', 'user_id', 'generation', 'results'})
                identity(result, s['user_id'], s['generation'])
                require(type(result['results']) is list and len(result['results']) == len(batch), 'Invalid acknowledgment coverage')
                for item, ack in zip(batch, result['results']):
                    keys(ack, {'message_id', 'status', 'seq'})
                    require(ack['message_id'] == item[0] and ack['status'] in ('accepted', 'duplicate') and type(ack['seq']) is int and 0 < ack['seq'] <= MAX_SEQ, 'Invalid acknowledgment')
                require(len({ack['seq'] for ack in result['results']}) == len(batch), 'Duplicate acknowledgment sequence')
                with transaction(conn):
                    require(state(conn)['generation'] == s['generation'], 'Concurrent generation change')
                    for item, ack in zip(batch, result['results']):
                        self._ack(conn, item[0], item[2], s['generation'], ack['seq'])
                    conn.execute('UPDATE cloud_state SET last_push_at=? WHERE id=1', (utc_now_iso(),))
                ordered = ordered[len(batch):]
            except CloudSyncError as exc:
                require(type(exc.ids) in (list, tuple) and set(exc.ids) <= set(ids), 'Invalid error IDs')
                if exc.code == 'request_too_large' and len(batch) > 1:
                    max_count = max(1, len(batch) // 2)
                    continue
                if exc.code in ('payload_conflict', 'thread_conflict', 'invalid_message', 'unsupported_envelope_version', 'missing_ancestor') and exc.ids:
                    rejected = set(exc.ids)
                    for mid in rejected:
                        conn.execute('UPDATE cloud_envelopes SET state=?,reason=? WHERE message_id=?', ('retryable' if exc.code == 'missing_ancestor' else 'permanently_rejected', 'blocked_by_ancestor' if exc.code == 'missing_ancestor' else exc.code, mid))
                    if exc.code == 'missing_ancestor':
                        for item in batch:
                            if item[0] in rejected:
                                for ancestor in item[1]['references']:
                                    conn.execute("UPDATE cloud_envelopes SET state='pending',reason=NULL,retry_at=0,ack_generation=NULL,ack_seq=NULL,ack_at=NULL WHERE message_id=? AND state='acknowledged'", (ancestor,))
                                    conn.execute("UPDATE emails SET cloud_synced_at=NULL WHERE id=? AND EXISTS (SELECT 1 FROM cloud_envelopes WHERE message_id=? AND state='pending')", (ancestor,ancestor))
                                self._block(conn, item[0], item[1])
                    conn.execute('UPDATE cloud_state SET last_error=? WHERE id=1', (exc.code,))
                    keep = []
                    for item in ordered:
                        if item[0] in rejected:
                            continue
                        if rejected.intersection(item[1]['references']):
                            rejected.add(item[0])
                            self._block(conn,item[0],item[1])
                        else:
                            keep.append(item)
                    ordered = keep
                    continue
                raise

    @staticmethod
    def _lookup(conn, mid):
        r = conn.execute('SELECT envelope_json FROM cloud_envelopes WHERE message_id=?', (mid,)).fetchone()
        return strict_loads(r[0]) if r and r[0] else None

    def _materialize(self, conn, e, seq, generation):
        payload = validate_envelope(e)
        mid, t = e['message_id'], e['thread']
        existing = conn.execute('SELECT id FROM emails WHERE id=?', (mid,)).fetchone()
        if existing:
            _, local = freeze(conn, mid, self.device_id)
            require(local == payload, 'payload_conflict')
            self._ack(conn, mid, payload, generation, seq)
            return
        validate_ancestry(e, lambda ref: self._lookup(conn, ref))
        token = 'cloud-import:v1:' + mid
        require(conn.execute('SELECT 1 FROM emails WHERE client_token=?', (token,)).fetchone() is None, 'cloud import token conflict')
        service = InboxService(conn)
        def inbox(address):
            local, project = address.split('@')
            pid = service.ensure_project(project)
            stored = conn.execute('SELECT slug FROM projects WHERE id=?', (pid,)).fetchone()[0]
            require(stored == project, 'project mapping collision')
            conn.execute('INSERT INTO inboxes(project_id,local_part,created_at) VALUES (?,?,?) ON CONFLICT(project_id,local_part) DO NOTHING', (pid, local, e['sent_at']))
            return service._get_inbox_id(address)
        sender = inbox(e['sender'])
        project = service.ensure_project(t['home_project'])
        old = conn.execute('SELECT * FROM threads WHERE id=?', (t['thread_id'],)).fetchone()
        if old:
            metadata = conn.execute('SELECT metadata_json FROM cloud_threads WHERE thread_id=?', (t['thread_id'],)).fetchone()
            if metadata is None:
                root = conn.execute('SELECT id FROM emails WHERE thread_id=? AND reply_to_email_id IS NULL', (t['thread_id'],)).fetchone()
                require(root is not None, 'thread_conflict')
                freeze(conn, root[0], self.device_id)
                metadata = conn.execute('SELECT metadata_json FROM cloud_threads WHERE thread_id=?', (t['thread_id'],)).fetchone()
            require(metadata[0] == canonical(t), 'thread_conflict')
            require(old['home_project_id'] == project and old['subject'] == t['subject'] and normalize_time(old['created_at']) == t['created_at'], 'thread_conflict')
        else:
            conn.execute('INSERT INTO threads(id,home_project_id,subject,created_at,last_email_at) VALUES (?,?,?,?,?)', (t['thread_id'], project, t['subject'], t['created_at'], t['created_at']))
            conn.execute('INSERT INTO cloud_threads VALUES (?,?)', (t['thread_id'], canonical(t)))
        conn.execute('''INSERT INTO emails(id,thread_id,from_inbox_id,subject,body_markdown,reply_to_email_id,client_token,sent_at,sender_session)
            VALUES (?,?,?,?,?,?,?,?,?)''', (mid,t['thread_id'],sender,e['subject'],e['body_markdown'],e['reply_to_email_id'],token,e['sent_at'],e['sender_session']))
        conn.execute('INSERT INTO cloud_envelopes(message_id,envelope_json) VALUES (?,?)', (mid,payload))
        participants, pos = {sender}, 0
        for kind in ('to', 'cc'):
            for address in e['recipients'][kind]:
                iid = inbox(address)
                participants.add(iid)
                conn.execute('INSERT INTO email_recipients(email_id,inbox_id,kind,position) VALUES (?,?,?,?)', (mid,iid,kind,pos))
                pos += 1
        for pos, ref in enumerate(e['references']):
            conn.execute('INSERT INTO email_references VALUES (?,?,?)', (mid,ref,pos))
        for iid in participants:
            conn.execute('''INSERT INTO thread_inboxes VALUES (?,?,?) ON CONFLICT(thread_id,inbox_id)
                DO UPDATE SET joined_at=MIN(joined_at,excluded.joined_at)''', (t['thread_id'],iid,e['sent_at']))
        conn.execute('UPDATE threads SET last_email_at=MAX(last_email_at,?) WHERE id=?', (e['sent_at'],t['thread_id']))
        self._ack(conn, mid, payload, generation, seq)


class SyncWorker(threading.Thread):
    """One worker per serve DB; no sync failure escapes into local operations."""
    def __init__(self, db_path):
        super().__init__(name='agent-inbox-cloud-sync', daemon=True)
        self.db_path = db_path
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def run(self):
        import fcntl
        conn = None
        lock = None
        try:
            lock_path = Path(str(Path(self.db_path).resolve()) + '.cloud.lock')
            lock = lock_path.open('a')
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while not self.stopping.is_set():
                self.wake.clear()
                try:
                    config = load_config()
                    if not config or config.get('enabled') is not True:
                        return
                    if conn is None:
                        conn = get_connection(self.db_path)
                    SyncEngine(CloudClient(config['endpoint'], config['device_token']), config['device_id'],
                               cancelled=lambda: self.stopping.is_set() or load_config() != config).sync_once(conn)
                except Exception as exc:
                    logger.debug('Cloud sync failed (%s)', type(exc).__name__)
                self.wake.wait(SYNC_INTERVAL)
        finally:
            if conn is not None:
                conn.close()
            if lock is not None:
                lock.close()


def repository_identity(value):
    """Canonical clone identity; credentials and transport do not distinguish repos."""
    import re
    value = value.strip()
    if re.match(r'^[^/@]+@[^/:]+:', value):
        host, path = value.split('@', 1)[1].split(':', 1)
        value = 'https://' + host + '/' + path
    p = urllib.parse.urlsplit(value)
    if p.hostname:
        return p.hostname.lower() + (':' + str(p.port) if p.port and p.port not in (22,443,80) else '') + '/' + p.path.strip('/').removesuffix('.git')
    # A bare token is a literal identity, not a filesystem path: resolving it
    # against the CWD minted identities like /Users/x/<slug> for checkouts that
    # never existed. Only path-shaped or actually-present values canonicalize.
    path = Path(value).expanduser()
    if value.startswith(('/', '.', '~')) or path.exists():
        return str(path.resolve())
    return value


def map_project(conn, repo, slug):
    import re
    from agent_inbox.cloud_protocol import SLUG
    require(type(slug) is str and re.fullmatch(SLUG, slug), 'Explicit canonical slug required')
    repo = repository_identity(repo)
    with transaction(conn):
        existing = conn.execute('SELECT slug FROM project_mappings WHERE repo_identity=?', (repo,)).fetchone()
        # One identity keeps one slug forever, but many identities (clones of
        # the same repository, per the spec) may legitimately share a slug.
        require(existing is None or existing[0] == slug, 'Repository mapping cannot be changed')
        conn.execute('INSERT INTO project_mappings VALUES (?,?) ON CONFLICT(repo_identity) DO NOTHING', (repo, slug))
        conn.execute("UPDATE cloud_envelopes SET state='pending',reason=NULL,retry_at=0 WHERE state='retryable' AND reason='unresolved_project_mapping'")
        conn.execute('UPDATE cloud_state SET push_retry_at=0 WHERE id=1')
