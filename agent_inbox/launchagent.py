"""macOS LaunchAgent management for Agent Inboxes."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

from agent_inbox.config import (
    DIR_MODE,
    FILE_MODE,
    LAUNCH_AGENT_LABEL,
    LAUNCH_AGENT_PLIST,
    get_data_dir,
)


def generate_plist_dict(python_path: Optional[str] = None) -> dict:
    """Generate the plist configuration dictionary."""
    py_exec = python_path or sys.executable
    data_dir = get_data_dir()
    log_out = str(data_dir / "server.log")
    log_err = str(data_dir / "server.err.log")

    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            py_exec,
            "-m",
            "agent_inbox.cli",
            "serve",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_out,
        "StandardErrorPath": log_err,
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        },
    }


def generate_plist_xml(python_path: Optional[str] = None) -> str:
    """Generate XML string for the LaunchAgent plist."""
    plist_dict = generate_plist_dict(python_path)
    return plistlib.dumps(plist_dict).decode("utf-8")


def install_launchagent(plist_path: Optional[Path] = None, python_path: Optional[str] = None) -> Path:
    """Write the LaunchAgent plist to disk."""
    target_plist = plist_path or LAUNCH_AGENT_PLIST
    target_plist.parent.mkdir(parents=True, exist_ok=True)
    
    # Ensure data directory exists
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, DIR_MODE)
    except OSError:
        pass

    xml_content = generate_plist_xml(python_path)
    target_plist.write_text(xml_content, encoding="utf-8")
    try:
        os.chmod(target_plist, 0o644)
    except OSError:
        pass
    return target_plist


def unload_launchagent(plist_path: Optional[Path] = None) -> bool:
    """Unload the LaunchAgent via launchctl."""
    target_plist = plist_path or LAUNCH_AGENT_PLIST
    if not target_plist.exists():
        return False
    try:
        res = subprocess.run(
            ["launchctl", "unload", str(target_plist)],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def load_launchagent(plist_path: Optional[Path] = None) -> bool:
    """Load the LaunchAgent via launchctl."""
    target_plist = plist_path or LAUNCH_AGENT_PLIST
    if not target_plist.exists():
        return False
    try:
        # First attempt unload in case old version was loaded
        subprocess.run(
            ["launchctl", "unload", str(target_plist)],
            capture_output=True,
            text=True,
            check=False,
        )
        res = subprocess.run(
            ["launchctl", "load", "-w", str(target_plist)],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def uninstall_launchagent(plist_path: Optional[Path] = None) -> bool:
    """Unload and remove the LaunchAgent plist."""
    target_plist = plist_path or LAUNCH_AGENT_PLIST
    unload_launchagent(target_plist)
    if target_plist.exists():
        try:
            target_plist.unlink()
            return True
        except OSError:
            return False
    return True
