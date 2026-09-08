# Agent Inbox and Reservations — technical specification

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
0600; an owned data directory is mode 0700. Setup must not chmod an unrelated,
pre-existing shared directory just because a custom database path lies in it.

Mutating operations that require exclusion use SQLite transactions. Acquiring a
reservation or allocating an agent name uses `BEGIN IMMEDIATE` so contenders
cannot both make a successful decision from the same stale snapshot.

Mail and reservation history are durable. There is no filesystem enforcement:
Git and the actual files remain authoritative for source state.

### Runtime and UI

Python 3.11+ runs the CLI and HTTP API without third-party runtime packages:

```sh
python3 bin/agent-inbox serve
python3 bin/agent-inbox --help
```

The repository does not bundle a browser or desktop UI. Clients consume the HTTP
API; the CLI supports every coordination operation. Optional cloud mail sync is
separate from the local service and is not required for local operation.

Each HTTP request thread owns a SQLite connection and closes it in a `finally`
block, including when routing or response writing fails. Connection setup and
socket-bind failures close connections before propagating the error. No cleanup
relies on garbage collection. The server's main connection and optional background
workers have separate lifetimes.

`GET /healthz` returns `service: "agent-inboxes"`, the server process `pid`,
`version`, `status`, and `db`. A failed database probe returns HTTP 503.

## 3. Agent identity

A mailbox address is lowercase `agent-slug@project-slug`.

Project identity comes from an explicit `AGENT_INBOX_PROJECT` override, otherwise
the Git origin repository basename, otherwise the Git root/directory fallback.
Agent identity comes from `AGENT_INBOX_AGENT`, otherwise runtime-family detection.
`whoami` derives and registers the mailbox. Session IDs distinguish executions
sharing the same address; `AGENT_INBOX_SESSION` can explicitly supply one.

Session precedence is explicit `AGENT_INBOX_SESSION`, then recognized runtime
session variables (including `CLAUDE_CODE_SESSION_ID`, legacy `CLAUDE_SESSION_ID`,
and Codex session identifiers), then `CLAUDE_PID` plus process birth time, then
parent PID plus process birth time. Runtime values are hashed into short session
slugs. A shell-parent fallback is best effort; harnesses that launch a fresh shell
for each command must provide a stable runtime ID, agent PID or explicit override.

`claim` atomically selects the lowest free family name: `codex`, `codex-2`, etc.
The current implementation searches up to 99 slots. A name becomes reclaimable
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

Thread reads return `reading_as` at the top level and `your_role` for each email:
`to`, `cc`, `sender`, or `participant`. The role comes from stored recipient and
sender records, never from body text. A sender who is also a recipient gets their
recipient role. A historical thread participant may read later mail without being
an addressee on that specific email. Thread listings expose `your_roles` for the
requesting inbox's unread messages; a thread can contain both To and CC roles.

The CLI prints the reading identity, each email's role, and body boundaries.
Hooks count addressed threads separately from CC-only threads and label subjects
as untrusted data. These cues describe routing, not authenticated human authority.

A read receipt is not task acceptance or task completion. Those require an
explicit response or other evidence of the work.

## 5. Delivery and attention

Agents check unread mail and announcements at session start, before long work, and before handoff.
`watch` long-polls for new unread activity. A timeout is normal; it is not an error
that should cause busy polling. Runtime hooks can inject mail into supported agent
turns. Hook availability must be checked rather than assumed.

Mail delivery and scheduling are separate: receiving mail does not guarantee that
a model has interrupted its current action or begun the requested task. Preserve
polling checkpoints even when hooks are installed.

### Durable announcements

Announcements are local broadcast records, separate from email threads. They
contain an opaque `ann_...` ID, sender address, optional project scope, subject,
Markdown body and creation timestamp. A separate receipt maps announcement ID and
inbox ID to an acknowledgment timestamp. No per-inbox message fanout is required.

The default scope is the sender's project. `--all-projects` explicitly selects
all projects on this local database. Listing selects matching project or global
records at read time, so inboxes registered after publication see earlier
announcements. Acknowledging affects only that inbox; repeated acknowledgments
succeed without changing the original receipt. An out-of-scope acknowledgment
returns 404. Listing never acknowledges automatically.

Creation requires an idempotency key. Retrying identical content with the same
key returns the original announcement. Reusing it with different content or scope
returns 409. Subjects are required and limited to 500 characters; bodies are
required and limited to 100,000 characters. There is no edit/delete or expiration
operation. Listings return newest records first, with a default and maximum of
200 per request. `--unread` plus explicit acknowledgment lets callers drain an
older backlog. `sequence` is a local arrival identifier used for hook deduplication.

```sh
agent-inbox announce --subject 'Build environment' --body-file notice.md
agent-inbox announce --all-projects --subject 'Service maintenance' --body-file notice.md
agent-inbox announcements --unread
agent-inbox announcements --ack ann_EXAMPLE
```

Both commands return JSON. `announce --from` selects a sender explicitly;
`announcements --inbox` selects a reader. `announce --idempotency-key` supports
retries across separate CLI invocations. Otherwise each invocation generates a
new key. Announcement output and bodies remain untrusted task data.

Runtime hooks emit a separately deduplicated announcement notice, including for
future inboxes. The existing `watch` cursor and wake behavior remain email-only;
announcement checking uses hooks or the explicit polling checkpoints. Announcements
and their receipts are not exported through cloud mail synchronization.

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

Default TTL is 15 minutes. The server bounds requested TTL to 1 minute–2 hours. Explicit
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
| GET | `/healthz` | Service identity and database health |
| POST | `/v1/announcements` | Create local announcement; idempotency required |
| GET | `/v1/announcements?inbox={address}` | List announcements; optional `unread=true`, `limit` |
| POST | `/v1/announcements/{id}/read` | Acknowledge for body field `inbox` |
| POST | `/v1/emails` | Create a message/thread; idempotency required |
| POST | `/v1/emails/{id}/reply` | Reply within a thread; idempotency required |
| GET | `/v1/inboxes/{address}/watch` | Long-poll unread activity |
| POST | `/v1/projects/{project}/reservations` | Atomic acquire; idempotency required |
| POST | `/v1/projects/{project}/reservations/renew` | Explicit renewal |
| POST | `/v1/projects/{project}/reservations/release` | Release owned keys |
| GET | `/v1/projects/{project}/reservations` | Active leases; optional history |
| GET | `/v1/projects/{project}/reservations/wait` | Observe availability for holder/session |
| GET | `/v1/reservations` | Observe leases across local projects; optional history |

An announcement creation body contains `from`, `subject`, `body_markdown` and an
optional boolean `all_projects` (default false). Listing returns `announcements`;
creation returns the stored record. The acknowledgment response includes `id`,
`reading_as` and `acknowledged: true`.

An acquire body contains `paths` or `resources`, `holder`, `session`, `reason`,
optional `ttl_seconds`, and optional `force`. A conflict returns 409 with conflicts;
invalid requests return validation errors. Wait requests include the holder and
requested keys, with a bounded timeout. Consumers must inspect results, not assume
that every successful HTTP response grants a lease.

### Reservation readback compatibility

Reservation GET responses retain `reservations` for active rows and add `history`
when `history=1` is requested. The additive `entries` field contains the active
rows followed by any requested history, with an explicit `active` boolean on every
row. Active results are not limited by the history limit. Finished history defaults
to 200 rows and is capped at 200, ordered by release time and ID descending.
`InboxClient.list_reservations_everything(limit=200)` requests this combined view;
its name does not imply an unbounded history export. Older clients keep their
existing keys and behavior.

## 11. Optional cloud sync and its limits

Optional cloud synchronization uses authenticated immutable message envelopes,
a durable outbox, stable IDs and thread ancestry. A local database binds to one
endpoint/account, with explicit repository-to-project mappings, idempotent sync
and parent-before-child validation. See [the cloud protocol](cloud-sync-spec.md)
for the complete wire contract. Local acceptance and cloud acceptance are separate
states; offline mail remains usable locally.

The default cloud configuration is `~/.config/agent-inbox/cloud.json`.
`AGENT_INBOX_CLOUD_CONFIG` selects a separate configuration file. Test or isolated
instances must isolate this path as well as `AGENT_INBOX_DIR` / `AGENT_INBOX_DB`;
changing the database path alone does not change cloud account configuration.
Do not mix database bindings or copy account configuration between installations.

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

## 14. Service operations

The macOS service uses the installed `com.nate.agent-inbox` LaunchAgent. The
operations module validates its label and Python module invocation before acting.
It targets that job through `launchctl`, never a process found by its listening
port.

```sh
scripts/start.sh status
scripts/start.sh start
scripts/start.sh restart
scripts/start.sh stop
scripts/start.sh fg
# Equivalent entry point for packaged Python installations:
python3 -m agent_inbox.operations status
```

`start` bootstraps an unloaded job or kickstarts a loaded job without terminating
an already-running process. `restart` explicitly boots out the owned job and
bootstraps it again. `stop` boots out the job to prevent KeepAlive respawning it.
`fg` runs the ordinary foreground server. Readiness is bounded and verifies that
the health response PID matches the LaunchAgent's PID; a different listener cannot
make startup look successful. Older runtimes lacking the PID field need updating
before they can pass this stronger check. A port collision returns a diagnostic.
Status also reports the client-configured database path and other `.db` files in
the configured data directory, without opening their contents. Review the configured
service error log rather than killing an unrelated listener.

Foreground operation works without launchctl; background control requires macOS
and an installed plist. These commands do not install or switch runtimes. Setup
owns installation. A configured database remains the service's source of truth;
creating or selecting another file does not repair the original service.

## 15. Database recovery

Recovery is an explicit offline reconciliation workflow that creates a new output.
It never modifies an input database or replaces an existing output.

```sh
scripts/merge-db.sh first.db second.db --output recovered.db
# Equivalent packaged entry point:
python3 -m agent_inbox.recovery first.db second.db --output recovered.db
```

1. Open each existing input read-only and take a SQLite backup snapshot, including
   committed WAL contents. Upgrade only the temporary snapshot's schema.
2. Reject failed integrity/foreign-key checks, cloud account bindings, cloud
   synchronization records and unsupported tables.
3. Remap project integer IDs through case-insensitive project slugs and inbox IDs
   through project plus local name. Never equate unrelated numeric IDs.
4. Preserve opaque thread, email and announcement IDs. Matching IDs must agree on
   immutable content; conflicting content or idempotency tokens abort recovery.
   Reconcile duplicate read/join receipts using the earliest timestamp.
5. Remap reservation owners/projects and allocate new local row IDs. Imported
   active reservations become finished history with `released_by: "recovery"`.
   Sessions and agent-name leases are omitted: recovery never revives execution
   ownership. Duplicate reservation identities retain one historical row.
6. Rebuild thread activity from imported mail, validate the complete result,
   commit and checkpoint its WAL, close connections, then atomically publish the
   new file without clobbering an existing path. Failures remove temporary output.

Source databases can remain open while snapshots are taken, but snapshots are
independent points in time: stop writers before final cutover if every latest
message must be included. Review the result before configuring a service to use it.
The utility performs no cutover and does not stop or restart services. Cloud-bound
recovery requires a separate account-aware process and is deliberately refused.
Inputs are the preserved backups; the output contains local mail, announcements,
receipts, mappings and reservation history, not transient hook/update/session state.

## 16. Source references

- [README and commands](../README.md)
- [Reservation design history](file-reservations-spec.md)
- [Cloud protocol](cloud-sync-spec.md)
- Implementation: `agent_inbox/service.py`, `db.py`, `server.py`, `client.py`,
  `cli.py`, `identity.py`, `hooks.py`, `operations.py`, and `recovery.py`.
