"""Command-Line Interface for Agent Inboxes."""

import argparse
import errno
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from agent_inbox import __version__
from agent_inbox.client import InboxClient
from agent_inbox.cloudsync import DEFAULT_URL, CloudClient, load_config, save_config
from agent_inbox.config import (
    DIR_MODE,
    LAUNCH_AGENT_PLIST,
    get_data_dir,
    get_db_path,
    get_host,
    get_port,
    get_server_url,
)
from agent_inbox.identity import derive_agent, derive_identity, derive_project, derive_repo_key, derive_session
from agent_inbox.launchagent import (
    install_launchagent,
    load_launchagent,
    uninstall_launchagent,
)
from agent_inbox.models import InboxError, ServerNotRunningError
from agent_inbox.hooks import hooks_status, install_hooks, run_hook_check, uninstall_hooks
from agent_inbox.project_setup import ONBOARDING_PROMPT, copy_to_clipboard, setup_global, setup_project
from agent_inbox.server import run_server


def _print_error(message: str, code: Optional[str] = None) -> None:
    """Print formatted error to stderr."""
    if code:
        sys.stderr.write(f"Error ({code}): {message}\n")
    else:
        sys.stderr.write(f"Error: {message}\n")


def _read_body(body_arg: Optional[str], body_file_arg: Optional[str]) -> str:
    """Read body from text argument, file, or stdin."""
    if body_file_arg is not None:
        if body_file_arg == "-":
            return sys.stdin.read()
        p = Path(body_file_arg)
        if not p.exists():
            raise ValueError(f"Body file not found: {body_file_arg}")
        return p.read_text(encoding="utf-8")
    if body_arg is not None:
        return body_arg
    raise ValueError("Either --body or --body-file is required")


def cmd_whoami(args: argparse.Namespace, client: InboxClient) -> int:
    """Print and auto-create the derived inbox address and session id."""
    try:
        _, _, address = derive_identity()
        res = client.put_inbox(address)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(address)
            session_id = res.get("session_id") or client.session_id
            if session_id:
                print(f"session: {session_id}")
            others = (res.get("active_sessions") or 1) - 1
            if others > 0:
                plural = "s are" if others != 1 else " is"
                print(
                    f"note: {others} other session{plural} currently active on {address} — "
                    f"you are not the only agent at this address."
                )
        return 0
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Run loopback server in foreground."""
    try:
        run_server(host=args.host, port=args.port, db_path=args.db, verbose=args.verbose)
        return 0
    except OSError as e:
        if getattr(e, "errno", None) == errno.EADDRINUSE:
            _print_error(
                f"Port {args.port} is already in use. Check the existing service with "
                f"python3 -m agent_inbox.operations status; use restart for the installed LaunchAgent."
            )
            return 1
        _print_error(str(e))
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_announcements(args, client):
    try:
        if args.command == "announce":
            sender = args.from_addr or derive_identity()[2]
            result = client.post_announcement(sender, args.subject, _read_body(args.body, args.body_file), args.all_projects, args.idempotency_key)
        else:
            viewer = args.inbox or derive_identity()[2]
            if args.ack:
                result = client.acknowledge_announcement(args.ack, viewer)
            else:
                result = {"reading_as": viewer, "announcements": client.list_announcements(viewer, args.unread, args.limit)}
        # Structured output preserves the distinction between routing metadata and body.
        print(json.dumps(result, indent=2))
        return 0
    except (InboxError, ValueError, OSError) as e:
        _print_error(str(e))
        return 1


def cmd_send(args: argparse.Namespace, client: InboxClient) -> int:
    """Send an email to start a new thread."""
    try:
        from_addr = args.from_addr
        if not from_addr:
            _, _, from_addr = derive_identity()

        # Parse recipients (support multiple --to / --cc and comma-separated)
        to_list = []
        for t in args.to or []:
            to_list.extend([x.strip() for x in t.split(",") if x.strip()])

        cc_list = []
        for c in args.cc or []:
            cc_list.extend([x.strip() for x in c.split(",") if x.strip()])

        body = _read_body(args.body, args.body_file)

        res = client.send_email(
            from_addr=from_addr,
            to_addrs=to_list,
            cc_addrs=cc_list,
            subject=args.subject,
            body_markdown=body,
        )

        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Sent email {res['email_id']} in thread {res['thread_id']} ({res.get('delivery_status', 'local only')})")
        return 0
    except ValueError as e:
        _print_error(str(e), "invalid_argument")
        return 1
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def _normalized_subject(text: str) -> str:
    import re as _re
    text = (text or "").strip().casefold()
    text = _re.sub(r"^(re|fwd?):\s*", "", text)
    return _re.sub(r"\s+", " ", text)


def cmd_reply(args: argparse.Namespace, client: InboxClient) -> int:
    """Reply to an existing email in a thread.

    Threading discipline: one topic per thread. Accepts an email id (eml_...)
    or a thread id (thr_... — replies to its newest email). When --subject is
    declared and differs materially from the thread's subject, the reply is
    refused: a different topic belongs in a NEW thread via `send`.
    """
    try:
        from_addr = args.from_addr
        if not from_addr:
            _, _, from_addr = derive_identity()

        body = _read_body(args.body, args.body_file)

        email_id = args.email_id
        thread_subject = None
        if email_id.startswith("thr_"):
            thread = client.get_thread(from_addr, email_id)
            thread_subject = str(thread.get("subject") or (thread.get("thread") or {}).get("subject") or "")
            emails = thread.get("emails") or []
            if not emails:
                _print_error("Thread has no emails to reply to.", "invalid_argument")
                return 1
            email_id = emails[-1]["email_id"]
        declared = getattr(args, "subject", None)
        if declared is not None:
            if thread_subject is None:
                _print_error(
                    "To declare --subject on a reply, pass the thread id (thr_...) so the topic can be checked — or drop --subject. A different topic belongs in a new thread: use `send`.",
                    "invalid_argument",
                )
                return 1
            if _normalized_subject(declared) != _normalized_subject(thread_subject):
                _print_error(
                    f"different topic → use send. This thread is '{thread_subject}'; your declared subject is '{declared}'. One topic per thread.",
                    "different_topic",
                )
                return 1

        to_list = None
        if args.to:
            to_list = []
            for t in args.to:
                to_list.extend([x.strip() for x in t.split(",") if x.strip()])

        cc_list = None
        if args.cc:
            cc_list = []
            for c in args.cc:
                cc_list.extend([x.strip() for x in c.split(",") if x.strip()])

        res = client.reply_email(
            email_id=email_id,
            from_addr=from_addr,
            body_markdown=body,
            to_addrs=to_list,
            cc_addrs=cc_list,
        )

        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if thread_subject is None:
                try:
                    thread = client.get_thread(from_addr, res["thread_id"])
                    thread_subject = str(thread.get("subject") or (thread.get("thread") or {}).get("subject") or "")
                except Exception:
                    thread_subject = ""
            if thread_subject:
                print(f"Replying in thread: '{thread_subject}'")
            print(f"Sent reply {res['email_id']} in thread {res['thread_id']} ({res.get('delivery_status', 'local only')})")
        return 0
    except ValueError as e:
        _print_error(str(e), "invalid_argument")
        return 1
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_list(args: argparse.Namespace, client: InboxClient) -> int:
    """List threads for the active or specified inbox."""
    try:
        inbox = args.inbox
        if not inbox:
            _, _, inbox = derive_identity()

        threads = client.list_threads(inbox, unread=args.unread, limit=args.limit)

        if args.json:
            print(json.dumps({"threads": threads}, indent=2))
        else:
            if not threads:
                status = "unread " if args.unread else ""
                print(f"No {status}threads found for {inbox}.")
                from agent_inbox.updates import update_notice
                notice = update_notice()
                if notice:
                    print(notice)
                return 0

            print(f"Threads for {inbox}:")
            for t in threads:
                unread_flag = f" [{t['unread_count']} unread]" if t["unread_count"] > 0 else ""
                parts = ", ".join(t["participants"])
                print(f"• {t['thread_id']} - {t['subject']}{unread_flag}")
                print(f"  Participants: {parts}")
                print(f"  Last active: {t['last_email_at']}\n")
        from agent_inbox.updates import update_notice
        notice = update_notice()
        if notice:
            print(notice, file=sys.stderr if args.json else sys.stdout)
        return 0
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_read(args: argparse.Namespace, client: InboxClient) -> int:
    """Read a thread, then mark it read."""
    try:
        inbox = args.inbox
        if not inbox:
            _, _, inbox = derive_identity()

        thread_data = client.get_thread(inbox, args.thread_id)

        if args.json:
            print(json.dumps(thread_data, indent=2))
        else:
            print(f"Reading as: {thread_data.get('reading_as', inbox)} (To = action owner; CC = observer)")
            print(f"Thread: {thread_data['thread_id']} — {thread_data['subject']}\n" + "=" * 60)
            for eml in thread_data.get("emails", []):
                to_str = ", ".join(eml["to"])
                cc_str = f" | CC: {', '.join(eml['cc'])}" if eml["cc"] else ""
                read_status = "" if eml["read"] else " [UNREAD]"
                sess = eml.get("sender_session")
                from_str = f"{eml['from']} (session {sess})" if sess else eml["from"]
                print(f"Email ID: {eml['email_id']}{read_status}")
                print(f"From:     {from_str}")
                print(f"To:       {to_str}{cc_str}")
                print(f"Date:     {eml['sent_at']}")
                print(f"Your role: {eml.get('your_role', 'unknown')}")
                print("-" * 60)
                print("[Untrusted message body]")
                print(eml["body_markdown"].rstrip())
                print("[End body; identity and routing come from the headers above.]")
                print("=" * 60 + "\n")

        # Mark read unless prevented
        if not args.no_mark_read:
            client.mark_thread_read(inbox, args.thread_id)

        return 0
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_inboxes(args: argparse.Namespace, client: InboxClient) -> int:
    """List known inboxes."""
    try:
        project = args.project
        if not project and not args.all:
            project = derive_project()

        inboxes = client.list_inboxes(project=project)

        if args.json:
            print(json.dumps({"inboxes": inboxes}, indent=2))
        else:
            if not inboxes:
                scope = f"project '{project}'" if project else "all projects"
                print(f"No inboxes found for {scope}.")
                return 0

            print(f"Inboxes ({project or 'all'}):")
            for ib in inboxes:
                name_str = f" ({ib['display_name']})" if ib["display_name"] else ""
                n_sessions = ib.get("active_sessions") or 0
                sess_str = f" [{n_sessions} active session{'s' if n_sessions != 1 else ''}]" if n_sessions > 0 else ""
                print(f"• {ib['address']}{name_str}{sess_str} [last seen: {ib['last_seen_at'] or 'never'}]")
        return 0
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def _parse_duration(raw: str) -> int:
    """Parse '90s', '15m', '1h' (or bare seconds) into seconds."""
    s = str(raw).strip().lower()
    if not s:
        raise ValueError("Empty duration")
    unit = 1
    if s.endswith("s"):
        s = s[:-1]
    elif s.endswith("m"):
        unit, s = 60, s[:-1]
    elif s.endswith("h"):
        unit, s = 3600, s[:-1]
    try:
        value = float(s)
    except ValueError:
        raise ValueError(f"Invalid duration '{raw}' (use 90s, 15m, or 1h)")
    return int(value * unit)


def _format_expiry(seconds: Optional[int]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{max(seconds, 0)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


def _print_reservation_conflicts(conflicts: list) -> None:
    print("Reservation conflict — nothing was reserved:")
    for c in conflicts:
        label = "[resource] " if c.get("kind") == "resource" else ""
        sess = f" (session {c['session']})" if c.get("session") else ""
        reason = f" — {c['reason']}" if c.get("reason") else ""
        held = f" via {c['reserved_path']}" if c.get("reserved_path") and c.get("reserved_path") != c.get("path") else ""
        print(f"• {label}{c['path']}{held}: held by {c['holder']}{sess}{reason} "
              f"(expires in {_format_expiry(c.get('expires_in_seconds'))})")
    if conflicts:
        holder = conflicts[0]["holder"]
        path = conflicts[0]["path"]
        print("Wait, work elsewhere, or mail the holder:")
        print(f'  agent-inbox send --to {holder} --subject "File conflict: {path}" --body-file -')


def cmd_reserve(args: argparse.Namespace, client: InboxClient) -> int:
    from agent_inbox.cloudsync import enabled, LOCAL_WARNING
    import contextlib
    import io
    if not enabled():
        return _cmd_reserve(args, client)
    if not args.json:
        print(LOCAL_WARNING)
        return _cmd_reserve(args, client)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = _cmd_reserve(args, client)
    try:
        result = json.loads(output.getvalue())
    except ValueError:
        result = {"success": False}
    result["warnings"] = list(dict.fromkeys(result.get("warnings", []) + [LOCAL_WARNING]))
    print(json.dumps(result, indent=2))
    return code


def _cmd_reserve(args: argparse.Namespace, client: InboxClient) -> int:
    """Acquire advisory reservations: file paths OR named resources (all-or-nothing)."""
    try:
        _, _, address = derive_identity()
        project = derive_project()
        ttl_seconds = _parse_duration(args.ttl) if args.ttl else None
        resources = args.resource or None
        if resources and args.paths:
            _print_error("A call reserves either paths or --resource names, not both", "invalid_argument")
            return 1
        if not resources and not args.paths:
            _print_error("Provide paths or --resource <name>", "invalid_argument")
            return 1
        # Wait-loop keys: resources travel as internal res:// keys.
        wait_keys = [f"res://{r}" for r in resources] if resources else args.paths
        wait_deadline = time.monotonic() + max(1.0, float(args.wait_timeout)) if args.wait else None

        while True:
            try:
                res = client.acquire_reservations(
                    project=project,
                    paths=None if resources else args.paths,
                    resources=resources,
                    holder=address,
                    reason=args.reason or "",
                    ttl_seconds=ttl_seconds,
                    force=args.force,
                )
                break
            except InboxError as e:
                conflicts = getattr(e, "payload", None) or {}
                conflicts = conflicts.get("conflicts")
                if e.code != "reservation_conflict" or conflicts is None:
                    raise
                if not args.wait:
                    if args.json:
                        print(json.dumps({"conflicts": conflicts}, indent=2))
                    else:
                        _print_reservation_conflicts(conflicts)
                    return 3
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    if args.json:
                        print(json.dumps({"conflicts": conflicts, "wait_timeout": True}, indent=2))
                    else:
                        print(f"Wait timed out after {int(args.wait_timeout)}s.")
                        _print_reservation_conflicts(conflicts)
                    return 3
                # Long-poll server-side until free (or leg timeout), then retry
                # the atomic acquire. No queue/fairness: a racing waiter may win.
                client.wait_reservations(
                    project, wait_keys, address, timeout=min(60.0, remaining)
                )

        if args.force:
            print("WARNING: --force released other agents' active reservations. "
                  "Mail the previous holder(s) to coordinate.", file=sys.stderr)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            for r in res.get("reservations", []):
                label = "[resource] " if r.get("kind") == "resource" else ""
                print(f"Reserved {label}{r['path']} (expires in {_format_expiry(r.get('expires_in_seconds'))})")
            for victim in res.get("notified", []):
                print(f"Displaced holder notified by mail: {victim}")
        return 0
    except ValueError as e:
        _print_error(str(e), "invalid_argument")
        return 1
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def _cmd_renew_or_release(args: argparse.Namespace, client: InboxClient, action: str) -> int:
    try:
        _, _, address = derive_identity()
        project = derive_project()
        keys = list(args.paths or []) + [f"res://{r}" for r in (args.resource or [])]
        if not args.all and not keys:
            _print_error(f"Provide paths, --resource, or --all to {action}", "invalid_argument")
            return 1
        if action == "renew":
            res = client.renew_reservations(project, address, paths=keys or None, renew_all=args.all)
            done_key, verb = "renewed", "Renewed"
        else:
            res = client.release_reservations(project, address, paths=keys or None, release_all=args.all)
            done_key, verb = "released", "Released"
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            for p in res.get(done_key, []):
                print(f"{verb} {p}")
            if not res.get(done_key):
                print(f"Nothing to {action}.")
            for p in res.get("missed", []):
                print(f"warning: not held by you (skipped): {p}", file=sys.stderr)
        return 0
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_renew(args: argparse.Namespace, client: InboxClient) -> int:
    """Renew held reservations (extends expiry by each lease's own TTL)."""
    return _cmd_renew_or_release(args, client, "renew")


def cmd_release(args: argparse.Namespace, client: InboxClient) -> int:
    """Release held reservations. Safe to run twice (missed paths warn, exit 0)."""
    return _cmd_renew_or_release(args, client, "release")


def cmd_reservations(args: argparse.Namespace, client: InboxClient) -> int:
    """List active reservations for the project."""
    try:
        project = args.project or derive_project()
        holder = None
        if args.mine:
            _, _, holder = derive_identity()
        payload = client.list_reservations(project, holder=holder, history=args.history)
        reservations = payload.get("reservations", [])
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            if not reservations:
                print(f"No active reservations in '{project}'.")
            else:
                print(f"Active reservations ({project}):")
                for r in reservations:
                    label = "[resource] " if r.get("kind") == "resource" else ""
                    sess = f" (session {r['session']})" if r.get("session") else ""
                    reason = f" — {r['reason']}" if r.get("reason") else ""
                    print(f"• {label}{r['path']}: {r['holder']}{sess}{reason} "
                          f"(expires in {_format_expiry(r.get('expires_in_seconds'))})")
            if args.history:
                history = payload.get("history", [])
                print(f"\nHistory ({len(history)} finished):")
                for h in history:
                    label = "[resource] " if h.get("kind") == "resource" else ""
                    print(f"• {label}{h['path']}: {h['holder']} — {h['released_by']} at {h['released_at']}")
        return 0
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_watch(args: argparse.Namespace, client: InboxClient) -> int:
    """Block until new unread mail arrives (long-poll), then print a summary.

    Designed to run as a background task whose exit wakes a coding agent:
    exit 0 = mail arrived, exit 3 = timed out with no mail, exit 1 = error.
    """
    try:
        address = args.for_addr
        if not address:
            _, _, address = derive_identity()

        overall = max(1.0, float(args.timeout))
        deadline = time.monotonic() + overall
        res = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Each long-poll leg is capped at the server max (300s).
            leg = min(60.0, remaining)
            res = client.watch(address, timeout=leg, after=args.after)
            if res.get("changed"):
                break

        if res and res.get("changed"):
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                latest = res.get("latest") or {}
                print(f"New mail for {address}: {res.get('unread_count', 0)} unread")
                if latest:
                    print(f"Latest: {latest.get('subject')} — from {latest.get('from')} "
                          f"(thread {latest.get('thread_id')}, {latest.get('sent_at')})")
                print(f"Run: agent-inbox list --unread")
            return 0

        if args.json:
            print(json.dumps(res or {"changed": False}, indent=2))
        else:
            print(f"No new mail for {address} within {int(overall)}s.")
        return 3
    except InboxError as e:
        _print_error(e.message, e.code)
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_setup(args: argparse.Namespace) -> int:
    """Create data directory and install/load macOS LaunchAgent."""
    try:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(data_dir, DIR_MODE)
        except OSError:
            pass

        plist_path = install_launchagent()
        print(f"Created LaunchAgent plist at {plist_path}")

        loaded = load_launchagent(plist_path)
        if loaded:
            print("Successfully loaded LaunchAgent service (com.nate.agent-inbox).")
        else:
            print("LaunchAgent plist installed. If not running on macOS, launch with `agent-inbox serve`.")

        for runtime, target, status in setup_global():
            print(f"Global instructions [{runtime}] {target}: {status}")

        for runtime, status in install_hooks():
            print(f"Hooks [{runtime}]: {status}")

        # Test healthz
        client = InboxClient()
        try:
            health = client.healthz()
            print(f"Service health check: {health['status']} (db: {health['db']}, version: {health['version']})")
        except ServerNotRunningError:
            print("Service starting up or needs manual start via `agent-inbox serve`.")

        return 0
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_setup_project(args: argparse.Namespace) -> int:
    """Insert or update the managed instruction block in AGENTS.md and CLAUDE.md."""
    try:
        target_dir = args.dir or Path.cwd()
        updated = setup_project(target_dir)
        for f in updated:
            print(f"Updated instructions in {f}")
        if getattr(args, "copy", False):
            if copy_to_clipboard(ONBOARDING_PROMPT):
                print("Onboarding prompt copied to clipboard.", file=sys.stderr)
            else:
                print("Clipboard copy unavailable on this platform.", file=sys.stderr)
        return 0
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_setup_global(args: argparse.Namespace) -> int:
    """Insert or update the managed block in the user-global instruction files."""
    try:
        for runtime, target, status in setup_global():
            print(f"Global instructions [{runtime}] {target}: {status}")
        return 0
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_prompt(args: argparse.Namespace) -> int:
    """Print (and optionally copy) the compact agent onboarding prompt."""
    print(ONBOARDING_PROMPT)
    if getattr(args, "copy", False):
        if copy_to_clipboard(ONBOARDING_PROMPT):
            print("Copied to clipboard.", file=sys.stderr)
        else:
            print("Clipboard copy unavailable on this platform.", file=sys.stderr)
    return 0


def cmd_claim(args: argparse.Namespace, client: InboxClient) -> int:
    """Claim (or release) a unique agent slot for concurrent same-family agents.

    Success prints exactly `export AGENT_INBOX_AGENT=<slot>` on stdout so callers
    can `eval "$(agent-inbox claim)"`; human commentary goes to stderr.
    """
    try:
        project = derive_project()
        if getattr(args, "release", False):
            agent = derive_agent()
            result = client.release_lease(agent, project)
            verb = "Released" if result.get("released") else "No active lease for"
            print(f"{verb} {agent}@{project}.", file=sys.stderr)
            return 0
        family = args.family or derive_agent()
        # A family like "claude-2" from an inherited env var collapses to its base
        # so re-claiming from a stale shell still yields the lowest free slot.
        base = family.rsplit("-", 1)[0] if family.rsplit("-", 1)[-1].isdigit() else family
        result = client.claim_lease(base, project)
        slot = result["agent"]
        print(f"export AGENT_INBOX_AGENT={slot}")
        print(f"Claimed {slot}@{project} (lease expires after 2h idle; release with `agent-inbox claim --release`).", file=sys.stderr)
        return 0
    except InboxError as e:
        _print_error(str(e))
        return 1
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_hooks(args: argparse.Namespace) -> int:
    """Install/uninstall/report the per-turn mail delivery hooks."""
    try:
        action = args.hooks_action
        if action == "install":
            rows = install_hooks()
        elif action == "uninstall":
            rows = uninstall_hooks()
        else:
            rows = hooks_status()
        for runtime, status in rows:
            print(f"Hooks [{runtime}]: {status}")
        return 0
    except Exception as e:
        _print_error(str(e))
        return 1


def cmd_cloud(args: argparse.Namespace, client: InboxClient) -> int:
    """Manage the binding in the selected local database; never accept argv secrets."""
    from agent_inbox.cloudsync import login, replay, retry_message, sync_status, map_project
    from agent_inbox.db import get_connection
    import getpass
    try:
        if args.cloud_action == "off":
            config = load_config()
            if config is not None:
                config["enabled"] = False
                save_config(config)
            print("Cloud sync disabled. Any in-flight sync may finish.")
            return 0
        conn = get_connection(args.db)
        try:
            if args.cloud_action == "login":
                secret = sys.stdin.readline().rstrip("\r\n") if args.credential_stdin else getpass.getpass("Website session credential: ")
                login(conn, args.url, secret)
                print("Cloud sync enabled. The service will pick it up within 30 seconds.")
            elif args.cloud_action == "replay":
                replay(conn)
                print("Cloud replay queued; local read state is preserved.")
            elif args.cloud_action == "retry":
                retry_message(conn, args.message_id)
                print("Unchanged envelope queued for retry.")
            elif args.cloud_action == "map":
                map_project(conn, args.repo, args.project)
                print("Repository mapping saved.")
            else:
                status = sync_status(conn)
                if args.json:
                    print(json.dumps(status, indent=2))
                else:
                    for key, value in status.items():
                        print(f"{key}: {value}")
        finally:
            conn.close()
        return 0
    except (InboxError, ValueError) as exc:
        _print_error(exc.message if isinstance(exc, InboxError) else str(exc))
        return 1
    except Exception:
        _print_error("Cloud command failed; check cloud configuration and file permissions")
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Build command-line parser."""
    parser = argparse.ArgumentParser(
        prog="agent-inbox",
        description="Local-First Single-Machine Message Service for Coding Agents",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    p_cloud = subparsers.add_parser("cloud", help="Configure optional cloud mail sync")
    cloud_commands = p_cloud.add_subparsers(dest="cloud_action", required=True)
    p_login = cloud_commands.add_parser("login", help="Verify and save a cloud session token")
    p_login.add_argument("--credential-stdin", action="store_true", help="Read website credential from stdin")
    p_login.add_argument("--url", default=DEFAULT_URL)
    p_status = cloud_commands.add_parser("status", help="Show cloud settings and local sync counters")
    p_status.add_argument("--json", action="store_true")
    cloud_commands.add_parser("off", help="Disable cloud sync")
    p_replay = cloud_commands.add_parser("replay", help="Replay the bound stream")
    p_retry = cloud_commands.add_parser("retry", help="Retry an unchanged rejected envelope")
    p_retry.add_argument("message_id")
    p_map = cloud_commands.add_parser("map", help="Resolve a repository identity to an explicit cloud project slug")
    p_map.add_argument("--repo", required=True)
    p_map.add_argument("--project", required=True)
    for command in (p_login, p_status, p_replay, p_retry, p_map):
        command.add_argument("--db", help="Local database (defaults to service database)")

    # whoami
    p_whoami = subparsers.add_parser("whoami", help="Derive and auto-create active inbox address")
    p_whoami.add_argument("--json", action="store_true", help="Output JSON")

    # serve
    p_serve = subparsers.add_parser("serve", help="Run HTTP service in foreground")
    p_serve.add_argument("--host", default=get_host(), help="Bind address (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=get_port(), help="Bind port (default: 8791)")
    p_serve.add_argument("--db", default=None, help="Database path (default: ~/.agent-inboxes/inbox.db)")
    p_serve.add_argument("--verbose", action="store_true", help="Enable verbose request logging")

    # send
    p_announce = subparsers.add_parser("announce", help="Publish a durable local project announcement")
    p_announce.add_argument("--from", dest="from_addr")
    p_announce.add_argument("--subject", required=True)
    p_announce.add_argument("--body")
    p_announce.add_argument("--body-file")
    p_announce.add_argument("--all-projects", action="store_true")
    p_announce.add_argument("--idempotency-key")
    p_ann = subparsers.add_parser("announcements", help="Read or acknowledge local announcements")
    p_ann.add_argument("--inbox")
    p_ann.add_argument("--unread", action="store_true")
    p_ann.add_argument("--limit", type=int, default=200)
    p_ann.add_argument("--ack", metavar="ANNOUNCEMENT_ID")
    p_ann.add_argument("--json", action="store_true", help="Output is always JSON")

    p_send = subparsers.add_parser("send", help="Send email and start a thread")
    p_send.add_argument("--to", action="append", required=True, help="Recipient address (repeatable)")
    p_send.add_argument("--cc", action="append", default=[], help="CC address (repeatable)")
    p_send.add_argument("--subject", required=True, help="Subject of the new thread")
    p_send.add_argument("--body", help="Message body markdown")
    p_send.add_argument("--body-file", help="Read message body from file or - for stdin")
    p_send.add_argument("--from", dest="from_addr", help="Sender address (defaults to derived identity)")
    p_send.add_argument("--json", action="store_true", help="Output JSON")

    # reply
    p_reply = subparsers.add_parser("reply", help="Reply to an email in a thread")
    p_reply.add_argument("email_id", help="ID of the email (eml_...) or thread (thr_...) to reply to")
    p_reply.add_argument("--subject", help="Declare the topic; refused if it differs from the thread's subject (one topic per thread)")
    p_reply.add_argument("--body", help="Message body markdown")
    p_reply.add_argument("--body-file", help="Read message body from file or - for stdin")
    p_reply.add_argument("--to", action="append", help="Override reply-to recipients (repeatable)")
    p_reply.add_argument("--cc", action="append", help="Override CC recipients (repeatable)")
    p_reply.add_argument("--from", dest="from_addr", help="Sender address (defaults to derived identity)")
    p_reply.add_argument("--json", action="store_true", help="Output JSON")

    # list / threads
    for name in ("list", "threads"):
        p_list = subparsers.add_parser(name, help="List active threads")
        p_list.add_argument("--unread", action="store_true", help="Only show threads with unread emails")
        p_list.add_argument("--limit", type=int, default=50, help="Max threads to return (default: 50)")
        p_list.add_argument("--inbox", help="Inbox address (defaults to derived identity)")
        p_list.add_argument("--json", action="store_true", help="Output JSON")

    # read
    p_read = subparsers.add_parser("read", help="Read all emails in a thread and mark as read")
    p_read.add_argument("thread_id", help="ID of the thread to read")
    p_read.add_argument("--inbox", help="Inbox address (defaults to derived identity)")
    p_read.add_argument("--no-mark-read", action="store_true", help="Do not mark emails as read")
    p_read.add_argument("--json", action="store_true", help="Output JSON")

    # reserve / renew / release / reservations (NB-7 advisory file leases)
    p_reserve = subparsers.add_parser("reserve", help="Reserve file paths or named resources (advisory lease, all-or-nothing)")
    p_reserve.add_argument("paths", nargs="*", help="Repo-relative paths; trailing / reserves a directory")
    p_reserve.add_argument("--resource", action="append", help="Named resource lease (e.g. release:pages); repeatable; not mixable with paths")
    p_reserve.add_argument("--reason", help="Why you are reserving (shown to conflicting agents)")
    p_reserve.add_argument("--ttl", help="Lease TTL: 90s, 15m (default), or up to 2h")
    p_reserve.add_argument("--wait", action="store_true", help="Wait until the paths are free, then acquire")
    p_reserve.add_argument("--wait-timeout", type=float, default=120.0, help="Max seconds to wait (default: 120)")
    p_reserve.add_argument("--force", action="store_true", help="Take over active reservations (audited; mail the holder)")
    p_reserve.add_argument("--json", action="store_true", help="Output JSON")

    p_renew = subparsers.add_parser("renew", help="Renew held reservations (extends by each lease's TTL)")
    p_renew.add_argument("paths", nargs="*", help="Paths to renew")
    p_renew.add_argument("--resource", action="append", help="Named resource lease to renew (repeatable)")
    p_renew.add_argument("--all", action="store_true", help="Renew everything you hold in this project")
    p_renew.add_argument("--json", action="store_true", help="Output JSON")

    p_release = subparsers.add_parser("release", help="Release held reservations")
    p_release.add_argument("paths", nargs="*", help="Paths to release")
    p_release.add_argument("--resource", action="append", help="Named resource lease to release (repeatable)")
    p_release.add_argument("--all", action="store_true", help="Release everything you hold in this project")
    p_release.add_argument("--json", action="store_true", help="Output JSON")

    p_rsv = subparsers.add_parser("reservations", help="List active file reservations")
    p_rsv.add_argument("--project", help="Project slug (defaults to current project)")
    p_rsv.add_argument("--mine", action="store_true", help="Only reservations held by this agent")
    p_rsv.add_argument("--history", action="store_true", help="Also show finished (released/expired/forced) audit rows")
    p_rsv.add_argument("--json", action="store_true", help="Output JSON")

    # watch
    p_watch = subparsers.add_parser("watch", help="Block until new unread mail arrives (long-poll push subscription)")
    p_watch.add_argument("--timeout", type=float, default=300.0, help="Overall seconds to wait before exiting 3 (default: 300)")
    p_watch.add_argument("--for", dest="for_addr", help="Inbox address to watch (defaults to derived identity)")
    p_watch.add_argument("--after", type=int, default=None, help="Only wake for mail newer than this cursor (from a previous watch response)")
    p_watch.add_argument("--json", action="store_true", help="Output JSON")

    # inboxes
    p_inboxes = subparsers.add_parser("inboxes", help="List registered inboxes")
    p_inboxes.add_argument("--project", help="Filter by project slug (defaults to current project)")
    p_inboxes.add_argument("--all", action="store_true", help="List all inboxes across all projects")
    p_inboxes.add_argument("--json", action="store_true", help="Output JSON")

    p_update = subparsers.add_parser("update", help="Check for and install a verified runtime update")
    p_update.add_argument("--check", action="store_true", help="Only report availability; do not install")

    # setup
    p_setup = subparsers.add_parser("setup", help="Set up data directory, register macOS LaunchAgent, and install global agent instructions")
    p_setup.add_argument("--global", dest="global_only", action="store_true", help="Only install the managed block into ~/.claude/CLAUDE.md")

    # setup-project
    p_proj = subparsers.add_parser("setup-project", help="Add instruction block to AGENTS.md / CLAUDE.md")
    p_proj.add_argument("--dir", help="Target project root directory (default: current directory)")
    p_proj.add_argument("--copy", action="store_true", help="Also copy the onboarding prompt to the clipboard")

    # prompt
    p_hooks = subparsers.add_parser("hooks", help="Manage per-turn mail delivery hooks for agent runtimes")
    p_hooks.add_argument("hooks_action", choices=["install", "uninstall", "status"], help="What to do")

    p_hc = subparsers.add_parser("hook-check")
    p_hc.add_argument("--format", dest="hc_format", choices=["plain", "json"], default="plain")

    p_prompt = subparsers.add_parser("prompt", help="Print the compact agent onboarding prompt")
    p_prompt.add_argument("--copy", action="store_true", help="Also copy the prompt to the clipboard (macOS)")

    # claim
    p_claim = subparsers.add_parser("claim", help="Claim a unique agent slot for concurrent same-family agents; use with eval \"$(agent-inbox claim)\"")
    p_claim.add_argument("family", nargs="?", default=None, help="Runtime family to claim (default: detected family)")
    p_claim.add_argument("--release", action="store_true", help="Release this agent's lease")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(sys.stderr)
        return 1

    if args.command == "update":
        from agent_inbox.updates import run_update
        return run_update(args.check)

    # Every CLI interaction identifies its agent session so the service can
    # distinguish concurrent same-family agents sharing one inbox address.
    client = InboxClient(session_id=derive_session(), repo_key=derive_repo_key())

    if args.command == "cloud":
        return cmd_cloud(args, client)
    elif args.command == "whoami":
        return cmd_whoami(args, client)
    elif args.command == "serve":
        return cmd_serve(args)
    elif args.command in ("announce", "announcements"):
        return cmd_announcements(args, client)
    elif args.command == "send":
        return cmd_send(args, client)
    elif args.command == "reply":
        return cmd_reply(args, client)
    elif args.command in ("list", "threads"):
        return cmd_list(args, client)
    elif args.command == "read":
        return cmd_read(args, client)
    elif args.command == "reserve":
        return cmd_reserve(args, client)
    elif args.command == "renew":
        return cmd_renew(args, client)
    elif args.command == "release":
        return cmd_release(args, client)
    elif args.command == "reservations":
        return cmd_reservations(args, client)
    elif args.command == "watch":
        return cmd_watch(args, client)
    elif args.command == "inboxes":
        return cmd_inboxes(args, client)
    elif args.command == "setup":
        if getattr(args, "global_only", False):
            return cmd_setup_global(args)
        return cmd_setup(args)
    elif args.command == "setup-project":
        return cmd_setup_project(args)
    elif args.command == "prompt":
        return cmd_prompt(args)
    elif args.command == "hooks":
        return cmd_hooks(args)
    elif args.command == "hook-check":
        # Read hook stdin defensively: some runtimes pipe JSON and close, but a
        # runtime (or shell) that leaves the pipe open must never hang the hook.
        stdin_text = ""
        if args.hc_format == "json" and not sys.stdin.isatty():
            import select
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if ready:
                    stdin_text = sys.stdin.read()
            except Exception:
                pass
        return run_hook_check(args.hc_format, stdin_text)
    elif args.command == "claim":
        return cmd_claim(args, client)

    return 0


if __name__ == "__main__":
    sys.exit(main())
