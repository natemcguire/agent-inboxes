"""Project instruction block setup for AGENTS.md and CLAUDE.md."""

import re
from pathlib import Path
from typing import List, Optional, Union

START_MARKER = "<!-- agent-inboxes:start -->"
END_MARKER = "<!-- agent-inboxes:end -->"

MANAGED_BLOCK = """<!-- agent-inboxes:start -->
## Agent Inboxes

Use the local `agent-inbox` CLI for durable coordination with agents in other sessions, worktrees, or projects. Your address is `<agent>@<project>`: the project is derived from Git, and the agent name comes from `AGENT_INBOX_AGENT` or your runtime family. Run `agent-inbox whoami` before using it. Concurrent agents of the same family in one project must be launched with unique names such as `AGENT_INBOX_AGENT=codex-worker1`.

Polling checkpoints:
- At session start, run `agent-inbox list --unread` and handle relevant mail before new work.
- Immediately before a task expected to take more than 10 minutes, check unread mail again.
- After finishing or handing off work, send any required completion reply, then check unread mail once more before ending the session.
- Do not background-poll or claim real-time delivery; these checkpoints are the contract.

Addressing:
- Use full lowercase addresses. Use `agent-inbox inboxes --project <slug>` when the recipient is not already named in the task; do not guess a person's address.
- Put action owners in `to` and observers in `cc`. Message only the smallest relevant set of agents.
- Use `send` for a new topic and `reply` for an existing one. Keep one topic per thread, preserve the subject, and reply to the newest relevant email.

Send mail for cross-session requests, blockers, handoffs, decisions that change another agent's work, shared interface changes, and completion notices another agent is waiting for. Do not send routine progress chatter, information already recorded in the repo/ticket, or notes only useful to your current session.

Agent Inboxes never reserves files or grants permission to edit them. Use the separate NB-7 file reservation system for write-lock coordination; a message about a file is not a lock.

Core commands:
`agent-inbox send --to <address> --subject "<topic>" --body-file <path-or->`
`agent-inbox list --unread`
`agent-inbox read <thread-id>`
`agent-inbox reply <email-id> --body-file <path-or->`
<!-- agent-inboxes:end -->"""


def inject_or_replace_block(content: str) -> str:
    """Replace existing managed block or append to content."""
    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        re.DOTALL,
    )
    if pattern.search(content):
        return pattern.sub(MANAGED_BLOCK, content)
    else:
        trimmed = content.rstrip()
        if trimmed:
            return f"{trimmed}\n\n{MANAGED_BLOCK}\n"
        return f"{MANAGED_BLOCK}\n"


def setup_project(target_dir: Optional[Union[str, Path]] = None) -> List[Path]:
    """
    Ensure AGENTS.md and CLAUDE.md have the managed block.
    If neither file exists, create AGENTS.md.
    Returns list of updated or created file paths.
    """
    root = Path(target_dir).resolve() if target_dir else Path.cwd().resolve()
    agents_file = root / "AGENTS.md"
    claude_file = root / "CLAUDE.md"

    updated = []

    candidates = [f for f in (agents_file, claude_file) if f.exists()]
    if not candidates:
        # Neither exists -> create AGENTS.md
        agents_file.write_text(f"# Project Instructions\n\n{MANAGED_BLOCK}\n", encoding="utf-8")
        updated.append(agents_file)
    else:
        for f in candidates:
            orig = f.read_text(encoding="utf-8")
            new_content = inject_or_replace_block(orig)
            if new_content != orig:
                f.write_text(new_content, encoding="utf-8")
            updated.append(f)

    return updated
