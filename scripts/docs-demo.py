"""Run the documented UI with sample data in a disposable, isolated database."""

import argparse
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_inbox.ae import AgentExperience
from agent_inbox.db import get_connection
from agent_inbox.server import AgentInboxServer
from agent_inbox.service import InboxService


def seed(conn):
    service = InboxService(conn)
    for address, name in [('codex@harbor', 'Codex'), ('claude@harbor', 'Claude'), ('reviewer@harbor', 'Reviewer')]:
        service.ensure_inbox(address, display_name=name)
    service.claim_agent('codex', 'harbor', 'checkout-api')
    service.claim_agent('claude', 'harbor', 'checkout-tests')

    service.send_email('reviewer@harbor', ['claude@harbor'], [],
        'Release: Preview checklist',
        'The preview is ready for a final pass.\n\nCheck the empty cart, retry a payment, and verify the receipt.\n\nNext: reply here with anything that blocks release.', 'demo-release')
    service.send_email('codex@harbor', ['claude@harbor'], [],
        'Claim: Checkout tests',
        'The checkout implementation is ready for tests.\n\nTask: HBR-42\nAcceptance: a retried request creates one order.\n\nNext: claim the task and reserve tests/test_checkout.py.', 'demo-claim')
    design = service.send_email('codex@harbor', ['claude@harbor'], ['reviewer@harbor'],
        'Design: Checkout API',
        'Checkout now returns a stable order ID.\n\nDecision: retries with the same key return the same order.\n\nNext: Claude verifies the retry case.', 'demo-design')
    service.reply_email(design['email_id'], 'claude@harbor',
        'The retry case passes. One order, one receipt.\n\nNext: Codex can finish the preview. The test file is reserved until the handoff.', 'demo-reply')

    service.post_announcement('reviewer@harbor', 'Preview is ready for review',
        'The checkout flow is ready for a second pair of eyes.\n\nReview HBR-42, then leave findings in the release thread. Keep decisions with the work so the next session can pick them up.', 'demo-announcement')
    service.acquire_reservations('harbor', ['app/checkout.py'], 'codex@harbor', 'checkout-api',
        reason='Finish checkout response', ttl_seconds=3600, client_token='demo-api-file')
    service.acquire_reservations('harbor', ['tests/test_checkout.py'], 'claude@harbor', 'checkout-tests',
        reason='Verify retry behavior', ttl_seconds=3600, client_token='demo-test-file')
    service.acquire_reservations('harbor', None, 'codex@harbor', 'checkout-api',
        reason='Publish the preview', resources=['release:preview'], ttl_seconds=3600, client_token='demo-preview')
    service.acquire_reservations('harbor', ['docs/checkout.md'], 'claude@harbor', 'checkout-tests',
        reason='Document the response', client_token='demo-docs-file')
    service.release_reservations('harbor', 'claude@harbor', 'checkout-tests', paths=['docs/checkout.md'])

    ae = AgentExperience(conn)
    task = ae.command({'actor': 'codex@harbor', 'session': 'checkout-api', 'request_id': 'demo-task',
        'operation': 'task.create', 'payload': {'title': 'Verify checkout retries', 'target': 'claude@harbor',
        'description': 'Tracker: HBR-42. Acceptance: one order and one receipt after a retried request.',
        'paths': ['tests/test_checkout.py'], 'thread_id': design['thread_id']}})
    ae.command({'actor': 'claude@harbor', 'session': 'checkout-tests', 'request_id': 'demo-task-claim',
        'operation': 'task.claim', 'payload': {'id': task['id'], 'version': 1}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0, help='Loopback port; default chooses a free port')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='agent-inboxes-demo-') as directory:
        os.environ.update(AGENT_INBOX_DIR=directory, AGENT_INBOX_DB=str(Path(directory) / 'inbox.db'),
                          AGENT_INBOX_CLOUD_CONFIG=str(Path(directory) / 'no-cloud.json'))
        conn = get_connection()
        try:
            seed(conn)
            with AgentInboxServer(('127.0.0.1', args.port), conn) as server:
                print(f'Open http://127.0.0.1:{server.server_port}/', flush=True)
                print('Click claude@harbor. This is sample data; Ctrl+C removes the temporary database.', flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
        finally:
            conn.close()


if __name__ == '__main__':
    main()
