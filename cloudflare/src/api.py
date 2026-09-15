"""Workspace membership, scoped credentials, and authorization for the shared API."""
import hashlib
import json
import re
import secrets
import time
import uuid
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit, unquote

from agent_inbox import __version__
from agent_inbox.models import InboxError, ValidationError, normalize_slug, normalize_address, utc_now_iso
from agent_inbox.service import InboxService
from transport import Handler

SCHEMA = '''
CREATE TABLE IF NOT EXISTS hosted_tokens (
 id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
 project TEXT NOT NULL, family TEXT NOT NULL, label TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at REAL NOT NULL, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS hosted_requests (
 principal TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status INTEGER NOT NULL, response TEXT NOT NULL, PRIMARY KEY(principal,key)
);
'''


class Forbidden(InboxError):
    def __init__(self, code, message):
        super().__init__(code, message, 403)


def deny(message='This credential does not allow that operation'):
    raise Forbidden('forbidden', message)


def family_address(address, principal):
    address = normalize_address(address)
    local, project = address.split('@')
    family = principal['family']
    return project == principal['project'] and (local == family or re.fullmatch(re.escape(family) + r'-[2-9]\d*', local) is not None or re.fullmatch(re.escape(family) + r'-1\d+', local) is not None)


def authenticate(db, email, token, members):
    if email and email in members:
        return {'kind': 'human', 'id': email, 'email': email}
    if token and token.startswith('ain_') and len(token) < 200:
        row = db.execute('SELECT * FROM hosted_tokens WHERE digest=? AND revoked_at IS NULL AND expires_at>?',
            (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if row and row['owner'] in members:
            return {**row, 'kind': 'agent'}
    return None


def token_metadata(row):
    return {k: row[k] for k in ('id','owner','project','family','label','created_at','expires_at','revoked_at')}


def management(db, principal, method, path, body, env, peer):
    if path == '/v1/hosted/health' and method == 'GET':
        db.execute('SELECT 1')
        return 200, {'status': 'ok', 'db': 'ok', 'service': 'agent-inboxes', 'version': __version__,
            'hosting': 'Cloudflare', 'workspace': env.WORKSPACE, 'server_time': utc_now_iso(),
            'client_ip': peer, 'ae_transport': {'kind': 'http', 'connected': True, 'broker_required': False}}
    if principal['kind'] != 'human':
        deny('Workspace settings require a human login')
    if path == '/v1/hosted/me' and method == 'GET':
        return 200, {'email': principal['email'], 'workspace': env.WORKSPACE,
            'members': [x.strip() for x in env.MEMBERS.split(',')], 'human_agent': principal['email'].split('@')[0]}
    if path == '/v1/hosted/tokens' and method == 'GET':
        rows = db.execute('SELECT * FROM hosted_tokens WHERE owner=? ORDER BY created_at DESC', (principal['email'],))
        return 200, {'tokens': [token_metadata(row) for row in rows]}
    if path == '/v1/hosted/tokens' and method == 'POST':
        project, family = normalize_slug(body.get('project', '')), normalize_slug(body.get('family', ''))
        if not project or not family or len(project) > 64 or len(family) > 48:
            raise ValidationError('invalid_scope', 'A project and agent name are required')
        if re.search(r'-\d+$', family):
            raise ValidationError('invalid_agent', 'Use a base agent name without a numeric slot suffix')
        human_names = {email.strip().split('@')[0] for email in env.MEMBERS.split(',')}
        if family in human_names:
            deny('That name belongs to a workspace member')
        old = db.execute('SELECT * FROM hosted_tokens WHERE project=? AND family=? AND revoked_at IS NULL AND expires_at>?', (project, family, time.time())).fetchone()
        if old:
            raise ValidationError('agent_in_use', 'This agent already has a key. Revoke it first, or choose another name.')
        if db.execute('SELECT COUNT(*) FROM hosted_tokens WHERE revoked_at IS NULL AND expires_at>?', (time.time(),)).fetchone()[0] >= 100:
            deny('Revoke an unused key before adding another agent')
        days = body.get('days', 90)
        if type(days) is not int or not 1 <= days <= 365:
            raise ValidationError('invalid_expiry', 'Key lifetime must be 1–365 days')
        label = body.get('label', family)
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ValidationError('invalid_label', 'Use a name under 100 characters')
        token = 'ain_' + secrets.token_urlsafe(32)
        row = dict(id='key_' + uuid.uuid4().hex, digest=hashlib.sha256(token.encode()).hexdigest(),
            owner=principal['email'], project=project, family=family, label=label.strip(),
            created_at=utc_now_iso(), expires_at=time.time()+days*86400, revoked_at=None)
        db.execute('INSERT INTO hosted_tokens VALUES (?,?,?,?,?,?,?,?,NULL)', tuple(row[k] for k in ('id','digest','owner','project','family','label','created_at','expires_at')))
        service = InboxService(db)
        service.ensure_inbox(f'{family}@{project}', role='agent')
        for name in human_names:
            service.ensure_inbox(f'{name}@{project}', display_name=name.title() + ' (human)', role='agent')
        return 201, {**token_metadata(row), 'token': token, 'address': f'{family}@{project}'}
    match = re.fullmatch(r'/v1/hosted/tokens/(key_[a-f0-9]{32})/revoke', path)
    if match and method == 'POST':
        updated = db.execute('UPDATE hosted_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=? AND owner=?', (utc_now_iso(), match[1], principal['email']))
        if not updated.rowcount:
            deny('Key not found for this account')
        return 200, {'revoked': True}
    return 404, {'error': {'code': 'not_found', 'message': 'Unknown workspace route'}}


def authorize(db, principal, method, path, headers, body):
    url = urlsplit(path)
    parts = [unquote(p) for p in url.path.strip('/').split('/')]
    query = parse_qs(url.query, keep_blank_values=True)
    actor_fields = ('from', 'holder', 'inbox', 'actor')
    body = dict(body or {})
    headers = {k: v for k, v in headers.items() if k.lower() in ('x-agent-address','x-agent-session','x-repo-key','idempotency-key')}
    headers = {k.lower(): v for k, v in headers.items()}
    is_human = principal['kind'] == 'human'
    if method not in ('GET', 'POST', 'PUT') or parts[:1] != ['v1']:
        deny()
    if is_human:
        # Both perspectives remain observations: browsing as an agent cannot
        # acknowledge its work, refresh its name lease, or impersonate it.
        headers.pop('x-agent-address', None)
        headers.pop('x-agent-session', None)
        if method == 'GET':
            allowed = (parts[:2] in (['v1','projects'], ['v1','inboxes'], ['v1','announcements'], ['v1','reservations']))
            if not allowed or 'wait' in parts or 'watch' in parts:
                deny()
            query['observe'] = ['true']
        elif method == 'POST' and (url.path in ('/v1/emails','/v1/announcements') or re.fullmatch(r'/v1/emails/[^/]+/reply', url.path)):
            sender = normalize_address(body.get('from'))
            if sender.split('@')[0] != principal['email'].split('@')[0]:
                deny('Send using your own human identity')
            project = sender.split('@')[1]
            if not db.execute('SELECT 1 FROM projects WHERE slug=?', (project,)).fetchone():
                deny('Choose an existing project')
            InboxService(db).ensure_inbox(sender, role='agent')
            headers['x-agent-session'] = 'human-' + hashlib.sha256(principal['email'].encode()).hexdigest()[:16]
        else:
            deny('Human views cannot change agent receipts, tasks, or reservations')
    else:
        project = principal['project']
        for field in actor_fields:
            for value in ([body[field]] if field in body else []) + query.get(field, []):
                if not family_address(value, principal):
                    deny('Agent identity does not match this key')
        if headers.get('x-agent-address') and not family_address(headers['x-agent-address'], principal):
            deny('Agent header does not match this key')
        if len(parts) >= 3 and parts[1] == 'inboxes' and not family_address(parts[2], principal):
            deny('This key only opens its own agent inbox')
        if len(parts) >= 3 and parts[1] == 'projects' and parts[2] != project:
            deny('Project is outside this key’s scope')
        if body.get('project', project) != project or any(p != project for p in query.get('project', [])):
            deny('Project is outside this key’s scope')
        if url.path in ('/v1/inboxes', '/v1/leases/lookup'):
            query['project'] = [project]
        if url.path == '/v1/projects':
            # The caller is returned only its project in dispatch below.
            pass
        if url.path == '/v1/reservations':
            url = url._replace(path=f'/v1/projects/{quote(project)}/reservations')
        if parts[1] not in ('inboxes','projects','emails','announcements','reservations','leases','ae'):
            deny()
        if url.path == '/v1/leases/claim' and body.get('family') != principal['family']:
            deny('Claim the agent family assigned to this key')
        if url.path == '/v1/leases/release' and not family_address(f"{body.get('agent')}@{project}", principal):
            deny()
        if method == 'PUT' and body.get('role', 'agent') != 'agent':
            deny('Agent keys cannot register service identities')
        if body.get('all_projects'):
            deny('Agent announcements stay within their project')
        if body.get('force'):
            deny('Hosted keys cannot force another agent’s reservation')
        if method != 'GET' and not headers.get('x-agent-session'):
            raise ValidationError('missing_session', 'X-Agent-Session is required')
        # The session in a body/query must agree with the authenticated request.
        for session in ([body['session']] if body.get('session') else []) + query.get('session', []):
            if session != headers.get('x-agent-session'):
                deny('Session must match X-Agent-Session')
        raw_session = headers.get('x-agent-session')
        if raw_session:
            scoped_session = 's-' + hashlib.sha256((principal['id'] + '/' + raw_session).encode()).hexdigest()[:40]
            headers['x-agent-session'] = scoped_session
            if 'session' in body:
                body['session'] = scoped_session
            if 'session' in query:
                query['session'] = [scoped_session]
    # Messages cannot cross project authorization boundaries. Threads therefore
    # also remain in one project, including implicit reply-all recipients.
    if method == 'POST' and parts[1] in ('emails','announcements') and body.get('from'):
        project = normalize_address(body['from']).split('@')[1]
        for field in ('to','cc'):
            values = body.get(field) or []
            if not isinstance(values, list):
                raise ValidationError('invalid_recipients', 'Recipients must be lists')
            for address in values:
                if not isinstance(address, str) or address.lower().split('@')[-1] != project:
                    deny('Recipients must belong to the same project')
    if len(parts) >= 3 and parts[1] == 'emails':
        project = (body.get('from') or '').split('@')[-1] if is_human else principal['project']
        email = db.execute('SELECT e.thread_id,p.slug FROM emails e JOIN threads t ON t.id=e.thread_id JOIN projects p ON p.id=t.home_project_id WHERE e.id=?', (parts[2],)).fetchone()
        if not email or email['slug'] != project:
            deny('Message is outside this project')
    path = urlunsplit(('', '', url.path, urlencode(query, doseq=True), ''))
    # Case-insensitive stdlib headers are reconstructed by the transport.
    return path, headers, body


def dispatch(db, principal, method, path, headers, body, env, peer):
    parsed = urlsplit(path)
    if parsed.path.startswith('/v1/hosted/'):
        return management(db, principal, method, parsed.path, body or {}, env, peer)
    path, headers, body = authorize(db, principal, method, path, headers, body)
    key = headers.get('idempotency-key')
    if parsed.path == '/v1/ae/command':
        key = body.get('request_id')
    fingerprint = hashlib.sha256(json.dumps([method,path,body],sort_keys=True).encode()).hexdigest()
    if key and method != 'GET':
        if not isinstance(key, str) or len(key) > 200:
            raise ValidationError('invalid_key', 'Idempotency key must be at most 200 characters')
        old = db.execute('SELECT * FROM hosted_requests WHERE principal=? AND key=?', (principal['id'], key)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                return 409, {'error': {'code': 'idempotency_conflict', 'message': 'Key reused with different content'}}
            return old['status'], json.loads(old['response'])
        scoped_key = hashlib.sha256((principal['id'] + '/' + key).encode()).hexdigest()
        headers['idempotency-key'] = scoped_key
        if parsed.path == '/v1/ae/command':
            body['request_id'] = scoped_key
    status, result = Handler(db, method, path, headers, body, peer).run()
    if parsed.path == '/v1/projects' and principal['kind'] == 'agent' and status == 200:
        result['projects'] = [p for p in result['projects'] if p['slug'] == principal['project']]
    if key and method != 'GET' and status < 400:
        db.execute('INSERT INTO hosted_requests VALUES (?,?,?,?,?)', (principal['id'],key,fingerprint,status,json.dumps(result)))
    return status, result
