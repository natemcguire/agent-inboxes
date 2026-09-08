# Independent Agent Experience Test

Independently test Agent Inboxes’ new Agent Experience (AE).

Repository: https://github.com/natemcguire/agent-inboxes
Baseline: master containing AE 2.1; record the exact tested commit.

## Background

Agent Inboxes is a standalone local coordination service for coding agents. It already provides durable mail, announcements, session identity, advisory file reservations, and a browser UI.

AE brings these together with durable tasks, dependencies, ownership, handoffs, decisions, subscriptions, context restoration, and a unified event journal. AE 2.1 removes the broker. One Python/SQLite/HTTP service provides briefs, event replay and policy watches.

The goal is a first-class experience for agents: enough relevant context to understand what needs doing, why, who owns it, what is blocked, and what happened before. Evaluate that experience, not just whether endpoints return 200.

Runtime delivery hooks were removed. Agents explicitly retrieve context and receive events. The separately bundled Claude /inbox skill remains available as a mailbox browser.

## Read first

- README.md
- docs/agent-experience-spec.md
- docs/coordination-system-spec.md
- tests/test_agent_experience.py
- scripts/verify-ae.py

## Testing environment

Use a fresh checkout, temporary database/data directory, and unused ports. Set AGENT_INBOX_DIR, AGENT_INBOX_DB and AGENT_INBOX_CLOUD_CONFIG before running commands. Disable unrelated automatic update activity in the test harness.

Do not use real inbox contents, change personal harness configuration, restart existing services, or run the general installer. No broker or pip dependency is required. Verify startup while the former broker ports are occupied.

## Test these scenarios independently

1. Run the full suite and scripts/verify-ae.py. Record passes, skips and exact failures.
2. Create two agent addresses and distinct runtime sessions. Have both race to claim one task. Exactly one should succeed.
3. Create dependent work. It must remain unclaimable until prerequisites complete, then become eligible and produce a relevant event.
4. Exercise claim, block, resume, complete and handoff. Reject stale versions and wrong-session mutations. Preserve handoff notes and transition history.
5. Restart the isolated service and restore context. Confirm assignments, blockers, decisions, subscriptions and history survive.
6. Verify context connects intended task paths to missing/conflicting file reservations. A task claim must not silently grant or release file leases.
7. Check mail To/CC roles, thread access, project filtering and subscriptions. Event acknowledgment must not claim work or mark mail read.
8. Test bootstrap and incremental briefs, then policy watches with routine batching, urgent bypass, hidden pages and independent consumer cursors. Verify no omitted matching events are skipped or acknowledged.
9. Retry identical commands with the same request ID; reject changed content using that ID. Interrupt the real service with SIGTERM, restart and recover through event replay without duplicating task transitions.
10. Test source-scoped cursors, empty filtered pages, pagination, bounded context and access to full history beyond excerpts.
11. Confirm hook-check is silent and hooks install is unavailable. Inspect cleanup behavior using temporary fixture configurations only.
12. Verify the existing browser UI, /inbox skill command and runtime archive contents remain intact.

Finally, perform a realistic small handoff between two agent identities. Can the receiving agent determine the next action from service context without relying on this chat? Identify missing context, excessive noise or confusing commands.

Distinguish real protocol/process tests from mocked tests. A watch returning is not evidence the harness scheduled a turn or an agent performed work. Do not claim cross-machine locking or per-agent authentication.

## Report

- Commit and environment tested
- Pass/fail/skip results with evidence
- Bugs with minimal reproductions and severity
- Agent-experience gaps
- Whether it is ready for daily use, and why

Do not modify implementation or push changes during this review. Stop only the temporary processes you started.

## Deliver findings through Agent Inbox

Send your findings back through Agent Inbox, not just in chat.

- Recipient: `codex@bin-project`
- Intended receiving session: `s-e223f01c`

Use the normal local inbox service for this report, outside your isolated test environment. Run `agent-inbox whoami` first and send from your own identity. This reporting step is authorized to write to the normal inbox; all test traffic must remain in the temporary database.

Write your full report to a Markdown file, then run:

```sh
agent-inbox send \
  --to codex@bin-project \
  --subject "Design: AE independent test findings" \
  --body-file /absolute/path/to/report.md
```

Include the intended receiving session in the report, along with the tested commit, results, reproductions, and readiness assessment. Keep the returned thread ID and use `agent-inbox reply` for subsequent updates on this review. Session metadata identifies the intended reader; the mailbox address is the actual delivery destination.

If blocked, send the blocker through the same inbox. If the normal inbox service is unreachable, report that in chat rather than sending into the temporary test database. Sending mail does not automatically wake the receiving agent.
