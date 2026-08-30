"""Identity derivation for Agent Inboxes."""

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


def derive_identity(cwd: Optional[Union[str, Path]] = None) -> Tuple[str, str, str]:
    """
    Derive full identity tuple: (agent_slug, project_slug, full_address).
    """
    agent = derive_agent()
    project = derive_project(cwd)
    address = f"{agent}@{project}"
    return agent, project, address
