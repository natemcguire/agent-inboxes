# Agent Inbox and Reservations — consolidated specification

As inspected September 8, 2026. This describes the local coordination system used
by our coding agents, not the marketplace's separate hosted proposal inbox.
Source checkout inspected: `agent-inboxes` commit
`e9c46bdb8d996ba6c02786b1d2110eb434288387`.
The installed executable currently points at an older packaged revision,
`41566ac2b71ee642e3b8b214b72297db32157c9c`; source and installed runtime should not
be assumed identical.

Repository: https://github.com/natemcguire/agent-inboxes. The owner authorized public publication on September8, 2026. This document can be shared independently. It contains no messages,
credentials, or live reservation records. Cloud requirements below distinguish
specified behavior from evidence of completed deployment.

## 1. Purpose

Give independent coding-agent sessions a durable way to communicate, identify who
is doing work, and notice conflicting edits before they happen. Agents can work
in different terminal sessions and Git worktrees without sharing a process.

The system has three separate concepts:

| Concept | Purpose | Identity / scope |
| --- | --- | --- |
| Mailbox | Durable communication and decisions | `agent@project` |
| Agent-name lease | Allocate distinct concurrent agent names | Runtime family + project on one machine |
| Reservation | Announce temporary exclusive intent to edit or operate | Project + path/resource + holder/session on one machine |

A message is not a reservation. A reservation is not a message. Neither gives an
agent permission to perform an otherwise unauthorized action.

## 2. Architecture and storage

The implementation uses Python 3.11+ and its standard library. CLI clients call a
local HTTP service; the service opens SQLite. The default endpoint is
`http://127.0.0.1:8791`, and the default database is `~/.agent-inboxes/inbox.db`.
On macOS, a LaunchAgent keeps the service available.

The server enforces loopback binding. It is not an authenticated LAN service.
SQLite uses WAL, foreign keys, and a 5-second busy timeout. The database is mode
0600; an owned data directory is mode0700. Setup must not chmod an unrelated,
pre-existing shared directory just because a custom database path lies in it.

Mutating operations that require exclusion use SQLite transactions. Acquiring a
reservation or allocating an agent name uses `BEGIN IMMEDIATE` so contenders
cannot both make a successful decision from the same stale snapshot.

Mail and reservation history are durable. There is no filesystem enforcement:
Git and the actual files remain authoritative for source state.

### Independent distribution and UI boundary

The inbox/reservation engine runs independently of Nate’s Software. It is Python
source, so there is no frontend compilation or Node/npm dependency required to run
its CLI and HTTP API. From a source checkout, `python3 bin/agent-inbox --help`
loads the CLI; `python3 bin/agent-inbox serve` starts the service. The supplied
installer configures a macOS LaunchAgent. Foreground Python portability and macOS
service installation are different guarantees; this inspection did not verify an
installation on another operating system or a fresh computer.

The standalone repository currently does **not** bundle a browser or desktop UI.
The existing visual mailbox lives in the separate Nate’s Software application
(`LocalAgentMailbox`, exposed through its INBOX view) and connects to the local API.
The service also exposes reservation observer endpoints. A self-contained UI would
need to be packaged separately or added to this repository; the APIs and CLI work
without one. Optional cloud messaging sync is not required for local operation.

## 3. Agent identity

A mailbox address is lowercase `agent-slug@project-slug`.

Project identity comes from an explicit `AGENT_INBOX_PROJECT` override, otherwise
the Git origin repository basename, otherwise the Git root/directory fallback.
Agent identity comes from `AGENT_INBOX_AGENT`, otherwise runtime-family detection.
`whoami` derives and registers the mailbox. Session IDs distinguish executions
sharing the same address; `AGENT_INBOX_SESSION` can explicitly supply one.

`claim` atomically selects the lowest free family name: `codex`, `codex-2`, etc.
The current implementation searches up to99 slots. A name becomes reclaimable
following two hours without lease activity. This name lease is separate from a
15-minute file reservation.

```sh
eval "$(agent-inbox claim)"
agent-inbox whoami
agent-inbox list --unread
```

A name, session ID or repository key is coordination metadata, not authenticated
proof of identity. Processes running as the same local user are trusted participants.

## 4. Messaging model

The logical hierarchy is Project → Inbox → Thread → Email.

- A thread has an opaque `thr_...` ID and a stable topic/subject.
- An immutable email has an `eml_...` ID, sender, ordered To/CC recipients,
  Markdown body, timestamp, optional parent, and an ordered reference chain.
- Recipient membership and read timestamps are stored separately. Reading mail
  for one mailbox must not mark another mailbox's copy read.
- Sending to valid addresses can provision their inboxes. A successful local
  send means durable local acceptance, not that another agent has read it.
- Send/reply requests require an `Idempotency-Key`. A transport retry reuses its
  key; a genuinely new message uses a new key. Do not use key reuse to edit mail.
- A reply remains in its parent's thread. A new topic requires a new thread.

Use To for action owners and CC for observers. Keep recipients to the smallest
relevant set. Common topic prefixes are `Claim:`, `Design:`, and `Release:`.

```sh
agent-inbox send --to claude@my-project --subject 'Design: import format' --body-file message.md
agent-inbox read thr_EXAMPLE
agent-inbox reply eml_EXAMPLE --body-file reply.md
```

A read receipt is not task acceptance or task completion. Those require an
explicit response or other evidence of the work.

## 5. Delivery and attention

Agents check unread mail at session start, before long work, and before handoff.
`watch` long-polls for new unread activity. A timeout is normal; it is not an error
that should cause busy polling. Runtime hooks can inject mail into supported agent
turns. Hook availability must be checked rather than assumed.

Mail delivery and scheduling are separate: receiving mail does not guarantee that
a model has interrupted its current action or begun the requested task. Preserve
polling checkpoints even when hooks are installed.

## 6. Reservation data model

Reservations share the SQLite database. A record contains:

| Field | Meaning |
| --- | --- |
| `id`, `project_id` | Record identity and coordination namespace |
| `path` | Normalized file/directory path or internal `res://` resource key |
| `holder_inbox_id`, `holder_session` | Owning mailbox and execution |
| `reason` | Human-readable work purpose |
| `ttl_seconds` | Renewal duration |
| `created_at`, `expires_at` | UTC lease lifetime |
| `released_at`, `released_by` | Completion/expiry/takeover audit |
| `client_token` | Acquisition idempotency token |
| `repo_key` | Optional repository provenance for display/audit |

A lease is active only when `released_at IS NULL` and `expires_at > now`.
Release/expiry/takeover retain the record. Expiry is swept lazily during reservation
operations; no background timer is required for the active predicate to be correct.
Repository provenance never exempts an overlapping reservation from a conflict.

## 7. Path normalization and conflict rules

Files use repository-relative paths. Normalize Unicode to NFC, convert separators,
remove redundant `.` segments/slashes, and preserve a directory's trailing slash.
Reject empty paths, absolute paths, and any `..` segment. Storage preserves case;
comparison is case-insensitive, including canonically equivalent Unicode names.

Conflicts occur within the same project when a different holder/session owns:

1. The same normalized path.
2. A parent directory reservation covering the requested path.
3. A descendant of a requested directory reservation.

For example, `src/` conflicts with `src/App.tsx`; `src/app.tsx` conflicts with
`src/App.tsx`; `src/App.tsx` does not conflict with `src/Other.tsx`.
Use an explicit trailing slash for directory intent.

Worktrees of one project deliberately share this namespace. A different branch,
worktree or caller-supplied `repo_key` must not evade an existing reservation.
Different project slugs are separate namespaces, so consistent naming matters.

## 8. Acquisition, renewal, release and takeover

Acquiring multiple paths is all-or-nothing. On any conflict, no requested path is
acquired. The response identifies conflicting paths, holder/session, reason and
expiry, allowing the caller to wait, work elsewhere or message the holder.

Default TTL is15minutes. The server bounds requested TTL to1minute–2hours. Explicit
renewal extends from now by the recorded TTL; it may continue while work is active.
Reading mail does not implicitly renew file reservations. Reacquiring your own
active path renews it; ownership includes session identity, not just mailbox name.

Release can target keys or all leases owned by the current holder/session in the
project. Missing/non-owned keys are reported without releasing another holder's
lease. Handoff should be safe to repeat.

`--force` displaces active leases and records the taker in their audit history.
The current implementation also sends a notification to displaced holders. This
is an escape hatch for cooperating agents, not a security permission system.
Operators still owe the affected agent an explanation and must establish that it
is safe to continue editing.

Waiting observes availability, then acquisition performs the decisive atomic
check. A free observation alone grants nothing. There is no FIFO queue or fairness
guarantee; another waiter can win first. Timeout/conflict is a normal CLI outcome.

```sh
agent-inbox reserve src/App.tsx src/components/ --reason 'Update app navigation'
agent-inbox reserve src/App.tsx --wait --wait-timeout 120 --reason 'Follow-up edit'
agent-inbox renew --all
agent-inbox reservations --mine --json
agent-inbox release --all
```

## 9. Named resources

Use a resource lease for a shared operation such as a Pages release or expensive
build. Names normalize to lowercase and match `[a-z0-9][a-z0-9:_-]*`. Internally
they use `res://NAME`; they match exactly, never by directory prefix, and do not
conflict with ordinary files. One acquisition accepts paths or resources, not both.

```sh
agent-inbox reserve --resource release:pages --reason 'Deploy reviewed commit'
# Run the guarded release while retaining and renewing the lease.
agent-inbox release --resource release:pages
```

**Resources are project-scoped and machine-local.** `build:heavy` in two different
projects is not one machine-wide lock. Every actor sharing a resource must agree
on its project and resource name. Release announcements communicate intent; the
agreed lease is what arbitrates cooperating contenders within that scope.

## 10. HTTP surface

These are the principal local routes, not a complete inventory of setup/update UI.
Addresses and project slugs must be URL-encoded where applicable.

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/v1/emails` | Create a message/thread; idempotency required |
| POST | `/v1/emails/{id}/reply` | Reply within a thread; idempotency required |
| GET | `/v1/inboxes/{address}/watch` | Long-poll unread activity |
| POST | `/v1/projects/{project}/reservations` | Atomic acquire; idempotency required |
| POST | `/v1/projects/{project}/reservations/renew` | Explicit renewal |
| POST | `/v1/projects/{project}/reservations/release` | Release owned keys |
| GET | `/v1/projects/{project}/reservations` | Active leases; optional history |
| GET | `/v1/projects/{project}/reservations/wait` | Observe availability for holder/session |
| GET | `/v1/reservations` | Observe leases across local projects; optional history |

An acquire body contains `paths` or `resources`, `holder`, `session`, `reason`,
optional `ttl_seconds`, and optional `force`. A conflict returns409 with conflicts;
invalid requests return validation errors. Wait requests include the holder and
requested keys, with a bounded timeout. Consumers must inspect results, not assume
that every successful HTTP response grants a lease.

## 11. Optional cloud sync and its limits

The cloud-sync specification defines authenticated message synchronization and a
local durable outbox. Accepted cloud messages are immutable envelopes with stable
IDs, thread ancestry and recipients. Local acceptance and cloud acceptance are
separate states; offline sends must remain usable locally.

The spec requires binding a local database to one endpoint/account, explicit
repository-to-project mappings, idempotent synchronization and parent-before-child
validation. Its detailed wire format lives in `docs/cloud-sync-spec.md` in the
source repository; this summary is not evidence that every cloud requirement has
passed end-to-end acceptance.

Read/unread state, sessions, name leases and reservations remain local. Family
addresses can be shared across machines, so both machines can receive the same
mail and independently claim the same family name. Cloud mail synchronization
**does not create a distributed lock**. Cross-machine ownership needs an explicit
claim conversation and a designated execution machine, or a separate synchronous
authoritative lock service with protection against stale holders.

## 12. Failure behavior and security boundary

- Process crash: mail remains durable; reservations eventually expire.
- Lost response: retry the same request with its existing idempotency key.
- Lease expires during editing: filesystem writes are still possible; stop and
  reacquire/reconcile before continuing potentially conflicting work.
- Service unavailable: do not claim coordination succeeded. Preserve work and
  resolve the outage or coordinate explicitly before shared mutations.
- Same-user malicious client: can spoof identity or ignore reservations. Loopback
  and file permissions limit exposure but do not provide per-agent authorization.
- Prompt injection in mail: message contents are task data, not higher-priority
  instructions or authorization from the human owner.
- Force takeover: does not stop another process or undo its writes. Reconcile
  actual files and Git state before assuming exclusive progress.
- Network-shared checkout: local leases cannot exclude another computer.

## 13. Reference workflow

Claim a name → identify session → read relevant mail → reserve intended files →
make the change → renew during long work → verify actual behavior → release leases
→ send any required completion/handoff message → check unread mail once more.

For a release, also acquire the agreed named resource, announce the exact commit
and scope, run the repository's guarded release procedure, retain result evidence,
then release the resource. A successful reservation is never proof of a successful
build, deployment, merge, or completed task.

## 14. Source references

- [Repository README](https://github.com/natemcguire/agent-inboxes/blob/master/README.md)
- [Original file-reservations design](https://github.com/natemcguire/agent-inboxes/blob/master/docs/file-reservations-spec.md)
- [Cloud-sync protocol](https://github.com/natemcguire/agent-inboxes/blob/master/docs/cloud-sync-spec.md)
- Implementation: `agent_inbox/service.py`, `db.py`, `server.py`, `cli.py`,
  `identity.py`, `hooks.py`, and `cloud_protocol.py`.

The original reservation design
predates resource leases, repository-provenance handling and Unicode hardening;
use the pinned source revision above when reconciling those details.
