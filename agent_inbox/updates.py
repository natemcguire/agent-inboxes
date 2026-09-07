"""Explicit runtime updates and a silent, daily availability cache."""
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from agent_inbox import __version__
from agent_inbox.config import get_db_path
from agent_inbox.distribution import fetch, fetch_manifest, validate_manifest, verified_files, version_tuple

DAY = 24 * 60 * 60


def running_commit():
    root = Path(__file__).resolve().parent.parent
    if re.fullmatch(r'[0-9a-f]{40}', root.name):
        return root.name
    try:
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                       stderr=subprocess.DEVNULL, timeout=2).decode().strip()
    except (OSError, subprocess.SubprocessError):
        return None


def is_newer(manifest):
    # A different hash alone has no ordering; never downgrade or replace an
    # equal-version development checkout with an unrelated commit.
    return (manifest['commit'] != running_commit()
            and version_tuple(manifest['version']) > version_tuple(__version__))


def update_notice():
    """Read only; hook/list output must not initialize a DB or access the network."""
    try:
        with closing(sqlite3.connect(get_db_path().absolute().as_uri() + '?mode=ro', uri=True, timeout=0.1)) as conn:
            row = conn.execute('SELECT manifest FROM update_state WHERE id=1').fetchone()
        if row and row[0]:
            manifest = validate_manifest(json.loads(row[0]))
            if version_tuple(manifest['version']) > version_tuple(__version__):
                return f"Agent Inboxes {manifest['version']} available — run: agent-inbox update"
    except Exception:
        pass
    return None


def check_daily(db_path):
    """Claim the attempt before networking, including failures and concurrent serves."""
    from agent_inbox.db import get_connection
    conn = get_connection(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        now = time.time()
        row = conn.execute('SELECT checked_at FROM update_state WHERE id=1').fetchone()
        if row and now - row[0] < DAY:
            conn.commit()
            return
        conn.execute('INSERT INTO update_state(id,checked_at) VALUES(1,?) '
                     'ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at', (now,))
        conn.commit()
        manifest = fetch_manifest()
        conn.execute('UPDATE update_state SET manifest=? WHERE id=1', (json.dumps(manifest),))
    finally:
        conn.close()


class UpdateWorker(threading.Thread):
    def __init__(self, db_path):
        super().__init__(name='agent-inbox-update-check', daemon=True)
        self.db_path = db_path
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            try:
                check_daily(self.db_path)
            except Exception:
                pass
            self.stopped.wait(DAY)


def safe_directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f'Refusing conflicting installation directory: {path}')
    if not path.exists():
        path.mkdir(mode=0o700)


def install_update(payload, manifest):
    files = verified_files(payload, manifest['sha256'])
    prefix = Path.home()/'.local'
    for path in (prefix, prefix/'lib', prefix/'lib'/'agent-inboxes', prefix/'bin'):
        safe_directory(path)
    base = prefix/'lib'/'agent-inboxes'
    runtime = base/manifest['commit']
    launcher = prefix/'bin'/'agent-inbox'
    if launcher.is_symlink() or not launcher.is_file():
        raise ValueError('Update requires a regular ~/.local/bin/agent-inbox launcher.')
    # Serialize concurrent updaters through installation AND service setup.
    lock_fd = os.open(base/'.update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=base) as temp:
            staging = Path(temp)
            for name, content in files.items():
                target = staging/name
                target.parent.mkdir(mode=0o700, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o600)
            # Verify the payload reports the advertised version using the same
            # interpreter that is executing the current launcher.
            result = subprocess.run([sys.executable, '-B', str(staging/'bin'/'agent-inbox'), '--version'],
                                    capture_output=True, text=True, timeout=15, check=True)
            if result.stdout.strip() != f"agent-inbox {manifest['version']}":
                raise ValueError('Archive version does not match manifest.')
            if runtime.exists() or runtime.is_symlink():
                if runtime.is_symlink() or not runtime.is_dir():
                    raise ValueError('Conflicting runtime target.')
                # A retry can reuse a complete, byte-identical install.
                actual = {str(p.relative_to(runtime)) for p in runtime.rglob('*') if not p.is_dir()}
                if actual != files.keys() or any((runtime/n).is_symlink() or (runtime/n).read_bytes() != b for n, b in files.items()):
                    raise ValueError('Existing runtime differs from verified archive.')
            else:
                os.rename(staging, runtime)
                staging.mkdir()  # TemporaryDirectory cleanup remains local.
        script = '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B ' + shlex.quote(str(runtime/'bin'/'agent-inbox')) + ' "$@"\n'
        fd, temp = tempfile.mkstemp(prefix='.agent-inbox-', dir=launcher.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(script)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o700)
            os.replace(temp, launcher)
        finally:
            Path(temp).unlink(missing_ok=True)
        result = subprocess.run([str(launcher), 'setup'])
        if result.returncode:
            raise RuntimeError('Runtime installed, but service setup failed. Retry: agent-inbox setup')


def run_update(check=False):
    try:
        manifest = fetch_manifest()
        old = f'{__version__} ({running_commit() or "unknown commit"})'
        if not is_newer(manifest):
            print(f'Agent Inboxes {old} is up to date.')
            return 0
        if check:
            print(f"Agent Inboxes {old} -> {manifest['version']} ({manifest['commit']}) available — run: agent-inbox update")
            return 0
        if version_tuple(__version__) < version_tuple(manifest['minimum_supported']):
            raise ValueError('This runtime is below minimum_supported; use the current installer.')
        install_update(fetch(manifest['url']), manifest)
        print(f"Agent Inboxes {old} -> {manifest['version']} ({manifest['commit']})")
        return 0
    except Exception as error:
        print(f'Update stopped: {error}', file=sys.stderr)
        return 1
