"""Verified, bounded runtime distribution. No third-party dependencies required."""
import hashlib
import io
import json
import re
import ssl
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit

MANIFEST_URL = 'https://nates-software.com/downloads/agent-inbox-manifest.json'
MAX_BYTES = 4 * 1024 * 1024
FILES = {'bin/agent-inbox'} | {f'agent_inbox/{name}.py' for name in (
    '__init__', 'cli', 'client', 'config', 'db', 'identity', 'launchagent',
    'models', 'project_setup', 'server', 'service', 'hooks', 'cloud_protocol',
    'cloudsync', 'distribution', 'updates')}


def check_url(url):
    target = urlsplit(url)
    if (target.scheme != 'https' or target.netloc != 'nates-software.com'
            or not target.path.startswith('/downloads/') or target.fragment):
        raise ValueError('Refusing URL outside the HTTPS download origin.')
    return url


class SameOriginHTTPS(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _curl_fetch(url, limit):
    # Do not use --location: validate every redirect before contacting its host.
    for _ in range(4):
        check_url(url)
        with tempfile.TemporaryDirectory() as tmp:
            body, headers = Path(tmp)/'body', Path(tmp)/'headers'
            result = subprocess.run([
                '/usr/bin/curl', '--silent', '--show-error', '--proto', '=https',
                '--max-time', '15', '--max-filesize', str(limit),
                '--dump-header', str(headers), '--output', str(body),
                '--write-out', '%{http_code}', url,
            ], capture_output=True, timeout=17)
            if result.returncode:
                raise RuntimeError('Verified TLS download failed.')
            status = int(result.stdout)
            if status in (301, 302, 303, 307, 308):
                locations = [line.split(':', 1)[1].strip() for line in
                             headers.read_text().splitlines() if line.lower().startswith('location:')]
                if not locations:
                    raise ValueError('Redirect has no location.')
                url = check_url(urljoin(url, locations[-1]))
                continue
            if status != 200:
                raise ValueError(f'Download returned HTTP {status}.')
            with body.open('rb') as source:
                payload = source.read(limit + 1)
            if len(payload) > limit:
                raise ValueError('Download exceeds size limit.')
            return payload
    raise ValueError('Too many download redirects.')


def fetch(url, limit=MAX_BYTES):
    check_url(url)
    contexts = [ssl.create_default_context()]
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass
    for context in contexts:
        try:
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=context), SameOriginHTTPS())
            request = urllib.request.Request(url, headers={'User-Agent': 'Agent-Inboxes/1'})
            with opener.open(request, timeout=15) as source:
                payload = source.read(limit + 1)
            if len(payload) > limit:
                raise ValueError('Download exceeds size limit.')
            return payload
        except urllib.error.URLError as error:
            if 'CERTIFICATE_VERIFY_FAILED' not in str(error):
                raise
    return _curl_fetch(url, limit)


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid runtime version.')
    return tuple(map(int, version.split('.')))


def validate_manifest(value):
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1:
        raise ValueError('Unsupported runtime manifest schema.')
    for key, pattern in [('commit', r'[0-9a-f]{40}'), ('sha256', r'[0-9a-f]{64}')]:
        if not isinstance(value.get(key), str) or not re.fullmatch(pattern, value[key]):
            raise ValueError(f'Invalid manifest {key}.')
    version_tuple(value.get('version'))
    version_tuple(value.get('minimum_supported'))
    check_url(value['url'])
    return value


def fetch_manifest():
    return validate_manifest(json.loads(fetch(MANIFEST_URL, 64 * 1024)))


def verified_files(payload, checksum, *, exact=False):
    if len(payload) > MAX_BYTES or hashlib.sha256(payload).hexdigest() != checksum:
        raise ValueError('Runtime archive checksum mismatch. Nothing was installed.')
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as archive:
        for member in archive:
            name = member.name
            # Future releases may add flat Python modules, never arbitrary paths.
            allowed = name == 'bin/agent-inbox' or re.fullmatch(r'agent_inbox/[a-zA-Z_][a-zA-Z_0-9]*\.py', name)
            total += member.size
            if (not allowed or not member.isfile() or name in files
                    or member.size < 0 or total > MAX_BYTES):
                raise ValueError('Unsafe or unexpected runtime archive entry.')
            files[name] = archive.extractfile(member).read()
    if not FILES <= files.keys() or (exact and files.keys() != FILES):
        raise ValueError('Runtime archive is incomplete or unexpected.')
    return files
