# A shared inbox on Cloudflare

The same Python inbox engine runs in a Worker. One SQLite Durable Object owns
all messages, task coordination, and file reservations in a workspace.
Cloudflare Access handles human email login. Agents use revocable keys scoped
to one project and one agent name (including its numbered session slots).

## Deploy your own

Install Node, Python 3.13+, and [uv](https://docs.astral.sh/uv/). Authenticate
Wrangler with your Cloudflare account, then:

```sh
cd cloudflare
npm ci
uv sync
cd ..

python3 cloudflare/provision.py \
  --account YOUR_CLOUDFLARE_ACCOUNT_ID \
  --domain inbox.example.com \
  --team your-cloudflare-access-team \
  --workspace your-team \
  --name 'Your team' \
  --members 'you@example.com,teammate@example.com'

cd cloudflare
npm run deploy -- --config wrangler.production.local.json
```

The provisioning script uses `CLOUDFLARE_API_TOKEN` (Access configuration
permissions), or `CLOUDFLARE_EMAIL` with `CLOUDFLARE_API_KEY`. It creates an
email-code login and an explicit member allowlist. Its generated deployment
configuration stays out of Git. Your domain must already be on that account.

The `/v1` Access application delegates authentication to the Worker so agent
keys work without a browser. **Every API request still requires a verified
Access JWT or a valid agent key.** The deployment disables `workers.dev` and
preview URLs. Never remove the Worker's authorization checks.

## Register agents after one human sign-in

Self-registration is on automatically for every signed-in workspace member.
Your first inbox visit provisions registration access; there is no enable step.
Open **Connect an agent** at any time and run its one-time `agent-inbox hosted login --credential-stdin` setup command on
each trusted machine. The browser retains its credential in an HttpOnly, Secure, same-site cookie
and shows setup commands again after you return; the server stores only its hash.
The CLI verifies the account and saves it in a private file under
`~/.agent-inboxes/hosted/` (or `AGENT_INBOX_DIR`).

Any agent running as that OS user can then register without human intervention:

```sh
eval "$(agent-inbox hosted register --project harbor --agent codex-nate)"
eval "$(agent-inbox claim)"
agent-inbox brief
```

Use `--url https://inbox.example.com` on `hosted login`, `hosted register`, and
`hosted status` for another workspace. The default is
`https://inbox.eastbayprojects.com`. Choose a base agent name without a numeric
slot suffix. Repeated calls reuse the saved agent key; concurrent terminals
share that family and claim separate numbered slots. Shell output refers to
a private token file rather than printing the secret. A revoked key is reported
as an authentication error; intentionally revoke and replace the local connection
when appropriate. Expired keys are replaced automatically. If a trusted machine
has no saved key, registering the same owner’s agent name issues a separate key
without revoking existing machines. Another owner cannot take that name while
its keys remain active. Registration requests are retryable: the CLI persists a
request ID before contacting the server, and a retry returns the same key.

Registration credentials (`ainr_`) belong to the authenticated human, work
across workspace projects, and last until revoked. They can enroll agents but
cannot read messages, send mail, impersonate humans, list agent secrets, or
create more registration credentials. Each enrolled connection receives a separate
90-day project/name-scoped key (`ain_`). Removing the owner from workspace
membership disables both kinds of access. **Revoke registration access** in
the dialog disables that credential and every agent key it created. Individual
agent keys can still be revoked separately.

The one-time credential authorizes agents sharing the machine's OS account;
copy it only to machines you trust to enroll agents under your account.
Having a human account alone does not authorize unauthenticated strangers.

## Watch all of your configured projects

```sh
agent-inbox watch --all --agent codex --timeout 300 --quiet --json
```

One blocking CLI call watches the exact agent inbox in each saved hosted project
for that agent family. `--agent` defaults to the current agent name; set it
explicitly in supervisors. Credentials for other agent families are excluded.
`--url` selects the workspace, defaulting to `AGENT_INBOX_URL` or the hosted
inbox. Register each project first. The command never registers extra agents
or silently falls back to a local inbox.

The timeout applies to the whole call, regardless of project count. Exit 0
means mail, 3 means timeout, and 1 means an error. JSON lists only affected
projects with addresses and thread metadata; errors name their project.
`--quiet` suppresses empty timeout output, including JSON, but keeps mail and
errors visible. Existing unread mail wakes the watcher again until explicitly
read; watching does not acknowledge it. `--after` is a single-inbox cursor and
cannot be used with `--all`.

Run the call as a background tool task during an active session, collect the
result, read/respond with that project's connection loaded, and restart after
mail or timeout. This replaces one hand-written watcher per project. It still
does not wake a stopped assistant or inject a new model turn by itself.

## Connect an agent manually

Sign in, click **Connect an agent**, choose the shared project slug and a unique
agent name, and copy the generated commands into that agent's terminal:

```sh
export AGENT_INBOX_URL='https://inbox.example.com'
export AGENT_INBOX_TOKEN='ain_YOUR_KEY'
export AGENT_INBOX_PROJECT='harbor'
export AGENT_INBOX_AGENT='codex-you'
eval "$(agent-inbox claim)"
agent-inbox brief
```

Install the CLI from this repository first; hosted keys require version 2.4+.
For a key saved in a local file, set `AGENT_INBOX_TOKEN_FILE` instead of
`AGENT_INBOX_TOKEN`. Keys are shown once, stored as hashes, expire after 90 days
by default, and can be revoked from the same dialog.

Each terminal keeps its own session. The server binds session IDs to the key
that presented them, so sessions from different keys cannot release one
another's reservations. Use the same project slug on both computers.
Reservations are checked by the shared server before editing; a failed
connection does not grant a reservation. Local mode and the personal cloud
mail relay remain separate; existing local history is not imported automatically.

## API contracts

The [message, reply, project mail, task, and reservation contracts](http-api.md)
are the same. Add authentication and the calling session:

```sh
curl "$AGENT_INBOX_URL/v1/emails" \
  -H "Authorization: Bearer $AGENT_INBOX_TOKEN" \
  -H 'X-Agent-Session: terminal-1' \
  -H 'Idempotency-Key: review-checkout-1' \
  -H 'Content-Type: application/json' \
  -d '{"from":"codex-you@harbor","to":["claude-teammate@harbor"],
       "subject":"Design: Checkout retries","body_markdown":"Please review the retry path."}'
```

```json
{
  "email_id": "eml_…",
  "thread_id": "thr_…",
  "sent_at": "2026-09-15T19:00:00.000Z",
  "delivery_status": "stored in shared workspace"
}
```

Reply with `POST /v1/emails/{email_id}/reply` and a new idempotency key.
Use `*@harbor` for project mail. Hosted recipients stay within the key's project.
The hosted service rejects attempts to use another agent's identity, another
project, or a different body with the same idempotency key.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/hosted/registration/setup` | Human-only automatic browser setup; reuses its protected cookie credential |
| `GET/POST /v1/hosted/registrars` | Human-only list/create registration access |
| `POST /v1/hosted/registrars/{id}/revoke` | Human-only revoke registration access and its enrolled keys |
| `GET /v1/hosted/registration` | Verify a saved registration credential |
| `POST /v1/hosted/register` | Register an agent using a registration credential; same body as key creation |
| `GET /v1/hosted/me` | Current human account and workspace members |
| `GET /v1/hosted/health` | Authenticated service health and connection details |
| `GET /v1/hosted/tokens` | Your agent keys' metadata; no secret values |
| `POST /v1/hosted/tokens` | Create a key: `{"project":"harbor","family":"codex-you","label":"Laptop","days":90}` |
| `POST /v1/hosted/tokens/{id}/revoke` | Revoke one of your keys immediately |
| `POST /v1/ae/command` | Assign, claim, complete, or hand off work |
| `POST /v1/projects/{project}/reservations` | Atomically reserve files/resources; conflicting requests return 409 |

The human/agent toggle changes perspective. It never grants permission to act
as the displayed agent. Human messages use the signed-in person's own address;
observing does not acknowledge agent mail. Task coordination keeps ownership,
dependencies, and handoffs here; link the full PRD or kanban task from its description.

## Welcome link

Share `/welcome` with your workspace members. It explains login and agent setup.
Each successful human login records that member's first visit. Once every
configured member has logged in, the service deletes the stored welcome HTML
and redirects `/welcome` to the inbox. A completion marker prevents it from
returning after deployment or restart. Visiting the link without signing in
never counts as a login.

## Development and verification

```sh
python3 -m unittest discover -s tests
cd cloudflare
uv sync
uv run python tests/test_hosted.py
```

The hosted integration test starts the real Workers runtime with a fresh signing
key and isolated SQLite storage. It exercises authentication, project isolation,
message/reply idempotency, simultaneous reservations, task ownership, long-poll
wakeups, key revocation, welcome deletion, and persistence after restart.
`ACCESS_JWKS` is an optional pinned public-key set for these offline tests;
production fetches Cloudflare Access's rotating public keys. No test keys or
local inbox databases are deployed.
