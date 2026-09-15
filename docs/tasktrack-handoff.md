# Tasktrack — build handoff

Build a complete local kanban app for a person working with independent coding
agents. Give it its own repository, browser UI, CLI, HTTP API and data store.
Agent Inboxes is an optional integration.

The board answers **what needs doing, why, who owns it and what counts as done**.
An agent returning after a lost session should find its task, the latest progress,
the next action and the relevant conversation without reconstructing terminal history.

## 1. Starting point and delivery boundary

- Working name and target repository: `tasktrack`. Use a separate checkout,
  preferably alongside `agent-inboxes`; do not build the tracker inside this repo.
- The user's notes describe an existing Tasktrack: Python standard library,
  SQLite, Bootstrap/vanilla JavaScript, a `tt` CLI and a service on port 7777.
  They include projects, tasks/epics, comments, attachments and an event history.
  Preserve useful existing implementation after inspecting it.
- The notes mention Tasktrack commit `874fa91`, project settings, and an inbox
  integration on an `integration` branch. Those sources were not available in
  this checkout. The photos are design evidence, not proof those features work.
- Source photos: `IMG_3592.jpeg` through `IMG_3596.jpeg`. The important requirements
  and corrections from them are captured below; the builder need not have them.
- Agent Inboxes baseline for this handoff: commit
  [`97d5909`](https://github.com/natemcguire/agent-inboxes/tree/97d59091c3b651d360c5257ac725e79bcca7c890).
  Its AE task queue already has claims, version checks and handoffs, but no kanban
  UI. Tasktrack integration is not implemented in that baseline.

**First action:** locate the existing Tasktrack checkout or remote, inspect its
instructions, schema and running processes, and record what is reusable. If no
source is available, start the separate repository from this specification. Use
temporary data while building; preserve any existing user database and attachments.

Ship Tasktrack as a standalone application with MIT licensing and an optional
inbox link. Build and verify in the separate checkout, on a branch such as
`feature/tasktrack-v1`. Keep Agent Inboxes' current public behavior intact and do
not silently migrate AE tasks or user data.

## 2. The product

### Project navigation and board

- List projects as clickable items in the sidebar. Provide project creation and
  settings for key, name and project brief. Remember the selected project in a URL
  that can be bookmarked and survives a reload.
- Show four columns: **Backlog → In progress → Review → Done**. These are stored as
  `backlog`, `in_progress`, `review`, `done`.
- Cards show task reference, title, assignee, priority, epic and a visible blocked
  indicator. Opening a card shows its full detail at a stable URL.
- Support creating/editing cards, moving between columns and ordering within a
  column. Drag-and-drop must have a keyboard/menu equivalent. A move uses the same
  server validation as CLI/API changes; collect missing review evidence in a dialog.
- Filter by assignee, epic, priority and blocked state; search task references,
  titles and descriptions. Show filtered and total counts. Paginate large columns
  explicitly; never present a limited response as the whole project.
- Support archive/restore and an archived view. Keep history and stable task links.

### Task detail and planning

A task contains its description, acceptance criteria, assignee, priority,
dependencies, progress/checkpoint, discussion links, comments, attachments and
chronological history. Editing and reading these must be comfortable in the browser.

Use `kind: task | epic` in one task model. In v1, an ordinary task may have one
parent epic in the same project; epics cannot nest. Show an epic's child tasks and
completion counts. A project's brief and an epic's description can hold the full
PRD in Markdown, with optional links to external documents. Requirements must be
readable through the API and CLI as well as the UI.

Comments are durable task notes. Linked inbox threads hold conversations. An agent
should save the resulting decision or next action on the task and link the thread;
the applications do not mirror every comment and reply into each other.

### Visual direction

Use a white background, dark readable text, restrained accent colors and generous
spacing. Body text should be at least 17px and card titles at least 18px. Use clear
labels, visible focus states, useful empty states and inline validation. No dark
mode for v1. On narrow screens, offer a usable column switcher or stacked board;
task detail, forms and navigation must remain accessible without clipped controls.

The feeling should be a small, finished developer tool. Carry the clarity of older
Rails/Heroku documentation into the README and API guide: short explanations,
copyable examples, actual screenshots and a fast path to using the product.

### Scope boundary

V1 includes the board, project/task/epic detail, assignment, claims and recovery,
comments, attachments, history, CLI/API parity, migration/backup and inbox links.
Do not add agent spawning, a general process supervisor, workflow scripting,
automatic task generation, cloud synchronization, Jira synchronization, accounts,
billing or a plugin marketplace. The tracker records and exposes work; the user's
agent harness decides when to run an agent.

## 3. Ownership, workflow and recovery

### One task authority

Tasktrack owns task content, assignment, board status, dependencies and execution
claims. Agent Inboxes owns messages, threads and file/resource reservations.

For a project connected to Tasktrack, agents mutate the task through Tasktrack.
An inbox integration may display those records and include them in a brief. Do
not create a parallel AE task with separately editable ownership and status.
Existing AE-only projects continue to work as they do now. Any later migration
needs an explicit ID mapping, backup and reconciliation of unfinished work.

### Assignment is distinct from an execution claim

- `assignee` names the human or agent responsible for the task. Assignment alone
  does not mean a runtime has started working.
- `execution` names the actor and session that have explicitly claimed the task.
  Only one execution claim can exist for a task. Claims are atomic, including
  across the CLI and HTTP server using separate SQLite connections.
- A claimant must be the assignee, or the task must be unassigned. Claiming an
  unassigned task assigns it to the claimant. A claim requires a session ID and
  is allowed from `backlog` or unclaimed `in_progress`, never `review` or `done`.
- Reassignment of claimed work requires an explicit `reassign` action and a reason.
  It clears the old execution claim and records the previous/new assignee.
- Session disappearance does not complete, abandon or reassign work. Display
  recorded timestamps; do not label a runtime alive/dead without evidence.
- A replacement session of the same actor uses `resume` with the current task
  version. A different actor uses an explicit handoff/reassignment.

### State rules

| Action | Result and requirements |
| --- | --- |
| Create | Starts in `backlog`; an assignee is optional. |
| Start / claim | Moves `backlog` to `in_progress`; requires a description, acceptance criteria, and no outstanding blockers. Claim also records the actor/session. |
| Move | Reorders or makes a permitted status transition. It never silently assigns work or acquires a claim. |
| Block / unblock | Sets or clears an explicit blocker reason. Blocking is an overlay on the four columns, not a fifth status. |
| Submit | Moves `in_progress` to `review`; requires a result summary and evidence; releases the execution claim. |
| Complete | Moves `review` to `done`; records the reviewing actor and acceptance note. Evidence must remain available on the task. |
| Request changes | Moves `review` to `in_progress`; requires a note; the assignee must claim again. |
| Return to backlog | From `in_progress` or `review`; requires a reason/checkpoint and clears the execution claim. |
| Reopen | Moves `done` to `backlog`; requires a reason and preserves earlier completion evidence. |
| Handoff | Saves a structured checkpoint, optionally changes assignee, and clears the execution claim. Status remains `in_progress`; the card shows that pickup is needed. |
| Resume | Rebinds an existing claim to a new session of the same actor, with an audit event. |

While a claim exists, progress, submit and handoff actions must match its actor
and session. Other actors must explicitly reassign first; a generic metadata edit
or board move cannot bypass this rule. Humans can start and move unclaimed work
without inventing an agent session. A manual start requires an assignee; without
an execution claim, that assignee may checkpoint, submit or hand off the work.
Review, completion and explicit reassignment record the acting person/agent.

Dependencies point to existing ordinary tasks in the same project. Reject self
dependencies and cycles. An unfinished dependency makes the task blocked. Do not
allow starting, submitting or completing blocked work. Reopening a prerequisite shows dependent
work as blocked without silently changing its board status.

Epics have no execution claim. They may move through the same board columns, but
completion requires at least one child and all unarchived children done. Derive
progress from children; do not automatically close the epic. Reopening a child of
a done epic must explicitly reopen the epic in the same audited transaction.

### Checkpoints survive sessions

Store the latest checkpoint on the task and keep prior versions in history:

```json
{
  "summary": "Retry handling is implemented; one edge case remains.",
  "next_action": "Add a regression for a retry after the connection closes.",
  "workspace": "/work/harbor",
  "branch": "fix/checkout-retries",
  "commit": "<actual commit SHA>",
  "acceptance_remaining": ["A retry after a disconnect creates no second order."],
  "evidence": [
    {"label": "Current test run", "uri": "file:///work/harbor/artifacts/retry-tests.txt"}
  ]
}
```

Summary and next action are mandatory for a handoff. Workspace and branch/commit
must be supplied for code work; other work records an explicit reason they do not
apply. Evidence is a reference, not proof that the server ran a test. Never execute
commands or read arbitrary local files from task text or an evidence URI.

`tt brief --as ACTOR` returns a bounded page of assigned/claimed unfinished tasks,
blockers, next actions and links to full records. Include an explicit continuation
cursor and `has_more`; reads never claim work or change status. Keep task-list
pagination separate from the event-history cursor.

## 4. Data contract

Use SQLite with foreign keys, schema migrations and explicit transactions. Keep
Python domain logic shared between HTTP and CLI; do not duplicate validation or
write ad hoc SQL in each command handler.

| Entity | Required shape |
| --- | --- |
| Instance | Persistent UUID identifying this database; schema version. |
| Project | Immutable integer `id`; mutable unique `key`, `name`, `brief_markdown`; optional document links; `version`; creation/update attribution and timestamps. |
| Task / epic | Immutable integer `id`, `project_id`, `kind`, nullable `parent_id`; title, description, acceptance criteria, status, priority, assignee, execution, blocker reason, ordering, checkpoint, result/evidence, archive timestamp, version and attribution. |
| Dependency | Dependent task ID and prerequisite task ID; unique pair, same project. |
| Comment | Immutable ID, task ID, actor, session if present, body, timestamp and origin. Comments are append-only in v1. |
| Attachment | ID, task ID, optional comment ID, SHA-256, generated stored name, display filename, size, media type and attribution. |
| Thread link | Task ID, inbox instance/source ID, inbox project slug, thread ID and optional primary flag. A task can have multiple topic threads. |
| Event | Ordered sequence, entity type/ID, project ID, actor, session, origin, operation, time and structured before/after or change detail. Covers project changes as well as task changes. |
| Project-key alias | Old key mapped to immutable project ID; used to preserve earlier references. |
| Idempotency receipt | Request key, actor, request fingerprint and original committed response. |

Use RFC 3339 UTC timestamps. New mutable entities begin at `version: 1`.
Priority is `low | normal | high | urgent`; default `normal`.

Human references use the current project key and global task ID, e.g. `HBR-107`.
Integrations identify a task by **instance UUID + immutable task ID**. Project keys,
display references, URLs and agent session names are not substitutes for that ID.
The default browser task URL is `/tasks/107`, independent of project renames.

### Validation and project renames

- New project keys: trim, uppercase, then validate `[A-Z][A-Z0-9]{0,9}`. Names are
  trimmed, nonempty and at most 200 characters. Inspect legacy keys before adding
  constraints to an existing database; report incompatible rows without discarding them.
- Task titles are trimmed, nonempty and at most 120 characters. Backlog drafts may
  have an incomplete description. Starting work requires a nonempty description
  and at least one explicit acceptance criterion.
- Replace the prototype's minimum prose lengths and phrase blacklist with these
  structural requirements. Padding a description or adding “so that” is not evidence
  of a usable task. Return field-specific errors that tell the author what is missing.
- Renaming a project changes its key/name, not its ID or task relationships. Keep
  old keys as aliases to the same project; do not allow another project to claim
  an alias. Renaming back to an alias already owned by the same project is allowed.
- Old project URLs resolve to the current project. Old references such as
  `HBR-107` still resolve after a key change. Verify the task belongs to the
  resolved project before returning it.
- Project rename works in UI, API and CLI, and records old/new values and actor.
  An unchanged update is a no-op with no version bump or misleading change event.
- Archive retains IDs/history; it does not satisfy dependencies or unfinished epic
  requirements. Resolve dependency/parent relationships explicitly before archiving
  unfinished work that other work relies on. Do not hard-delete records in v1.

### Concurrency and retries

Every mutation requires an explicit actor and records its origin (`ui`, `cli` or
`api`). Agents may use full inbox addresses; humans may use configured local names.
These are cooperative attribution, not authenticated user accounts.

Updating an existing project/task requires `expected_version`. Perform validation,
conditional state changes, the event append and the idempotency receipt in one
transaction. Stale versions return `409` with the current version and a way to fetch
the current record. The UI preserves unsaved input and explains the conflict.

Every HTTP mutation requires `Idempotency-Key`. An identical retry by the same actor
returns the original response without another change/event. Reusing a key for a
different method, path or body returns `409`. Check an existing receipt before
rejecting its original expected version. The CLI uses the same receipt mechanism
and supports an explicit request key for retries across invocations.

Appending an independent comment/attachment requires an idempotency key but no
task version, because it does not replace task state. It adds an event without
incrementing the task's state version. Metadata/state/claim changes increment that
version once per committed operation. Failed operations leave neither partial
records nor successful-looking events.

Use separate SQLite connections per concurrent request and short transactions.
Choose WAL and bounded busy handling; return a retryable error when contention
exceeds the bound. A read-only SQLite connection still participates in concurrency;
it does not make direct cross-application database access an independent interface.
See [SQLite isolation](https://www.sqlite.org/isolation.html).

## 5. HTTP and CLI surface

Serve the UI and versioned JSON API from `http://127.0.0.1:7777` by default.
Bind to loopback, reject foreign browser origins and do not enable wildcard CORS.
Validate content types and render user Markdown safely. Keep database, uploads,
configuration and backups outside the source checkout in a configurable data directory.

| Route | Behavior |
| --- | --- |
| `GET /api/v1/health` | Version, schema version and persistent `instance_id`. |
| `GET, POST /api/v1/projects` | List/create projects. |
| `GET, PATCH /api/v1/projects/{id}` | Full project and version-checked edits, including rename. |
| `GET /api/v1/projects/by-key/{key}` | Resolve current keys and aliases. |
| `GET, POST /api/v1/tasks` | Filtered/paginated task list and task/epic creation. |
| `GET, PATCH /api/v1/tasks/{id}` | Full task and ordinary metadata edits; protected workflow fields cannot be patched around action validation. |
| `POST /api/v1/tasks/{id}/{action}` | `move`, `claim`, `resume`, `checkpoint`, `handoff`, `reassign`, `block`, `unblock`, `submit`, `complete`, `request-changes`, `reopen`, `archive`, `restore`. |
| `GET, POST /api/v1/tasks/{id}/comments` | Paginated comments / append comment. |
| `GET, POST /api/v1/tasks/{id}/attachments` | Attachment metadata / upload; download by attachment ID. |
| `GET /api/v1/tasks/{id}/history` | Paginated task events with original actor/session and transition detail. |
| `GET /api/v1/events` | Ordered project/task events, including project renames. |
| `GET /api/v1/brief` | Bounded current work context for an actor. |

Use `X-Actor` for the actor, `X-Session` for session-bound operations and `X-Via`
for origin (default `api`; UI/CLI set their own origin). These headers describe
the caller, not a permission system. List
responses contain `items`, `next_cursor` and `has_more`; default limit 50, maximum
200. Document allowed filters (`project_id`, `status`, `assignee`, `parent_id`,
`priority`, `blocked`, `archived`, `q`) and stable ordering. Unknown filters/fields
return actionable errors rather than appearing to work.

Event reads use the instance UUID plus an `after` sequence and a limit. Wrong-source
cursors fail explicitly. Expose `has_more` even when filtering produces an empty
page; advance only through examined history. Reading events has no write side effect.

Status codes: `201` for creation, `200` for successful reads/actions, `400` for
malformed input or missing actor, `404` for unknown objects, `409` for version,
claim, key or idempotency conflicts, `422` for workflow/field validation, `413` for
oversized uploads, and `503` for bounded storage unavailability.

All action bodies include `expected_version`. Additional fields are:

| Action | Payload beyond the version |
| --- | --- |
| `move` | `status`, optional `before_id`, plus the same reason/result/acceptance fields required by the equivalent transition. |
| `claim`, `resume` | Session supplied in `X-Session`; no additional body fields. |
| `checkpoint` | `checkpoint` object with the shape above. |
| `handoff` | `checkpoint`, optional `to` assignee; omitted `to` retains the assignee, explicit `null` makes it available for pickup. |
| `reassign` | `assignee` (actor or null), `reason`. |
| `block`, `unblock` | `reason` explaining the blocker or its resolution. |
| `submit` | `result: {summary, evidence}`; evidence is a nonempty list of `{label, uri}`. |
| `complete` | `acceptance_note` describing how the criteria were satisfied. |
| `request-changes`, `reopen` | `reason`; reopening a child of a done epic also requires explicit `reopen_parent: true`. |
| `archive`, `restore` | `reason`; archiving claimed work requires handoff/reassignment first. |

A move back to backlog requires `reason` and the latest `checkpoint`. All routes
delegate to the same transition functions, so dragging a card cannot skip a
requirement enforced by its named action. When reopening a completed epic is
required, reject the request until the caller explicitly includes it.

Metadata edits accept `dependency_ids` and `thread_links` as complete replacement
lists under the task version check. A thread link has `{source, project, thread_id,
primary}`; `source` is the inbox database UUID. Allow at most one primary link.
Upload uses multipart form fields `file` and optional `comment_id`; download is
`GET /api/v1/attachments/{id}/content`. Fingerprint attachment retries from their
metadata and file hash rather than a multipart boundary.

```json
{
  "error": {
    "code": "version_conflict",
    "message": "Task 107 changed. Fetch the current task before retrying.",
    "current_version": 4,
    "fields": {}
  }
}
```

### Example: create a task and claim it

These are **target contracts**, not commands verified against an existing build.
The finished project's documentation must replace illustrative responses with
responses captured from a disposable running instance.

```sh
curl -sS http://127.0.0.1:7777/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -H 'X-Actor: nate' -H 'X-Via: api' \
  -H 'Idempotency-Key: task-create-001' \
  -d '{
    "project_id": 12,
    "kind": "task",
    "parent_id": 99,
    "title": "Make checkout retries safe",
    "description_markdown": "A disconnected client retries checkout. Preserve the original order and receipt.",
    "acceptance_criteria": [
      "Repeating a checkout request creates one order.",
      "A retry returns the original receipt."
    ],
    "assignee": "claude@harbor",
    "priority": "high",
    "dependency_ids": []
  }'
```

Projects/tasks 12 and 99 are illustrative; use actual IDs returned by project and
epic creation. The task response contains the submitted fields plus:

```json
{
  "instance_id": "cb5438fb-4af8-4f62-bd40-5f775b2c06dd",
  "id": 107,
  "project_id": 12,
  "project_key": "HBR",
  "reference": "HBR-107",
  "status": "backlog",
  "assignee": "claude@harbor",
  "execution": null,
  "version": 1,
  "url": "http://127.0.0.1:7777/tasks/107",
  "created_by": "nate",
  "created_via": "api",
  "created_at": "2026-09-15T15:00:00Z"
}
```

```sh
curl -sS http://127.0.0.1:7777/api/v1/tasks/107/claim \
  -H 'Content-Type: application/json' \
  -H 'X-Actor: claude@harbor' -H 'X-Session: checkout-1' \
  -H 'X-Via: api' -H 'Idempotency-Key: task-claim-001' \
  -d '{"expected_version":1}'
```

Successful claim returns the full task at version 2, status `in_progress`, with
`execution: {actor, session, claimed_at}`. A competing claim returns `409` and
cannot overwrite it. An identical retry returns the first claim's response.

### CLI parity

`tt` must expose every user-facing write, including project rename. It may operate
directly on the local database while the HTTP server is stopped, provided it uses
the same domain service, migrations, transactions, attribution and retry rules.
Print usable human output by default and stable JSON with `--json`.

```sh
tt serve --host 127.0.0.1 --port 7777
tt project create HBR "Harbor" --as nate
tt project update 12 --key HARBOR --name "Harbor" --version 1 --as nate
tt task create HARBOR --file task.json --as nate --request-id task-create-001
tt task list --project HARBOR --assignee claude@harbor --json
tt task get 107 --json
tt task claim 107 --version 1 --as claude@harbor --session checkout-1
tt task checkpoint 107 --file checkpoint.json --version 2 --as claude@harbor --session checkout-1
tt task handoff 107 --to codex@harbor --file checkpoint.json --version 3 --as claude@harbor --session checkout-1
tt brief --project HARBOR --as codex@harbor --json
tt task history 107 --json
```

These are independent usage examples; IDs, versions and input files must come
from current records. Accept explicit `--as` and a configured `TT_ACTOR`; refuse
unattributed mutations. Accept `--session` / `TT_SESSION` for runtime identity.
Provide documented nonzero exit codes for validation, conflict and unavailability.

For board ordering, the move contract accepts `expected_version`, `status` and an
optional `before_id` anchor. Compute ordering in a transaction; clients do not write
raw ranks. Reject an anchor in a different project/column. Preserve deterministic
ordering after restart and concurrent moves. Dependency and thread-link edits use
version-checked task metadata operations and their associated validation.

## 6. Agent Inboxes integration

The tracker is fully usable with Agent Inboxes stopped or absent. The initial
integration is stored task/thread references and an **Open conversation** link;
implementing a new inbox adapter is a separately coordinated change in the inbox
repository. Do not claim that the current inbox brief already reads Tasktrack.

Store the task reference used by an adapter as:

```json
{
  "provider": "tasktrack",
  "instance_id": "cb5438fb-4af8-4f62-bd40-5f775b2c06dd",
  "task_id": 107,
  "project_id": 12
}
```

An inbox-side adapter may fetch task titles, assignments and summaries over the
versioned Tasktrack HTTP API. Keep a bounded timeout (at most one second for
autocomplete), limited results, cancellation of superseded searches and a small
cache. A missing/stopped tracker makes enrichment unavailable; sending, reading
and replying to mail must continue. Never open or migrate the other app's SQLite
database, and never fabricate a successful task lookup from stale cached data.

The notes' `@#107` syntax is a task citation, never an email recipient. Resolve it
only when a configured tracker instance/project makes it unambiguous. Prefer a
project-qualified display reference or stable task link. Unknown/ambiguous references
stay visible as unresolved text; they must not become `107@tasktrack` mailboxes.

Thread links store inbox source, project and thread IDs. Resolve browser URLs using
the configured inbox base URL and its documented navigation contract; do not guess
a thread URL. A local port change should not require rewriting stored identities.
Keep one topic per thread; multiple threads can link to the same task or epic.

File reservations stay in Agent Inboxes. A tracker claim never reserves files or
releases another session's leases. An agent checks `agent-inbox whoami`, reserves
the actual paths, then works. The task can cite reservation IDs/status when an
adapter exists, but the tracker must not maintain a second reservation system.

## 7. Persistence, attachments and migration

Prefer the existing Python 3.11+ / SQLite / vanilla JavaScript implementation.
Reuse Bootstrap if present, vendoring the assets needed to work offline. Avoid a
mandatory frontend build or external runtime service. Test-only browser tooling is
fine. Split domain logic, HTTP, CLI and UI into understandable modules.

Uploads are bounded (10 MiB per file by default) and content-addressed. The supplied
filename is display metadata, never a path. Download by attachment ID; check that
a supplied comment ID belongs to the task. Store blobs with atomic filesystem
writes, then commit their metadata; handle interrupted uploads without broken
references. Preserve a blob while any attachment still references it. Do not render
uploaded HTML as executable application content.

Provide `tt backup` and a documented restore procedure covering the database and
every referenced blob. Use SQLite's backup facilities for a live database snapshot,
then collect the immutable blobs referenced by that snapshot. Coordinate cleanup so
it cannot delete those blobs during backup. See the
[SQLite backup API](https://www.sqlite.org/backup.html) and
[Python SQLite backup interface](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).

If the prototype database exists, rehearse migration on a copy. Preserve project/task
IDs, parent relationships, comments, actors, timestamps and attachment hashes. Map
legacy `thread_id` values into thread links; obtain missing inbox source/project
identity from explicit configuration. Keep unresolved legacy links marked as such.
Do not invent historical actors or rename events; label gaps from the legacy data.

Migrations must be versioned, repeat-safe and fail without partially upgraded data.
An older binary must refuse an unsupported newer schema. Document changes to the
prototype's unversioned `/api` routes and CLI commands, retaining straightforward
compatibility aliases where possible. Do not promise compatibility without exercising
the actual legacy entry points.

## 8. Build sequence and evidence

Deliver working vertical slices in the new repository:

1. **Inspect and preserve.** Record the starting commit/schema, reuse decisions,
   source availability and backup location. Add the MIT license and a short README.
2. **Usable board.** Shared domain model, migrations, project settings, task/epic
   detail, board movement/order, CLI and HTTP. Demonstrate browser-created work
   appearing in CLI/API and surviving restart.
3. **Reliable agent work.** Claims, version conflicts, dependencies, checkpoints,
   handoffs, review/evidence, comments, attachments and full audit history.
4. **Finish and verify.** Stable rename links, recovery/backup, optional conversation
   links, polished empty/error/mobile states, real screenshots and executable API
   examples. Report the tracker commit, how to run it and any remaining limitations.

### Acceptance scenarios

| Scenario | Required observable result |
| --- | --- |
| Independent use | With Agent Inboxes stopped, create a project, epic and tasks; use the board, CLI and API successfully. |
| Full board | Create/edit/filter/reorder/move cards through all four columns. Reload and restart preserve content, order and status. Exercise keyboard/menu movement too. |
| Requirements | A task is traceable to its epic and project brief; API/CLI return the actual descriptions and acceptance criteria. |
| Quality gate | A short but complete task can start. A long description without acceptance criteria cannot. A draft can still be saved in backlog. |
| Rename | UI, CLI and API rename the same project; IDs, children and attachments survive; old URLs/references resolve; a project event records the actor and change. |
| Claim race | Two real processes claim the same version, including one CLI writer and one HTTP writer. Exactly one wins; one claim/event is persisted. |
| Stale edit / retry | A stale board edit cannot overwrite a newer CLI edit. Replaying an identical command creates no duplicate record/event; changed content with the same key fails. |
| Lost session | Actor A checkpoints and exits. A replacement session reads the task/brief, sees next action and evidence, explicitly resumes, and continues. Actor B cannot silently steal the claim. |
| Handoff | A hands off to B. A's execution claim is cleared, the new assignee and checkpoint persist, and B can claim using the current version. |
| Dependencies / epic | Cycles and cross-project parents fail. Blocked starts/submissions fail. An epic cannot finish with unfinished children. Reopening a child explicitly reopens a completed epic. |
| History integrity | Changes from UI/CLI/API record the real supplied actor/origin. Failed writes and no-ops add no successful change events. Project renames are included. |
| Attachments | Upload/download preserves exact bytes; comment/task mismatch and traversal-style filenames cannot escape storage; interrupted upload does not leave a broken attachment. |
| Backup / migration | Restore into an isolated directory and compare IDs, row relationships, histories and blob hashes. Test the supported legacy schema and an unsupported newer schema. |
| Integration boundary | Task/thread links survive project rename; absent inbox leaves the board usable. A future inbox adapter must also prove that absent Tasktrack cannot break mail. |
| Browser quality | Exercise real clicks, forms, movement, conflict handling, project rename and reload on desktop and a narrow viewport. Capture board, task detail and history screenshots. |

### Tests must prove behavior

Use real temporary SQLite databases and a disposable HTTP process for persistence,
transaction and API boundaries. Use actual CLI subprocesses for parsing/exit codes
and races. Share fixtures and combine cases when they cover the same failure mode.
Never use the owner's real database, inbox or installed service as a test fixture.

Do not count HTTP 200 alone, a DOM ID existing, HTML containing a heading, or a
mock returning its configured value as evidence that a workflow works. Assertions
must check persisted state, the failed competing operation, exact relationships or
visible behavior. Demonstrate that disabling the claim guard, skipping the rename
update and duplicating an idempotent write each makes the relevant regression fail.

Browser coverage requires a real browser. If the execution environment cannot run
one, label those scenarios unverified and provide a runnable reproduction; do not
claim static markup checks covered the interaction or call the release verified.

## 9. Final handoff from the builder

Provide the new repository location and commit, implemented scope, migration/backup
results, test commands and outcomes, actual UI screenshots, and concise startup
instructions. Include a short API guide with real request/response transcripts for
project creation/rename, task creation/claim, conflict/retry, checkpoint/handoff,
review/completion and attachment upload/download.

Keep the public README brief: the product pitch, screenshot, install/start, one
agent workflow, API/docs links and MIT license. Put the complete reference in docs.
Record outstanding work with concrete next action, workspace, branch/commit,
acceptance criteria and evidence so another agent can continue without this chat.
