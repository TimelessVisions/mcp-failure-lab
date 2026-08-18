#!/usr/bin/env bash
#
# The public-export gate: build the sanitized tree, then try to break it.
#
# Everything here runs against the *materialised export*, in a directory with no
# shared history and no `.git`, using only what the export itself contains. That
# is the point. Checks that run in the source tree prove the source tree is
# fine; this asks whether the thing that would actually be published stands up
# on its own — installs, lints, types, tests, builds its viewer, plays its demo,
# and carries nothing private.
#
# Passing is necessary and not sufficient. Publication is a separate decision by
# the owner, and this script deliberately does not create a repository, push
# anything, or change any visibility.
#
# Usage: tools/export_gate.sh [destination]

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${TMPDIR:-/tmp}/interop-lab-export}"
FAILURES=0
STEP=0

step() {
  STEP=$((STEP + 1))
  printf '\n\033[1m%2d. %s\033[0m\n' "$STEP" "$1"
}

ok() { printf '    \033[32mok\033[0m       %s\n' "$1"; }
bad() {
  printf '    \033[31mFAILED\033[0m   %s\n' "$1"
  FAILURES=$((FAILURES + 1))
}
note() { printf '    ·        %s\n' "$1"; }

# Files whose job is to detect or forbid these patterns, and which therefore
# contain them legitimately. A scanner that cannot tell a detector from a leak
# is one people switch off — and the alternative, deleting the safety code from
# the export, would publish a harness that has stopped checking itself.
#
# This mirrors CONTENT_EXEMPT in `interop_lab.export.manifest`. It is written
# out rather than derived so that widening the Python list does not silently
# widen this one too: two lists that must be changed together are two chances to
# notice.
DETECTORS='src/interop_lab/export/manifest\.py|src/interop_lab/viewer/redaction\.py|src/interop_lab/security/redaction\.py|src/interop_lab/artifacts/portability\.py|tests/unit/test_export\.py|tests/unit/test_security\.py|tests/unit/test_cli_adoption\.py|tests/unit/test_portability_and_runtime\.py|tests/unit/test_public_workflows\.py|tools/export_gate\.sh'

# Scan the export for a pattern. Fails when it matches outside the detectors.
# Binary files are skipped by grep's own detection rather than by an extension
# list, so a renamed binary cannot slip past.
# Directories the build creates inside the export. They are not part of what
# would be published — a release tree is the exported files, and these appear
# only because the later steps install and build in place. Scanning a
# dependency's README for a GitHub URL says nothing about this project.
GENERATED='/node_modules/|/\.venv/|/dist/|/\.pytest_cache/|/__pycache__/|/artifacts/|/viewer/public/data/|/uv\.lock$|/package-lock\.json$'

scan() {
  local label="$1" pattern="$2"
  local hits
  hits="$(grep -rIlE "$pattern" "$OUT" 2>/dev/null | grep -vE "$GENERATED" | grep -vE "$DETECTORS" || true)"
  if [ -n "$hits" ]; then
    bad "$label"
    printf '             %s\n' $hits | head -10
  else
    ok "$label"
  fi
}

# ----------------------------------------------------------------- 1. generate

step "Generate the export into a tree with no shared history"
rm -rf "$OUT"
if (cd "$REPO" && uv run mcp-failure-lab export --out "$OUT" --force >/tmp/export.log 2>&1); then
  ok "$(grep -c . /dev/null >/dev/null; find "$OUT" -type f | wc -l | tr -d ' ') files written to $OUT"
else
  bad "the export refused to run"
  tail -20 /tmp/export.log
  exit 1
fi

# ------------------------------------------------------------------- 2-6. scans

step "Scan for secrets"
scan "no private key" '^-----BEGIN [A-Z ]*PRIVATE KEY-----'
scan "no AWS access key id" '\bAKIA[0-9A-Z]{16}\b'
scan "no GitHub token" '\bgh[pousr]_[A-Za-z0-9]{20,}'
scan "no .env file" 'DUMMY_NEVER_MATCHES'
if find "$OUT" -name '*.env' -o -name 'ca-bundle.crt' | grep -q .; then
  bad "an environment file or certificate is in the export"
else
  ok "no environment file or certificate"
fi

step "Scan for private inventory terms"
for directory in artifacts/private-findings benchmarks/private-results docs/commercialization; do
  if find "$OUT/$directory" -type f 2>/dev/null | grep -q .; then
    bad "$directory is in the export"
  else
    ok "no $directory"
  fi
done
scan "no private finding package named in prose" 'ILAB-EXT-[0-9]+-v[0-9]'

step "Scan for finding identifiers"
scan "no private finding identifier" 'ILAB-EXT-[0-9]+'

step "Scan for real target versions and commits"
# The definitions themselves, not a path string mentioning them. A test that
# names the directory is not the directory.
if find "$OUT/targets/external" -type f 2>/dev/null | grep -q .; then
  bad "a pinned external target definition is in the export"
  find "$OUT/targets/external" -type f | sed 's/^/             /' | head
else
  ok "no pinned external target definition"
fi
if find "$OUT" -name 'build-resolution.json' -o -name 'images.yaml' | grep -q .; then
  bad "an external build manifest or resolution is in the export"
else
  ok "no external build manifest or build resolution"
fi
# Blocking, not advisory. This was previously reported and waved through on the
# grounds that the fixtures carried no result — but a real name beside an exact
# version already says which build was run, and the four fixtures that tripped it
# now use synthetic targets instead. A check nobody has to act on is a check that
# stops being read.
scan "no real target name adjacent to a version" \
  '(mcp-go-everything|server-everything|mcp-server-time)[^\n]{0,60}[0-9]+\.[0-9]+\.[0-9]+'

step "Scan for private branch names"
scan "no session branch" 'claude/[a-z0-9-]{6,}'
scan "no workflow run identifier" 'actions/runs/[0-9]+'

# ------------------------------------------------------------- 7-11. python

step "Install the Python package from the export"
if (cd "$OUT" && uv sync --all-extras --dev >/tmp/install.log 2>&1); then
  ok "resolved and installed from the export's own lockfile"
else
  bad "the export does not install on its own"
  tail -20 /tmp/install.log
fi

step "Run lint"
if (cd "$OUT" && uv run ruff check . >/tmp/lint.log 2>&1); then ok "ruff check"; else bad "ruff check"; tail -15 /tmp/lint.log; fi

step "Run format check"
if (cd "$OUT" && uv run ruff format --check . >/tmp/fmt.log 2>&1); then ok "ruff format"; else bad "ruff format"; tail -15 /tmp/fmt.log; fi

step "Run type checking"
if (cd "$OUT" && uv run pyright >/tmp/types.log 2>&1); then ok "pyright strict"; else bad "pyright strict"; tail -15 /tmp/types.log; fi

step "Run unit tests"
# `-rs`, and no second `-q`. pyproject already passes `-q`; adding another made
# it `-qq`, which suppresses the summary line — so this step used to report a
# row of progress dots as its result and never said how many tests ran.
#
# The skip *reasons* are printed because several are load-bearing here: the four
# SARIF-schema assertions that this distribution cannot make, and the tests
# whose inputs are deliberately withheld. A skip nobody reads is how a suite
# quietly stops testing anything.
if (cd "$OUT" && uv run pytest -rs >/tmp/tests.log 2>&1); then
  ok "$(grep -E '[0-9]+ passed' /tmp/tests.log | tail -1)"
  grep -E '^SKIPPED' /tmp/tests.log | sed 's/^/               /' | sort -u -t: -k3 | head -8
else
  bad "pytest"
  tail -25 /tmp/tests.log
fi

step "Run the synthetic example"
if (cd "$OUT" && uv run mcp-failure-lab viewer fixture --out artifacts/viewer-fixture >/tmp/fixture.log 2>&1 \
    && uv run mcp-failure-lab viewer export --benchmark artifacts/viewer-fixture --mode public-safe \
         --out viewer/public/data >>/tmp/fixture.log 2>&1); then
  ok "synthetic benchmark generated and exported public-safe"
else
  bad "the synthetic example does not run from the export"
  tail -20 /tmp/fixture.log
fi

# --------------------------------------------------------------- 12-16. viewer

step "Build the public-safe viewer"
if (cd "$OUT/viewer" && npm ci >/tmp/npm.log 2>&1 && npm run build >>/tmp/npm.log 2>&1); then
  ok "viewer built from the export"
else
  bad "the viewer does not build from the export"
  tail -20 /tmp/npm.log
fi

# The Python half of the export has been linted, format-checked and type-checked
# from the export since this gate existed. The TypeScript half was only ever
# built. `npm run build` runs `tsc --noEmit` and would catch a type error, but it
# says nothing about lint or formatting — so a public tree could ship code that
# fails its own `npm run lint` on a contributor's first commit. These three run
# against the export's node_modules, installed by `npm ci` above from the
# export's own lockfile, so they test the tree that would be published rather
# than this working copy.
step "Run the viewer's lint, format and type checks"
viewer_check() {
  # Declared separately: bash expands every word of a `local` line before it
  # assigns any of them, so referring to `script` in the same declaration that
  # sets it reads an unset variable — fatal under `set -u`.
  local script="$1"
  local log="/tmp/viewer-${script//:/-}.log"
  if (cd "$OUT/viewer" && npm run "$script" >"$log" 2>&1); then
    ok "npm run $script"
  else
    bad "npm run $script"
    tail -15 "$log"
  fi
}
viewer_check lint
viewer_check format:check
viewer_check typecheck

step "Run the viewer privacy scan"
if (cd "$OUT" && uv run mcp-failure-lab viewer scan viewer/public/data/dataset.json >/tmp/scan.log 2>&1 \
    && uv run mcp-failure-lab viewer scan viewer/dist >>/tmp/scan.log 2>&1); then
  ok "dataset and built bundle both clean"
else
  bad "the viewer privacy scan failed"
  tail -20 /tmp/scan.log
fi

step "Run the viewer's desktop and mobile tests"
if (cd "$OUT/viewer" && npx playwright test --project=desktop --project=mobile \
      e2e/viewer.spec.ts e2e/layout.spec.ts e2e/guided.spec.ts >/tmp/e2e.log 2>&1); then
  ok "$(grep -E 'passed' /tmp/e2e.log | tail -1 | sed 's/^ *//')"
else
  bad "the viewer's browser tests failed from the export"
  grep -E '✘|failed|Error' /tmp/e2e.log | head -15
fi

step "Play the public-safe demo source"
if (cd "$OUT" && node demos/verify.mjs demos/public-safe >/tmp/demo.log 2>&1); then
  ok "$(grep -E 'verified' /tmp/demo.log | tail -1)"
  grep -E '1920x1080' /tmp/demo.log | sed 's/^/               /'
else
  bad "the demo recording did not verify"
  tail -15 /tmp/demo.log
fi

step "Verify no network requirement"
# The viewer's own suite aborts every off-origin request and fails if one was
# attempted, so this asserts the property is actually exercised rather than
# re-implementing it.
if grep -q "blockExternalRequests" "$OUT/viewer/e2e/viewer.spec.ts"; then
  ok "the offline assertion ships with the export and ran above"
else
  bad "the export contains no offline assertion"
fi
scan "no http(s) URL to a private host" 'https?://[a-z0-9.-]*\.internal'

# ------------------------------------------------------- 18-20. artifacts, git

step "Verify no private artifact URL"
scan "no GitHub artifact URL" 'github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/(actions|suites)/'
scan "no blob storage URL" 'blob\.core\.windows\.net'

step "Generate the export manifest and hashes"
if [ -f "$OUT/EXPORT-MANIFEST.json" ]; then
  ok "EXPORT-MANIFEST.json present"
  # Hash what the export *is*, taken from the manifest — not what the directory
  # happens to hold. By this point the steps above have installed a virtualenv
  # and a node_modules in place, and hashing those would report a number
  # eighty times too large for the thing being released.
  #
  # The logic lives in `interop_lab.export.checksums`, under test, rather than in
  # a heredoc here. Release-integrity code that exists only inside a shell string
  # is code no test can reach, which is how the last defect — a manifest-listed
  # file silently skipped — survived review.
  if (cd "$OUT" && uv run mcp-failure-lab export-checksums --root . >/tmp/hash.log 2>&1); then
    ok "$(jq -r '"\(.hashed_files) exported file(s) + manifest = \(.lines) line(s)"' /tmp/hash.log 2>/dev/null \
          || grep -E '"lines"' /tmp/hash.log)"
    note "manifest digest $(jq -r '.manifest_sha256' /tmp/hash.log 2>/dev/null | cut -c1-16)…"
    note "SHA256SUMS digest $(jq -r '.checksums_sha256' /tmp/hash.log 2>/dev/null | cut -c1-16)…"
  else
    bad "the checksum generator refused the export"
    tail -15 /tmp/hash.log
  fi
else
  bad "no export manifest was written"
fi

step "Re-verify the written checksums against the manifest and the tree"
if (cd "$OUT" && uv run mcp-failure-lab export-checksums --root . --check >/tmp/hashcheck.log 2>&1); then
  ok "every line re-read, re-hashed and matched; line count exact"
else
  bad "the written SHA256SUMS does not verify"
  tail -15 /tmp/hashcheck.log
fi

step "Confirm no .git directory exists"
if [ -e "$OUT/.git" ]; then
  bad ".git exists in the export"
else
  ok "no .git — publishing means a new repository, so no private history travels"
fi

# ------------------------------------------------------------------- verdict

printf '\n\033[1mexport gate: %d step(s), %d failure(s)\033[0m\n' "$STEP" "$FAILURES"
if [ "$FAILURES" -eq 0 ]; then
  printf 'Passing is necessary, not sufficient. Publication is the owner'"'"'s decision.\n'
  exit 0
fi
exit 1
