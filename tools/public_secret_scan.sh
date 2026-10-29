#!/usr/bin/env bash
#
# Secret and private-term scan for the public tree.
#
# Usage: tools/public_secret_scan.sh [root]
#
# Generated test reports are excluded by name. public-junit.xml and pytest.log
# are written by public CI from the suite, and they repeat detector fixtures
# that already live in the excluded test files. Excluding those reports does
# not exclude any other path: a secret in an ordinary file still fails.
#
# Files whose job is to detect or redact these shapes contain them as patterns
# and are excluded by path. A scanner that cannot tell a detector from a leak
# is one people switch off.

set -uo pipefail

root="${1:-.}"
cd "$root"

status=0
detectors='src/interop_lab/export/manifest\.py|src/interop_lab/viewer/redaction\.py|src/interop_lab/security/redaction\.py|src/interop_lab/artifacts/portability\.py|tests/unit/test_export\.py|tests/unit/test_security\.py|tests/unit/test_cli_adoption\.py|tests/unit/test_portability_and_runtime\.py|tests/unit/test_public_workflows\.py|tools/export_gate\.sh|\.github/workflows/public-ci\.yml'
generated='/node_modules/|/\.venv/|/dist/|/\.git/|/__pycache__/|/\.pytest_cache/|/artifacts/|/viewer/public/data/|uv\.lock|package-lock\.json|(^|/)public-junit\.xml$|(^|/)pytest\.log$'

scan() {
  local label="$1" pattern="$2" hits
  hits="$(grep -rIlE "$pattern" . 2>/dev/null \
    | grep -vE "$generated" | grep -vE "$detectors" || true)"
  if [ -n "$hits" ]; then
    echo "FAILED  $label"
    echo "$hits" | head -5
    status=1
  else
    echo "ok      $label"
  fi
}

scan "no private key" '^-----BEGIN [A-Z ]*PRIVATE KEY-----'
scan "no AWS access key id" '\bAKIA[0-9A-Z]{16}\b'
scan "no GitHub token" '\bgh[pousr]_[A-Za-z0-9]{20,}'
scan "no private finding identifier" 'ILAB-EXT-[0-9]+'
scan "no workflow run identifier" 'actions/runs/[0-9]+'
if find . -name '*.env' -not -path './node_modules/*' | grep -q .; then
  echo "FAILED  an environment file is in the tree"
  status=1
else
  echo "ok      no environment file"
fi
exit "$status"
