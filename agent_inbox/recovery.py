"""Merge local-only database snapshots into a new file; never alter inputs."""

import argparse
import os
from pathlib import Path
import sqlite3
import tempfile

from agent_inbox.db import get_connection


class RecoveryError(ValueError):
    pass


def _check(conn):
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RecoveryError('Database integrity check failed')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise RecoveryError('Database contains orphaned foreign keys')
    binding = conn.execute('SELECT endpoint,user_id FROM cloud_state WHERE id=1').fetchone()
    if binding and any(binding):
        raise RecoveryError('Cloud-bound databases cannot be merged by this local recovery tool')
    for table in ('cloud_envelopes', 'cloud_threads'):
        if conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
            raise RecoveryError('Cloud synchronization records require account-specific recovery')


def merge_databases(inputs, output):
    """Build and validate privately, then publish without replacing an existing file.

    IDs for projects/inboxes are remapped through their natural keys. Reservation
    IDs are local integers, so imported reservations receive new IDs. Opaque mail
    IDs must agree on immutable content. Transient sessions and name leases are
    deliberately omitted; reservations are retained as history, never resurrected.
    """
    output = Path(output).expanduser().absolute()
    if output.exists():
        raise RecoveryError('Output must be a new path')
    paths = [Path(p).expanduser().resolve(strict=True) for p in inputs]
    if len(paths) < 2:
        raise RecoveryError('Provide at least two input databases')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.inbox-recovery-', dir=output.parent) as tmp:
        target = Path(tmp) / 'merged.db'
        dst = get_connection(target)
        try:
            dst.execute('BEGIN IMMEDIATE')
            dst.execute('PRAGMA defer_foreign_keys=ON')
            for index, path in enumerate(paths):
                snapshot = Path(tmp) / f'input-{index}.db'
                # SQLite backup includes committed WAL records and gives a consistent snapshot.
                original = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
                try:
                    snap = sqlite3.connect(snapshot)
                    try:
                        original.backup(snap)
                    finally:
                        snap.close()
                finally:
                    original.close()
                # Only the snapshot is upgraded to the current schema.
                src = get_connection(snapshot)
                try:
                    _check(src)
                    known = {row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    incoming = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if incoming - known:
                        raise RecoveryError('Input contains unsupported tables')
                    projects, inboxes = {}, {}
                    for row in src.execute('SELECT * FROM projects'):
                        dst.execute('INSERT OR IGNORE INTO projects(slug,created_at) VALUES (?,?)', (row['slug'],row['created_at']))
                        projects[row['id']] = dst.execute('SELECT id FROM projects WHERE slug=?', (row['slug'],)).fetchone()[0]
                    for row in src.execute('SELECT * FROM inboxes'):
                        project = projects[row['project_id']]
                        dst.execute('INSERT OR IGNORE INTO inboxes(project_id,local_part,display_name,created_at,last_seen_at) VALUES (?,?,?,?,?)',
                                    (project,row['local_part'],row['display_name'],row['created_at'],row['last_seen_at']))
                        inboxes[row['id']] = dst.execute('SELECT id FROM inboxes WHERE project_id=? AND local_part=?', (project,row['local_part'])).fetchone()[0]
                    definitions = [
                        ('threads', ('id',), {'home_project_id': projects}, {'last_email_at','activity_id'}),
                        ('thread_inboxes', ('thread_id','inbox_id'), {'inbox_id': inboxes}, {'joined_at'}),
                        ('emails', ('id',), {'from_inbox_id': inboxes}, {'delivery_id','cloud_synced_at'}),
                        ('email_recipients', ('email_id','inbox_id'), {'inbox_id': inboxes}, {'read_at'}),
                        ('email_references', ('email_id','position'), {}, set()),
                        ('announcements', ('id',), {}, set()),
                        ('announcement_receipts', ('announcement_id','inbox_id'), {'inbox_id': inboxes}, {'read_at'}),
                        ('project_mappings', ('repo_identity',), {}, set()),
                    ]
                    for table, keys, maps, mutable in definitions:
                        for row in src.execute(f'SELECT * FROM {table}'):
                            data = dict(row)
                            for column, mapping in maps.items():
                                data[column] = mapping[data[column]]
                            if table == 'emails':
                                data['delivery_id'] = 0
                            if table == 'threads':
                                data['activity_id'] = 0
                            where = ' AND '.join(f'{k}=?' for k in keys)
                            old = dst.execute(f'SELECT * FROM {table} WHERE {where}', tuple(data[k] for k in keys)).fetchone()
                            if old:
                                if any(old[k] != v for k,v in data.items() if k not in mutable):
                                    raise RecoveryError(f'Conflicting immutable record in {table}')
                                # Preserve the earliest read/join timestamp; thread activity is rebuilt.
                                for column in mutable & {'read_at','joined_at'}:
                                    vals = [v for v in (old[column],data[column]) if v is not None]
                                    if vals:
                                        dst.execute(f'UPDATE {table} SET {column}=? WHERE {where}', (min(vals), *(data[k] for k in keys)))
                            else:
                                cols = ','.join(data)
                                dst.execute(f'INSERT INTO {table} ({cols}) VALUES ({",".join("?" for _ in data)})', tuple(data.values()))
                    for row in src.execute('SELECT * FROM reservations'):
                        data = dict(row)
                        data.pop('id')
                        data['project_id'] = projects[data['project_id']]
                        data['holder_inbox_id'] = inboxes[data['holder_inbox_id']]
                        if data['released_at'] is None:
                            from agent_inbox.models import utc_now_iso
                            data['released_at'] = utc_now_iso()
                            data['released_by'] = 'recovery'
                        # A stable source identity avoids collapsing different sessions/leases.
                        identity = ('project_id','path','holder_inbox_id','holder_session','created_at','client_token')
                        where = ' AND '.join(f'{k} IS ?' for k in identity)
                        old = dst.execute(f'SELECT * FROM reservations WHERE {where}', tuple(data[k] for k in identity)).fetchone()
                        if old:
                            for key in ('reason','ttl_seconds','repo_key'):
                                if old[key] != data[key]:
                                    raise RecoveryError('Conflicting reservation history')
                        else:
                            dst.execute(f'INSERT INTO reservations ({",".join(data)}) VALUES ({",".join("?" for _ in data)})', tuple(data.values()))
                finally:
                    src.close()
            dst.execute('UPDATE threads SET last_email_at=COALESCE((SELECT MAX(sent_at) FROM emails WHERE thread_id=threads.id),last_email_at), activity_id=COALESCE((SELECT MAX(delivery_id) FROM emails WHERE thread_id=threads.id),0)')
            _check(dst)
            dst.commit()
            dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except BaseException:
            dst.rollback()
            raise
        finally:
            dst.close()
        # Atomic no-clobber publication on the same filesystem.
        os.link(target, output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        print(merge_databases(args.inputs, args.output))
    except (RecoveryError, sqlite3.Error, OSError) as exc:
        parser.exit(1, f'Recovery failed: {exc}\n')


if __name__ == '__main__':
    main()
