# Cloud client reconciliation to spec v1.1

Binding contract: [cloud-sync-spec.md](cloud-sync-spec.md), document v1.1,
wire protocol/envelope version 1. This audit covers the Python client and local
service, not the independently implemented relay server.

| Area | Previous implementation | Reconciliation |
| --- | --- | --- |
| Immutable envelope | Unversioned flat payload, hostname reconstructed each attempt | Exact complete envelope, canonical thread metadata, parent and ordered references; canonical UTF-8 payload retained once for retry, imports, and recovery |
| Canonical JSON | Ordinary JSON, no payload comparison | RFC 8785 serialization for the envelope's fixed ASCII-key/string/integer-1 subset; strict duplicate-key, type, field, Unicode, timestamp, metadata, and byte validation |
| Identity | URL/token config, no account binding | Device credential exchange and initial pull; permanent HTTPS-origin/user binding; canonical endpoint and response identity checks; credentials via hidden prompt/stdin |
| Project addresses | Inferred basename; 63-character dash-only slugs | Explicit persisted repository/slug mappings before first export; collisions fail visibly; canonical dots/underscores and 128-character components preserved; unmapped offline mail remains local |
| Push acknowledgment | Any successful request marked captured rows synced | Exact ordered per-ID coverage, identity, generation, accepted/duplicate statuses, and bounded positive sequences; canonical snapshot comparison and atomic acknowledgment; push never advances pull cursor |
| Batching | Row order, count only | Ancestor-first dependency ordering, request byte/count caps, rebatching on 413, valid neighbors proceed after named rejection |
| Outbound state | Nullable sync timestamp only | Pending/retryable/acknowledged/permanently-rejected state, reasons, attempts, deadlines, generation/sequence/time acknowledgment; blocked dependencies wait for changes; explicit unchanged-message retry |
| Failures | Push failure suppressed pull; fixed periodic retry | Independent directions and deadlines, exponential positive-jitter backoff, Retry-After, 401 suspension until login, durable health and queue status |
| Pull | Transaction per page, but blind existing-ID skip and lossy inserts | Strict page validation; atomic canonical payload, mail, parent/reference/recipient/membership rows, delivery counters, acknowledgments, and cursor; conflict/token/FK failures roll back the entire page |
| Threads | Reply metadata could define thread; timestamp projection incomplete | Immutable canonical identity, maximum author timestamp, minimum participant join time, membership union, distinct same-subject IDs |
| Import tokens | `cloud:<id>` | Exactly `cloud-import:v1:<id>` on new imports; reserved against local callers; legacy token collisions are explicit errors; existing local tokens unchanged |
| Stream reset/replay | Cursor only | Persist generation, atomically invalidate old acknowledgments, replay from zero preserving reads, then recover unmatched retained local/imported envelopes in ancestry order |
| Local notifications | Hooks compared timestamp strings; external stamp files | Persistent transactional delivery counter, per-message delivery ID, thread activity ID, atomic SQLite hook stamps; replay retains counters; delayed mail triggers hooks |
| Worker/service | Periodic worker and send/reply nudge | Retained those hooks; added per-DB worker exclusion, finite 5-second connect/30-second total timeout, cancellation between requests, local queue reporting |
| Reservations | No cloud scope disclosure | Required exact LOCAL SCOPE warning on text/structured acquisition results, including errors |
| Backup | No cloud-specific recovery guidance | Document consistent SQLite backup of all state together; test replay of a restored backup without duplicate hooks |

Already conforming foundations retained: globally random prefixed message/thread
IDs; local integer foreign keys resolved through addresses; local send/reply
transactions and ordered references; recipient positions; `(sent_at, id)` thread
message ordering; rowid watch visibility; machine-local read/session/lease state;
atomic 0600 config replacement; redirect credential protection; optional sync in
serve with send/reply nudges. The previous pull transaction boundary was retained,
but its materialization and validation semantics were replaced.

Validation is offline: strict protocol tests and mock transport failures plus the
repository's local HTTP/service/end-to-end suite. No production cloud account,
live relay deployment, or remote persistence claim is involved.
