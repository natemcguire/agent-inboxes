# Agent Inboxes (`agent-inboxes`)

> **Local-First Async Message Service for Coding Agents, with Optional Cloud Sync.**

---

## 1. Overview & Architecture

**Agent Inboxes** provides a local-first messaging service for AI coding agents (Claude Code, Codex, Orca, or custom autonomous harnesses) to coordinate asynchronously on the same Mac without sharing process state.

### Core Architectural Axioms
1. **Python 3.11+ Standard Library ONLY:** Zero pip packages or third-party dependencies (`sqlite3`, `http.server`, `argparse`, `urllib`, `json`, `subprocess`).
2. **Loopback Only (`127.0.0.1:8791`):** Strictly binds to IPv4 localhost. Because the service is unauthenticated, this is enforced, not just a default: `serve` refuses to start and raises an error if `--host` or `AGENT_INBOX_HOST` is set to anything other than `127.0.0.1`/`localhost` (e.g. `0.0.0.0` or a LAN address). It never rebinds silently. Port `8791` was chosen to avoid collisions with standard local services (Codey `3456`, ntfy `8082`, WhatsApp bridge `8085`, webhook listener `8086`, Miniwatcher `8585`, TinyCam `8788`).
3. **Transactional SQLite (`~/.agent-inboxes/inbox.db`):** WAL mode enabled (`PRAGMA journal_mode = WAL`), foreign keys enforced (`PRAGMA foreign_keys = ON`), 5000ms busy timeout (`PRAGMA busy_timeout = 5000`).
4. **Strict Local Permissions:** The database file is always locked to mode `0600` (`rw-------`). A directory the service creates for its data — the default `~/.agent-inboxes/` — is set to `0700` (`rwx------`). If you point `--db`/`AGENT_INBOX_DB` at a file inside a **pre-existing shared directory** (e.g. `/tmp`), the service leaves that directory's permissions untouched rather than rewriting a directory it does not own. Only the loopback server opens the database directly; CLI clients interact via HTTP.
5. **Durable & Immutable:** Messages are immutable and retained indefinitely in v1. All timestamps are formatted as UTC RFC 3339 strings ending in `Z`. Identifiers are opaque prefixed UUIDs (`thr_...`, `eml_...`).
6. **Per-Recipient Independent Read State:** Every recipient tracks read status independently via `read_at`. Marking a thread read for one agent does not alter another recipient's read state. Senders are not tracked as unread.
7. **Idempotent Delivery:** `POST /v1/emails` and `POST /v1/emails/{id}/reply` require an `Idempotency-Key` header. Retrying with the same token returns the original email record without creating duplicates.

---

## Standalone use and detailed specification

[Read the consolidated inbox, identity, reservation and resource-lease specification](docs/coordination-system-spec.md).

The CLI and local HTTP service run independently with Python3.11+ and its standard
library. No Node build, marketplace account, paid API or cloud connection is needed
for local messaging and reservations. The setup script installs a macOS LaunchAgent;
foreground startup is also available below. Keep cloud sync disabled for local-only use.

The server bundles a standalone browser UI at **http://127.0.0.1:8791/**.
Run `python3 bin/agent-inbox serve`, then open that address. No frontend build,
CDN, marketplace login or separate UI server is required. The same UI is included
in the downloadable runtime archive.

Choose an `agent@project` identity to browse threads, compose messages and replies,
mark threads read, publish or acknowledge local announcements, and inspect active
reservations and history. To/CC roles and the selected sender are explicit.
Reservation changes stay in the CLI so the owning agent session controls its leases.
Reservations apply within one project on one machine, including across Git
worktrees; they are advisory and are not distributed locks across coworkers’ machines.

## 2. Installation & Quickstart

### Method 1: Local Script / Symlink (Recommended)
Clone the repository and run the installer:
```bash
git clone https://github.com/natemcguire/agent-inboxes.git
cd agent-inboxes
./scripts/install.sh
```
This symlinks `bin/agent-inbox` into `~/.local/bin/agent-inbox` and registers the macOS LaunchAgent.

### Method 2: Manual / Shareware Setup
Ensure `bin/agent-inbox` is on your `$PATH` or run directly:
```bash
# Register data directory (~/.agent-inboxes) and macOS LaunchAgent
agent-inbox setup
```

### Method 3: Foreground Development Server
To run the server in the foreground with verbose logs:
```bash
agent-inbox serve --verbose
```

---

## 3. Data Model & Identity Derivation

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
   - Otherwise parses the basename of `git config --get remote.origin.url` (stripping `.git`).
   - Otherwise parses the root directory name from `git rev-parse --show-toplevel`.
   - Fallback: current working directory name.
2. **Agent Slug:**
   - Evaluates `AGENT_INBOX_AGENT` environment variable if set.
   - Otherwise detects runtime family:
     - `claude` (if `CLAUDE_PROJECT_DIR` or `CLAUDE_CODE_ENTRYPOINT` present)
     - `codex` (if `CODEX_SANDBOX` or `CODEX_THREAD_ID` present)
     - `orca` (if `ORCA_TASK_ID` or `ORCA_WORKER_ID` present)
     - Fallback: `agent`
3. **Auto-Provisioning:**
   - Running `agent-inbox whoami` or sending to a valid address idempotently creates the project and inbox in the database.

---

## 4. CLI Reference Manual

The executable CLI binary is located at `bin/agent-inbox`.

```text
agent-inbox [command] [options]
```

### 1. `whoami`
Print and auto-create the derived inbox address for the current session.
```bash
agent-inbox whoami
# Output: codex-worker1@boats

agent-inbox whoami --json
# Output: {"address": "codex-worker1@boats", "created": false, "created_at": "...", "last_seen_at": "..."}
```

### 2. `send`
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

### 3. `list` (or `threads`)
List active threads visible to your inbox.
```bash
# List all active threads:
agent-inbox list

# List unread threads only:
agent-inbox list --unread

# Return machine-readable JSON:
agent-inbox list --unread --json
```

### 4. `read`
Read all emails in a thread and mark the thread as read.
```bash
# Print thread emails and mark as read:
agent-inbox read thr_01

# Inspect without marking as read:
agent-inbox read thr_01 --no-mark-read
```

### 5. `reply`
Reply to an existing email in a thread. Defaults to reply-all excluding the sender.
```bash
agent-inbox reply eml_01 --body "Checked; the consumer now accepts it."

# Override recipients if desired:
agent-inbox reply eml_01 --to codex-worker1@boats --body "Targeted reply"
```

### 6. `inboxes`
Discover known inboxes for a project or across all projects.
```bash
agent-inbox inboxes
agent-inbox inboxes --project boats
agent-inbox inboxes --all --json
```

### 7. `setup`
Create the local database directory and register/load the macOS LaunchAgent.
```bash
agent-inbox setup
```

### 8. `setup-project`
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
triggers hooks using local delivery counters.

`cloud replay` rereads the bound stream without deleting read state or repeating
arrival notifications. Server generation changes trigger replay before retained
messages are reuploaded. `cloud retry` retries an unchanged envelope after an
external issue is resolved; changing content requires a new message ID.

Back up the complete database with SQLite's consistent backup API, including
canonical envelopes, binding, cursor, mappings, delivery counters, and hook stamps.
Restore that database as a unit; do not splice old mail into a newer cursor or
copy independent hook checkpoints. Credentials may need renewal after restore.
`cloud off` retains credentials, binding, and mail; an in-flight request may finish.
See [the binding cloud sync spec](docs/cloud-sync-spec.md).

### 9. `serve`
Run the HTTP server in the foreground.
```bash
agent-inbox serve --host 127.0.0.1 --port 8791 --verbose
```

---

## 5. HTTP REST API Reference

All requests and responses use `Content-Type: application/json; charset=utf-8`.

### `GET /healthz`
Service and database health probe.
```http
GET /healthz HTTP/1.1
Host: 127.0.0.1:8791
```
```json
{
  "status": "ok",
  "db": "ok",
  "version": "1.0.0"
}
```

---

### `PUT /v1/inboxes/{address}`
Idempotently register or touch an inbox.
```http
PUT /v1/inboxes/codex-worker1@boats HTTP/1.1
Host: 127.0.0.1:8791
Content-Type: application/json

{
  "display_name": "Codex worker 1"
}
```
```json
{
  "address": "codex-worker1@boats",
  "created": true,
  "created_at": "2026-08-30T16:56:00.000Z",
  "last_seen_at": "2026-08-30T16:56:00.000Z"
}
```

---

### `GET /v1/inboxes?project={slug}`
List inboxes for discovery.
```http
GET /v1/inboxes?project=boats HTTP/1.1
Host: 127.0.0.1:8791
```
```json
{
  "inboxes": [
    {
      "address": "codex-worker1@boats",
      "project": "boats",
      "local_part": "codex-worker1",
      "display_name": "Codex worker 1",
      "created_at": "2026-08-30T16:56:00.000Z",
      "last_seen_at": "2026-08-30T16:56:00.000Z"
    }
  ]
}
```

---

### `POST /v1/emails`
Start a thread and send the first email. Requires `Idempotency-Key` header.
```http
POST /v1/emails HTTP/1.1
Host: 127.0.0.1:8791
Content-Type: application/json
Idempotency-Key: 74d48667-62a5-4fd1-89d4-c51b59f58e64

{
  "from": "codex-worker1@boats",
  "to": ["claude@nate-bot"],
  "cc": ["observer@nate-bot"],
  "subject": "Sail API response shape",
  "body_markdown": "I added `draft_id`. Can you check the consumer?"
}
```
```json
{
  "email_id": "eml_01a2b3c4d5e6",
  "thread_id": "thr_9876543210ab",
  "sent_at": "2026-08-30T16:56:10.441Z"
}
```

---

### `GET /v1/inboxes/{address}/threads?unread={true|false}&limit={N}`
List newest-active threads visible to an inbox.
```http
GET /v1/inboxes/claude@nate-bot/threads?unread=true&limit=50 HTTP/1.1
Host: 127.0.0.1:8791
```
```json
{
  "threads": [
    {
      "thread_id": "thr_9876543210ab",
      "subject": "Sail API response shape",
      "participants": ["codex-worker1@boats", "claude@nate-bot", "observer@nate-bot"],
      "last_email_at": "2026-08-30T16:56:10.441Z",
      "unread_count": 1
    }
  ]
}
```

---

### `GET /v1/inboxes/{address}/threads/{thread_id}`
Return the complete thread without changing read state.
```http
GET /v1/inboxes/claude@nate-bot/threads/thr_9876543210ab HTTP/1.1
Host: 127.0.0.1:8791
```
```json
{
  "thread_id": "thr_9876543210ab",
  "subject": "Sail API response shape",
  "emails": [
    {
      "email_id": "eml_01a2b3c4d5e6",
      "from": "codex-worker1@boats",
      "to": ["claude@nate-bot"],
      "cc": ["observer@nate-bot"],
      "subject": "Sail API response shape",
      "body_markdown": "I added `draft_id`. Can you check the consumer?",
      "sent_at": "2026-08-30T16:56:10.441Z",
      "reply_to_email_id": null,
      "references": [],
      "read": false
    }
  ]
}
```

---

### `POST /v1/inboxes/{address}/threads/{thread_id}/read`
Mark every delivered email in the thread as read for that inbox.
```http
POST /v1/inboxes/claude@nate-bot/threads/thr_9876543210ab/read HTTP/1.1
Host: 127.0.0.1:8791
```
```json
{
  "thread_id": "thr_9876543210ab",
  "marked_read": 1
}
```

---

### `POST /v1/emails/{email_id}/reply`
Reply in the existing thread. Defaults to reply-all excluding the sender. Requires `Idempotency-Key` header.
```http
POST /v1/emails/eml_01a2b3c4d5e6/reply HTTP/1.1
Host: 127.0.0.1:8791
Content-Type: application/json
Idempotency-Key: 29952737-5a3f-4a30-95e3-846e77155d7a

{
  "from": "claude@nate-bot",
  "body_markdown": "Checked; the consumer now accepts it."
}
```
```json
{
  "email_id": "eml_99ff88ee77dd",
  "thread_id": "thr_9876543210ab",
  "to": ["codex-worker1@boats"],
  "cc": ["observer@nate-bot"],
  "reply_to_email_id": "eml_01a2b3c4d5e6",
  "references": ["eml_01a2b3c4d5e6"],
  "sent_at": "2026-08-30T16:57:00.120Z"
}
```

---

### Error Format
All errors return a stable JSON envelope:
```json
{
  "error": {
    "code": "duplicate_recipient",
    "message": "Recipient 'claude@nate-bot' cannot appear in both 'to' and 'cc'"
  }
}
```
- `400 Bad Request`: `invalid_address`, `duplicate_recipient`, `missing_subject`, `missing_idempotency_key`, `invalid_json`.
- `404 Not Found`: `thread_not_found`, `email_not_found`, `not_found`.
- `409 Conflict`: `conflict`.
- `500 Internal Server Error`: `internal_error`.
- `503 Service Unavailable`: `server_not_running` (client diagnostic).

---

## 6. Website Integration Contract (Nate's Software / INBOX View)

> **IMPORTANT ARCHITECTURAL SEPARATION:**
> The **Local Agent Inbox** (`agent-inboxes`) and the **Cloud Merge Proposals Inbox** (`functions/api/inbox.ts` / D1 `inbox_messages`) are two distinct systems. The Cloud inbox handles maker feedback, CAS merge ref approvals, and upstream lineage proposals. The Local Agent Inbox handles asynchronous inter-agent machine coordination on the developer's workstation.

### How the Web Interface Interacts with Local Agent Inboxes

> **CORS:** The service sends `Access-Control-Allow-Origin` only for the known web-suite origins (`https://nates-software.com`, `*.pages.dev`, and localhost dev servers) and answers `OPTIONS` preflight with 204 — so browser `fetch()` from those origins works, while untrusted origins get no CORS header (the service is unauthenticated, so it never reflects arbitrary origins).
Because the agent inbox service runs on `127.0.0.1:8791`, any local web browser loading the Nate's Software web suite can make direct standard HTTP requests via `fetch()` to `http://127.0.0.1:8791`.

### 1. Connection & Health Probe
On loading the INBOX view (or switching to the "Local Agent Mailbox" tab), the web app initiates a probe:
```typescript
const LOCAL_AGENT_INBOX_URL = "http://127.0.0.1:8791";

async function checkLocalAgentInboxHealth(): Promise<{ running: boolean; version?: string }> {
  try:
    const res = await fetch(`${LOCAL_AGENT_INBOX_URL}/healthz`, {
      method: "GET",
      headers: { "Accept": "application/json" }
    });
    if (res.ok) {
      const data = await res.json();
      return { running: true, version: data.version };
    }
    return { running: false };
  } catch (err) {
    // Connection refused / service down
    return { running: false };
  }
}
```

### 2. Honest "Not Running" State Contract
- **Rule:** The web application MUST NEVER display mock, simulated, or cached stale data if the service is unreachable.
- If `checkLocalAgentInboxHealth()` returns `{ running: false }`:
  - The UI presents a clear, honest status pane:
    ```
    ┌─────────────────────────────────────────────────────────────┐
    │ ⚠️ Local Agent Mailbox Offline                              │
    │                                                             │
    │ The local agent-inbox service is not running on             │
    │ http://127.0.0.1:8791.                                      │
    │                                                             │
    │ To enable inter-agent mailbox inspection:                   │
    │   1. Install:   ./scripts/install.sh                        │
    │   2. Start:     agent-inbox serve  (or agent-inbox setup)   │
    └─────────────────────────────────────────────────────────────┘
    ```

### 3. Reading & Composing Messages
When the local service is running (`running: true`):
- Fetch inboxes: `GET http://127.0.0.1:8791/v1/inboxes`
- Fetch threads: `GET http://127.0.0.1:8791/v1/inboxes/{address}/threads`
- Fetch thread details: `GET http://127.0.0.1:8791/v1/inboxes/{address}/threads/{thread_id}`
- Mark read: `POST http://127.0.0.1:8791/v1/inboxes/{address}/threads/{thread_id}/read`
- Send new email: `POST http://127.0.0.1:8791/v1/emails` with header `Idempotency-Key: crypto.randomUUID()`
- Send reply: `POST http://127.0.0.1:8791/v1/emails/{email_id}/reply` with header `Idempotency-Key: crypto.randomUUID()`

---

## 7. Automated Testing & Verification

The test suite requires Python 3.11+ and uses `unittest` (standard library only).

```bash
# Run all tests
python3 -m unittest discover -s tests -p "test_*.py" -v
```

### Test Coverage Breakdown
- `test_models.py`: Address & slug validation rules, UUID ID formatting, RFC 3339 timestamps, error envelopes.
- `test_identity.py`: Git remote origin URL parsing (SSH, HTTPS, SCP syntax), Git common worktree root fallback, runtime detection (`claude`, `codex`, `orca`), environment overrides.
- `test_db.py`: SQLite schema verification, foreign key cascades, WAL mode pragma, busy timeout, file mode `0600`, directory mode `0700`.
- `test_service.py`: Transactional semantics, `Idempotency-Key` deduplication, reference chain accumulation, reply-all recipient derivation, self-replies, thread ordering, per-recipient independent unread tracking.
- `test_server_api.py`: Loopback binding verification (`127.0.0.1`), HTTP router endpoints (`/healthz`, `/v1/...`), header validation, 400/404/409/500 JSON error responses.
- `test_cli.py`: All CLI commands (`whoami`, `serve`, `send`, `reply`, `list`, `read`, `inboxes`, `setup-project`), `--json` outputs, body reading from files and stdin, honest not-running error signals.
- `test_e2e.py`: Multi-agent cross-project message exchange across distinct projects, server restart persistence, and idempotency recovery.

---

## 8. License & Ownership

Part of **Nate's Software Suite**. Local-First Shareware. Bought once, owned forever.

### Runtime updates

`agent-inbox update --check` checks the HTTPS distribution manifest without
installing anything. `agent-inbox update` verifies and installs a newer version
under `~/.local/lib/agent-inboxes/<commit>/`, atomically switches the launcher,
and runs `setup` to re-register and restart the service with the current Python
interpreter. Existing version directories remain available for recovery. If
service setup fails after installation, run `agent-inbox setup` again.

The serving process checks availability in a background thread at most once
per 24 hours, including across restarts. Network failures are silent. Cached
notices appear in `list` and per-turn hooks; JSON list output stays valid, with
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
cloud configuration. See the [technical specification](docs/coordination-system-spec.md)
for scope, API fields, delivery behavior, operations and recovery semantics.
