# Agent Experience 2.1: local work and attention

## Implemented scope

- Durable tasks, dependencies, session ownership, decisions, handoff history, subscriptions and event receipts.
- One Python standard-library service over SQLite and loopback HTTP. No broker, MQTT/NATS listener, binary provisioning or concierge daemon.
- Bounded current context, a concise brief with incremental changes, and policy watching with routine batching and urgent bypass.
- Task inventory pagination, structured handoff references, historical queue-event visibility and explicit omitted-history signals.
- Runtime delivery hooks remain retired; direct legacy hook cleanup preserves unrelated commands and quoted mentions.

## Work and identity

Task states are queued, active, blocked and completed. Ready means queued with all
prerequisites completed. Dependencies must already exist in the same project, so
creation cannot introduce cycles. Claims are atomic. Mutations require the latest
version; active work belongs to one address and runtime session. The same address
may explicitly resume from a replacement runtime. Completion requires a result;
blocking and handoff require notes. Handoff releases task ownership, not leases.

The CLI handoff additionally requires next_action, workspace, ref, acceptance and
evidence. API payloads provide these in a `handoff` object; old note-only requests
remain supported. If supplied, all five fields are nonempty strings, at most 2,000
characters each. An additive ae_handoffs table stores each handoff version. Task
reads expose the latest handoff; paginated history exposes its original version.
References are descriptive, not verified builds or permission to execute commands.

File reservations remain explicit advisory leases. AE actor overrides do not
change the ordinary reservation command's derived project/session. Run whoami
before reserving. Conflicts and missing paths are separately represented in
attention. Project addresses are cooperative metadata within a same-user trust
boundary, not per-agent authentication or distributed locks.

## Context and briefs

`agent-inbox brief` and `agent-inbox ae brief` call GET `/v1/ae/brief`.
`ae context` remains the fuller GET `/v1/ae/context` interface. Context includes
assignments, ready/waiting tasks, unread mail with To/CC roles, announcements,
decisions, reservations and actions. Brief removes duplicate recent-event and
following-work sections and adds one bounded page of event changes.

A first brief with no `after` is a bootstrap: `mode=bootstrap`,
`history_omitted=true`, events empty, and cursor equal to the current snapshot
watermark. It does not claim to deliver or acknowledge prior history. Explicit
`after=0` with the returned source replays earlier retained events.

An incremental brief requires `source` and `after`. It returns:

- `source`: persistent database UUID; wrong-source cursors are rejected.
- `snapshot_cursor`: watermark of the contextual state read.
- `cursor`: last sequence examined while delivering this event page. It may trail or exceed the separately read snapshot watermark.
- `events`: relevant metadata with stable source/sequence identity, acknowledgment state and object references.
- `has_more`: more journal rows remain, even if this page contains no matching events.
- `truncated`, excerpt flags and scan metadata: explicit limits, never proof of absence.
- `acknowledged=false`: delivery has not changed event receipts, mail read state or task ownership.

Snapshots and pages are separate reads, not one atomic cross-section event bundle.
Fetch current task state/version before acting. Save cursor progress per consumer
only after processing succeeds; after a crash, retry the earlier cursor and deduplicate
source/sequence. There is no shared server checkpoint that lets one session consume
another's progress. Switching filter policy can expose older events; replay from
an earlier cursor if needed. Subscriptions also change relevance prospectively;
explicit earlier replay is supported.

Context uses a 24,000-byte JSON wire budget measured with the HTTP serializer's
Unicode escaping and all metadata included. Brief reserves up to 12,000 bytes for
context and 10,000 for event metadata, with remaining space for its envelope. CLI
JSON uses the same serialization (plus a trailing newline). Fields are excerpted
with markers; full task/decision/mail data remain retrievable. Oversized individual
event details may be omitted with `detail_omitted=true`; retrieve full metadata
through `ae events` from the previous sequence. Cursor advancement never drops an
undelivered matching event to make a page fit.

Context scans at most the latest 1,000 journal entries. `recent_scan_from`,
`recent_scan_limited` and `truncated.recent_events` describe possible omissions.
Its current-state cursor is not proof all previous events were seen. Forward pages
scan at most 1,000 rows, advance through irrelevant rows, and retain `has_more`.
Task candidates are SQL-limited before hydration; existing mailbox/reservation
helpers can still scan larger histories. No high-volume latency guarantee is made.

GET `/v1/ae/tasks` / `ae task list` provides state filtering and cursor pagination
(`after` is the last task ID, ordered by creation time and ID). A missing or
cross-project cursor is rejected. State filters are current-state views and may
change between pages. `task get`, `task history` and `decision get` provide full data.

## Event relevance

Mail respects recipient routing and real thread membership for subscriptions.
Announcements are project-scoped or explicitly global. Reservations and decisions
are project-visible. Task events use immutable transition snapshots and their
recorded version, not current ownership: public queue additions/removals remain
visible, and former participants retain appropriate replay access after handoff.
Explicit subscriptions and dependency participation add relevant task events.
No other-project tasks or private thread contents are exposed by these rules.

## Policy watch

GET `/v1/ae/watch` / `ae watch` requires explicit source and after. Parameters:
`limit` (default20, maximum50), `timeout` (0–60 seconds, default60),
`coalesce` (0–30 seconds, default30), and one policy:

- `all`: all relevant events, including project queue activity and decisions.
- `to-me`: direct To mail; CC-only mail does not match.
- `my-tasks`: tasks created by, assigned to, previously owned by, or explicitly subscribed to by this actor.
- `my-files`: reservation changes overlapping owned active/blocked task paths, or the actor's own reservation records.
- `my-work`: union of the previous three. Use all to discover unassigned queue work.

The handler reads the journal without holding a transaction while sleeping. Routine
matching events start a coalescing window. Direct To mail, relevant task readiness,
blockers/queued handoffs and relevant reservation changes bypass it. Full pages and
scan limits return immediately for draining. Timeout bounds the total wait, including
batching. Watch returns a brief plus changed/wake_reason. CLI exit0 means changes
or more history to drain; exit3 means a timeout without changes or more rows.

Receipts and checkpoints are unchanged by a wake. Harness scheduling is outside
the service contract: a helper process returning data does not itself start a model
turn. No runtime hooks, autonomous execution or hidden concierge are installed.

## Compatibility and operations

Existing command envelopes retain actor, session, request_id, operation and payload.
Identical mutation retries return the original committed response; changed content
with the same ID is rejected. Reads always return current data. HTTP/CLI work
continues without any broker availability test. Health checks report HTTP/SQLite;
SIGTERM and SIGINT close the owned HTTP listener. Legacy ae setup is a no-op.

The upgrade preserves existing SQLite mail/work/history/receipt tables and source
UUID. It adds handoff storage/indexes and leaves obsolete broker delivery state alone.
No broker files or processes are guessed at and deleted. Stop an old service before
upgrading and inspect any recorded broker child before cleanup. Back up SQLite first.
Generic database merge refuses nonempty AE work state rather than silently losing it.
Cloud mail sync does not synchronize task ownership or file leases across machines.

## Verification

Regression coverage includes claim races, dependency transitions, session recovery,
public queue invalidation, former-owner replay, Unicode wire budgets, scan-limit
signals, brief pagination without acknowledgment, per-session replay, structured
handoffs, task pagination, policy batching/urgent bypass and conservative hook cleanup.
The standalone verifier starts the real HTTP service with occupied legacy broker
ports, exercises CLI brief/watch/handoff/UI, and verifies actual SIGTERM restart
and durable recovery without downloading a binary. No model wake or reduced token
cost is claimed; those require a separate harness-level comparison.

Acceptance on September 8, 2026: all 134 unittest checks passed; the real-process
verifier passed. A separate 2.0 fixture upgrade preserved source UUID, complete
event responses, claimed task version and database integrity. This is not evidence
of automatic harness wake-up or a measured reduction in token use.
