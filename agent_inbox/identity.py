"""Identity derivation for Agent Inboxes."""

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple, Union
from urllib.parse import urlparse

from agent_inbox.models import normalize_slug


def _extract_repo_name_from_url(url: str) -> Optional[str]:
    """Extract repository slug from git remote URL."""
    if not url:
        return None
    url = url.strip()

    # Handle SCP-like git URLs (e.g. git@github.com:owner/repo.git)
    if ":" in url and not url.startswith("http://") and not url.startswith("https://") and not url.startswith("ssh://") and not url.startswith("file://"):
        path_part = url.split(":", 1)[1]
    else:
        parsed = urlparse(url)
        path_part = parsed.path if parsed.path else url

    path_part = path_part.rstrip("/")
    if path_part.endswith(".git"):
        path_part = path_part[:-4]
    
    parts = [p for p in path_part.split("/") if p]
    if parts:
        return parts[-1]
    return None


def derive_project(cwd: Optional[Union[str, Path]] = None) -> str:
    """
    Derive the canonical project slug:
    1. AGENT_INBOX_PROJECT environment variable if set.
    2. Basename of git remote.origin.url.
    3. Git common worktree or repository root directory name.
    4. Fallback to current working directory name.
    """
    from agent_inbox.cloudsync import enabled, map_project, repository_identity
    if enabled():
        from agent_inbox.db import get_connection
        working = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        try:
            result = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=working,
                                    capture_output=True, text=True, timeout=2, check=False)
            raw_repo = result.stdout.strip() or str(working)
        except (OSError, subprocess.TimeoutExpired):
            raw_repo = str(working)
        repo = repository_identity(raw_repo)
        conn = get_connection()
        try:
            explicit = os.environ.get("AGENT_INBOX_PROJECT")
            if explicit:
                map_project(conn, raw_repo, normalize_slug(explicit))
            row = conn.execute("SELECT slug FROM project_mappings WHERE repo_identity=?", (repo,)).fetchone()
            if row is not None:
                return row[0]
            # Unmapped local work still works offline. Export is held with an
            # unresolved_project_mapping reason until explicitly resolved.
        finally:
            conn.close()

    env_project = os.environ.get("AGENT_INBOX_PROJECT")
    if env_project and env_project.strip():
        return normalize_slug(env_project.strip())

    working_dir = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()

    # Try git config remote.origin.url
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            repo_name = _extract_repo_name_from_url(res.stdout.strip())
            if repo_name:
                return normalize_slug(repo_name)
    except Exception:
        pass

    # Try git common-dir or top-level
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            top_level = Path(res.stdout.strip())
            return normalize_slug(top_level.name)
    except Exception:
        pass

    # Fallback to working directory name
    return normalize_slug(working_dir.name)


def derive_agent() -> str:
    """
    Derive the agent local-part slug:
    1. AGENT_INBOX_AGENT environment variable if set.
    2. Detected runtime family: claude, codex, orca, or fallback to 'agent'.
    """
    env_agent = os.environ.get("AGENT_INBOX_AGENT")
    if env_agent and env_agent.strip():
        return normalize_slug(env_agent.strip())

    # Detect runtime family from environment variables
    env_keys = os.environ.keys()
    
    # Check Codex indicators
    if any(k.startswith("CODEX_") for k in env_keys):
        return "codex"
    
    # Check Orca indicators
    if any(k.startswith("ORCA_") for k in env_keys):
        return "orca"
    
    # Check Claude indicators
    if any(k.startswith("CLAUDE_") for k in env_keys):
        return "claude"

    return "agent"


# Environment variables that carry a runtime-provided session identifier.
# Checked in order after the explicit AGENT_INBOX_SESSION override.
_RUNTIME_SESSION_ENV_KEYS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "ORCA_SESSION_ID",
    "ORCA_TASK_ID",
)


def _short_session_hash(value: str) -> str:
    """Stable short lowercase session slug of the form s-3f9a1c2b."""
    return "s-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def derive_session() -> str:
    """
    Derive a short session identifier distinguishing concurrent agents that
    share the same inbox address (e.g. two Claude sessions in one project):
    1. AGENT_INBOX_SESSION environment variable if set (normalized slug).
    2. A runtime session env var (CLAUDE_CODE_SESSION_ID, CODEX_SESSION_ID, ...),
       hashed to a stable short slug.
    3. CLAUDE_PID plus process start time, when supplied by the harness.
    4. Parent PID plus process start time as a best-effort fallback; separate
       tool shells require a runtime session ID or stable harness PID.
    """
    env_session = os.environ.get("AGENT_INBOX_SESSION")
    if env_session and env_session.strip():
        return normalize_slug(env_session.strip())[:32]

    for key in _RUNTIME_SESSION_ENV_KEYS:
        val = os.environ.get(key)
        if val and val.strip():
            return _short_session_hash(f"{key}:{val.strip()}")

    # A harness may invoke each CLI command through a different shell. Prefer
    # its stable agent PID to that shell's PPID, and include process birth time.
    claude_pid = os.environ.get("CLAUDE_PID", "")
    ppid = int(claude_pid) if claude_pid.isdigit() and int(claude_pid) > 0 else os.getppid()
    start_time = ""
    try:
        res = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(ppid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0:
            start_time = res.stdout.strip()
    except Exception:
        pass
    return _short_session_hash(f"ppid:{ppid}:{start_time}")


def derive_repo_key(cwd: Optional[Union[str, Path]] = None) -> Optional[str]:
    """
    Derive a stable repository identity key for file reservations.

    Two different repos that share a directory basename must not cross-conflict,
    while all worktrees of ONE repo must share a key. `git rev-parse
    --git-common-dir` points every worktree at the main repository's .git
    directory; hashing its symlink-resolved absolute path gives exactly that
    identity. Returns 12 lowercase hex chars, or None outside a git repo.

    AGENT_INBOX_REPO_KEY overrides derivation (useful for tests and non-git
    setups); it is normalized to lowercase [0-9a-f-] and truncated to 64 chars.
    """
    env_key = os.environ.get("AGENT_INBOX_REPO_KEY")
    if env_key and env_key.strip():
        cleaned = "".join(c for c in env_key.strip().lower() if c.isalnum() or c == "-")
        return cleaned[:64] or None

    working_dir = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        common_dir = Path(res.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = working_dir / common_dir
        real = common_dir.resolve()
        return hashlib.sha256(str(real).encode("utf-8")).hexdigest()[:12]
    except Exception:
        return None


def derive_identity(cwd: Optional[Union[str, Path]] = None) -> Tuple[str, str, str]:
    """
    Derive full identity tuple: (agent_slug, project_slug, full_address).
    """
    agent = derive_agent()
    project = derive_project(cwd)
    address = f"{agent}@{project}"
    return agent, project, address
