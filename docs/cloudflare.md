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

## Connect an agent

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
