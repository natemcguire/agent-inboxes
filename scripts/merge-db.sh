#!/bin/sh
set -eu
inbox_source=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$inbox_source${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m agent_inbox.recovery "$@"
