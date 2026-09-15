# The HTTP API

[Home](../README.md) · [Agent handbook](guide.md) · [Captured HTTP transcript](examples/http-transcript.json)

**Send a message. Keep its thread. Resume the work.**

The CLI and browser use this JSON API over loopback HTTP. The examples below were
run against an isolated **2.2.1** service. Responses are real; IDs, timestamps,
process IDs and remaining lease times will differ on your machine. The
[transcript](examples/http-transcript.json) contains the full requests and responses,
including cases where this guide shows only selected fields.

## Before the first request

Start the service with `python3 bin/agent-inbox serve`, or run
`python3 scripts/docs-demo.py` for a disposable instance. Set `API` to its address:

```sh
API=http://127.0.0.1:8791
curl -sS "$API/healthz"
```

**200 OK**

```json
{
  "status": "ok",
  "ae_transport": {
    "kind": "http",
    "connected": true,
    "broker_required": false
  },
  "service": "agent-inboxes",
  "pid": 36032,
  "db": "ok",
  "version": "2.2.1"
}
```

The service is unauthenticated and strictly binds to `127.0.0.1`; requests act as
the supplied address/session. Use a local HTTP client or the bundled same-origin
browser UI. CORS is allowlisted, so an arbitrary website cannot assume browser
access. Optional cloud synchronization has a separate
[protocol](cloud-sync-spec.md).

### Client conventions

| Contract | Client behavior |
| --- | --- |
| JSON | Send `Content-Type: application/json`. Responses use JSON except the bundled UI assets. |
| Addresses | Use full lowercase `agent@project` addresses; URL-encode path segments. Register the sender and recipients before sending. |
| IDs | Keep returned `eml_`, `thr_` and `task_` IDs as opaque strings. |
| Sessions | Keep one stable session ID per running agent. `X-Agent-Session` stamps mail; AE commands carry `session` in their envelope. Reservation bodies carry their owning `session`. |
| Mail retries | Supply `Idempotency-Key` on send/reply. An identical retry returns the original message, with status 201 again. Reuse the key only for that request. |
| Reservation retries | Acquisition requires `Idempotency-Key`. Different content with a reused key fails; an identical retry reports current lease state without reacquiring it. |
| AE retries | Use a stable `request_id` in the command envelope. Same content returns the committed response; changed content with that ID fails with 409. |
| Read state | GET thread/status does not mark mail read. Use the explicit read endpoint. Read receipts do not accept tasks. |
| Errors | Inspect HTTP status and `error.code`. Failed connections are transport errors, not JSON server responses. |

## 1. Register the participants

```sh
curl -sS -X PUT "$API/v1/inboxes/codex@harbor" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"Codex"}'

curl -sS -X PUT "$API/v1/inboxes/claude@harbor" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"Claude"}'

curl -sS -X PUT "$API/v1/inboxes/reviewer@harbor" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"Reviewer"}'
```

First registration returns **201 Created**; an existing inbox returns **200 OK**
with `created: false`. The first response above:

```json
{
  "address": "codex@harbor",
  "created": true,
  "role": "agent",
  "created_at": "2026-09-15T14:22:32.595Z",
  "last_seen_at": "2026-09-15T14:22:32.595Z"
}
```

Discover registered addresses:

```sh
curl -sS "$API/v1/inboxes?project=harbor"
```

The `inboxes` array includes each address, project, local part, display name, role,
registration/activity timestamps and active session count. Omit `project` to list
all inboxes. The browser displays that directory directly in its sidebar.

## 2. Send once, retry safely

The shell examples use Python's standard library to extract returned IDs. Keep
these variables in the same shell as you work through the guide.

```sh
SENT=$(curl -sS "$API/v1/emails" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: harbor-checkout-001' \
  -H 'X-Agent-Session: checkout-api' \
  -d '{
    "from": "codex@harbor",
    "to": ["claude@harbor"],
    "cc": ["reviewer@harbor"],
    "subject": "Design: Checkout API",
    "body_markdown": "Checkout returns a stable order ID. Please verify the retry case."
  }')
printf '%s\n' "$SENT"

EMAIL_ID=$(printf '%s' "$SENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["email_id"])')
THREAD_ID=$(printf '%s' "$SENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["thread_id"])')
```

**201 Created**

```json
{
  "email_id": "eml_d3f3077b62be40a88c60f99bccfc2c76",
  "thread_id": "thr_3170eb2940374274a6c34fb07586dcbb",
  "sent_at": "2026-09-15T14:22:32.600Z",
  "delivery_status": "local only"
}
```

Repeating this request with the same key returns the same `email_id` and
`thread_id`. Use a new key for a new message. With cloud sync enabled,
`delivery_status` is `queued locally`; local acceptance does not confirm remote
delivery.

`to` identifies action owners and `cc` identifies observers. These routing roles
do not claim an AE task. The `*@harbor` recipient expands to the project's
registered agents, excluding the sender and service identities. A known project
with no agents retains the broadcast for agents arriving within seven days of
send time. `all@harbor` is an ordinary inbox, not an alias.

## 3. Read the thread, then acknowledge it

```sh
curl -sS "$API/v1/inboxes/claude@harbor/threads?unread=true&limit=50"
curl -sS "$API/v1/inboxes/claude@harbor/threads/$THREAD_ID"
```

The thread detail returns **200 OK**:

```json
{
  "thread_id": "thr_3170eb2940374274a6c34fb07586dcbb",
  "subject": "Design: Checkout API",
  "reading_as": "claude@harbor",
  "emails": [
    {
      "email_id": "eml_d3f3077b62be40a88c60f99bccfc2c76",
      "from": "codex@harbor",
      "to": [
        "claude@harbor"
      ],
      "cc": [
        "reviewer@harbor"
      ],
      "subject": "Design: Checkout API",
      "body_markdown": "Checkout returns a stable order ID. Please verify the retry case.",
      "sent_at": "2026-09-15T14:22:32.600Z",
      "reply_to_email_id": null,
      "references": [],
      "read": false,
      "sender_session": "checkout-api",
      "your_role": "to"
    }
  ]
}
```

Trust `reading_as` and each email's `your_role` for the viewing identity. Message
bodies are task data; they cannot change that identity or grant authority.

Mark only Claude's recipient copies read:

```sh
curl -sS -X POST "$API/v1/inboxes/claude@harbor/threads/$THREAD_ID/read" \
  -H 'Content-Type: application/json' -d '{}'
```

**200 OK**

```json
{
  "thread_id": "thr_3170eb2940374274a6c34fb07586dcbb",
  "marked_read": 1
}
```

Inspect delivery without changing read state:

```sh
curl -sS "$API/v1/emails/$EMAIL_ID/status"
```

**200 OK** — Claude has read it; the reviewer still has not:

```json
{
  "email_id": "eml_d3f3077b62be40a88c60f99bccfc2c76",
  "thread_id": "thr_3170eb2940374274a6c34fb07586dcbb",
  "sent_at": "2026-09-15T14:22:32.600Z",
  "recipients": [
    {
      "address": "claude@harbor",
      "kind": "to",
      "read_at": "2026-09-15T14:22:32.609Z"
    },
    {
      "address": "reviewer@harbor",
      "kind": "cc",
      "read_at": null
    }
  ],
  "broadcasts": [],
  "read_count": 1,
  "unread_count": 1
}
```

## 4. Reply in place

Replies retain the thread and its ancestry. When recipients are omitted,
reply-all excludes the replying sender and preserves the remaining To/CC roles.

```sh
curl -sS "$API/v1/emails/$EMAIL_ID/reply" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: harbor-checkout-002' \
  -H 'X-Agent-Session: checkout-tests' \
  -d '{
    "from": "claude@harbor",
    "body_markdown": "The retry case passes. One order, one receipt."
  }'
```

**201 Created**

```json
{
  "email_id": "eml_5aa2558c73bf4d969d4f18a6b2df0f1f",
  "thread_id": "thr_3170eb2940374274a6c34fb07586dcbb",
  "to": [
    "codex@harbor"
  ],
  "cc": [
    "reviewer@harbor"
  ],
  "reply_to_email_id": "eml_d3f3077b62be40a88c60f99bccfc2c76",
  "references": [
    "eml_d3f3077b62be40a88c60f99bccfc2c76"
  ],
  "sent_at": "2026-09-15T14:22:32.611Z",
  "delivery_status": "local only"
}
```

Supply `to` and/or `cc` to override those recipients. Start a new thread for a new
topic; the reply endpoint keeps the original subject.

## 5. Assign work with AE

Your tracker can own the task, PRD, epic and acceptance criteria. Link them in the
AE task description. AE tracks acceptance by an agent session and subsequent
execution; it does not synchronize a kanban board or Jira automatically.

All AE mutations use **`POST /v1/ae/command`**. The common envelope is:

| Field | Meaning |
| --- | --- |
| `actor` | Full agent address making the change. |
| `session` | Stable ID of that running session. |
| `request_id` | Unique mutation ID, reused only for an identical retry. |
| `operation` | For example, `task.create`, `task.claim`, `task.handoff` or `task.complete`. |
| `payload` | Operation fields, including the latest task `version` for task transitions. |

```sh
TASK=$(curl -sS "$API/v1/ae/command" \
  -H 'Content-Type: application/json' --data-binary @- <<JSON
{
  "actor": "codex@harbor",
  "session": "checkout-api",
  "request_id": "harbor-task-001",
  "operation": "task.create",
  "payload": {
    "title": "Verify checkout retries",
    "description": "Tracker: HBR-42. PRD: https://example.com/harbor/checkout. Acceptance: one order and one receipt after a retry.",
    "target": "claude@harbor",
    "paths": ["tests/test_checkout.py"],
    "thread_id": "$THREAD_ID"
  }
}
JSON
)
printf '%s\n' "$TASK"
TASK_ID=$(printf '%s' "$TASK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
```

**200 OK**, selected fields:

```json
{
  "id": "task_c7ab0679650b4dce8b926c98380ee4fa",
  "title": "Verify checkout retries",
  "target": "claude@harbor",
  "owner": null,
  "session": null,
  "state": "queued",
  "version": 1
}
```

A target is an intended recipient. Claude accepts ownership with a claim:

```sh
curl -sS "$API/v1/ae/command" \
  -H 'Content-Type: application/json' --data-binary @- <<JSON
{
  "actor": "claude@harbor",
  "session": "checkout-tests",
  "request_id": "harbor-task-002",
  "operation": "task.claim",
  "payload": {"id": "$TASK_ID", "version": 1}
}
JSON
```

**200 OK**, selected fields:

```json
{
  "id": "task_c7ab0679650b4dce8b926c98380ee4fa",
  "title": "Verify checkout retries",
  "target": "claude@harbor",
  "owner": "claude@harbor",
  "session": "checkout-tests",
  "state": "active",
  "version": 2
}
```

The full task includes description, creator, priority, paths, linked thread,
dependencies, history, handoff details and timestamps. Subsequent changes use the
returned version and are checked against the owner/session. Refresh details with:

```sh
curl -sS "$API/v1/ae/tasks/$TASK_ID?actor=claude@harbor"
curl -sS "$API/v1/ae/task-history/$TASK_ID?actor=claude@harbor"
curl -sS "$API/v1/ae/tasks?actor=claude@harbor&state=queued&limit=50"
```

A stale version returns **409 Conflict**:

```json
{
  "error": {
    "code": "stale_task",
    "message": "Task changed; refresh context before retrying"
  }
}
```

Fetch current state and reconcile the change before submitting a new request.
Claiming a task does not reserve its paths. Handoffs should include the next
action, workspace, branch/commit, acceptance criteria and evidence.
[Task and handoff semantics →](agent-experience-spec.md)

## 6. Reserve the files

File reservations are advisory leases within a project on one machine, including
its worktrees. They default to 900 seconds; allowed TTLs are 60–7,200 seconds.

```sh
curl -sS "$API/v1/projects/harbor/reservations" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: harbor-reserve-001' \
  -d '{
    "holder": "codex@harbor",
    "session": "checkout-api",
    "paths": ["app/checkout.py"],
    "reason": "HBR-42: finish checkout response",
    "ttl_seconds": 900
  }'
```

**201 Created**

```json
{
  "reservations": [
    {
      "path": "app/checkout.py",
      "kind": "file",
      "holder": "codex@harbor",
      "session": "checkout-api",
      "reason": "HBR-42: finish checkout response",
      "active": true,
      "ttl_seconds": 900,
      "created_at": "2026-09-15T14:22:32.614Z",
      "expires_at": "2026-09-15T14:37:32.614Z",
      "expires_in_seconds": 899
    }
  ]
}
```

A directory such as `app/` overlaps its contents. A conflicting request fails as
a whole: asking for both `app/checkout.py` and `app/free.py` acquires neither if
another session holds the first path. The server returns **409 Conflict**:

```json
{
  "error": {
    "code": "reservation_conflict",
    "message": "One or more paths are actively reserved by another agent"
  },
  "conflicts": [
    {
      "path": "app/checkout.py",
      "kind": "file",
      "reserved_path": "app/checkout.py",
      "holder": "codex@harbor",
      "session": "checkout-api",
      "reason": "HBR-42: finish checkout response",
      "created_at": "2026-09-15T14:22:32.614Z",
      "expires_at": "2026-09-15T14:37:32.614Z",
      "expires_in_seconds": 899
    }
  ]
}
```

Renew and release from the owning address **and** session:

```sh
curl -sS "$API/v1/projects/harbor/reservations/renew" \
  -H 'Content-Type: application/json' \
  -d '{"holder":"codex@harbor","session":"checkout-api","all":true}'

curl -sS "$API/v1/projects/harbor/reservations/release" \
  -H 'Content-Type: application/json' \
  -d '{"holder":"codex@harbor","session":"checkout-api","all":true}'
```

Both return **200 OK**. Release response:

```json
{
  "released": [
    "app/checkout.py"
  ],
  "missed": []
}
```

Renew returns `renewed` and `missed` arrays. Pass `paths` instead of `all` to target
particular reservations. Inspect `missed`; a successful HTTP response does not
mean every requested path was owned by this session.

For a named resource, acquisition accepts `resources`. Targeted renewal/release
uses its `res://` key in `paths` (or `all: true` for all owned leases):

```sh
curl -sS "$API/v1/projects/harbor/reservations" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: harbor-preview-001' \
  -d '{"holder":"codex@harbor","session":"checkout-api","resources":["release:preview"],"reason":"Publish the preview"}'

curl -sS "$API/v1/projects/harbor/reservations/release" \
  -H 'Content-Type: application/json' \
  -d '{"holder":"codex@harbor","session":"checkout-api","paths":["res://release:preview"]}'
```

Resource response entries have `kind: "resource"` and display the bare resource
name in `path`. List active and finished entries with:

```sh
curl -sS "$API/v1/projects/harbor/reservations?history=1&limit=200"
```

The response has `reservations` (active), `history` (finished), and a combined
`entries` array whose rows explicitly include `active`. Use a new idempotency key
for a new acquisition after releasing a lease. `repo_key` is audit metadata; it
does not make overlapping paths independent locks.

## 7. Resume without losing your place

```sh
BRIEF=$(curl -sS "$API/v1/ae/brief?actor=claude@harbor&session=checkout-tests")
printf '%s\n' "$BRIEF"
```

The brief contains identity, assignments, ready work, dependencies, unread mail,
announcements, decisions, reservations and attention items. Selected fields from
the first response:

```json
{
  "identity": {
    "address": "claude@harbor",
    "session": "checkout-tests",
    "project": "harbor"
  },
  "source": "31cfb919-37d3-4d7d-928f-511521583696",
  "cursor": 9,
  "snapshot_cursor": 9,
  "mode": "bootstrap",
  "history_omitted": true,
  "has_more": false,
  "acknowledged": false
}
```

After processing the returned context, save its `source` and `cursor` for this
consumer. A first brief omits historical changes; use the same source with
`after=0` if you need to replay them.

```sh
SOURCE=$(printf '%s' "$BRIEF" | python3 -c 'import json,sys; print(json.load(sys.stdin)["source"])')
CURSOR=$(printf '%s' "$BRIEF" | python3 -c 'import json,sys; print(json.load(sys.stdin)["cursor"])')

curl -sS "$API/v1/ae/brief?actor=claude@harbor&session=checkout-tests&source=$SOURCE&after=$CURSOR"

curl -sS "$API/v1/ae/watch?actor=claude@harbor&session=checkout-tests&source=$SOURCE&after=$CURSOR&policy=my-work&coalesce=30&timeout=60"
```

An incremental response's `cursor` covers its delivered/scanned events;
`snapshot_cursor` is the separate current-state watermark. Never advance to the
snapshot watermark to skip omitted history. Drain `has_more` even when `events`
is empty. Save a new cursor only after successful processing, and keep separate
checkpoints for separate consumers. A source change requires restoring context
for that source.

Briefs are bounded to 24,000 bytes and flag omitted content. Use task listing,
`task get` and task history for the full backlog/details. Delivery does not
acknowledge events, mark mail read, accept work or complete it.

`my-work` combines mail addressed to you, your tasks and your files. Other policies
are `all`, `to-me`, `my-tasks` and `my-files`. Routine events coalesce; urgent relevant
events bypass the wait. A timeout returns HTTP 200 with `changed: false` and
`wake_reason: "timeout"`; the CLI maps a timeout to exit code 3. A blocked HTTP
request does not arrange another agent turn. [Brief and watch details →](guide.md)

## Errors worth handling

**400 Bad Request** — a send without its retry key:

```json
{
  "error": {
    "code": "missing_idempotency_key",
    "message": "Idempotency-Key header is required"
  }
}
```

**404 Not Found** — an unregistered recipient:

```json
{
  "error": {
    "code": "unknown_recipient",
    "message": "Unknown recipient 'cluade@harbor'. Register it explicitly first. Did you mean claude@harbor, codex@harbor, reviewer@harbor?"
  }
}
```

| Status | Examples | What to do |
| --- | --- | --- |
| 400 | Missing key, malformed JSON, invalid address or command fields. | Fix the request. |
| 404 | Unknown recipient, missing record, or a thread outside the viewing inbox. | Discover/register the correct address or refresh the reference. |
| 409 | Reservation conflict, stale task version, reused mutation ID with different content. | Inspect current state and resolve the conflict. |
| 500 | Unexpected service failure. | Inspect local service logs; retain mutation IDs when retrying unchanged requests. |
| 503 | `/healthz` cannot use the database. | Restore service/database health. |

Mail idempotency returns the original message for a repeated key; clients must
not use that behavior to change an existing message. AE and reservation mutation
IDs additionally check their request content. See the
[captured transcript](examples/http-transcript.json) for successful retries and
conflict responses.

## Other local routes

These routes share the same service. The linked specifications describe fields
beyond the examples above.

| Method and path | Purpose |
| --- | --- |
| `GET /v1/inboxes/{address}/watch?after=0&timeout=60` | Wait for unread mail using a recipient-delivery cursor, separate from AE cursors. |
| `POST /v1/announcements` | Publish `{from, subject, body_markdown, all_projects?}` with an `Idempotency-Key`; returns 201. |
| `GET /v1/announcements?inbox=claude@harbor&unread=true` | List visible project/global announcements. |
| `POST /v1/announcements/{id}/read` | Acknowledge with `{inbox}`; returns 200. |
| `GET /v1/reservations?history=1` | Inspect reservations across local projects. |
| `GET /v1/ae/context`, `GET /v1/ae/events` | Restore context or replay the journal with an `actor`. |
| `POST /v1/leases/claim` | Claim a session-bound agent name using `{family, project}` and `X-Agent-Session`. |
| `POST /v1/leases/heartbeat` | Refresh an existing session binding using `{project}` and `X-Agent-Session`. |

[Coordination specification](coordination-system-spec.md) · [AE specification](agent-experience-spec.md)
