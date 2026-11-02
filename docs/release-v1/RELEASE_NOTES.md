# Release notes — conformance requirements slice

Distribution version remains `0.1.0`. This is not a `1.0.0` release and not a release candidate.

## What changed

- Unscoped `mcp-failure-lab conformance` runs `server --requirements <profile date>` on the pinned `@modelcontextprotocol/conformance@0.2.0-alpha.11` CLI.
- The scored outcome of that run follows the official process exit code. Not-scored check failures do not, by themselves, flip a zero exit into a failure, and a non-zero exit is never a pass.
- `--scenario` remains available. It passes `--spec-version` and is recorded as `selector=scenario`. It is not a requirement-set result.
- Artifacts record `selector`, `verdict_source`, `checks_skipped`, and `checks_informational`.
- Public CI runs the requirements command and the `tools-list` probe against the local HTTP fixtures.

## Upgrade

No dependency bump. Re-run `npm ci --prefix tools/conformance` if `node_modules` is missing. An unscoped conformance invocation that used to pass `--spec-version` now runs the frozen server set and can exit 1 on a target that only passed a single scenario.

## Unresolved

See `BLOCKERS.md`. The CI gate still treats a missing conformance remeasurement as a non-event when a baseline recorded an outcome. Public CI on `main` is red for a junit secret-scan false positive that this branch does not fix. Client conformance and Docker containment were not measured here.
