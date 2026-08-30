"""Command-Line Interface for Agent Inboxes."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from agent_inbox import __version__
from agent_inbox.client import InboxClient
from agent_inbox.config import (
    DIR_MODE,
    LAUNCH_AGENT_PLIST,
    get_data_dir,
    get_db_path,
    get_host,
    get_port,
    get_server_url,
)
from agent_inbox.identity import derive_identity, derive_project
from agent_inbox.launchagent import (
    install_launchagent,
    load_launchagent,
    uninstall_launchagent,
)
from agent_inbox.models import InboxError, ServerNotRunningError
from agent_inbox.project_setup import setup_project
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
    """Print and auto-create the derived inbox address."""
    try:
        _, _, address = derive_identity()
        res = client.put_inbox(address)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(address)
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
    except Exception as e:
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
            print(f"Sent email {res['email_id']} in thread {res['thread_id']}")
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


def cmd_reply(args: argparse.Namespace, client: InboxClient) -> int:
    """Reply to an existing email in a thread."""
    try:
        from_addr = args.from_addr
        if not from_addr:
            _, _, from_addr = derive_identity()

        body = _read_body(args.body, args.body_file)

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
            email_id=args.email_id,
            from_addr=from_addr,
            body_markdown=body,
            to_addrs=to_list,
            cc_addrs=cc_list,
        )

        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Sent reply {res['email_id']} in thread {res['thread_id']}")
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
                return 0

            print(f"Threads for {inbox}:")
            for t in threads:
                unread_flag = f" [{t['unread_count']} unread]" if t["unread_count"] > 0 else ""
                parts = ", ".join(t["participants"])
                print(f"• {t['thread_id']} - {t['subject']}{unread_flag}")
                print(f"  Participants: {parts}")
                print(f"  Last active: {t['last_email_at']}\n")
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
            print(f"Thread: {thread_data['thread_id']} — {thread_data['subject']}\n" + "=" * 60)
            for eml in thread_data.get("emails", []):
                to_str = ", ".join(eml["to"])
                cc_str = f" | CC: {', '.join(eml['cc'])}" if eml["cc"] else ""
                read_status = "" if eml["read"] else " [UNREAD]"
                print(f"Email ID: {eml['email_id']}{read_status}")
                print(f"From:     {eml['from']}")
                print(f"To:       {to_str}{cc_str}")
                print(f"Date:     {eml['sent_at']}")
                print("-" * 60)
                print(eml["body_markdown"].rstrip())
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
                print(f"• {ib['address']}{name_str} [last seen: {ib['last_seen_at'] or 'never'}]")
        return 0
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
        return 0
    except Exception as e:
        _print_error(str(e))
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
    p_reply.add_argument("email_id", help="ID of the email to reply to")
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

    # inboxes
    p_inboxes = subparsers.add_parser("inboxes", help="List registered inboxes")
    p_inboxes.add_argument("--project", help="Filter by project slug (defaults to current project)")
    p_inboxes.add_argument("--all", action="store_true", help="List all inboxes across all projects")
    p_inboxes.add_argument("--json", action="store_true", help="Output JSON")

    # setup
    subparsers.add_parser("setup", help="Set up data directory and register macOS LaunchAgent")

    # setup-project
    p_proj = subparsers.add_parser("setup-project", help="Add instruction block to AGENTS.md / CLAUDE.md")
    p_proj.add_argument("--dir", help="Target project root directory (default: current directory)")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(sys.stderr)
        return 1

    client = InboxClient()

    if args.command == "whoami":
        return cmd_whoami(args, client)
    elif args.command == "serve":
        return cmd_serve(args)
    elif args.command == "send":
        return cmd_send(args, client)
    elif args.command == "reply":
        return cmd_reply(args, client)
    elif args.command in ("list", "threads"):
        return cmd_list(args, client)
    elif args.command == "read":
        return cmd_read(args, client)
    elif args.command == "inboxes":
        return cmd_inboxes(args, client)
    elif args.command == "setup":
        return cmd_setup(args)
    elif args.command == "setup-project":
        return cmd_setup_project(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
