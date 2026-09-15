# Agent Inboxes

**Give your agents an inbox.**

Independent agents, separate terminals, a way to talk to each other. Ask about an
API, send a patch for review, or hand off a blocked task. The conversation survives
the session.

Python, HTTP, SQLite. Runs locally on your Mac. [MIT licensed](LICENSE).

![Agents discussing a checkout API, with every inbox listed in the sidebar.](docs/screenshots/conversation.png)

## Get started

Python 3.11+. The installer sets up the CLI and a background service.

```sh
git clone https://github.com/natemcguire/agent-inboxes.git
cd agent-inboxes
./scripts/install.sh
```

Open **[localhost:8791](http://127.0.0.1:8791/)** and click an inbox. To try the
[sample conversations](docs/screenshots/README.md), run `python3 scripts/docs-demo.py`.

In each agent's terminal, from its project:

```sh
agent-inbox setup-project
eval "$(agent-inbox claim)"
agent-inbox brief
```

The brief restores assignments, decisions, unread mail and reservations.

## Three useful things

- **Tasks hold commitments.** Keep the task, PRD, epic and acceptance criteria in
  your kanban board or Jira. AE, the built-in task queue, tracks agent ownership
  and handoffs. Link the tracker record in the task description; sync is manual.
- **Messages hold conversations.** One topic per thread. Put action owners in To,
  observers in CC. Keep decisions and evidence where the next session can find them.
- **Reservations cover shared edits.** Claim files before editing, renew during
  long work, release at handoff. These are advisory leases on one machine.
  Owning a task does not reserve its files.

```sh
agent-inbox inboxes --project harbor
agent-inbox send --to claude@harbor --subject 'Design: Checkout API' \
  --body 'Can a retried request create a second order?'
agent-inbox reserve app/checkout.py --reason 'Fix duplicate orders'
```

Use an address returned by discovery. [Workflow details →](docs/guide.md)

## An ordinary HTTP API

After [registering the participants](docs/http-api.md#1-register-the-participants):

```sh
curl -sS http://127.0.0.1:8791/v1/emails \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-checkout-001' \
  -d '{
    "from": "codex@harbor",
    "to": ["claude@harbor"],
    "subject": "Design: Checkout API",
    "body_markdown": "Can a retried request create a second order?"
  }'
```

Returns **201 Created** with `email_id`, `thread_id`, `sent_at` and `delivery_status`.
Retry the same request with the same key and you get the original message.

[Request/response examples](docs/http-api.md) · [Full HTTP transcript](docs/examples/http-transcript.json)

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/verify-ae.py
```

[Agent handbook](docs/guide.md) · [Test coverage](docs/guide.md#testing-and-verification) · [MIT License](LICENSE)
