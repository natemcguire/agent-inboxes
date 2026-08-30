#!/usr/bin/env bash
# Agent Inboxes local installer script
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_PATH="${SCRIPT_DIR}/bin/agent-inbox"
TARGET_DIR="${HOME}/.local/bin"

echo "==> Installing Agent Inboxes..."

mkdir -p "${TARGET_DIR}"
chmod +x "${BIN_PATH}"

# Symlink to ~/.local/bin/agent-inbox
ln -sf "${BIN_PATH}" "${TARGET_DIR}/agent-inbox"
echo "==> Symlinked ${BIN_PATH} -> ${TARGET_DIR}/agent-inbox"

# Ensure PATH contains ~/.local/bin
if [[ ":$PATH:" != *":${TARGET_DIR}:"* ]]; then
    echo "Notice: ${TARGET_DIR} is not in your PATH."
    echo "Add 'export PATH=\"\$HOME/.local/bin:\$PATH\"' to your shell profile (~/.zshrc or ~/.bash_profile)."
fi

# Run setup to configure data dir and LaunchAgent plist
echo "==> Running agent-inbox setup..."
"${BIN_PATH}" setup

echo "==> Agent Inboxes installation complete!"
echo "    Run 'agent-inbox whoami' to verify your active inbox."
