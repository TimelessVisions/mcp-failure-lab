#!/usr/bin/env bash
# Replay this reproduction. Portable: resolves its own directory, so the package
# can be moved anywhere and still runs.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
uv run mcp-failure-lab replay "$SCRIPT_DIR/repro.yaml"
