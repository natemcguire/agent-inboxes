"""Shared environment isolation for tests that may consult local configuration."""

import os
from pathlib import Path
from unittest.mock import patch


def isolated_inbox(directory):
    """Keep implicit database and cloud-config lookups inside a test directory."""
    root = Path(directory)
    return patch.dict(os.environ, {
        'AGENT_INBOX_DIR': str(root),
        'AGENT_INBOX_DB': str(root / 'inbox.db'),
        'AGENT_INBOX_CLOUD_CONFIG': str(root / 'no-cloud.json'),
    })
