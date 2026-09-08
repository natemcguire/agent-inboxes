"""Explicit macOS service control; never signal a process discovered by port."""
import argparse
import os
import plistlib
import re
import subprocess
import sys
import time

from agent_inbox.config import LAUNCH_AGENT_LABEL, LAUNCH_AGENT_PLIST, get_data_dir, get_db_path
from agent_inbox.client import InboxClient


def control(action):
    if action == 'fg':
        from agent_inbox.cli import main
        return main(['serve'])
    if sys.platform != 'darwin':
        raise RuntimeError('LaunchAgent control requires macOS; use fg for foreground service')
    if not LAUNCH_AGENT_PLIST.exists():
        raise RuntimeError('No installed LaunchAgent; run agent-inbox setup first')
    with LAUNCH_AGENT_PLIST.open('rb') as stream:
        plist = plistlib.load(stream)
    args = plist.get('ProgramArguments', [])
    if plist.get('Label') != LAUNCH_AGENT_LABEL or args[1:] != ['-m','agent_inbox.cli','serve']:
        raise RuntimeError('LaunchAgent does not match the expected inbox service')
    domain = f'gui/{os.getuid()}'
    target = f'{domain}/{LAUNCH_AGENT_LABEL}'
    def run(*args):
        return subprocess.run(['launchctl', *args], capture_output=True, text=True)
    def owned_health():
        state = run('print', target)
        match = re.search(r'^\s*pid = (\d+)\s*$', state.stdout, re.MULTILINE)
        if state.returncode or not match:
            return False
        health = InboxClient(timeout=1).healthz()
        return (health.get('service') == 'agent-inboxes' and health.get('db') == 'ok'
                and health.get('pid') == int(match.group(1)))
    loaded = run('print', target).returncode == 0
    if action == 'status':
        print(f'LaunchAgent: {"loaded" if loaded else "stopped"}')
        print(f'Client-configured database: {get_db_path()}')
        candidates = [str(p) for p in get_data_dir().glob('*.db') if p.resolve() != get_db_path().resolve()]
        if candidates:
            print('Other database files in data directory: ' + ', '.join(sorted(candidates)))
        try:
            healthy = owned_health()
            print(f'Owned HTTP service healthy: {healthy}')
            return 0 if healthy else 1
        except Exception:
            print('HTTP service unavailable')
            return 1
    if action in ('stop', 'restart') and loaded:
        result = run('bootout', target)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        loaded = False
    if action == 'stop':
        return 0
    if not loaded:
        result = run('bootstrap', domain, str(LAUNCH_AGENT_PLIST))
    else:
        result = run('kickstart', target)  # No -k: start if stopped, retain a running process.
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    # Bound readiness; an occupied port remains a diagnosis, never a kill target.
    for _ in range(20):
        try:
            if owned_health():
                print('Agent Inbox is responding')
                return 0
        except Exception:
            pass
        time.sleep(.25)
    raise RuntimeError('Service did not become healthy; inspect server.err.log and the configured port')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start','stop','restart','status','fg'])
    args = parser.parse_args()
    try:
        return control(args.action)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
