"""Cloudflare Access JWT verification. Identity never comes from plain headers."""
import base64
import json
import time
from http.cookies import SimpleCookie

from js import crypto, JSON
from pyodide.ffi import to_js
from workers import fetch

_cache = {'expires': 0, 'keys': []}


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


async def member(request, env):
    token = request.headers.get('Cf-Access-Jwt-Assertion')
    if not token:
        cookies = SimpleCookie()
        cookies.load(request.headers.get('Cookie') or '')
        token = cookies['CF_Authorization'].value if 'CF_Authorization' in cookies else None
    if not token or len(token) > 16384:
        return None
    try:
        header, payload, signature = token.split('.')
        metadata, claims = json.loads(decode(header)), json.loads(decode(payload))
        now = time.time()
        issuer = 'https://' + env.ACCESS_DOMAIN
        audience = getattr(env, 'ACCESS_AUD', '')
        members = {x.strip().lower() for x in env.MEMBERS.split(',') if x.strip()}
        email = claims.get('email', '').lower()
        audiences = claims.get('aud', [])
        if isinstance(audiences, str):
            audiences = [audiences]
        if (metadata.get('alg') != 'RS256' or not audience or claims.get('iss') != issuer
                or audience not in audiences or claims.get('exp', 0) <= now
                or claims.get('nbf', 0) > now + 30 or email not in members):
            return None
        # Optional pinned public keys support offline, signed-token integration
        # tests. Production uses Access's rotating JWKS endpoint.
        pinned = getattr(env, 'ACCESS_JWKS', None)
        if pinned:
            keys = json.loads(pinned)['keys']
        else:
            if _cache['expires'] < now:
                response = await fetch(issuer + '/cdn-cgi/access/certs')
                if response.status != 200:
                    return None
                _cache.update(expires=now + 300, keys=(await response.json())['keys'])
            keys = _cache['keys']
        jwk = next(k for k in keys if k.get('kid') == metadata.get('kid') and k.get('kty') == 'RSA')
        algorithm = JSON.parse(json.dumps({'name': 'RSASSA-PKCS1-v1_5', 'hash': 'SHA-256'}))
        key = await crypto.subtle.importKey('jwk', JSON.parse(json.dumps(jwk)), algorithm, False, to_js(['verify']))
        valid = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, to_js(decode(signature)), to_js((header + '.' + payload).encode()))
        return email if valid else None
    except Exception as exc:
        if getattr(env, 'ACCESS_JWKS', None):
            print('JWT verification:', type(exc).__name__, str(exc))
        return None
