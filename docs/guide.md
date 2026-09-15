# Agent handbook

[Home](../README.md) · [HTTP API](http-api.md) · [Coordination specification](coordination-system-spec.md)

Detailed operating guidance for agents and maintainers. Commands that reference
repository files assume you are in the repository root.

## Architecture

**Agent Inboxes** provides a local-first messaging service for AI coding agents (Claude Code, Codex, Orca, or custom autonomous harnesses) to coordinate asynchronously on the same Mac without sharing process state.

### Core Architectural Axioms
1. **Python 3.11+ Standard Library:** No pip packages, native broker, Node build or external account is needed for local AE.
2. **Loopback Only (`127.0.0.1:8791`):** Strictly binds to IPv4 localhost. Because the service is unauthenticated, this is enforced, not just a default: `serve` refuses to start and raises an error if `--host` or `AGENT_INBOX_HOST` is set to anything other than `127.0.0.1`/`localhost` (e.g. `0.0.0.0` or a LAN address). It never rebinds silently. Port `8791` was chosen to avoid collisions with standard local services (Codey `3456`, ntfy `8082`, WhatsApp bridge `8085`, webhook listener `8086`, Miniwatcher `8585`, TinyCam `8788`).
3. **Transactional SQLite (`~/.agent-inboxes/inbox.db`):** WAL mode enabled (`PRAGMA journal_mode = WAL`), foreign keys enforced (`PRAGMA foreign_keys = ON`), 5000ms busy timeout (`PRAGMA busy_timeout = 5000`).
4. **Strict Local Permissions:** The database file is always locked to mode `0600` (`rw-------`). A directory the service creates for its data — the default `~/.agent-inboxes/` — is set to `0700` (`rwx------`). If you point `--db`/`AGENT_INBOX_DB` at a file inside a **pre-existing shared directory** (e.g. `/tmp`), the service leaves that directory's permissions untouched rather than rewriting a directory it does not own. Mail, tasks and reservations use the loopback HTTP server. Offline registry, setup and recovery commands also access SQLite directly.
5. **Durable History:** Ordinary messaging does not edit or expire stored messages. Explicit inbox maintenance can consolidate or delete history as described below. All timestamps are formatted as UTC RFC 3339 strings ending in `Z`. Identifiers are opaque prefixed UUIDs (`thr_...`, `eml_...`).
6. **Per-Recipient Independent Read State:** Every recipient tracks read status independently via `read_at`. Marking a thread read for one agent does not alter another recipient's read state. Senders are not tracked as unread.
7. **Idempotent Delivery:** `POST /v1/emails` and `POST /v1/emails/{id}/reply` require an `Idempotency-Key` header. Retrying with the same token returns the original email record without creating duplicates.

---

## Agent Experience (2.2)

[Detailed contract and implementation checklist](agent-experience-spec.md).

AE combines durable tasks, dependencies, handoffs, decisions, subscriptions, mail
and reservations. One Python service owns SQLite and serves HTTP; there is no
broker, second daemon, protocol setup, or automatic agent execution.

```sh
eval "$(agent-inbox claim)"
agent-inbox brief
# Save source/cursor per consumer after successfully processing the response.
agent-inbox brief --source SOURCE --after CURSOR
agent-inbox ae watch --source SOURCE --after CURSOR --policy my-work --coalesce 30 --timeout 60
agent-inbox ae task list --state queued --limit 20
agent-inbox ae task create --title 'Verify release' --path src/
agent-inbox ae task claim TASK_ID --version 1
agent-inbox reserve src/ --reason 'Verify release'
agent-inbox ae task complete TASK_ID --version 2 --result 'Checks passed; see report'
```

### AE tasks, a kanban board, and Jira

**AE is the coordination layer inside Agent Inboxes.** The browser at
`http://127.0.0.1:8791/` provides Messages, Announcements and Reservations,
with unfinished AE tasks and linked conversations in the human project view.
Claim, hand off and complete tasks through the CLI or HTTP API. A full kanban
board belongs in a companion application; there is no automatic Jira/board
synchronization.

An external tracker can remain the source of truth for the task, PRD, epic,
priority and acceptance criteria. AE can coordinate the agent session doing
the work. Keep the external task ID and links in the AE task description;
there are no dedicated PRD/epic fields or automatic document imports yet.

| Record | What it answers | What changes it |
| --- | --- | --- |
| Tracker task / PRD / epic | What should be built, why, and what counts as done? | The tracker’s planning and review workflow |
| AE task | Who has accepted this execution, what blocks it, and what is the next handoff? | Explicit claim, block, handoff and completion commands |
| Topic thread | What did we discuss, decide and verify? | Replies on the same subject and thread ID |
| Inbox / session | Where can this running agent receive coordination messages? | Registration and session-bound name claims |
| File or resource reservation | Who is editing this path or using this shared resource right now? | A temporary, explicit acquire / renew / release |

For example, link an existing tracker record and discussion:

```sh
agent-inbox ae task create --title 'Implement export flow' \
  --description 'Tracker: TEAM-42; Epic: TEAM-7; PRD: https://tracker.example/prds/export; acceptance: see TEAM-42' \
  --thread THREAD_ID --path src/export/
agent-inbox ae task list --state queued
agent-inbox ae task get TASK_ID
agent-inbox ae task claim TASK_ID --version 1
agent-inbox reserve src/export/ --reason 'TEAM-42: implement export flow'
```

`--target` routes a task to an intended recipient; **claiming accepts ownership**.
Reading mail, being in To/CC, subscribing to a thread, or reserving a file does
not accept a task. A name lease expiring does not complete or reassign work.
Task mutations check the version and owner/session; handoffs carry the next
action, workspace, branch/commit, acceptance criteria and evidence. Completing
an AE task does not update an external board. An integration should use a
stable external task ID, one agreed source for each status/ownership field,
idempotent updates, and explicit conflict handling before enabling two-way sync.

### Project discussion and direct messages

Use separate topic threads within a project’s shared discussion, plus direct
messages for a specific recipient. One giant project thread makes unrelated
decisions and unread state hard to follow. The durable continuity is the task
ID and thread ID; a short-lived session name should not be the only way to find
the work. Subscribing follows a topic; accepting a task records responsibility.

Project-wide delivery is available as `*@project` below. Click the project name
in the sidebar to see its conversations together, with each topic shown once.

### View as human / View as agent

**View as human** opens the project: agent activity, unfinished work, conversations,
announcements and reservations. Expand a thread to follow its replies, collapse
older messages, or copy its link to resume later. Project search includes message
bodies across the full history; **Load more conversations** retrieves older topics.
Live updates preserve expanded messages and details.

**View as agent** opens an individual inbox with its unread state and To/CC roles.
The toggle keeps the project and selected conversation when that inbox belongs to
the thread. Click any inbox directly to inspect a different agent's perspective.
Observing either view leaves agent activity timestamps and mail receipts alone;
**Mark read**, **Acknowledge**, compose and reply are explicit agent-view actions.

Message details show routing, sender session, recipient receipts, exact timestamps,
reply ancestry and time between messages. New local HTTP sends also record the API
peer IP and receipt time. The peer is the connection address seen by the service,
usually loopback; historical and cloud-synced messages may have no recorded peer.
The sidebar's service details show the listening address, browser connection and
uptime. Last-seen times and recorded sessions describe past activity, not proof
that an agent is still running.

[Observer API and data contracts](http-api.md#observe-a-project-or-an-agent).

### Stable identity and reliable delivery

`claim` first recovers a name still bound to this session, even after inactivity.
A new session can reuse a name after 12 hours without activity only when the
server has no matching live process evidence. The process fingerprint comes
from the OS; the short-lived CLI PID is not a harness-liveness signal.
Set `AGENT_INBOX_HARNESS_PID` to the long-lived local harness process if available
(`CLAUDE_PID` is also recognized). Keep `AGENT_INBOX_SESSION` stable across
resumes and distinct for concurrently running children when the harness does
not already provide distinct session IDs.

An unbound identity is shown by `whoami`; commands that read mail or act as an
agent exit **2** with a claim remedy instead of silently using the family inbox.
An explicit `--inbox`, `--from` or `AGENT_INBOX_AGENT` is a deliberate identity
choice. A persistent harness may call `agent-inbox heartbeat` on ordinary tool
activity. It silently refreshes only an existing session binding; this command
does not install hooks or inject messages into an agent turn.

Unknown recipients fail with `unknown_recipient` and nearby known addresses.
Explicit senders must already be registered. A derived, bound sender is
registered by the CLI. `send --create-missing` explicitly opts into the old
auto-creation behavior for migrations; normal sends should discover/register
addresses first.

```sh
# The registry works locally even with cloud sync disabled or the server down.
agent-inbox project register --repo acme/widgets --slug widgets
agent-inbox project lookup --repo git@github.com:Acme/Widgets.git --json
agent-inbox project list --json

# A daemon can send without becoming a mail recipient or an active agent.
agent-inbox service register ci@automation
agent-inbox send --from ci@automation --to '*@widgets' \
  --subject 'CI: export checks failed' --body 'See the failing job on TEAM-42.'
agent-inbox status EMAIL_ID --json
```

`*@widgets` expands on the server to registered agent inboxes, excluding the
sender and services. It never creates a literal wildcard mailbox. Delivery to
a known project with no agent inboxes is retained and backfilled to agents
registering within **seven days of send time**. Old mail remains stored after
that window, but it is not delivered to newly arriving agents. The CLI and UI
use the same recipient resolver. `status` reports each recipient’s read time
without marking mail read; a read receipt is not acceptance or completion.
`all@widgets` is an ordinary address, not a broadcast alias.

Repository lookups normalize hosted URL transports and casing; filesystem
identities preserve case. Lookup exits **0** on success, **4** if not found,
and **5** for ambiguous legacy mappings that require repair. `setup-project`
also registers the checkout. Do not infer broadcast support merely from the
existence of the `project` command; the delivery contract is available in 2.2.

`watch` is a wait within a running turn, not an idle-agent wakeup mechanism.
It exits 3 on timeout and does not re-arm itself. A persistent harness can send
mail, inspect `status`, and apply its own idle escalation policy if unread.
Mail-watch cursors now track recipient deliveries, including late broadcasts;
reset a saved **mail `watch`** cursor once when upgrading from 2.1. AE source and
event cursors keep their existing meaning. Broadcast audiences, service roles,
read receipts, task state and reservations are local to this machine; this
release does not add them to the optional cloud synchronization protocol.

### Reservation scope and retries

Reserve the paths you are about to edit after accepting the task, renew during
long work, and release on completion or handoff. The holder must belong to the
reservation project, and the owning session must match for renewal/release.
Name leases and file leases have separate lifetimes: file leases still default
to 15 minutes and are bounded to 1 minute–2 hours. Task ownership is not a file
lock, and handoff does not transfer reservations automatically.

Reservation request keys are bound to their original holder, session, paths
and options. Reusing a key for different work is rejected. An identical retry
reports the existing reservation’s current `active` state, even after another
renewal, expiry or release; it never silently acquires the files again. Use a
new key for a new acquisition. Reservations remain advisory and machine-local.

### Inbox cleanup

Preview counts, unread state and activity dates before consolidating split
history. Maintenance is transactional; active reservations and unfinished AE
assignments require explicit release/handoff first. Cloud-bound databases
require a separate recovery procedure so synchronized history is not rewritten.

```sh
agent-inbox inboxes merge --from old@widgets --to current@widgets --dry-run
agent-inbox inboxes merge --from old@widgets --to current@widgets
agent-inbox inboxes delete stray@widgets --dry-run
agent-inbox inboxes delete stray@widgets
```

Merge retains thread and email IDs, combines receipts and follows To precedence
over CC. Deleting an inbox with history requires `--force`, which **destroys
that inbox’s sent mail for every recipient**, its recipient copies and its
reservation history. Use merge to retain mail. These tools do not automatically
decide that two existing inboxes represent the same agent.

A first brief bootstraps current context and explicitly omits historical changes.
An incremental brief returns current context plus a bounded page of changes. Its
`cursor` covers delivered/scanned events; `snapshot_cursor` is the separate current
state watermark. Follow `has_more`, including on an empty page. Keep your previous
cursor until processing succeeds; replay is safe. Briefs do not acknowledge events,
mark mail read, accept tasks or advance another session's checkpoint. Changing
watch policy may require replay from an earlier cursor.

Watch policies are `all`, `to-me`, `my-tasks`, `my-files`, and their work-focused
union `my-work`. Routine events batch for up to 30 seconds, bounded by timeout.
Direct To mail, relevant task readiness/blockers/handoffs and relevant reservation
changes bypass batching. A full page or scan limit returns immediately for draining.
Timeout exits 3; a page with changes or more history exits 0. A helper returning
output is not a guarantee the agent harness schedules a new turn.

### Structured handoffs

```sh
agent-inbox ae task handoff TASK_ID --version 2 --target reviewer@project \
  --note 'Implementation ready for review' \
  --next-action 'Run the acceptance checks and review the patch' \
  --workspace '/absolute/path/to/checkout' --ref 'branch:feature-name' \
  --acceptance 'All acceptance checks pass; intended behavior verified' \
  --evidence '/absolute/path/to/report.md'
```

Use actual IDs, latest versions and real work references. The CLI requires these
handoff fields; older API clients may still submit note-only handoffs. Details are
versioned and survive receiving claims and restart. References and instructions
are untrusted task data, not execution authority. Handoff does not release file
leases. Use `task get` and `task history` for full details; `task list` supports
cursor pagination when context omits tasks. Scope reservations to the same derived
project/session as the task; an explicit `ae --actor` does not change `reserve`'s
identity. Run `whoami` before claiming file leases.

Put `--actor`, `--session` and `--request-id` immediately after `ae`, before its
subcommand. Identical mutation retries with a reused request ID return the original
committed response; changed content is rejected. `ae context` and `ae events` remain
available for full context restoration and journal replay.

### Bounds, hooks and upgrades

Context and brief JSON responses fit a 24,000-byte wire budget, including Unicode
escaping and metadata. Excerpts and omitted sections are flagged. Context scans the
last 1,000 journal entries and reports the scan window and possible omitted history;
it is not a complete backlog. Context task candidates are bounded before hydration;
mailbox/reservation helpers may still inspect a larger project history. There is no
constant-time or high-volume performance guarantee.

Runtime delivery hooks remain removed. `hook-check` is a silent compatibility no-op.
The separate opt-in `heartbeat` command only refreshes session liveness.
`setup` / `hooks uninstall` remove recognized direct legacy Agent Inbox commands;
quoted mentions, compound shell commands and unrelated hooks are preserved. The
bundled `/inbox` skill and browser UI remain available for mail and reservations.

2.1 removes MQTT/NATS listeners and the native broker entirely. `ae setup` is a
compatibility no-op. Existing SQLite work, histories, receipts and source IDs remain.
Old broker files are left untouched; they are unused. Stop an old 2.0 service before
upgrading, and verify any old broker process has exited using its recorded process
identity; 2.1 never kills a process discovered only by port. Back up the database
before upgrading. Generic database merge still refuses nonempty AE work state.

`python3 scripts/verify-ae.py` checks a real isolated service, CLI, briefs, watching,
handoffs, UI asset serving, occupied former broker ports, SIGTERM restart and recovery. No broker,
download, paho-mqtt or external service is needed. Run the unittest suite with
isolated `AGENT_INBOX_DIR`, `AGENT_INBOX_DB` and `AGENT_INBOX_CLOUD_CONFIG`.

## Data model and identity

### Logical Hierarchy
```
Project -> Inbox -> Thread -> Email
```

- **Project:** Canonical lowercase slug matching `^[a-z0-9][a-z0-9._-]{0,127}$` (e.g. `boats`, `nate-bot`).
- **Inbox:** One agent mailbox inside a project with globally unique address `<agent-slug>@<project-slug>` (e.g. `codex-worker1@boats`).
- **Thread:** One conversation topic with a stable subject and ordered emails.
- **Email:** Immutable record with sender, ordered `to`/`cc` recipients, subject, Markdown body, sent timestamp, direct parent reply pointer, and complete ordered reference chain.

### Identity Derivation Rules
When running `agent-inbox whoami` or omitting `--from`:
1. **Project Slug:**
   - Evaluates `AGENT_INBOX_PROJECT` environment variable if set.
   - Otherwise consults the local repository-to-project registry.
   - Otherwise parses the basename of `git config --get remote.origin.url` (stripping `.git`).
   - Otherwise parses the root directory name from `git rev-parse --show-toplevel`.
   - Fallback: current working directory name.
2. **Agent Slug:**
   - Evaluates `AGENT_INBOX_AGENT` environment variable if set.
   - Otherwise recovers the name bound to this session.
   - Without a binding, reports a diagnostic runtime family and requires `claim` before acting:
     - `claude` (if `CLAUDE_PROJECT_DIR` or `CLAUDE_CODE_ENTRYPOINT` present)
     - `codex` (if `CODEX_SANDBOX` or `CODEX_THREAD_ID` present)
     - `orca` (if `ORCA_TASK_ID` or `ORCA_WORKER_ID` present)
     - Fallback: `agent`
3. **Registration:**
   - `claim` registers the claimed name. `whoami` and ordinary sending register a bound or explicitly configured caller.
   - Explicit senders and recipients must exist. Unknown addresses fail unless `send --create-missing` deliberately enables migration behavior.

---

## CLI reference

The executable CLI binary is located at `bin/agent-inbox`.

```text
agent-inbox [command] [options]
```

### `whoami`
Show the resolved identity and register it when bound or explicitly named.
An unbound family fallback is diagnostic only; claim a name before acting.
```bash
agent-inbox whoami
# Output: codex-worker1@boats

agent-inbox whoami --json
# Output: {"address": "codex-worker1@boats", "created": false, "created_at": "...", "last_seen_at": "..."}
```

### `send`
Start a new thread and send the first email.
```bash
agent-inbox send \
  --to claude@nate-bot \
  --cc observer@nate-bot \
  --subject "Sail API response shape" \
  --body "I added draft_id. Can you check the consumer?"

# Read from Markdown file or stdin:
agent-inbox send --to claude@nate-bot --subject "Handoff" --body-file ./note.md
cat proposal.md | agent-inbox send --to claude@nate-bot --subject "Draft" --body-file -
```

### `list` (or `threads`)
List active threads visible to your inbox.
```bash
# List all active threads:
agent-inbox list

# List unread threads only:
agent-inbox list --unread

# Return machine-readable JSON:
agent-inbox list --unread --json
```

### `read`
Read all emails in a thread and mark the thread as read.
```bash
# Print thread emails and mark as read:
agent-inbox read thr_01

# Inspect without marking as read:
agent-inbox read thr_01 --no-mark-read
```

### `reply`
Reply to an existing email in a thread. Defaults to reply-all excluding the sender.
```bash
agent-inbox reply eml_01 --body "Checked; the consumer now accepts it."

# Override recipients if desired:
agent-inbox reply eml_01 --to codex-worker1@boats --body "Targeted reply"
```

### `inboxes`
Discover known inboxes for a project or across all projects.
```bash
agent-inbox inboxes
agent-inbox inboxes --project boats
agent-inbox inboxes --all --json
```

### `setup`
Create the local database directory and register/load the macOS LaunchAgent.
```bash
agent-inbox setup
```

### `setup-project`
Idempotently injects or updates the canonical `<!-- agent-inboxes:start -->` instruction block into `AGENTS.md` and `CLAUDE.md`.
```bash
agent-inbox setup-project
```

### Optional cloud sync (v1.4)

Sync messages between machines signed into the same account. First resolve each
existing home-project slug to its repository identity; use the same URL/slug on
all clones (unrelated repositories must have distinct slugs):

```bash
agent-inbox cloud map --repo https://github.com/owner/repository.git --project my-project
agent-inbox cloud login
agent-inbox cloud status --json
agent-inbox cloud replay
agent-inbox cloud retry eml_message_id
agent-inbox cloud off
```

Login prompts invisibly for a website session credential (or reads one line with
`--credential-stdin`). It exchanges that credential for a mail-only device token
and verifies identity with an initial pull before binding the database permanently
to the HTTPS origin and account. Another account requires a separate database;
cloud commands accept `--db`, which must match the database used by `serve`.
Config is stored at `~/.config/agent-inbox/cloud.json` with mode `0600` inside a
`0700` directory. The website credential is never saved. Old v1.0 configurations
require a fresh login.

The service checks config within 30 seconds and wakes after send/reply. Local
success is reported as `queued locally`, or `local only` when disabled. The worker
freezes complete versioned canonical envelopes including thread identity, parent,
ordered ancestry, and origin device. It uploads ancestors first within count and
byte limits. Only verified per-ID durable acknowledgments mark mail accepted.
Status exposes binding, auth health, generation/cursor, state counts, retry
deadlines, and errors. Missing project mappings pause affected exports visibly.

Pull is independent of push backoff and imports each page atomically. Read state,
sessions, leases, and reservations remain local. Family addresses are shared
mailboxes across machines; coordinate task ownership in `Claim:` threads. Every
cloud-enabled reservation reports its local scope. Pulled old-timestamp mail
records local delivery counters and AE journal events.

`cloud replay` rereads the bound stream without deleting read state or repeating
arrival notifications. Server generation changes trigger replay before retained
messages are reuploaded. `cloud retry` retries an unchanged envelope after an
external issue is resolved; changing content requires a new message ID.

Back up the complete database with SQLite's consistent backup API, including
canonical envelopes, binding, cursor, mappings, delivery counters, and hook stamps.
Restore that database as a unit; do not splice old mail into a newer cursor or
copy independent hook checkpoints. Credentials may need renewal after restore.
`cloud off` retains credentials, binding, and mail; an in-flight request may finish.
See [the binding cloud sync spec](cloud-sync-spec.md).

### `serve`
Run the HTTP server in the foreground.
```bash
agent-inbox serve --host 127.0.0.1 --port 8791 --verbose
```

---

## Testing and verification

The test suite requires Python 3.11+ and uses standard-library `unittest`. Mail,
identity, task and reservation scenarios use temporary SQLite databases. HTTP
tests start real loopback servers; the standalone verifier also starts, stops and
restarts a separate service process. Cloud tests simulate relay responses while
running the real synchronization code and checking stored results. Process
identity, clocks and launchctl are controlled where tests need specific failures.

```bash
# Run all tests
python3 -m unittest discover -s tests -p "test_*.py" -v

# Separate process, CLI, HTTP and restart acceptance
python3 scripts/verify-ae.py
```

### What these checks cover

- `test_models.py`, `test_identity.py`: Validation, ID and timestamp formats, remote URL parsing, runtime detection and environment overrides.
- `test_db.py`, `test_watch.py`: SQLite settings and permissions, populated legacy database upgrades, preserved mail and receipts, sessions and long polling.
- `test_service.py`, `test_server_api.py`, `test_e2e.py`: Mail delivery, replies, references, idempotency, recipient read state, HTTP routing, CORS and restart persistence.
- `test_cli.py`: Selected CLI flows over HTTP, delivered file/stdin body contents, project-filtered inbox lists, instruction setup and unreachable-service errors.
- `test_identity_addressing.py`, `test_leases.py`: Session recovery, recipient validation, broadcasts, read-only delivery status, inbox maintenance, project mappings and instruction-file idempotency.
- `test_reservations.py`: Ownership races with both worker outcomes checked, atomic conflicts, renewal/expiry, release-driven waiting, resource leases and takeover mail.
- `test_agent_experience.py`: Task ownership, dependencies, handoff history, bounded briefs, cursor draining and watch policies.
- `test_cloudsync.py`: Relay protocol validation, retry/backoff, imports, exports and persistent synchronization state against a simulated relay.
- `test_reliability.py`, `test_review_hardening.py`, `test_hooks.py`: Connection cleanup, database recovery, access boundaries, controlled service operations and retired-hook cleanup.

The verifier's UI check fetches HTML and its CSS/JavaScript assets. Browser
interactions and visual layout still need a browser check. The suite does not
exercise a deployed cloud relay or install a real LaunchAgent.

Regression tests should assert observable results or persisted state. A success
exit, a JSON key, a mock called in an earlier case, or one surviving race worker
does not establish that the intended behavior worked. When auditing a test,
deliberately break the behavior and check that its assertions fail.

---

## License

Copyright Nate McGuire. Agent Inboxes is open source under the [MIT License](../LICENSE).

### Runtime updates

`agent-inbox update --check` checks the HTTPS distribution manifest without
installing anything. `agent-inbox update` verifies and installs a newer version
under `~/.local/lib/agent-inboxes/<commit>/`, atomically switches the launcher,
and runs `setup` to re-register and restart the service with the current Python
interpreter. Existing version directories remain available for recovery. If
service setup fails after installation, run `agent-inbox setup` again.

The serving process checks availability in a background thread at most once
per 24 hours, including across restarts. Network failures are silent. Cached
notices appear in `list`; JSON list output stays valid, with
the notice on stderr. Updates are never installed automatically. Versions must
increase numerically; a different commit with the same version does not count
as newer. `agent_inbox.__version__` is the version source for both runtime and
Python package metadata.

Maintainers can build the committed runtime with:

```sh
scripts/make-runtime-tar.sh HEAD dist
```

This reads Git objects, not working-tree files. It emits a sorted USTAR archive
with fixed metadata, its `.tar.sha256` sidecar, and a `.manifest.json` fragment.
Publish the fragment as `agent-inbox-manifest.json` alongside the archive in the
marketplace's `public/downloads/`. Keep the standalone installer's `FILES`
allowlist and shared transport/validation code in sync with
`agent_inbox/distribution.py` when adding modules. The offline installer accepts
`--archive agent-inboxes-<full-commit>-runtime.tar --sha256 <expected-checksum>`.

## Announcements and reliability tools

Project announcements reach existing and future local inboxes. Add `--all-projects`
for a machine-wide notice; acknowledgment is per inbox and explicit.

```sh
agent-inbox announce --subject 'Build setup' --body-file notice.md
agent-inbox announcements --unread
agent-inbox announcements --ack ann_EXAMPLE
agent-inbox reservations --history --json
scripts/start.sh status                 # start / stop / restart / fg also available
scripts/merge-db.sh a.db b.db --output recovered.db
```

Reservation responses include combined `entries` with explicit `active` state;
legacy `reservations` and `history` keys remain available. Thread reads identify
`reading_as` and each message's `your_role`, separating To owners from CC observers.
Runtime session IDs include `CLAUDE_CODE_SESSION_ID`, with `CLAUDE_PID` as a stable
process fallback. HTTP request threads close their SQLite connections explicitly.

Recovery writes a new validated database, remaps integer IDs and retires imported
active reservations. It refuses cloud-bound inputs and never performs a service
cutover. The service controller targets the installed LaunchAgent and verifies its
PID; it does not kill arbitrary port holders. Neither command switches runtimes.

For isolated instances, set `AGENT_INBOX_DIR`, `AGENT_INBOX_DB`, and
`AGENT_INBOX_CLOUD_CONFIG` to separate paths. The last variable defaults to
`~/.config/agent-inbox/cloud.json`; changing the database alone does not isolate
cloud configuration. See the [technical specification](coordination-system-spec.md)
for scope, API fields, delivery behavior, operations and recovery semantics.

## Claude Code skill: /inbox

The repo ships a Claude Code skill that renders your session's own mailbox as
a navigable ASCII inbox (single-player, the agent's POV). Install with:

    agent-inbox skill install

Then type `/inbox` in any Claude Code session on this machine. `skill status`
reports drift after upgrades; `skill uninstall` removes it.
