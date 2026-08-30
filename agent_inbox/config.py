"""Configuration constants and paths for agent-inboxes."""

import os
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
DEFAULT_DIR = Path.home() / ".agent-inboxes"
DEFAULT_DB_FILENAME = "inbox.db"
LAUNCH_AGENT_LABEL = "com.nate.agent-inbox"
LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

DIR_MODE = 0o700
FILE_MODE = 0o600


def get_data_dir() -> Path:
    """Return the data directory, respecting AGENT_INBOX_DIR if set."""
    custom = os.environ.get("AGENT_INBOX_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return DEFAULT_DIR.resolve()


def get_db_path() -> Path:
    """Return the SQLite database path, respecting AGENT_INBOX_DB or AGENT_INBOX_DIR if set."""
    custom_db = os.environ.get("AGENT_INBOX_DB")
    if custom_db:
        return Path(custom_db).expanduser().resolve()
    return get_data_dir() / DEFAULT_DB_FILENAME


def get_server_url() -> str:
    """Return the loopback HTTP base URL, respecting AGENT_INBOX_URL if set."""
    custom_url = os.environ.get("AGENT_INBOX_URL")
    if custom_url:
        return custom_url.rstrip("/")
    host = get_host()
    port = get_port()
    return f"http://{host}:{port}"


def get_host() -> str:
    """Return the server host (always 127.0.0.1 by default)."""
    return os.environ.get("AGENT_INBOX_HOST", DEFAULT_HOST)


def get_port() -> int:
    """Return the server port."""
    custom_port = os.environ.get("AGENT_INBOX_PORT")
    if custom_port:
        try:
            return int(custom_port)
        except ValueError:
            pass
    return DEFAULT_PORT
