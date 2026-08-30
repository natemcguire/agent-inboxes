"""Data models, validation rules, ID generators, and timestamp helpers."""

import datetime
import re
import uuid

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ADDRESS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}@[a-z0-9][a-z0-9-]{0,62}$")


class InboxError(Exception):
    """Base exception for Agent Inboxes domain errors."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }


class ValidationError(InboxError):
    """Validation failed (400 Bad Request)."""

    def __init__(self, code: str, message: str):
        super().__init__(code=code, message=message, status_code=400)


class NotFoundError(InboxError):
    """Resource not found (404 Not Found)."""

    def __init__(self, code: str, message: str):
        super().__init__(code=code, message=message, status_code=404)


class ConflictError(InboxError):
    """Resource conflict (409 Conflict)."""

    def __init__(self, code: str, message: str):
        super().__init__(code=code, message=message, status_code=409)


class ServerNotRunningError(InboxError):
    """Service is not running on 127.0.0.1:8791."""

    def __init__(self, message: str = "Local agent-inbox service is not running on 127.0.0.1:8791"):
        super().__init__(code="server_not_running", message=message, status_code=503)


def is_valid_slug(slug: str) -> bool:
    """Return True if slug matches [a-z0-9][a-z0-9-]{0,62}."""
    if not isinstance(slug, str):
        return False
    return bool(SLUG_PATTERN.match(slug.lower()))


def is_valid_address(address: str) -> bool:
    """Return True if address matches <agent-slug>@<project-slug>."""
    if not isinstance(address, str):
        return False
    return bool(ADDRESS_PATTERN.match(address.lower()))


def normalize_slug(raw: str) -> str:
    """
    Sanitize and canonicalize a raw string into a valid slug.
    Lowercases, replaces spaces/underscores/dots with dashes, trims leading dashes,
    removes disallowed chars, and caps length at 63.
    """
    if not raw:
        return "unknown"
    s = raw.lower().strip()
    s = re.sub(r"[\s_.]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    if not s or not s[0].isalnum():
        s = f"a{s}"
    s = s[:63]
    return s


def normalize_address(raw: str) -> str:
    """Canonicalize a valid address to lowercase or raise ValidationError."""
    if not raw or not isinstance(raw, str):
        raise ValidationError("invalid_address", "Address must be a non-empty string")
    clean = raw.strip().lower()
    if not ADDRESS_PATTERN.match(clean):
        raise ValidationError("invalid_address", f"Address '{raw}' is invalid. Must match <agent-slug>@<project-slug>")
    return clean


def parse_address(address: str) -> tuple[str, str]:
    """Parse address into (local_part, project_slug), validating format."""
    canonical = normalize_address(address)
    local_part, project_slug = canonical.split("@", 1)
    return local_part, project_slug


def generate_thread_id() -> str:
    """Generate opaque thread ID prefixed with thr_."""
    return f"thr_{uuid.uuid4().hex}"


def generate_email_id() -> str:
    """Generate opaque email ID prefixed with eml_."""
    return f"eml_{uuid.uuid4().hex}"


def utc_now_iso() -> str:
    """Return RFC 3339 UTC timestamp string with millisecond precision ending in Z."""
    now = datetime.datetime.now(datetime.timezone.utc)
    # Format: YYYY-MM-DDTHH:MM:SS.mmmZ
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
