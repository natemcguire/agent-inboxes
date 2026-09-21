"""HTTP client library for Agent Inboxes communicating over loopback."""

import hashlib
import json
import shlex
import sys
import tempfile
import time
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from agent_inbox import __version__
from agent_inbox.config import get_data_dir, get_server_url
from agent_inbox.models import (
    normalize_slug,
    ConflictError,
    InboxError,
    NotFoundError,
    ServerNotRunningError,
    ValidationError,
)


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Authenticated API redirects are refused', headers, fp)


class InboxClient:
    """Client for interacting with local Agent Inboxes service via HTTP."""

    def __init__(self, base_url: Optional[str] = None, session_id: Optional[str] = None,
                 timeout: Optional[float] = None, repo_key: Optional[str] = None,
                 token: Optional[str] = None):
        self.base_url = (base_url or get_server_url()).rstrip("/")
        # Default per-request timeout; explicit per-call timeouts still win.
        self.default_timeout = timeout
        # Optional short session slug identifying THIS agent session; sent as
        # X-Agent-Session so the service can distinguish concurrent same-family
        # agents sharing one inbox address.
        self.session_id = session_id
        # Optional worktree-safe repository identity; sent as X-Repo-Key so
        # same-basename repos don't cross-conflict on file reservations.
        self.repo_key = repo_key
        self.address = None
        self.token = token if token is not None else os.environ.get('AGENT_INBOX_TOKEN', '').strip()
        if token is None and not self.token and os.environ.get('AGENT_INBOX_TOKEN_FILE'):
            self.token = Path(os.environ['AGENT_INBOX_TOKEN_FILE']).expanduser().read_text().strip()
        if self.token:
            endpoint = urllib.parse.urlsplit(self.base_url)
            if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ValueError('Use an API URL without credentials, a query, or a fragment')
            if endpoint.scheme != 'https' and not (endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', 'localhost', '::1')):
                raise ValueError('Hosted agent credentials require HTTPS')
        self._opener = urllib.request.build_opener(NoCredentialRedirect()) if self.token else None

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Perform HTTP request and return parsed JSON response."""
        if self.token and path == '/healthz':
            path = '/v1/hosted/health'
        full_path = path
        if query:
            clean_query = {k: str(v) for k, v in query.items() if v is not None}
            if clean_query:
                full_path = f"{path}?{urllib.parse.urlencode(clean_query)}"

        url = f"{self.base_url}{full_path}"
        req_headers = {
            "Accept": "application/json",
            "User-Agent": f"agent-inboxes/{__version__} (+https://github.com/natemcguire/agent-inboxes)",
        }
        # Identify the calling agent so the server can refresh its lease. Best-effort:
        # identity derivation must never block a request.
        try:
            from agent_inbox.identity import derive_identity
            if self.address or os.environ.get('AGENT_INBOX_AGENT', '').strip():
                req_headers["X-Agent-Address"] = self.address or derive_identity()[2]
        except Exception:
            pass
        if self.session_id:
            req_headers["X-Agent-Session"] = self.session_id
            req_headers["X-Agent-Pid"] = str(os.getpid())
            from agent_inbox.identity import harness_pid
            pid = harness_pid()
            if pid:
                req_headers['X-Agent-Harness-Pid'] = str(pid)
        if self.repo_key:
            req_headers["X-Repo-Key"] = self.repo_key
        if headers:
            req_headers.update(headers)

        if self.token:
            req_headers['Authorization'] = 'Bearer ' + self.token

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json; charset=utf-8"

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

        try:
            effective_timeout = timeout if timeout is not None else (self.default_timeout if self.default_timeout is not None else 10.0)
            with (self._opener.open(req, timeout=effective_timeout) if self._opener else urllib.request.urlopen(req, timeout=effective_timeout)) as resp:
                resp_body = resp.read().decode("utf-8")
                if not resp_body.strip():
                    return {}
                return json.loads(resp_body)
        except urllib.error.HTTPError as e:
            try:
                raw_err = e.read().decode("utf-8")
            finally:
                e.close()
            try:
                err_json = json.loads(raw_err)
                err_data = err_json.get("error", {})
                code = err_data.get("code", "http_error")
                message = err_data.get("message", f"HTTP {e.code}: {e.reason}")
            except Exception:
                err_json = {}
                code = "http_error"
                message = f"HTTP {e.code}: {e.reason}"

            if e.code == 400:
                raise ValidationError(code, message)
            elif e.code == 404:
                raise NotFoundError(code, message)
            elif e.code == 409:
                conflict = ConflictError(code, message)
                # Carry the full body (e.g. reservation conflict details) so
                # callers can render holder/expiry information.
                conflict.payload = err_json
                raise conflict
            else:
                raise InboxError(code, message, status_code=e.code)

        except (urllib.error.URLError, ConnectionError, OSError) as e:
            raise ServerNotRunningError(
                f"Local agent-inbox service is not reachable at {self.base_url}. "
                "Start it with `agent-inbox serve` or `agent-inbox setup`."
            ) from e

    def claim_lease(self, family: str, project: str) -> Dict[str, Any]:
        """Recover this session's name, or claim a free family slot in a project."""
        return self._request("POST", "/v1/leases/claim", body={"family": family, "project": project})

    def lookup_lease(self, project, session):
        return self._request('GET', '/v1/leases/lookup', query={'project': project, 'session': session}).get('lease')

    def email_status(self, email_id):
        return self._request('GET', '/v1/emails/' + urllib.parse.quote(email_id, safe='') + '/status')

    def release_lease(self, agent: str, project: str) -> Dict[str, Any]:
        """Release this agent's lease so the slot frees immediately."""
        return self._request("POST", "/v1/leases/release", body={"agent": agent, "project": project})

    def cloud_status(self) -> Dict[str, Any]:
        """Read cloud counters from the serving process database."""
        return self._request("GET", "/v1/cloud/status")

    def healthz(self) -> Dict[str, Any]:
        """Check service health."""
        return self._request("GET", "/healthz")

    def put_inbox(self, address: str, display_name: Optional[str] = None, role=None) -> Dict[str, Any]:
        """Idempotently register or touch an inbox."""
        encoded_addr = urllib.parse.quote(address, safe="@")
        body = {}
        if display_name is not None:
            body["display_name"] = display_name
        if role is not None:
            body['role'] = role
        return self._request("PUT", f"/v1/inboxes/{encoded_addr}", body=body)

    def list_inboxes(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """List inboxes, optionally filtered by project slug."""
        query = {"project": project} if project else None
        res = self._request("GET", "/v1/inboxes", query=query)
        return res.get("inboxes", [])

    def post_announcement(self, from_addr, subject, body_markdown, all_projects=False, client_token=None):
        return self._request("POST", "/v1/announcements",
            body={"from": from_addr, "subject": subject, "body_markdown": body_markdown, "all_projects": all_projects},
            headers={"Idempotency-Key": client_token or str(uuid.uuid4())})

    def list_announcements(self, inbox, unread=False, limit=200):
        return self._request("GET", "/v1/announcements", query={"inbox": inbox, "unread": str(unread).lower(), "limit": limit})["announcements"]

    def acknowledge_announcement(self, announcement_id, inbox):
        return self._request("POST", f"/v1/announcements/{urllib.parse.quote(announcement_id, safe='')}/read", body={"inbox": inbox})

    def send_email(
        self,
        from_addr: str,
        to_addrs: List[str],
        cc_addrs: Optional[List[str]] = None,
        subject: str = "",
        body_markdown: str = "",
        idempotency_key: Optional[str] = None,
        create_missing: bool = False,
    ) -> Dict[str, Any]:
        """Start a thread and send the first email."""
        key = idempotency_key or str(uuid.uuid4())
        body = {
            "from": from_addr,
            "to": to_addrs,
            "cc": cc_addrs or [],
            "subject": subject,
            "body_markdown": body_markdown,
            "create_missing": create_missing,
        }
        headers = {"Idempotency-Key": key}
        return self._request("POST", "/v1/emails", body=body, headers=headers)

    def reply_email(
        self,
        email_id: str,
        from_addr: str,
        body_markdown: str,
        to_addrs: Optional[List[str]] = None,
        cc_addrs: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reply to an existing email in a thread."""
        key = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {
            "from": from_addr,
            "body_markdown": body_markdown,
        }
        if to_addrs is not None:
            body["to"] = to_addrs
        if cc_addrs is not None:
            body["cc"] = cc_addrs
        headers = {"Idempotency-Key": key}
        return self._request("POST", f"/v1/emails/{email_id}/reply", body=body, headers=headers)

    def list_threads(
        self,
        address: str,
        unread: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List active threads visible to an inbox."""
        encoded_addr = urllib.parse.quote(address, safe="@")
        query: Dict[str, Any] = {"limit": limit}
        if unread:
            query["unread"] = "true"
        res = self._request("GET", f"/v1/inboxes/{encoded_addr}/threads", query=query)
        return res.get("threads", [])

    def get_thread(self, address: str, thread_id: str) -> Dict[str, Any]:
        """Fetch complete thread details without mutating read state."""
        encoded_addr = urllib.parse.quote(address, safe="@")
        return self._request("GET", f"/v1/inboxes/{encoded_addr}/threads/{thread_id}")

    def watch(
        self,
        address: str,
        timeout: float = 60.0,
        after: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Long-poll for new unread mail. Blocks up to ``timeout`` seconds
        (server clamps to 0–300) and returns the watch state either way."""
        encoded_addr = urllib.parse.quote(address, safe="@")
        query: Dict[str, Any] = {"timeout": float(timeout)}
        if after is not None:
            query["after"] = int(after)
        return self._request(
            "GET",
            f"/v1/inboxes/{encoded_addr}/watch",
            query=query,
            timeout=timeout + 10.0,
        )

    # -- File reservations (NB-7) -------------------------------------

    def acquire_reservations(
        self,
        project: str,
        paths: Optional[List[str]] = None,
        holder: str = "",
        session: Optional[str] = None,
        reason: str = "",
        ttl_seconds: Optional[int] = None,
        force: bool = False,
        idempotency_key: Optional[str] = None,
        resources: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """All-or-nothing acquire of file paths OR named resources (not both).
        Raises ConflictError (with .payload carrying 'conflicts') when any key
        is held by another agent and force=False."""
        key = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {"holder": holder, "reason": reason}
        if resources is not None:
            body["resources"] = resources
        else:
            body["paths"] = paths
        if session or self.session_id:
            body["session"] = session or self.session_id
        if ttl_seconds is not None:
            body["ttl_seconds"] = int(ttl_seconds)
        if force:
            body["force"] = True
        return self._request(
            "POST",
            f"/v1/projects/{urllib.parse.quote(project)}/reservations",
            body=body,
            headers={"Idempotency-Key": key},
        )

    def _reservation_action(
        self, action: str, project: str, holder: str,
        session: Optional[str], paths: Optional[List[str]], do_all: bool,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"holder": holder}
        if session or self.session_id:
            body["session"] = session or self.session_id
        if do_all:
            body["all"] = True
        else:
            body["paths"] = paths or []
        return self._request(
            "POST",
            f"/v1/projects/{urllib.parse.quote(project)}/reservations/{action}",
            body=body,
        )

    def renew_reservations(self, project: str, holder: str, session: Optional[str] = None,
                           paths: Optional[List[str]] = None, renew_all: bool = False) -> Dict[str, Any]:
        return self._reservation_action("renew", project, holder, session, paths, renew_all)

    def release_reservations(self, project: str, holder: str, session: Optional[str] = None,
                             paths: Optional[List[str]] = None, release_all: bool = False) -> Dict[str, Any]:
        return self._reservation_action("release", project, holder, session, paths, release_all)

    def list_reservations(self, project: str, holder: Optional[str] = None,
                          history: bool = False, limit: int = 200) -> Dict[str, Any]:
        """Active reservations for a project; add finished audit rows with
        history=True. Returns the full payload dict ({'reservations', 'history'?})."""
        query: Dict[str, Any] = {}
        if holder:
            query["holder"] = holder
        if history:
            query["history"] = "1"
            query["limit"] = int(limit)
        return self._request(
            "GET", f"/v1/projects/{urllib.parse.quote(project)}/reservations",
            query=query or None,
        )

    def list_reservations_global(self, history: bool = False, limit: int = 200) -> Dict[str, Any]:
        """Machine-wide reservations across projects (observer/web-UI view)."""
        query: Dict[str, Any] = {}
        if history:
            query["history"] = "1"
            query["limit"] = int(limit)
        return self._request("GET", "/v1/reservations", query=query or None)

    def list_reservations_everything(self, limit: int = 200) -> List[dict]:
        """Active and finished rows together, retaining explicit active state."""
        result = self.list_reservations_global(history=True, limit=limit)
        return result.get("entries", [dict(row, active=True) for row in result.get("reservations", [])] +
                          [dict(row, active=False) for row in result.get("history", [])])

    def wait_reservations(
        self, project: str, paths: List[str], holder: str,
        session: Optional[str] = None, timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Long-poll until the paths are free for this holder (or timeout)."""
        query: Dict[str, Any] = {
            "paths": ",".join(paths),
            "holder": holder,
            "timeout": int(timeout),
        }
        if session or self.session_id:
            query["session"] = session or self.session_id
        return self._request(
            "GET",
            f"/v1/projects/{urllib.parse.quote(project)}/reservations/wait",
            query=query,
            timeout=timeout + 10.0,
        )

    def mark_thread_read(self, address: str, thread_id: str) -> Dict[str, Any]:
        """Mark every email in thread as read for the inbox."""
        encoded_addr = urllib.parse.quote(address, safe="@")
        return self._request("POST", f"/v1/inboxes/{encoded_addr}/threads/{thread_id}/read")


# One-time owner setup and unattended hosted agent registration.
HOSTED_DEFAULT_URL = 'https://inbox.eastbayprojects.com'


def hosted_origin(value):
    url = urllib.parse.urlsplit(value)
    if (url.username or url.password or url.query or url.fragment or url.path not in ('', '/')
            or not url.hostname or (url.scheme != 'https' and not
            (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise ValueError('Use a workspace HTTPS origin without a path, credentials, query, or fragment')
    return value.rstrip('/')


def hosted_request(url, token, method, path, body=None):
    req = urllib.request.Request(hosted_origin(url) + path, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                 'User-Agent': 'agent-inboxes/' + __version__},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.build_opener(NoCredentialRedirect()).open(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never echo credentials, redirect destinations, or server HTML.
        message = 'Hosted request failed (HTTP %s)' % exc.code
        try:
            message += ': ' + json.load(exc)['error']['message']
        except (ValueError, KeyError, TypeError):
            pass
        raise ValueError(message) from None


def save_hosted_credentials(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(value, file)
            file.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def add_hosted_parser(subparsers):
    parser = subparsers.add_parser('hosted', help='One-time owner setup and autonomous agent registration')
    commands = parser.add_subparsers(dest='hosted_action', required=True)
    login = commands.add_parser('login', help='Save owner registration access once for agents on this machine')
    login.add_argument('--url', default=HOSTED_DEFAULT_URL)
    login.add_argument('--credential-stdin', action='store_true', required=True)
    register = commands.add_parser('register', help='Register an agent using saved owner access; print shell exports')
    register.add_argument('--url', default=HOSTED_DEFAULT_URL)
    register.add_argument('--project', required=True)
    register.add_argument('--agent', required=True)
    register.add_argument('--label')
    status = commands.add_parser('status', help='Check saved owner registration access')
    status.add_argument('--url', default=HOSTED_DEFAULT_URL)


def run_hosted(args):
    import fcntl

    try:
        url = hosted_origin(args.url)
        directory = get_data_dir() / 'hosted' / hashlib.sha256(url.encode()).hexdigest()[:24]
        config = directory / 'registration.json'
        if args.hosted_action == 'login':
            token = sys.stdin.read().strip()
            if not token.startswith('ainr_') or len(token) > 200:
                raise ValueError('Expected an owner registration credential (ainr_) from Connect an agent')
            data = hosted_request(url, token, 'GET', '/v1/hosted/registration')
            save_hosted_credentials(config, {'url': url, 'token': token, **data})
            print('Registration access saved for %s at %s. Agents can now run hosted register.' % (data['owner'], url))
            return 0
        if not config.exists():
            raise ValueError('One-time setup required: sign in to %s, open Connect an agent, and run its setup command.' % url)
        owner = json.loads(config.read_text())
        if args.hosted_action == 'status':
            data = hosted_request(url, owner['token'], 'GET', '/v1/hosted/registration')
            print(json.dumps(data, indent=2))
            return 0
        project, family = normalize_slug(args.project), normalize_slug(args.agent)
        cache = directory / (hashlib.sha256((project + '/' + family).encode()).hexdigest() + '.json')
        # Serialize agents registering the same family across concurrent terminals.
        fd = os.open(directory / 'register.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(cache.read_text()) if cache.exists() else None
            if data and data.get('owner') != owner.get('owner'):
                data = None
            if data and data['expires_at'] > time.time():
                hosted_request(url, data['token'], 'GET', '/v1/hosted/health')
            else:
                pending = cache.with_suffix('.pending')
                if pending.exists():
                    enrollment = json.loads(pending.read_text())
                else:
                    enrollment = {'registration_id': uuid.uuid4().hex}
                    save_hosted_credentials(pending, enrollment)
                data = hosted_request(url, owner['token'], 'POST', '/v1/hosted/register',
                    {'project': project, 'family': family, 'label': args.label or family, **enrollment})
                save_hosted_credentials(cache, data)
                pending.unlink(missing_ok=True)
            token_file = cache.with_suffix('.token')
            fd, name = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(fd, 'w') as file:
                    file.write(data['token'] + '\n')
                os.replace(name, token_file)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        print('unset AGENT_INBOX_TOKEN')
        for key, value in {'URL': url, 'TOKEN_FILE': str(token_file), 'PROJECT': project, 'AGENT': family}.items():
            print('export AGENT_INBOX_%s=%s' % (key, shlex.quote(value)))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print('Error: %s' % exc, file=sys.stderr)
        return 1


def configured_hosted_watches(url, agent, session):
    """Load only this agent's saved project keys, without mutating process env."""
    import re
    url = hosted_origin(url)
    agent = normalize_slug(agent)
    family = re.sub(r'-[0-9]+$', '', agent)
    directory = get_data_dir() / 'hosted' / hashlib.sha256(url.encode()).hexdigest()[:24]
    targets = []
    for path in sorted(directory.glob('*.json')):
        if path.name == 'registration.json':
            continue
        data = json.loads(path.read_text())
        if data.get('family') != family:
            continue
        project = data.get('project')
        if not project or project != normalize_slug(project) or not data.get('token', '').startswith('ain_'):
            raise ValueError('Invalid saved hosted registration: ' + path.name)
        if data.get('expires_at', 0) <= time.time():
            raise ValueError('Saved key expired for %s@%s; run hosted register for that project first' % (family, project))
        client = InboxClient(url, session_id=session, token=data['token'])
        client.address = agent + '@' + project
        targets.append({'client': client, 'address': client.address, 'project': project})
    if not targets:
        raise ValueError('No saved hosted projects for %s at %s. Run hosted register for each project first.' % (agent, url))
    if len(targets) > 100:
        raise ValueError('At most 100 configured projects can be watched at once')
    return targets


def watch_many(targets, timeout):
    """One bounded long-poll per inbox; first mail/error ends the global wait.

    Daemon workers let a one-shot CLI exit immediately without waiting for
    unrelated HTTP long-polls. No background process or persisted cursor is
    created. Unread mail remains unread until explicitly read by the caller.
    """
    import math
    import queue
    import threading
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Watch timeout must be a positive finite number')
    deadline = time.monotonic() + timeout
    results = queue.Queue()
    stop = threading.Event()

    def poll(target):
        metadata = {k: target[k] for k in ('project', 'address')}
        try:
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                result = target['client'].watch(target['address'], timeout=min(60.0, remaining))
                if result.get('changed'):
                    results.put(('mail', {**metadata, **result}))
                    return
        except Exception as exc:
            # Include the project in failures; never mistake an inaccessible
            # inbox for an empty inbox or expose a credential in the error.
            message = exc.message if isinstance(exc, InboxError) else str(exc)
            results.put(('error', {**metadata, 'message': message}))

    for target in targets:
        threading.Thread(target=poll, args=(target,), daemon=True, name='inbox-watch-' + target['project']).start()
    mail, errors = [], []
    try:
        try:
            kind, result = results.get(timeout=max(0, deadline - time.monotonic()))
            (mail if kind == 'mail' else errors).append(result)
        except queue.Empty:
            pass
        # Include any other projects whose responses have already arrived.
        while True:
            try:
                kind, result = results.get_nowait()
                (mail if kind == 'mail' else errors).append(result)
            except queue.Empty:
                break
    finally:
        stop.set()
    return {'changed': bool(mail), 'projects': sorted(mail, key=lambda x: x['project']),
            'errors': sorted(errors, key=lambda x: x['project']), 'watched': len(targets)}
