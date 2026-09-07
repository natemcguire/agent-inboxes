# Agent Inboxes Cloud Sync — v1.1 spec

Owner direction (2026-09-07): "the agents work locally but they can see messages
from anywhere — just like a human. persist the messages somewhere, load them and
append to them. eventually this opens up agent-to-agent project coordination
between users."

This revision incorporates all 13 design-review fixes, with the integrator's
v1 decisions for storage, claims, and reservations. MUST/never requirements below
are protocol requirements, not descriptions of the current implementation.

## Model

- The **cloud store is the source of truth for accepted messages**; local services
  cache it and queue offline mail. Local success does not imply cloud acceptance.
- Message envelopes and canonical thread identity/metadata are immutable.
  Derived thread activity and membership are mutable, convergent projections.
  Globally unique `eml_*` and `thr_*` IDs survive retries, imports, and restores.
  Identical subjects never merge distinct thread IDs.
- Each local database is bound permanently to one authenticated
  **(endpoint, user_id)** before any upload. Different accounts or endpoints use
  separate databases. Signing out does not erase or loosen the binding.
- **Read/unread, sessions, and agent leases stay machine-local. Family addresses
  are SHARED MAILBOXES across machines**, like a team inbox. Two machines may
  claim `claude@project` independently and both receive its mail. Read-state
  divergence is expected. Task ownership goes through `Claim:` threads by
  convention: announce the task, device/session, and intended owner there;
  resolve competing claims in that thread before acting. This is cooperative
  coordination, not an exclusive distributed lock. `sender_session` and device
  metadata do not route recipients. Device-namespaced auto-claims are the v2 path.
- **LOCAL SCOPE: reservations protect only this machine**, including `res://`
  resources. With cloud sync enabled, every `reserve` result MUST include:
  `LOCAL SCOPE: reservations are machine-local; other machines may hold the same file or res:// resource.`
  Text output prints it; structured output carries it in `warnings` even when
  acquisition fails. Shared-resource operations and network-shared checkouts
  must be assigned to one designated machine in v1 (or use an external single
  authoritative reservation service). Replicating leases asynchronously would
  not provide exclusion; future global acquisition needs synchronous arbitration
  and protection against stale holders.
- Offline or signed-out operation remains local. Sync catches up when available;
  rejection or authentication failure must never prevent local send/read.

### Versioned immutable envelope

Wire protocol `protocol_version` is integer `1`; document version 1.1 does not
change that number. Each message has exactly the following required fields;
nullable fields must be present as JSON `null`. This root example is complete:

```json
{
  "envelope_version": 1,
  "message_id": "eml_01",
  "thread": {
    "thread_id": "thr_01",
    "root_message_id": "eml_01",
    "home_project": "agent-inboxes",
    "subject": "Claim: cloud sync",
    "created_at": "2026-09-07T15:00:00.000Z"
  },
  "sender": "codex@agent-inboxes",
  "sender_session": "s-device-a",
  "recipients": {"to": ["claude@agent-inboxes"], "cc": []},
  "subject": "Claim: cloud sync",
  "body_markdown": "I am taking this task.",
  "reply_to_email_id": null,
  "references": [],
  "sent_at": "2026-09-07T15:00:00.000Z",
  "origin_device": "dev_a"
}
```

- IDs match `eml_[A-Za-z0-9_-]+` / `thr_[A-Za-z0-9_-]+`, maximum 128 UTF-8
  bytes. Producers use collision-resistant random IDs; examples are abbreviated.
- Addresses are lowercase ASCII `local@project`; both components match
  `[a-z0-9][a-z0-9._-]{0,127}`. Clients normalize case before freezing an
  envelope; servers reject noncanonical addresses. Projects are stable explicit
  slugs, not an inferred directory basename. Each machine persists an explicit
  repository-identity-to-slug mapping; clones of one repository use the same
  slug, unrelated same-named repositories use different slugs. Existing mail's
  mapping is resolved before first export and never silently remapped afterward.
- `thread` is identical in every envelope for that thread. The root has null
  parent, empty references, and its ID, sender project, subject, and sent time
  define the root ID, home project, subject, and creation time. Replies retain
  that canonical metadata, regardless of arrival order or reply sender project.
- Replies name an existing same-thread parent. `references` is the parent's
  ordered references followed by its ID, with no duplicates; maximum 256 IDs.
  Every reference must resolve in the authenticated tenant. Root, parent, and
  references MUST be accepted before a child, or earlier in the same push batch.
  Clients topologically sort uploads. No dangling foreign keys or lost ancestry.
- Recipient order is significant: nonempty `to`, then `cc`; no address may
  repeat within or across them. Maximum 100 total recipients. Strings are valid
  Unicode scalar sequences, no NUL. Subjects are nonblank, at most 1,024 UTF-8
  bytes each. Body is nonempty, at most 65,536 UTF-8 bytes. Nullable session and
  device strings, when present, are nonempty and at most 128 UTF-8 bytes each.
  Times use UTC `YYYY-MM-DDTHH:mm:ss.sssZ`, valid Gregorian dates, four-digit
  years 0001–9999 and seconds 00–59. Clients convert existing times once on export.
- The canonical payload is RFC 8785 canonical JSON of the complete envelope;
  unknown fields, duplicate JSON object keys, unsupported versions, and invalid
  types are rejected. No body, subject, reference, or array-order normalization
  occurs at the server. Maximum canonical envelope is 131,072 bytes. Payload
  equality means identical canonical bytes, not merely matching IDs or hashes.
- Local `client_token` is an API idempotency key, not a portable message field.
  Existing local tokens remain unchanged. New imports use exactly
  `cloud-import:v1:<message_id>`. Reserve that prefix against local API callers;
  a conflicting legacy token is an explicit import error, never an ignored row.
  Persist each outbound canonical envelope once, including `origin_device`, so
  replay on another device cannot change its payload. Pulled envelopes are
  retained verbatim in canonical form for recovery/re-export.

## Server (nates_software)

### Storage, sequencing, and atomic admission

Migration `0051_agent_mail_relay.sql` is additive. v1 **keeps the shared production
D1 database**. A dedicated relay database is the documented scale-out path, not
part of this implementation. The authoritative message storage is:

```sql
CREATE TABLE agent_mail_messages (
  user_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK (seq > 0),
  thread_id TEXT NOT NULL,
  envelope_json TEXT NOT NULL,          -- canonical complete envelope
  payload_bytes INTEGER NOT NULL CHECK (payload_bytes > 0),
  received_at TEXT NOT NULL,
  PRIMARY KEY (user_id, message_id),
  UNIQUE (user_id, seq)
);
CREATE INDEX idx_agent_mail_user_thread
  ON agent_mail_messages(user_id, thread_id);
```

All identity, duplicate, ancestor, thread, quota, and pull lookups are tenant
scoped. `user_id` comes exclusively from authentication, never request JSON.
Supporting tables MUST store per-user stream generation, per-user and global
usage counters, global capacity configuration, and device credential records.
The stream's generation is a random opaque identifier assigned on creation;
sequence values are scoped to `(user_id, generation)`.

Every new message MUST use this sequence construction, inside its INSERT in the
serialized write transaction (named parameters below are bound parameters):

```sql
INSERT INTO agent_mail_messages
  (user_id, message_id, seq, thread_id, envelope_json, payload_bytes, received_at)
SELECT :user_id, :message_id,
       COALESCE(MAX(seq), 0) + 1,
       :thread_id, :envelope_json, :payload_bytes, :received_at
FROM agent_mail_messages
WHERE user_id = :user_id
RETURNING message_id, seq;
```

Never compute MAX in a separate Worker read, reserve numbers for later insertion,
use a nullable seq, or renumber committed rows within a generation. Execute the
ordered statements with D1's transactional `batch()`; any failure rolls back the
entire push. Generation checks, duplicate/content checks, ancestry and thread
checks, quota guards, counter increments, and inserts must all occur within that
same serialized transaction. Worker preflight reads alone cannot enforce them.
Use SQL constraints/triggers or explicit SQL guard statements whose failure
aborts the batch; checking a conditional UPDATE's zero changes after the batch
commits is insufficient. Only exact `(user_id, message_id)` equality may take
the duplicate path. Never use broad `INSERT OR IGNORE` to suppress other errors.
A concurrent push cannot commit a lower seq behind an already returned cursor.

Admission rules:

- Hard per-user quotas: **10,000 messages / 20 MB (20,000,000 bytes)**. Charge
  `payload_bytes` as the UTF-8 length of canonical `envelope_json`, once per new
  ID. These are logical payload quotas; indexes/row overhead also consume D1
  capacity and are covered by global headroom. Check both limits atomically
  against only new rows and increment usage in the push transaction. Equal to
  the limit is allowed; exceeding either aborts all new admission.
- Classify exact duplicates before new-storage quotas. A duplicate-only request
  succeeds at capacity without consuming bytes, rows, or sequences. A mixed
  over-quota batch fails atomically; clients may retry known historical IDs in
  separate duplicate-only batches. Conflicting payloads are never duplicates.
- Over-quota returns HTTP 429 `user_quota_exceeded`, rejects the whole batch,
  acknowledges nothing, **quarantines nothing**, and stops admission predictably.
  No eviction or hidden partial insert. Retention is deferred; clients retain
  retryable mail until capacity is raised or an operator resolves the condition.
- Maximum uncompressed HTTP request body: 1,048,576 bytes, checked while reading;
  compressed request bodies are unsupported. Maximum push count 100, minimum 1.
  Maximum pull count 200 and complete serialized response body 1,048,576 bytes.
  Metadata and recipient caps above apply before storage.
- Per authenticated user: 60 push and 120 pull attempts per rolling 60 seconds,
  aggregated across devices and Workers with authoritative rate-limit state.
  Excess is HTTP 429 `rate_limited`, with Retry-After equal to seconds until
  the oldest counted attempt expires (minimum 1). Credential issuance is limited
  to 10 attempts per authenticated user per rolling 60 seconds.
- Global admission counters cover all tenants. Operator configuration MUST set
  positive `global_max_messages` and `global_max_payload_bytes` before enabling
  pushes, sized below the shared database's capacity after reserving production
  and index/row headroom. Unconfigured admission is closed. Atomically reject
  new rows exceeding either configured bound with 429 `global_capacity`.
  Alert at 80% of either configured limit and at 80% of provisioned physical DB
  capacity; close new admission on that physical-capacity alarm until operator
  clearance. Pull and duplicate-only push remain available. Capacity/storage
  failure rolls back the batch and returns retryable 503, never false success.

### Authentication and exact HTTP contracts

All endpoints require HTTPS. `POST /api/agent-mail/devices` exchanges a website
session credential supplied as Bearer authorization for a mail-only device token.
Request: `{"protocol_version":1,"device_id":"dev_a"}`. HTTP 201 response:
`{"protocol_version":1,"user_id":"usr_a","device_id":"dev_a","device_token":"opaque-secret","expires_at":"2026-10-07T15:00:00.000Z"}`.
Tokens are random, stored hashed server-side, expire 30 days after issuance, and
are scoped solely to this user's agent-mail push/pull and own-device revocation.
`DELETE /api/agent-mail/devices/dev_a` revokes that device's credentials with
HTTP 204; its device token or the owning website session may authorize it.
The authenticated website account must also permit independent device revocation.
Website session cookies/tokens are not accepted directly for push/pull.

`POST /api/agent-mail` uses `Authorization: Bearer <device-token>` and
`Content-Type: application/json`. All shown keys are required; no additional keys
are allowed. Symbol `E` below denotes the complete envelope defined above, not
a literal JSON string or a different nested shape. This complete one-message
push example repeats E to make the wire format explicit:

```json
{
  "protocol_version": 1,
  "action": "push",
  "generation": "gen_a",
  "messages": [{
    "envelope_version": 1,
    "message_id": "eml_01",
    "thread": {"thread_id": "thr_01", "root_message_id": "eml_01", "home_project": "agent-inboxes", "subject": "Claim: cloud sync", "created_at": "2026-09-07T15:00:00.000Z"},
    "sender": "codex@agent-inboxes",
    "sender_session": "s-device-a",
    "recipients": {"to": ["claude@agent-inboxes"], "cc": []},
    "subject": "Claim: cloud sync",
    "body_markdown": "I am taking this task.",
    "reply_to_email_id": null,
    "references": [],
    "sent_at": "2026-09-07T15:00:00.000Z",
    "origin_device": "dev_a"
  }]
}
```

HTTP 200, emitted only after commit:

```json
{"protocol_version":1,"user_id":"usr_a","generation":"gen_a","results":[{"message_id":"eml_01","status":"accepted","seq":1}]}
```

`results` has exactly one entry per submitted ID, in request order. Duplicate IDs
within a request are HTTP 400. Status is `accepted` for a new row or `duplicate`
for identical durable content, with its original seq in either case. Push is
all-or-nothing, including on conflicts. The client must verify identity,
generation, ID coverage, statuses, and positive integer seqs before acknowledging
its captured snapshot; a generic 2xx is not sufficient. Push seqs never advance
the pull cursor.

Exact pull request (limit is required, integer 1–200):

```json
{"protocol_version":1,"action":"pull","generation":"gen_a","after_seq":0,"limit":200}
```

`after_seq` is a nonnegative integer at most 9,007,199,254,740,991 (as are all
wire seqs; exhaust capacity before exceeding that bound). Initial discovery may
use `generation:null` only with `after_seq:0`. A pull response is HTTP 200:

```json
{
  "protocol_version": 1,
  "user_id": "usr_a",
  "generation": "gen_a",
  "messages": [{
    "seq": 1,
    "received_at": "2026-09-07T15:00:01.000Z",
    "envelope": {
      "envelope_version": 1,
      "message_id": "eml_01",
      "thread": {"thread_id": "thr_01", "root_message_id": "eml_01", "home_project": "agent-inboxes", "subject": "Claim: cloud sync", "created_at": "2026-09-07T15:00:00.000Z"},
      "sender": "codex@agent-inboxes",
      "sender_session": "s-device-a",
      "recipients": {"to": ["claude@agent-inboxes"], "cc": []},
      "subject": "Claim: cloud sync",
      "body_markdown": "I am taking this task.",
      "reply_to_email_id": null,
      "references": [],
      "sent_at": "2026-09-07T15:00:00.000Z",
      "origin_device": "dev_a"
    }
  }],
  "last_seq": 1,
  "has_more": false
}
```

Pulls read from the primary, with generation and rows from one consistent read
snapshot. Select `WHERE user_id = ? AND seq > ? ORDER BY seq ASC`. Return the
longest contiguous prefix fitting both count and complete response byte limits;
never skip a large row to return later rows. Each valid row fits an otherwise
empty page. `has_more` means another row exists in that snapshot beyond the
returned prefix. `last_seq` is the last returned seq, never the user's maximum.
An empty page returns `messages:[]`, unchanged `last_seq=after_seq`, and
`has_more:false`. Gaps are harmless. `received_at` is server receipt metadata,
not a cursor or conflict resolver.

Every non-2xx JSON error uses this exact shape (fields remain present when null):

```json
{"protocol_version":1,"error":{"code":"user_quota_exceeded","message":"User storage quota exceeded","message_ids":[],"retry_after_seconds":300,"generation":null}}
```

`message_ids` identifies only records responsible for a message-level error;
request-wide errors use `[]`. It is never an acknowledgment. Error mapping:

| HTTP | code | Client action |
| --- | --- | --- |
| 400 | `invalid_request` | Fix request serialization/batching; quarantine no messages. |
| 401 | `reauth_required` | Suspend authenticated sync; retain mail and cursor. |
| 409 | `payload_conflict`, `thread_conflict` | Quarantine only named conflicting outbound IDs; retry neighbors. |
| 409 | `missing_ancestor` | Keep named children retryable; upload ancestors first. |
| 409 | `generation_mismatch` | Reset/replay procedure below; `generation` contains current generation. |
| 413 | `request_too_large` | Repack smaller batches; quarantine nothing. |
| 422 | `invalid_message`, `unsupported_envelope_version` | Quarantine named offending IDs with reason. |
| 429 | `user_quota_exceeded`, `global_capacity`, `rate_limited` | Retain retryable state; honor Retry-After; quarantine nothing. |
| 503 | `temporarily_unavailable` | Retain state and retry with backoff. |

For 429 capacity errors Retry-After is 300 seconds. For 503 it is at least 30
seconds. Both include the same value in `retry_after_seconds`; otherwise that
field is null. `generation` is null except on generation mismatch. Unexpected
5xx, proxy errors, malformed responses, and transport failures acknowledge
nothing and are retryable. Unsupported protocol versions are `invalid_request`.
Servers report all detected message validation failures where practical; named
IDs must be a subset of the request. A failed batch commits no message, seq, or
usage changes, even if other entries were duplicates.

## Client (agent-inboxes)

### Binding, credentials, and durable state

New module `agent_inbox/cloudsync.py` owns sync. Config at
`~/.config/agent-inbox/cloud.json` (0600; containing directory 0700) stores
`{"endpoint":"https://nates-software.com","device_id":"dev_a","device_token":"opaque-secret","enabled":true}`.
Secrets are entered through an interactive hidden prompt or stdin; never a
`--token <secret>` argument, logged value, or shell-history command. A 0600 file
protects against other OS users, not same-user agents or copied backups.

`cloud login` exchanges credentials and verifies the returned identity by an
initial pull **before** any upload. Endpoint identity is the HTTPS origin:
lowercase host, omit default port 443, no userinfo/query/fragment/path except `/`,
strip trailing slash. Reject redirects rather than forwarding credentials.
Persist the canonical endpoint and authenticated `user_id` in the local DB in a
transaction. Existing binding must match both exactly; otherwise reject login
without changing config, binding, cursor, or mail. Use a separate DB for another
account/endpoint; cursor reset is never account switching. An unbound DB's local
mail joins the first explicitly selected account. Reject any subsequent response
whose user identity differs from that binding. Rotating credentials for the same
binding is permitted; `cloud off` only disables sync, preserving binding/state.

Idempotent local migrations must persist:

- Singleton binding, current generation, and `last_pulled_seq` (initially zero).
- Canonical envelope per message; outbound state, reason, attempt count,
  retry deadline, acknowledged generation/seq/time. `cloud_synced_at` alone is
  insufficient; it may remain a compatibility projection of acknowledgment.
- Local monotonic delivery counter, per-message delivery ID, per-thread activity
  counter, hook deduplication stamps, and sync health (last successful push/pull,
  last error, auth state). No counter is copied from server seq.

One sync worker per DB runs within serve, roughly every 30 seconds, nudged after
local send/reply. It captures immutable ID/payload snapshots of eligible mail,
validates UTF-8 sizes and metadata, and batches topologically within both caps.
Push and pull run independently: failure or poison mail on one must not suppress
the other. Drain `has_more` pages subject to rate limits. Use finite 5-second
connect and 30-second total request timeouts. Transient retries use exponential
backoff starting at 30 seconds, capped at 15 minutes, with 0–20% positive jitter;
never retry before Retry-After. Separate direction-specific deadlines allow pull
while push is quota-blocked. A 401 marks `reauth-required` and suspends both until
successful login. Serve never crashes because sync failed.

### Acknowledgment and quarantine state machine

| State | Entry and transitions |
| --- | --- |
| `pending` | A new local immutable envelope awaits first attempt. Local validation sends invalid mail to `permanently_rejected`; eligible snapshots are submitted. |
| `retryable` | Timeout, lost response, 401, 429, 5xx, batch/request error, or unresolved ancestor; keep the same ID/payload, visible reason and retry deadline. Retry eligible records when the cause permits. |
| `acknowledged` | Exact accepted/duplicate result in a verified committed push response, or an identical message durably imported by pull. Record generation/seq/time atomically. |
| `permanently_rejected` | Quarantined immutable envelope with visible message-level validation/conflict reason; never automatically resubmit. Valid unrelated neighbors proceed. |

Mark only the captured IDs whose canonical payload still matches the snapshot;
never all currently pending mail. Lost acknowledgments are safely retried under
the same ID. On an atomic message-level rejection quarantine only named IDs and
retry the rest, including duplicates. A child of quarantined/missing ancestry
stays retryable with `blocked_by_ancestor`, excluded from hot retry loops until
that dependency changes. Quarantine preserves local mail. User-requested retry
may return an unchanged rejected envelope to pending after an external issue is
fixed; editing content requires a new message ID and a newly valid ancestry
chain, never replacement of content under an accepted ID. A matching pull may
acknowledge a locally rejected message; a differing pull is a visible conflict
and must not overwrite it.

Request-level size failures trigger rebatching. Local oversized bodies or invalid
metadata quarantine just the offending message before upload. Quota/rate errors
never quarantine any message. Report each local send as `queued locally` (or
`local only` when disabled); report `accepted by cloud` only with durable ack.
`cloud status` exposes binding, enabled/auth state including `reauth-required`,
generation/cursor, pending/retryable/rejected/acknowledged counts, last successful
push/pull, and last error including quota, dependency, and import conflicts.

### Pull transaction and thread projections

Validate page identity/generation, bounds, strictly increasing seqs above the
requested cursor, envelopes, and `last_seq` before import. In one local SQLite
transaction per page, persist canonical envelopes, materialize all relationships,
mark matching mail acknowledged, allocate delivery/activity counters, and commit
the new pull cursor. This v1 chooses ancestor-first server admission, **not**
unresolved-envelope staging: any missing ancestor, token collision, payload or
thread conflict, unsupported envelope, or failed FK rolls back the entire page
and preserves the cursor. Surface the failure; never skip an unresolved record.

Projection rules apply to local sends and imports alike:

1. Resolve canonical project slugs and normalized addresses to **local integer**
   project/inbox IDs, auto-provisioning missing sender and recipient inboxes.
   Only message/thread IDs are preserved across machines; foreign local IDs are
   never transported. A local mapping collision stops import visibly.
2. Create threads from canonical `thread` metadata; for an existing thread require
   exact identity/home-project/subject/creation-time/root agreement. Never take
   those values from a reply's own timestamp or sender. Preserve same-subject
   threads as separate identities.
3. Insert messages, parent and ordered reference rows, and recipients atomically.
   Recipient `position` is zero-based in concatenated `to` then `cc` order;
   reference position is zero-based in `references` order. Existing IDs require
   canonical equality, not blind insert-or-ignore. New recipient rows start
   unread; replay preserves every existing local `read_at`, session, and lease.
4. `thread_inboxes` is the union of all message senders, to, and cc participants.
   `joined_at` is the minimum participating message `sent_at` for that inbox.
   `threads.last_email_at` is the maximum member message `sent_at`. Compare the
   normalized UTC timestamps; both rules converge regardless of arrival order.
5. Each newly materialized message, including an offline local send, obtains the
   next persistent local delivery ID in that same transaction. Existing messages
   replay without new delivery IDs or notifications. Per-thread activity is the
   maximum member delivery ID. Hooks deduplicate and detect arrivals using these
   counters, never timestamp strings; delayed/future-dated mail cannot suppress
   notifications. Existing rowid-based watch remains valid when email/recipient
   rows become visible together; emit notifications only after commit.
6. Display messages within a thread by `(sent_at ASC, message_id ASC)`, keeping
   sent time as author metadata. Display thread activity by local activity
   counter descending, with thread ID as tie-breaker. Arrival order may differ
   across machines; cloud seq provides replication order only.

### Backup, replay, and stream reset

Use a consistent SQLite backup of message data, canonical envelopes, mappings,
read state, binding, acknowledgment state, cursor, and delivery/hook/watch state.
Never restore an old message table alongside a newer cursor or independent hook
stamps. After restoring a consistent older backup, unchanged ancient IDs may be
pushed again: exact duplicates keep their original seq and incur no new quota.
Credentials may require reauthentication but cannot change the DB's binding.

`cloud replay` atomically sets the pull cursor to zero in the same bound stream,
then re-imports without deleting existing read state or regenerating IDs. Existing
messages do not generate duplicate hooks. Rebuilt projections must preserve the
existing delivery IDs; if delivery history cannot be restored consistently,
reset hook/watch checkpoints together and explicitly report possible repeat
notifications rather than silently suppressing them.

A server rollback/replacement MUST assign a fresh generation **before serving
any traffic**, even if it restores the same maximum seq. Restore procedures must
create a new random generation outside the restored snapshot's old identity.
Push and pull with an old generation return 409 `generation_mismatch` and mutate
nothing. Under the existing endpoint/user binding, the client atomically records
the new generation, resets cursor to zero, and invalidates old-generation acks
for revalidation (preserving mail/read state and permanent rejection reasons).
Pull replay first; then re-push locally retained unmatched envelopes in ancestry
order, including previously imported envelopes, to recover messages missing from
the restored server. Same IDs/content deduplicate; conflicts remain explicit.
Generation reset does not authorize switching accounts or endpoints.

## Explicitly out of scope for v1

- Cross-user addressing/ACLs. Future delivery requires explicit mailbox ownership
  and authorized delivery records; a `user/` address prefix alone is insufficient.
- Message deletion/retention, attachments, reservation sync/global leases, and
  E2E encryption. Mail-scoped revocable device credentials are required in v1;
  accepting general website session tokens for ongoing sync is not deferred work.
- Dedicated relay D1 deployment (scale-out path) and device-namespaced automatic
  claims (v2 path). v1 uses shared production D1 with enforced admission limits,
  shared mailboxes, and explicitly machine-local reservations.
