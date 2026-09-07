"""Loopback HTTP Server implementation for Agent Inboxes."""

import json
import sqlite3
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from agent_inbox import __version__
from agent_inbox.config import get_db_path, get_host, get_port
from agent_inbox.db import get_connection
from agent_inbox.models import (
    ConflictError,
    InboxError,
    NotFoundError,
    ValidationError,
)
from agent_inbox.service import InboxService


# The Nate's Software web suite (served from these origins) makes browser
# fetch() calls directly to this loopback service. Because that is a
# cross-origin request, we must emit CORS headers or the browser blocks it.
# The service is unauthenticated, so we do NOT reflect arbitrary origins with
# '*'; we allow only the known web-suite origins plus localhost dev servers.
ALLOWED_WEB_ORIGINS = frozenset({
    "https://nates-software.com",
    "https://www.nates-software.com",
    "https://nates-software.pages.dev",
    "http://localhost:5173",
    "http://localhost:4173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:4173",
    "http://127.0.0.1:3000",
})


class InboxRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Agent Inboxes local loopback API."""

    # Disable default logging to stderr in quiet mode / handle explicitly
    def log_message(self, format: str, *args: Any) -> None:
        # Override to prevent unsolicited stderr spam during tests
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def _cors_origin(self) -> Optional[str]:
        """Return the request Origin iff it's an allowed web-suite origin."""
        origin = self.headers.get("Origin")
        if origin and origin in ALLOWED_WEB_ORIGINS:
            return origin
        return None

    def _send_cors_headers(self) -> None:
        """Emit CORS headers for an allowed origin. Call before end_headers()."""
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Idempotency-Key, Accept",
            )
            # A page on https://nates-software.com fetching http://127.0.0.1
            # is a public->loopback request; Chrome's Private Network Access
            # rules require the server to opt in on the preflight, or the
            # browser blocks it even with valid CORS.
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self) -> None:
        """Answer CORS preflight requests (browsers send these before
        POST/JSON or requests carrying the Idempotency-Key header)."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        """Send JSON response with appropriate headers."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc: InboxError) -> None:
        """Send formatted JSON error response."""
        self._send_json(exc.status_code, exc.to_dict())

    def _read_json_body(self) -> Dict[str, Any]:
        """Read and parse JSON request body."""
        content_length_str = self.headers.get("Content-Length")
        if not content_length_str:
            return {}
        try:
            length = int(content_length_str)
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError) as e:
            raise ValidationError("invalid_json", f"Request body must be valid JSON: {str(e)}")

    def _get_idempotency_key(self) -> str:
        """Extract Idempotency-Key header or raise ValidationError."""
        key = self.headers.get("Idempotency-Key")
        if not key or not key.strip():
            raise ValidationError("missing_idempotency_key", "Idempotency-Key header is required")
        return key.strip()

    def _touch_actor(self) -> None:
        """Refresh the caller's agent lease from the X-Agent-Address header.
        Best-effort: only updates existing leases, never fails a request."""
        try:
            addr = self.headers.get("X-Agent-Address", "").strip()
            if addr and "@" in addr:
                agent, project = addr.split("@", 1)
                self._get_service().touch_lease(agent, project)
        except Exception:
            pass

    def _get_service(self) -> InboxService:
        """Get InboxService attached to this request thread's DB connection."""
        return InboxService(self.server.get_thread_connection())

    def _get_session_id(self) -> Optional[str]:
        """Optional X-Agent-Session header identifying the calling agent session."""
        sid = self.headers.get("X-Agent-Session")
        if sid and sid.strip():
            return sid.strip()[:64]
        return None

    def _get_session_pid(self) -> Optional[int]:
        """Optional X-Agent-Pid header (best-effort, informational)."""
        raw = self.headers.get("X-Agent-Pid")
        if raw:
            try:
                return int(raw.strip())
            except ValueError:
                pass
        return None

    def _touch_session(self, service: InboxService, address: str) -> Optional[str]:
        """Record session activity for the acting address if a session header
        is present. Returns the session id (or None). Never fails the request."""
        sid = self._get_session_id()
        if sid:
            try:
                service.touch_session(address, sid, pid=self._get_session_pid())
            except InboxError:
                return None
        return sid

    def do_GET(self) -> None:
        """Route GET requests."""
        try:
            self._touch_actor()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            # /healthz
            if path == "/healthz":
                # Check DB responsiveness
                try:
                    self.server.get_thread_connection().execute("SELECT 1").fetchone()
                    db_status = "ok"
                except Exception:
                    db_status = "error"
                self._send_json(HTTPStatus.OK, {
                    "status": "ok",
                    "db": db_status,
                    "version": __version__,
                })
                return

            # /v1/inboxes
            if path == "/v1/inboxes":
                service = self._get_service()
                project_slug = query.get("project", [None])[0]
                inboxes = service.list_inboxes(project_slug)
                self._send_json(HTTPStatus.OK, {"inboxes": inboxes})
                return

            # Path matching for /v1/inboxes/{address}/threads...
            parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
            if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "inboxes":
                address = parts[2]
                
                # /v1/inboxes/{address}/watch — long-poll for new unread mail.
                # Blocks this request's worker thread only (the server is
                # threaded with per-thread DB connections), polling SQLite
                # about once per second until new unread mail newer than the
                # `after` cursor arrives or the timeout elapses.
                if len(parts) == 4 and parts[3] == "watch":
                    service = self._get_service()
                    try:
                        timeout = float(query.get("timeout", ["60"])[0])
                    except ValueError:
                        timeout = 60.0
                    timeout = max(0.0, min(timeout, 300.0))
                    try:
                        after = int(query.get("after", ["0"])[0])
                    except ValueError:
                        after = 0

                    self._touch_session(service, address)
                    deadline = time.monotonic() + timeout
                    while True:
                        state = service.watch_state(address, after=after)
                        remaining = deadline - time.monotonic()
                        if state["changed"] or remaining <= 0:
                            break
                        time.sleep(min(1.0, remaining))
                    self._send_json(HTTPStatus.OK, state)
                    return

                # /v1/inboxes/{address}/threads
                if len(parts) == 4 and parts[3] == "threads":
                    service = self._get_service()
                    self._touch_session(service, address)
                    unread_val = query.get("unread", ["false"])[0].lower()
                    unread_only = unread_val in ("true", "1", "yes")
                    limit_str = query.get("limit", ["50"])[0]
                    try:
                        limit = int(limit_str)
                    except ValueError:
                        limit = 50
                    threads = service.list_threads(address, unread_only=unread_only, limit=limit)
                    self._send_json(HTTPStatus.OK, {"threads": threads})
                    return

                # /v1/inboxes/{address}/threads/{thread_id}
                if len(parts) == 5 and parts[3] == "threads":
                    thread_id = parts[4]
                    service = self._get_service()
                    self._touch_session(service, address)
                    thread_data = service.get_thread(address, thread_id)
                    self._send_json(HTTPStatus.OK, thread_data)
                    return

            raise NotFoundError("not_found", f"Cannot GET {path}")

        except InboxError as e:
            self._send_error(e)
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": {"code": "internal_error", "message": str(e)}
            })

    def do_PUT(self) -> None:
        """Route PUT requests."""
        try:
            self._touch_actor()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]

            # PUT /v1/inboxes/{address}
            if len(parts) == 3 and parts[0] == "v1" and parts[1] == "inboxes":
                address = parts[2]
                body = self._read_json_body()
                display_name = body.get("display_name")
                service = self._get_service()
                result = service.ensure_inbox(address, display_name=display_name)
                sid = self._touch_session(service, address)
                if sid:
                    result["session_id"] = sid
                    result["active_sessions"] = len(service.active_sessions(address))
                status = HTTPStatus.CREATED if result["created"] else HTTPStatus.OK
                self._send_json(status, result)
                return

            raise NotFoundError("not_found", f"Cannot PUT {path}")

        except InboxError as e:
            self._send_error(e)
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": {"code": "internal_error", "message": str(e)}
            })

    def do_POST(self) -> None:
        """Route POST requests."""
        try:
            self._touch_actor()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]

            # POST /v1/leases/claim | /v1/leases/release
            if path == "/v1/leases/claim":
                body = self._read_json_body()
                service = self._get_service()
                result = service.claim_agent(body.get("family", ""), body.get("project", ""))
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/v1/leases/release":
                body = self._read_json_body()
                service = self._get_service()
                result = service.release_agent(body.get("agent", ""), body.get("project", ""))
                self._send_json(HTTPStatus.OK, result)
                return

            # POST /v1/emails
            if path == "/v1/emails":
                idempotency_key = self._get_idempotency_key()
                body = self._read_json_body()
                from_addr = body.get("from")
                to_addrs = body.get("to", [])
                cc_addrs = body.get("cc", [])
                subject = body.get("subject", "")
                body_markdown = body.get("body_markdown", "")

                service = self._get_service()
                sender_session = self._get_session_id()
                res = service.send_email(
                    from_addr=from_addr,
                    to_addrs=to_addrs,
                    cc_addrs=cc_addrs,
                    subject=subject,
                    body_markdown=body_markdown,
                    client_token=idempotency_key,
                    sender_session=sender_session,
                )
                if from_addr:
                    self._touch_session(service, from_addr)
                self._send_json(HTTPStatus.CREATED, res)
                return

            # POST /v1/emails/{email_id}/reply
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "emails" and parts[3] == "reply":
                email_id = parts[2]
                idempotency_key = self._get_idempotency_key()
                body = self._read_json_body()
                from_addr = body.get("from")
                body_markdown = body.get("body_markdown", "")
                to_addrs = body.get("to")
                cc_addrs = body.get("cc")

                service = self._get_service()
                sender_session = self._get_session_id()
                res = service.reply_email(
                    reply_to_email_id=email_id,
                    from_addr=from_addr,
                    body_markdown=body_markdown,
                    client_token=idempotency_key,
                    to_addrs=to_addrs,
                    cc_addrs=cc_addrs,
                    sender_session=sender_session,
                )
                if from_addr:
                    self._touch_session(service, from_addr)
                self._send_json(HTTPStatus.CREATED, res)
                return

            # POST /v1/inboxes/{address}/threads/{thread_id}/read
            if len(parts) == 6 and parts[0] == "v1" and parts[1] == "inboxes" and parts[3] == "threads" and parts[5] == "read":
                address = parts[2]
                thread_id = parts[4]
                service = self._get_service()
                self._touch_session(service, address)
                count = service.mark_thread_read(address, thread_id)
                self._send_json(HTTPStatus.OK, {
                    "thread_id": thread_id,
                    "marked_read": count,
                })
                return

            raise NotFoundError("not_found", f"Cannot POST {path}")

        except InboxError as e:
            self._send_error(e)
        except Exception as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": {"code": "internal_error", "message": str(e)}
            })


class AgentInboxServer(ThreadingHTTPServer):
    """Threaded HTTP server with one SQLite connection per request thread.

    The server must be threaded so a blocked long-poll ``watch`` request cannot
    freeze other requests. SQLite connections are NOT safe for interleaved
    transactions across threads, so each request thread lazily opens its own
    connection (``threading.local``) to the same database file; WAL mode plus
    the 5000ms busy timeout make concurrent readers/writers safe. The ``db_conn``
    passed by the caller is kept for lifecycle compatibility (tests/serve close
    it) and to resolve the database file path.
    """

    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], db_conn: sqlite3.Connection, verbose: bool = False):
        super().__init__(server_address, InboxRequestHandler)
        self.db_conn = db_conn
        self.verbose = verbose
        # Resolve the backing database file so per-thread connections can be
        # opened against it. PRAGMA database_list reports (seq, name, file).
        db_file = None
        for row in db_conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main":
                db_file = row[2]
                break
        self.db_path = db_file if db_file else None
        self._thread_db = threading.local()

    def get_thread_connection(self) -> sqlite3.Connection:
        """Return this thread's SQLite connection, opening it on first use.

        Falls back to the shared ``db_conn`` only for a non-file (in-memory)
        database, where a second connection would not see the same data.
        Per-thread connections are closed by GC when their request thread dies.
        """
        if not self.db_path:
            return self.db_conn
        conn = getattr(self._thread_db, "conn", None)
        if conn is None:
            conn = get_connection(self.db_path)
            self._thread_db.conn = conn
        return conn


# The service is unauthenticated by design and MUST stay on the loopback
# interface. Binding it to 0.0.0.0 or a LAN address would expose every inbox
# on the machine to the local network. Fail loudly rather than silently
# rebinding, so whoever set a non-loopback host learns why it was refused.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def run_server(host: Optional[str] = None, port: Optional[int] = None, db_path: Optional[str] = None, verbose: bool = False) -> None:
    """Run loopback server in foreground."""
    target_host = host or get_host()
    target_port = port or get_port()

    if target_host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing to bind agent-inbox to non-loopback host '{target_host}'. "
            f"This service is unauthenticated and local-only; it may only bind "
            f"{' or '.join(sorted(LOOPBACK_HOSTS))}. "
            f"Remove --host / AGENT_INBOX_HOST or set it to 127.0.0.1."
        )

    conn = get_connection(db_path)
    
    server = AgentInboxServer((target_host, target_port), conn, verbose=verbose)
    print(f"Agent Inboxes service listening on http://{target_host}:{target_port}")
    print(f"Database: {get_db_path() if not db_path else db_path}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()
        conn.close()
