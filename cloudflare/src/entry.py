"""Authenticated Cloudflare entrypoint; each workspace owns one SQLite object."""
import asyncio
import json
import math
import time
from urllib.parse import parse_qs, urlsplit

from js import JSON, Request as JSRequest, Headers as JSHeaders
from workers import DurableObject, Response, WorkerEntrypoint

from agent_inbox.db import init_db
from agent_inbox.ae import initialize as initialize_ae
from agent_inbox.models import InboxError
from agent_inbox.ui import ASSETS
from api import SCHEMA, authenticate, dispatch
from auth import member, owner_allowed, route as auth_route
from storage import Database
import onboarding
import hosted_ui

SECURITY = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
    'Referrer-Policy':'same-origin',
    'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"}


def response(data, status=200, content_type='application/json; charset=utf-8', headers=None):
    return Response(json.dumps(data) if isinstance(data, dict) else data,
        status=status, headers={**SECURITY, 'Content-Type':content_type, **(headers or {})})


class Abort(Exception):
    def __init__(self, result):
        self.result = result


class WorkspaceEnvironment:
    """Request metadata without changing Worker bindings or durable storage IDs."""
    def __init__(self, env, workspace):
        self.env = env
        self.WORKSPACE = workspace

    def __getattr__(self, name):
        return getattr(self.env, name)


class Workspace(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        def initialize():
            db = Database(self.ctx.storage.sql)
            init_db(db)
            initialize_ae(db)
            db.executescript(SCHEMA)
            # Preserve the existing workspace and keys while assigning Nate's
            # legacy credentials to his canonical SSO account. No other owner
            # is migrated or granted membership.
            legacy = getattr(self.env, 'LEGACY_OWNER_EMAIL', '')
            owner = getattr(self.env, 'OWNER_EMAIL', '')
            if legacy and owner:
                for table in ('hosted_tokens', 'hosted_registrars'):
                    db.execute(f'UPDATE {table} SET owner=? WHERE owner=?', (owner, legacy))
            onboarding.initialize(db, self.env)
        self.ctx.storage.transactionSync(initialize)

    async def fetch(self, request):
        path = urlsplit(request.url).path
        workspace_env = WorkspaceEnvironment(self.env, request.headers.get('X-Hosted-Workspace') or self.env.WORKSPACE)
        email = request.headers.get('X-Hosted-Member') or ''
        token = request.headers.get('Authorization') or ''
        token = token.removeprefix('Bearer ')
        headers = dict(request.headers)
        body = None
        if request.method in ('PUT','POST'):
            try:
                raw = await request.text()
                if len(raw.encode()) > 256000:
                    return response({'error': {'code':'too_large','message':'Request exceeds 256 KB'}}, 413)
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError('Expected an object')
            except (ValueError, TypeError):
                return response({'error': {'code':'invalid_json','message':'Request body must be a JSON object'}}, 400)
        peer = request.headers.get('X-Hosted-Peer') or ''
        # Network authorization is outside the synchronous SQLite transaction.
        # The key is checked again inside it to honor concurrent revocation.
        allowed_members = {email} if email else set()
        if not email and token:
            db = Database(self.ctx.storage.sql)
            import hashlib
            digest = hashlib.sha256(token.encode()).hexdigest()
            table = 'hosted_registrars' if token.startswith('ainr_') else 'hosted_tokens'
            row = db.execute(f'SELECT owner FROM {table} WHERE digest=?', (digest,)).fetchone()
            if row and await owner_allowed(row['owner'], self.env):
                allowed_members.add(row['owner'])
        def operation():
            db = Database(self.ctx.storage.sql)
            members = allowed_members
            principal = authenticate(db, email, token, members)
            if principal is None:
                raise Abort(response({'error': {'code':'unauthorized','message':'Sign in or supply a valid agent key'}}, 401))
            if email:
                onboarding.sign_in(db, email, {x.strip().lower() for x in self.env.MEMBERS.split(',') if x.strip()})
            if request.method == 'GET' and path in ASSETS:
                if not email:
                    raise Abort(response({'error': {'code':'forbidden','message':'The browser UI requires a human login'}}, 403))
                kind, content = ASSETS[path]
                if kind.startswith('text/html'):
                    content = content.replace('<body>', '<body data-hosted="true">').replace('<script src="/ui.js"', '<script src="/hosted.js" defer></script><script src="/ui.js"')
                return response(content, content_type=kind)
            if request.method == 'GET' and path in ('/hosted.js','/welcome.css','/welcome'):
                if not email:
                    raise Abort(response({'error':{'code':'forbidden','message':'Sign in to continue'}},403))
                if path == '/hosted.js':
                    return response(hosted_ui.JS, content_type='text/javascript; charset=utf-8')
                if path == '/welcome.css':
                    return response(onboarding.CSS, content_type='text/css; charset=utf-8')
                welcome = onboarding.page(db)
                if welcome:
                    return response(welcome[0], content_type='text/html; charset=utf-8')
                return Response('',status=303,headers={**SECURITY,'Location':'/'})
            try:
                status, result = dispatch(db, principal, request.method, request.url, headers, body, workspace_env, peer)
                if status >= 400 or db.rolled_back:
                    raise Abort(response(result, status if status >= 400 else 500))
                if path == '/v1/hosted/registration/setup' and request.method == 'POST':
                    cookie = '__TOKEN__; Path=/v1/hosted/registration/setup; HttpOnly; Secure; SameSite=Strict; Max-Age=315360000'
                    return response(result, status, headers={'Set-Cookie': 'ain_registration=' + cookie.replace('__TOKEN__', result['token'])})
                return response(result, status)
            except InboxError as exc:
                raise Abort(response(exc.to_dict(), exc.status_code))
        try:
            return self.ctx.storage.transactionSync(operation)
        except Abort as exc:
            return exc.result
        except Exception as exc:
            print('Workspace operation failed:', type(exc).__name__, str(exc))
            return response({'error': {'code':'internal_error','message':'The workspace could not complete this operation'}},500)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlsplit(request.url)
        hosts = json.loads(getattr(self.env, 'WORKSPACE_HOSTS', '{}'))
        if hosts and url.hostname not in hosts:
            return response({'error': {'code':'not_found','message':'Unknown workspace host'}},404)
        if url.hostname == 'inbox.eastbayprojects.com' and not url.path.startswith('/v1/'):
            return Response('', status=308, headers={**SECURITY,
                'Location': 'https://inbox.nates-software.com' + url.path + ('?' + url.query if url.query else '')})
        if request.method not in ('GET','POST','PUT'):
            return response({'error': {'code':'method_not_allowed','message':'Method not allowed'}},405)
        # Do not reflect CORS or accept cross-site mutations with login cookies.
        origin = request.headers.get('Origin')
        if request.method != 'GET' and origin and origin != f'{url.scheme}://{url.netloc}':
            return response({'error': {'code':'forbidden','message':'Use this workspace origin'}},403)
        authentication = await auth_route(request, self.env)
        if authentication is not None:
            return authentication
        email = await member(request, self.env)
        authorization = request.headers.get('Authorization') or ''
        if not email and not authorization.startswith(('Bearer ain_', 'Bearer ainr_')):
            if request.method == 'GET' and not url.path.startswith('/v1/'):
                return Response('', status=302, headers={**SECURITY, 'Location':'/auth/login'})
            return response({'error': {'code':'unauthorized','message':'Sign in or supply an agent key'}},401)
        # Always replace client-supplied identity/provenance before forwarding.
        forwarded = JSHeaders.new(request.js_object.headers)
        forwarded.set('X-Hosted-Member', email or '')
        forwarded.set('X-Hosted-Peer', request.headers.get('CF-Connecting-IP') or '')
        forwarded.set('X-Hosted-Workspace', 'eastbayprojects' if url.hostname == 'inboxes.eastbayprojects.com' else getattr(self.env, 'SSO_TENANT_ID', self.env.WORKSPACE))
        internal = JSRequest.new(request.js_object, JSON.parse(json.dumps({'headers':dict(forwarded)})))
        workspace = hosts.get(url.hostname, self.env.WORKSPACE)
        stub = self.env.WORKSPACES.get(self.env.WORKSPACES.idFromName(workspace))
        query = parse_qs(url.query)
        timeout = 0
        is_watch = request.method == 'GET' and (url.path.endswith(('/watch','/wait')) or url.path == '/v1/ae/events')
        if is_watch:
            try:
                timeout = max(0, min(float(query.get('timeout', query.get('wait', ['0' if url.path == '/v1/ae/events' else '60']))[0]), 60))
            except (ValueError, TypeError):
                return response({'error': {'code':'invalid_timeout','message':'Invalid watch timeout'}},400)
        if not math.isfinite(timeout):
            return response({'error': {'code':'invalid_timeout','message':'Invalid watch timeout'}},400)
        coalesce = 0
        if url.path == '/v1/ae/watch':
            try:
                coalesce = float(query.get('coalesce', ['30'])[0])
                if not math.isfinite(coalesce) or not 0 <= coalesce <= 30:
                    raise ValueError()
            except ValueError:
                return response({'error': {'code':'invalid_coalesce','message':'Coalesce must be 0–30 seconds'}},400)
        batch_started = None
        deadline = time.monotonic() + timeout
        while True:
            result = await stub.fetch(internal.clone())
            if not is_watch or result.status != 200:
                return result
            data = await result.clone().json()
            now = time.monotonic()
            if data.get('has_more') or data.get('wake_reason') == 'urgent' or now >= deadline:
                return result
            changed = data.get('changed') or data.get('free') or data.get('events')
            if changed:
                if not coalesce:
                    return result
                if batch_started is None:
                    batch_started = now
                if now - batch_started >= coalesce:
                    return result
            await asyncio.sleep(min(1, max(0,deadline-time.monotonic())))
