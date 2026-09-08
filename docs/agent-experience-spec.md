# Agent Experience (AE): implementation checklist and contract

AE is the normal interface to coordinated work. An agent identifies its mailbox
and runtime session once, restores relevant context, receives changes, accepts
work, reports blockers/results, and hands off. Protocol selection is plumbing.

## Implementation checklist

- [x] A unified, durable event journal for mail delivery, announcements,
  reservations, task transitions, decisions and subscriptions. Events commit in
  the transaction that changes state. Reconnection uses a database-scoped cursor.
- [x] A context endpoint and CLI: identity/session, current assignments, ready
  queue, unmet dependencies, relevant decisions, unread mail/announcements,
  active reservations, subscriptions and actionable recent events. Bound each
  section and expose truncation; never pretend excerpts are the full history.
- [x] Durable work requests with priority, optional target agent, linked thread,
  dependency IDs and intended paths. Atomic claim, block, resume, complete and
  handoff, with expected-version checks and session ownership. Completion requires
  a result; blocking/handoff requires a reason or next-step note. Completing a
  dependency makes waiting work eligible without another message.
- [x] Explicit event acknowledgment, separate from accepting a task or marking
  mail read. Mail To/CC roles remain authoritative routing metadata.
- [x] Persistent subscriptions to specific tasks or threads; direct assignments,
  addressed mail, project queue/reservations/decisions and applicable announcements
  are relevant by default. Unrelated project events are excluded.
- [x] One managed NATS server with JetStream and an MQTT 3.1.1 listener, both
  enabled. One event ID and command contract across HTTP, NATS and MQTT. MQTT is
  mapped to NATS subjects by the broker, not a second inconsistent message store.
- [x] Durable event publication cursor and reconnect/retry. Broker notification
  receipt does not imply task execution. Clients deduplicate by source + sequence
  and recover missing changes from the authoritative HTTP journal.
- [x] Built-in CLI commands and project instructions. Runtime delivery hooks are
  removed; the old hook-check entry point is silent. Context and events supply
  attention without injecting turns or executing message bodies.
- [x] Isolated acceptance: two sessions race to claim, dependency completion,
  block/resume, handoff, reconstruction after restart, project isolation,
  message linkage, cursor replay, and actual MQTT↔NATS command/event traffic.

## Work model

States are `queued`, `active`, `blocked`, `completed`. A queued task may be waiting
on dependencies; `ready` is computed, never a stale stored boolean. Dependencies
must reference existing tasks in the same project, so new tasks cannot introduce
cycles. A task targets an address optionally; claim establishes an owner address
and runtime session. Handoff releases task ownership, returns it to the queue,
and records a durable note and optional new target. It does not silently release
unrelated file reservations. File reservations remain explicit advisory leases;
context shows them alongside intended task paths. They never authorize file writes.

Transitions use optimistic version checks plus SQLite `BEGIN IMMEDIATE`.
Only the owning address/session can change an active task. A replacement runtime
of that same address explicitly uses `resume` with the latest version to recover
an assignment. Address identity is coordination metadata within the existing
same-user trust boundary, not proof of a human's authorization.

Task creation can link a thread visible to its creator; context does not expose
that thread's body to other agents unless normal mailbox membership allows it.
Decisions are explicit project facts with source references, not model-generated
summaries presented as facts. Every body/note remains untrusted task data.

## Attention and recovery

`context` produces a bounded working set. `events --after` provides lossless
forward pagination over the retained journal, filtered to relevant events.
Acknowledgment stamps the actor's receipt; it does not erase history. Replay
cursors are scoped to a source UUID; a cursor from another database is rejected.
Subscription creation starts a new relevance rule; callers explicitly replay from
an earlier cursor if they need earlier matching events.

The journal starts at upgrade. Existing mail, announcements and reservations are
available immediately in context; they are not misrepresented as newly delivered
historical events. No retention deletion is introduced in this change.

## Transport and operations

Setup provisions a pinned, checksum-verified NATS binary into application data.
Normal `serve` supervises one loopback-only broker with authenticated NATS and
MQTT listeners, durable JetStream storage and locally stored mode-0600 credentials.
A missing binary is a clear setup error, not silent polling-only operation.
Managed children are terminated by owned process handle, never by port number.
No work account, external broker or remote machine is connected automatically.

Both protocols accept the same idempotent AE command envelope and deliver the
same event metadata. NATS request/reply and MQTT response topics return committed
results. Messages carry references, not the complete mailbox. Live notifications can duplicate or be missed by disconnected clients. Broker
flush confirms processing, not durable agent receipt. SQLite is the durable replay
and work-state authority.

This release provides one authoritative service. Sharing it across machines needs
a separately configured authenticated network boundary; it does not turn separate
local reservation databases into a distributed lock. No automatic agent execution
or elevated tool authority follows from a broker notification.

## API and protocol contract

`POST /v1/ae/command` accepts `actor`, `session`, `request_id`, `operation` and a
`payload` object. Mutations are idempotent: identical retries return the committed
response; reuse with changed content is rejected. Read operations always return
current data. Operations are `context`, `events`, `task.get`, `task.history`,
`task.create`, `task.claim`, `task.block`, `task.resume`, `task.complete`,
`task.handoff`, `decision.record`, `decision.get`, `subscribe`, `unsubscribe`, `ack`.
See `agent-inbox ae --help` and subcommand help for payload fields.

HTTP also exposes GET `/v1/ae/context`, `/v1/ae/events`, `/v1/ae/status`,
`/v1/ae/tasks/{id}`, `/v1/ae/task-history/{id}`, `/v1/ae/decisions/{id}`.
Event long polling waits at most 60 seconds. Actor identity is required for scoped
reads; cursor continuation requires the matching source UUID.

Broker commands use `ae.commands.SOURCE_UUID` (NATS) or
`ae/commands/SOURCE_UUID` (MQTT). Subscribe before publishing. Replies use
`ae.replies.SOURCE_UUID.SHA256_REQUEST_ID`, with slashes for MQTT; native NATS
request/reply inboxes are also supported. Responses contain `ok`, `request_id`,
and either `result` or `error`. Events use
`ae.events.SOURCE_UUID.PROJECT_UTF8_HEX` (or `global`), with slashes for MQTT.
The local `ae-agent` credential can submit commands and receive events/replies,
but cannot publish forged events. It is shared local client access, not per-agent
cryptographic authentication or per-project broker isolation.

Context targets a 24 KB JSON budget, excerpts body/note/result fields at 800
characters, and marks omitted sections and excerpts. Recent context scans the last
1,000 journal entries; it is not an exhaustive backlog. Forward event pagination
scans up to 1,000 entries per page, returns up to 200 matching events, and advances
the cursor through hidden events. Follow `has_more` even when a page is empty.
Task reads include the last ten transition snapshots; task history provides full
forward version pagination. Decisions and original messages remain retrievable.

Setup removes legacy delivery wiring only when explicitly run. This source change
does not itself edit an operator's installed runtime or personal configuration.
Generic database merge refuses databases containing AE work state. Deployments
must provision the pinned broker before enabling the 2.0 runtime.

## Verification record

On September 8, 2026, the isolated full suite passed 129 tests, including the
real MQTT/NATS bidirectional command/event test with paho-mqtt and the pinned
broker. `scripts/verify-ae.py` also passed against a real temporary HTTP service,
CLI and managed broker: startup, context, replay, claim, reservation guidance,
handoff/history, bundled UI route and owned broker shutdown. No installed personal
service, remote account or main checkout was changed by these checks.
