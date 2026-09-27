"""Nate's SSO: host-only sessions and live tenant/product authorization."""
import hmac
import json
import secrets
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit
from workers import fetch, Response
from js import Request as JSRequest, JSON

COOKIE = '__Host-ain_session'
STATE_COOKIE = '__Host-ain_state'
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Content-Type-Options': 'nosniff'}
CALLBACK = '''<!doctype html><meta charset="utf-8"><title>Signing in · Agent Inbox</title><p id="status">Completing sign-in…</p><script src="/auth/callback.js" defer></script>'''
CALLBACK_JS = '''(async()=>{const p=new URLSearchParams(location.hash.slice(1));history.replaceState(null,'','/auth/callback');try{const r=await fetch('/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:p.get('token'),state:p.get('state')})});if(!r.ok)throw new Error();location.replace('/');}catch{document.getElementById('status').textContent='Sign-in failed or access was not granted. Return to /auth/login to try again.';}})();'''


def cookie(request, name):
    try:
        values = SimpleCookie()
        values.load(request.headers.get('Cookie') or '')
        return values[name].value if name in values else ''
    except Exception:
        return ''


async def hub(env, path, body, service=False):
    try:
        origin = getattr(env, 'SSO_ORIGIN', 'https://auth.nates-software.com').rstrip('/')
        headers = {'Content-Type': 'application/json'}
        if service:
            binding = getattr(env, 'AUTH_HUB', None)
            if not binding:
                return None
            request = JSRequest.new('https://auth.internal' + path, JSON.parse(json.dumps({
                'method': 'POST', 'headers': headers, 'body': json.dumps(body)})))
            result = await binding.fetch(request)
            if result.status != 200:
                return None
            return json.loads(await result.text())
        result = await fetch(origin + path, method='POST', headers=headers, body=json.dumps(body))
        return await result.json() if result.status == 200 else None
    except Exception:
        return None


def audience(env):
    return getattr(env, 'SSO_APP_ID', 'agent-inboxes')


def tenant(env):
    return getattr(env, 'SSO_TENANT_ID', 'nates-software')


async def member(request, env):
    token = cookie(request, COOKIE)
    if not token or len(token) > 16384:
        return None
    data = await hub(env, '/session/introspect', {'token': token, 'audience': audience(env)})
    if not data or data.get('active') is not True or data.get('tenant_id') != tenant(env):
        return None
    email = data.get('email')
    return email.lower() if isinstance(email, str) and '@' in email else None


async def owner_allowed(email, env):
    data = await hub(env, '/internal/entitlement', {'email': email, 'audience': audience(env),
                                         'tenant_id': tenant(env)}, service=True)
    return bool(data and data.get('active') is True and data.get('tenant_id') == tenant(env))


async def route(request, env):
    url = urlsplit(request.url)
    path = url.path
    origin = f'{url.scheme}://{url.netloc}'
    if path == '/auth/login' and request.method == 'GET':
        state = secrets.token_urlsafe(32)
        location = getattr(env, 'SSO_ORIGIN', 'https://auth.nates-software.com').rstrip('/') + '/authorize?' + urlencode({
            'app': audience(env), 'return': origin + '/auth/callback', 'state': state, 'tenant': tenant(env)})
        return Response('', status=302, headers={**HEADERS, 'Location': location,
            'Set-Cookie': f'{STATE_COOKIE}={state}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600'})
    if path == '/auth/callback' and request.method == 'GET':
        return Response(CALLBACK, headers={**HEADERS, 'Content-Type':'text/html; charset=utf-8',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
    if path == '/auth/callback.js' and request.method == 'GET':
        return Response(CALLBACK_JS, headers={**HEADERS, 'Content-Type':'text/javascript; charset=utf-8'})
    if path == '/auth/session' and request.method == 'POST':
        try:
            raw = await request.text()
            if len(raw) > 20000:
                raise ValueError()
            body = json.loads(raw)
            state = cookie(request, STATE_COOKIE)
            if not state or not isinstance(body.get('state'), str) or not hmac.compare_digest(state, body['state']):
                raise ValueError()
            token = body.get('token')
            if not isinstance(token, str) or not token or len(token) > 16384:
                raise ValueError()
            data = await hub(env, '/session/exchange', {'token': token, 'audience': audience(env)})
            if not data or data.get('tenant_id') != tenant(env) or not isinstance(data.get('token'), str):
                raise ValueError()
            return Response('{"ok":true}', headers={**HEADERS, 'Content-Type':'application/json',
                'Set-Cookie': f'{COOKIE}={data["token"]}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000'})
        except Exception:
            return Response('{"error":"Sign-in failed or access denied"}', status=401, headers=HEADERS)
    if path == '/auth/logout' and request.method == 'POST':
        return Response('{"ok":true}', headers={**HEADERS, 'Content-Type':'application/json',
            'Set-Cookie': f'{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0'})
    return None
