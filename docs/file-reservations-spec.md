# File Reservations (NB-7) — Specification

Status: approved for implementation (Nate, 2026-09-07). Design reviewed with Codex via
thread `thr_531232841adb4965a6eafa8f1b0cb418`; incorporate its reply if it arrives
before implementation completes, otherwise proceed with this spec.

## 1. Purpose and stance

Advisory write-coordination for coding agents working concurrently in the same
project. A reservation is a **lease**, not a lock: nothing is enforced at the
filesystem level, git remains the source of truth, and no agent can ever be
permanently blocked. The system's job is to make conflicts visible *before*
they happen and to route agents toward the mailbox when they collide.

The existing protocol line stays true in both directions: a message about a
file is not a lock, and a reservation is not a message — work that affects
another agent still warrants mail.

## 2. Data model

New table in the existing SQLite schema (same guarded-ALTER/CREATE pattern as
the sessions migration):

```sql
CREATE TABLE IF NOT EXISTS reservations (
  id              INTEGER PRIMARY KEY,
  project_id      INTEGER NOT NULL REFERENCES projects(id),
  path            TEXT    NOT NULL,             -- normalized, repo-relative
  holder_inbox_id INTEGER NOT NULL REFERENCES inboxes(id),
  holder_session  TEXT,                         -- session slug (nullable for old clients)
  reason          TEXT    NOT NULL DEFAULT '',
  created_at      TEXT    NOT NULL,             -- ISO-8601 UTC
  expires_at      TEXT    NOT NULL,
  released_at     TEXT,                         -- NULL while active
  released_by     TEXT                          -- 'holder' | 'expired' | 'forced:<address>'
);
CREATE INDEX IF NOT EXISTS idx_reservations_active
  ON reservations(project_id, path) WHERE released_at IS NULL;
```

A reservation is **active** iff `released_at IS NULL AND expires_at > now`.
Rows are never deleted; release/expiry/takeover set `released_at` +
`released_by`, giving a free audit log. Expiry is lazy: any read or write that
touches reservations first sweeps expired-but-unreleased rows for that project
(sets `released_at = expires_at`, `released_by = 'expired'`). No background
timer needed.

### Path normalization

- Repo-relative POSIX paths (`src/lib/money.ts`), no leading `./`, no `..`
  segments, no absolute paths (reject with `validation_error`).
- Case-preserving store, **case-insensitive compare** (macOS default FS).
- Trailing `/` marks a directory reservation (`src/lib/`). Store with the
  trailing slash; it's semantically distinct from a file path.
- Identity is (project slug, normalized path) — the same file in two worktrees
  of one project maps to one reservation key. Clients pass repo-relative paths;
  the server never sees absolute paths.

## 3. Semantics

### TTL and renewal

- Default TTL **15 minutes** (most agent tasks finish in 10–15).
- `--ttl` may set 1m–2h at acquire time. The 2h cap bounds a single lease's
  *horizon* (`expires_at` can never be more than 2h from now), not total hold
  time: a lease may be renewed indefinitely while the holder is genuinely
  active. Rationale: staleness is what TTL fights; a legitimately long
  migration shouldn't be forced into `--force` churn.
- **Renewal**: `POST .../renew` extends `expires_at` to now + the lease's
  original TTL. Explicit only — CLI command `agent-inbox renew`. Do NOT
  auto-renew on unrelated CLI activity (an agent reading mail is not evidence
  it is still editing the file). The protocol block tells agents: if your task
  runs past ~10 minutes, renew what you hold when you check mail.

### Acquire

- One call, N paths, **all-or-nothing**. If any path conflicts, nothing is
  reserved and the response lists every conflict.
- Conflict = an active reservation by a *different* (address, session) on:
  - the exact same path, or
  - an ancestor directory reservation (`src/lib/` blocks `src/lib/x.ts`), or
  - a descendant (`src/lib/x.ts` blocks reserving `src/lib/`).
- Re-acquiring your own active path (same address + session) is idempotent and
  renews it.
- Conflict response carries, per conflict: path, holder address, holder
  session, reason, created_at, expires_at, and seconds-until-expiry — enough
  for the caller to decide wait / work elsewhere / mail the holder.
- Concurrency: acquire runs in a single SQLite transaction (`BEGIN IMMEDIATE`)
  so two simultaneous acquires cannot both succeed on overlapping paths.

### Release

- `release` by path(s) or `--all` (everything held by this address+session in
  this project). Releasing something you don't hold is a no-op warning, not an
  error (exit 0) — handoff scripts must be safe to run twice.

### Takeover

- Expired leases: acquire simply succeeds (sweep happens first).
- Active leases: `--force` acquires anyway, recording
  `released_by = 'forced:<taker address>'` on the old row. The CLI prints a
  loud warning and suggests mailing the previous holder. No permission model —
  it's advisory and audited, and cooperating agents don't force casually.

### Wait

- `--wait [--wait-timeout N]` long-polls server-side (same pattern as
  `/watch`: check every ~1s up to the request timeout, client loops) until all
  requested paths are free, then atomically acquires. No queue and no fairness
  guarantee; if two waiters race, one wins and the other keeps waiting. Exit 3
  on wait-timeout.

## 4. HTTP API (loopback service, same conventions as existing endpoints)

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| POST | `/v1/projects/{project}/reservations` | `{paths: [...], holder: address, session, reason, ttl_seconds?, force?}` + `Idempotency-Key` | `201 {reservations: [...]}` or `409 {error: 'reservation_conflict', conflicts: [...]}` |
| POST | `/v1/projects/{project}/reservations/renew` | `{paths or all: true, holder, session}` | `200 {renewed: [...], missed: [...]}` |
| POST | `/v1/projects/{project}/reservations/release` | `{paths or all: true, holder, session}` | `200 {released: [...], missed: [...]}` |
| GET | `/v1/projects/{project}/reservations` | `?holder=` optional | `200 {reservations: [...]}` active only |
| GET | `/v1/projects/{project}/reservations/wait` | `?paths=a,b&timeout=60` | `200 {free: true}` when all free, `200 {free: false, conflicts}` on timeout |

Acquire uses the existing Idempotency-Key machinery (same as send) so a
retried CLI call doesn't double-book. All endpoints 400 on bad paths, 404 on
unknown project only for GETs (POST acquire auto-creates the project row like
`ensure_project` does for send).

## 5. CLI

```
agent-inbox reserve <path>... [--reason "..."] [--ttl 15m] [--wait] [--wait-timeout 120] [--force] [--json]
agent-inbox renew   [<path>... | --all] [--json]
agent-inbox release [<path>... | --all] [--json]
agent-inbox reservations [--project <slug>] [--mine] [--json]
```

- Identity: derived exactly like send (address from `derive_identity`, session
  from `derive_session`).
- Exit codes: 0 acquired/released/renewed; **3** conflict or wait-timeout
  (matches `watch`'s no-mail exit); 1 connection/validation errors.
- Human conflict output must include holder, session, reason, expiry-in
  ("expires in 7m"), and a copy-pasteable suggestion:
  `agent-inbox send --to <holder> --subject "File conflict: <path>" --body-file -`.
- Durations accept `90s`, `15m`, `1h`.

## 6. Protocol block (project_setup.py)

Add a short section to the managed AGENTS.md/CLAUDE.md block (keep it tight —
it lives in every project's context):

- Before editing files another agent plausibly touches, `agent-inbox reserve
  <paths> --reason "..."` (15m default). On conflict, don't edit — wait, work
  elsewhere, or mail the holder.
- If your task runs long, `agent-inbox renew` at your mail checkpoints.
- `agent-inbox release --all` at handoff.
- Reservations are advisory and expire on their own; never treat one as
  permission to skip coordination mail, and never `--force` without messaging
  the holder.

Bump the managed-block version marker so `setup-project` refreshes existing
repos on next run.

## 7. Out of scope (do not build)

- Filesystem enforcement of any kind.
- Queueing, priorities, or fairness for waiters.
- Branch- or worktree-scoped reservations (path + project only, v1).
- Web UI in the NSW mailbox (read-only listing can come later).
- Cross-machine anything.

## 8. Tests (repo test conventions, focused — no shotgun suites)

1. Acquire/conflict/all-or-nothing: overlapping multi-path acquires, prefix
   conflicts both directions, own-lease idempotent re-acquire.
2. Expiry + lazy sweep + takeover audit (`expired`, `forced:`) with clock
   injection (freeze/patch `now` — no sleeps).
3. `--wait` unblocks promptly on release (short real timeout, e2e style like
   the watch test).
4. Concurrency: two threads acquiring the same path via `BEGIN IMMEDIATE` —
   exactly one wins.
5. Migration: a pre-reservations DB opens and upgrades cleanly.
```
