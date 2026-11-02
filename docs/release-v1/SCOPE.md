# Scope — this branch

This branch is the first Track A P0 slice. It is not MCP Failure Lab v1.0.

## In scope

- Unscoped `mcp-failure-lab conformance` invokes the pinned official CLI with `--requirements` set from the target profile (`2025-11-25` or `2026-07-28`).
- The scored verdict for that invocation is the official process exit code.
- A `--scenario` invocation stays a wire-scoped probe (`--scenario` plus `--spec-version`) and is stored as `selector=scenario`.
- SKIPPED and informational official check statuses are counted apart from passes.
- Public CI gains two steps that run those invocations against the local HTTP fixtures. The partial fixtures must not be recorded as a requirement-set pass.

## Out of scope

- Version `1.0.0`, tags, releases, merges, branch protection, secrets, and paid services.
- Bumping `mcp`, `@modelcontextprotocol/sdk`, or `@modelcontextprotocol/conformance`.
- The client conformance leg.
- Docker containment on a host without Docker.
- Removing the pyright `reportUnknown*` suppressions.
- The CI gate path that treats a missing conformance remeasurement as a pass (`A-P0-1`).
- The public CI secret-scan false red (`A-P1-1`).

## Acceptance for this slice

A partial fixture's unscoped conformance run exits 1 with `selector=requirements`. A `tools-list` scenario probe can exit 0 only with `selector=scenario`. Both are covered by unit tests and by live CLI runs on 2026-10-29.
