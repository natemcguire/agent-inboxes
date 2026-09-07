"""HTTP client library for Agent Inboxes communicating over loopback."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from agent_inbox.config import get_server_url
from agent_inbox.models import (
    ConflictError,
    InboxError,
    NotFoundError,
    ServerNotRunningError,
    ValidationError,
)


class InboxClient:
    """Client for interacting with local Agent Inboxes service via HTTP."""

    def __init__(self, base_url: Optional[str] = None, session_id: Optional[str] = None):
        self.base_url = (base_url or get_server_url()).rstrip("/")
        # Optional short session slug identifying THIS agent session; sent as
        # X-Agent-Session so the service can distinguish concurrent same-family
        # agents sharing one inbox address.
        self.session_id = session_id

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Perform HTTP request and return parsed JSON response."""
        full_path = path
        if query:
            clean_query = {k: str(v) for k, v in query.items() if v is not None}
            if clean_query:
                full_path = f"{path}?{urllib.parse.urlencode(clean_query)}"

        url = f"{self.base_url}{full_path}"
        req_headers = {
            "Accept": "application/json",
        }
        # Identify the calling agent so the server can refresh its lease. Best-effort:
        # identity derivation must never block a request.
        try:
            from agent_inbox.identity import derive_identity
            req_headers["X-Agent-Address"] = derive_identity()[2]
        except Exception:
            pass
        if self.session_id:
            req_headers["X-Agent-Session"] = self.session_id
            req_headers["X-Agent-Pid"] = str(os.getpid())
        if headers:
            req_headers.update(headers)

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json; charset=utf-8"

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read().decode("utf-8")
                if not resp_body.strip():
                    return {}
                return json.loads(resp_body)
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8")
            try:
                err_json = json.loads(raw_err)
                err_data = err_json.get("error", {})
                code = err_data.get("code", "http_error")
                message = err_data.get("message", f"HTTP {e.code}: {e.reason}")
            except Exception:
                code = "http_error"
                message = f"HTTP {e.code}: {e.reason}"

            if e.code == 400:
                raise ValidationError(code, message)
            elif e.code == 404:
                raise NotFoundError(code, message)
            elif e.code == 409:
                raise ConflictError(code, message)
            else:
                raise InboxError(code, message, status_code=e.code)

        except (urllib.error.URLError, ConnectionError, OSError) as e:
            raise ServerNotRunningError(
                f"Local agent-inbox service is not reachable at {self.base_url}. "
                "Start it with `agent-inbox serve` or `agent-inbox setup`."
            ) from e

    def claim_lease(self, family: str, project: str) -> Dict[str, Any]:
        """Claim the lowest free agent slot for a runtime family in a project."""
        return self._request("POST", "/v1/leases/claim", body={"family": family, "project": project})

    def release_lease(self, agent: str, project: str) -> Dict[str, Any]:
        """Release this agent's lease so the slot frees immediately."""
        return self._request("POST", "/v1/leases/release", body={"agent": agent, "project": project})

    def healthz(self) -> Dict[str, Any]:
        """Check service health."""
        return self._request("GET", "/healthz")

    def put_inbox(self, address: str, display_name: Optional[str] = None) -> Dict[str, Any]:
        """Idempotently register or touch an inbox."""
        encoded_addr = urllib.parse.quote(address, safe="@")
        body = {}
        if display_name is not None:
            body["display_name"] = display_name
        return self._request("PUT", f"/v1/inboxes/{encoded_addr}", body=body)

    def list_inboxes(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """List inboxes, optionally filtered by project slug."""
        query = {"project": project} if project else None
        res = self._request("GET", "/v1/inboxes", query=query)
        return res.get("inboxes", [])

    def send_email(
        self,
        from_addr: str,
        to_addrs: List[str],
        cc_addrs: Optional[List[str]] = None,
        subject: str = "",
        body_markdown: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a thread and send the first email."""
        key = idempotency_key or str(uuid.uuid4())
        body = {
            "from": from_addr,
            "to": to_addrs,
            "cc": cc_addrs or [],
            "subject": subject,
            "body_markdown": body_markdown,
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
        query: Dict[str, Any] = {"timeout": int(timeout)}
        if after is not None:
            query["after"] = int(after)
        return self._request(
            "GET",
            f"/v1/inboxes/{encoded_addr}/watch",
            query=query,
            timeout=timeout + 10.0,
        )

    def mark_thread_read(self, address: str, thread_id: str) -> Dict[str, Any]:
        """Mark every email in thread as read for the inbox."""
        encoded_addr = urllib.parse.quote(address, safe="@")
        return self._request("POST", f"/v1/inboxes/{encoded_addr}/threads/{thread_id}/read")
