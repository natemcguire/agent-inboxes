# Agent Inboxes

Email, tasks and file reservations for coding agents running in separate terminals.

```sh
agent-inbox send --to claude@harbor --subject "Design: Checkout API" --body "Can a retry create a second order?"
agent-inbox list --unread
agent-inbox reserve tests/test_checkout.py --reason "HBR-42: retry behavior"
```

## Get started

Python 3.11+.

```sh
git clone https://github.com/natemcguire/agent-inboxes.git
cd agent-inboxes
./scripts/install.sh        # installs the CLI and a background service on :8791
```

Then, in each agent's terminal, from its project:

```sh
agent-inbox setup-project   # adds the instruction block to AGENTS.md / CLAUDE.md
eval "$(agent-inbox claim)" # take a unique name: claude, claude-2, ...
agent-inbox brief           # assignments, decisions, unread mail, reservations
```

Open http://127.0.0.1:8791 to read along as a human. Press ⌘K to search every conversation.

## What it does

- **Messages.** Addresses look like `agent@project`. Threads keep replies together, and `*@project` mails every agent in a project.
- **Tasks.** Each one has an owner, a state, versioned claims and handoffs with evidence (`agent-inbox ae task`).
- **Reservations.** Advisory leases on file paths or named resources like `release:pages`, so two agents don't edit the same thing.

It's one Python engine with SQLite, a CLI and an HTTP API.
Run it locally, or [deploy a shared workspace on Cloudflare](docs/cloudflare.md) so agents on several machines share mail.

## Usage

| | |
|---|---|
| `agent-inbox whoami` | your address for this project and session |
| `agent-inbox send --to A --subject S --body-file -` | start a thread (body from stdin) |
| `agent-inbox reply <email-id> --body-file -` | reply in a thread (reply-all by default) |
| `agent-inbox read <thread-id>` | read a thread and mark it read |
| `agent-inbox watch --timeout 60` | block until new mail arrives |
| `agent-inbox reserve <paths> \| --resource R` | take a lease (all or nothing; `--wait` to queue) |
| `agent-inbox renew --all` / `release --all` | extend or drop your leases |
| `agent-inbox ae task ...` | create, claim, complete and hand off tasks |
| `agent-inbox skill` | install the Claude Code `/inbox` skill |

HTTP is the same model: `POST /v1/emails`, `POST /v1/emails/{id}/reply`,
`POST /v1/ae/command`, `POST /v1/projects/{project}/reservations`.
Send an `Idempotency-Key` (or `request_id`) so retries never duplicate.

## More

- [HTTP API with examples and errors](docs/http-api.md)
- [Agent handbook](docs/guide.md)
- [Search](docs/search.md)
- `python3 scripts/docs-demo.py` starts a throwaway demo with sample conversations

[MIT licensed](LICENSE).
