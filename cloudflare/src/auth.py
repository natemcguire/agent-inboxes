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
CALLBACK = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Signing in · Inbox</title><style>*{box-sizing:border-box}html{height:100%;background:#008080}body{margin:0;min-height:100vh;min-height:100dvh;display:grid;place-items:center;padding:24px;font:16px/1.5 Tahoma,Verdana,Arial,sans-serif;color:#111}.window{width:min(100%,440px);background:#c0c0c0;border:2px solid;border-color:#fff #222 #222 #fff;padding:3px;box-shadow:2px 2px 0 #004747}.titlebar{background:#000080;color:#fff;font-size:14px;font-weight:bold;padding:5px 9px}.content{text-align:center;padding:32px 20px 24px}.crosshair{position:relative;width:40px;height:40px;margin:0 auto 20px;border:2px solid #000080;border-radius:50%;animation:pulse 1.4s ease-in-out infinite}.crosshair:before,.crosshair:after{content:"";position:absolute;background:#000080}.crosshair:before{width:2px;height:52px;left:17px;top:-8px}.crosshair:after{height:2px;width:52px;top:17px;left:-8px}h1{font-size:21px;line-height:1.3;margin:0 0 12px}p{margin:0}footer{font-size:12px;text-align:center;margin:0 8px 8px;padding:8px;border:1px solid;border-color:#808080 #fff #fff #808080}a{display:inline-block;margin-top:20px;padding:8px 18px;background:#c0c0c0;color:#000;border:2px solid;border-color:#fff #222 #222 #fff;text-decoration:none;font-weight:bold}a:focus-visible{outline:2px dashed #000080;outline-offset:3px}[hidden]{display:none!important}.stopped{animation:none}@keyframes pulse{50%{opacity:.4}}@media(prefers-reduced-motion:reduce){.crosshair{animation:none}}</style></head><body><main class="window" aria-labelledby="heading"><div class="titlebar">I · Inbox · Sign in</div><div class="content"><div class="crosshair" id="indicator" aria-hidden="true"></div><h1 id="heading">Signing you in</h1><p id="status" role="status">Signing in with Nate’s SSO…</p><a id="retry" href="/auth/login" hidden>Try signing in again</a><noscript><p>JavaScript is needed to finish signing in. Enable it, then try again.</p><a href="/auth/login">Return to sign in</a></noscript></div><footer>SSO provided by Nate’s Software</footer></main><script src="/auth/callback.js"></script></body></html>'
CALLBACK_JS = '''(async()=>{const p=new URLSearchParams(location.hash.slice(1));history.replaceState(null,'','/auth/callback');try{const r=await fetch('/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:p.get('token'),state:p.get('state')})});if(!r.ok)throw new Error();location.replace('/');}catch{document.getElementById('status').textContent='Sign-in failed or access was not granted.';document.getElementById('heading').textContent='Sign-in couldn’t finish';document.getElementById('indicator').classList.add('stopped');document.getElementById('retry').hidden=false;}})();'''


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
    if path == '/auth/signed-out' and request.method == 'GET':
        return Response('', status=302, headers={**HEADERS, 'Location':
            getattr(env, 'SSO_ORIGIN', 'https://auth.nates-software.com').rstrip('/') + '/signed-out'})
    if path == '/auth/login' and request.method == 'GET':
        state = secrets.token_urlsafe(32)
        location = getattr(env, 'SSO_ORIGIN', 'https://auth.nates-software.com').rstrip('/') + '/authorize?' + urlencode({
            'app': audience(env), 'return': origin + '/auth/callback', 'state': state, 'tenant': tenant(env)})
        return Response('', status=302, headers={**HEADERS, 'Location': location,
            'Set-Cookie': f'{STATE_COOKIE}={state}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600'})
    if path == '/auth/callback' and request.method == 'GET':
        return Response(CALLBACK, headers={**HEADERS, 'Content-Type':'text/html; charset=utf-8',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'sha256-2s757Rb89AEJZRrJbbilIMmiruRcsEmoXq1IXkNHnjs='; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
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
        token = cookie(request, COOKIE)
        if token:
            result = await hub(env, '/session/logout', {'token': token, 'audience': audience(env)})
            if not result or result.get('ok') is not True:
                return Response('{"error":{"message":"Could not sign out of all products. Please try again."}}',
                    status=503, headers={**HEADERS, 'Content-Type':'application/json'})
        return Response('{"ok":true}', headers={**HEADERS, 'Content-Type':'application/json',
            'Set-Cookie': f'{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0'})
    return None
