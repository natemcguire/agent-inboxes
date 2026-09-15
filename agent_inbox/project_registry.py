"""Offline repository-to-project registry, shared with optional cloud sync."""

from agent_inbox.cloudsync import repository_identity
from agent_inbox.models import ConflictError, NotFoundError, ValidationError, is_valid_slug


def lookup_project(conn, repo):
    identity = repository_identity(repo)
    slugs = sorted({r['slug'] for r in conn.execute('SELECT repo_identity,slug FROM project_mappings')
                    if repository_identity(r['repo_identity']) == identity})
    if len(slugs) > 1:
        raise ConflictError('ambiguous', f"Repository '{identity}' has conflicting project mappings: {', '.join(slugs)}. Repair the registry.")
    return {'repo': identity, 'slug': slugs[0]} if slugs else None


def register_project(conn, repo, slug):
    if not isinstance(slug, str) or not is_valid_slug(slug) or slug != slug.lower():
        raise ValidationError('invalid_project', 'Use an explicit canonical lowercase project slug')
    identity = repository_identity(repo)
    conn.execute('BEGIN IMMEDIATE')
    try:
        old = lookup_project(conn, repo)
        if old and old['slug'] != slug:
            raise ConflictError('project_mapping_conflict', 'A repository already mapped to a different project cannot be remapped implicitly')
        conn.execute('INSERT OR IGNORE INTO project_mappings VALUES (?,?)', (identity, slug))
        from agent_inbox.service import InboxService
        InboxService(conn).ensure_project(slug)
        conn.execute("UPDATE cloud_envelopes SET state='pending',reason=NULL,retry_at=0 WHERE state='retryable' AND reason='unresolved_project_mapping'")
        conn.execute('UPDATE cloud_state SET push_retry_at=0 WHERE id=1')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {'repo': identity, 'slug': slug}


def run(args):
    import json
    from agent_inbox.db import get_connection
    from agent_inbox.models import InboxError
    conn = get_connection(args.db)
    try:
        if args.project_action == 'register':
            result = register_project(conn, args.repo, args.slug)
        elif args.project_action == 'lookup':
            result = lookup_project(conn, args.repo)
            if result is None:
                raise NotFoundError('not_found', f"No project registered for '{args.repo}'")
        else:
            result = {'projects': [dict(r) for r in conn.execute('SELECT repo_identity AS repo,slug FROM project_mappings ORDER BY repo_identity')]}
        print(json.dumps(result))
        return 0
    except InboxError as exc:
        print(json.dumps({'error': exc.code, 'code': exc.code, 'message': exc.message}))
        return {'not_found': 4, 'ambiguous': 5}.get(exc.code, 1)
    finally:
        conn.close()
