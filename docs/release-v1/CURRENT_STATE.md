# Current state — Track A

Audit date: 2026-10-29. Times below are UTC. Version in `pyproject.toml` is `0.1.0` and was not changed.

## Repository

| Item | Value |
|---|---|
| Repository | `TimelessVisions/mcp-failure-lab` (private) |
| Default branch | `main` |
| Audit baseline SHA | `d89ae1a79f2f0e4d632a7df4978dc324bcf303b4` |
| Baseline commit time | 2026-08-18 17:11:37 UTC (`2026-08-18 13:11:37 -0400`) |
| Baseline subject | MCP Failure Lab — initial public-safe release candidate |
| Work branch | `release/v1/A1-conformance-requirements` |
| Characterization commit | `7419791d2e6dcfff4d1c60f019dbe2ce1abb86e2` |
| Adapter fix | `8e368e410d02ea5740a2828b1c0385a011e2c254` |
| Open PRs / issues at audit start | none |
| Branch protection | Owner instruction: OFF, and it was not changed. `GET /repos/TimelessVisions/mcp-failure-lab/branches/main/protection` returned HTTP 403 (`Resource not accessible by integration`). This audit did not independently read the protection document. |

The implementation commit is the parent of the commit that adds this file. Re-verify the branch head with `git rev-parse HEAD`. Do not treat this file's presence as that hash.

## Runtime and pins (this VM)

Recorded 2026-10-29T15:18:01Z unless noted.

| Tool | Version |
|---|---|
| Python | 3.12.3 |
| uv | 0.8.17 (CI pin; installed for this audit) |
| Node | v22.23.4 |
| npm | 10.9.9 |
| pytest | 9.1.1 |
| ruff | 0.16.2 |
| pyright | 1.1.411 |
| Docker | not installed |

| Dependency | Pin in repo | Observed elsewhere on 2026-10-29 |
|---|---|---|
| `mcp` (Python SDK) | `==2.0.0` in `pyproject.toml` / `uv.lock` | PyPI index latest `2.3.0`. Not installed. |
| `@modelcontextprotocol/sdk` | `1.30.0` in `targets/typescript/package.json` | npm latest `1.32.2`. Not installed. |
| `@modelcontextprotocol/conformance` | `0.2.0-alpha.11` in `tools/conformance/package.json`, source SHA `c321dd32035556e6769d3724a8ee97d87c3faaac` (2026-08-07) | npm `latest` tag `0.1.16`; npm `alpha` tag `0.2.0-alpha.12`. Alpha.12 was not installed into the repo. |

Installed Python SDK constants (`mcp_types.version` from `mcp==2.0.0`): `HANDSHAKE_PROTOCOL_VERSIONS` ends at `2025-11-25`; `MODERN_PROTOCOL_VERSIONS` is `('2026-07-28',)`.

Installed TypeScript SDK `dist/esm/types.d.ts` dated revisions found: `2024-10-07`, `2025-03-26`, `2025-11-25`. No `2026-07-28` string in that file.

## CI workflows (real paths)

- `.github/workflows/public-ci.yml` — push and pull_request to `main`, plus `workflow_dispatch`. Job `verify`.
- `.github/workflows/public-release-gate.yml` — `workflow_dispatch`, and push/pull_request to `main` filtered by path.
- `.github/workflows/public-viewer.yml` — same path-filtered triggers.

No other workflow files are in the tree. Runtime matrix in those files: `UV_VERSION=0.8.17`, `NODE_VERSION=22`, `ubuntu-latest`, Python 3.12.

GitHub Actions on `main` at `d89ae1a` (2026-08-18T17:12:51Z):

| Workflow | Conclusion |
|---|---|
| Public CI | failure |
| Public Release Gate | success |
| Public Viewer | success |

Public CI failed in "No secret or private term in the tree". The log names `./public-junit.xml` for the AWS-access-key, GitHub-token, and private-finding patterns. The same three patterns match a junit file produced by `pytest` on this VM. The job is red. It is not a green badge earned by skipping tests.

## Pytest

Command, both times: `uv run pytest -rs --tb=line --junitxml=<path>` after `uv sync --all-extras --dev`.

| Run | UTC | Passed | Failed | Skipped | Errors | Inconclusive |
|---|---|---|---|---|---|---|
| Baseline, SHA `d89ae1a` | 2026-10-29T15:20:02Z – 15:20:38Z | 980 | 0 | 50 | 0 | 0 (pytest has no inconclusive result; 0 xfailed) |
| After the characterization tests and the adapter fix | 2026-10-29T15:28:15Z – 15:28:48Z | 986 | 0 | 50 | 0 | 0 |

Junit `tests` includes skipped (baseline 1030, after 1036). Passed above is `tests - failures - errors - skipped`.

Skip reasons after the fix, 50 total: 34 `docker or the pinned base image is unavailable`, 7 pinned `mirror.gcr.io/library/python` image absent, 4 OASIS SARIF schema withheld, 4 private denylist inventories absent, 1 external target definitions absent. Docker was not installed. Those skips are `NOT_MEASURED`, not passes.

## Conformance baseline

Pinned CLI: `node tools/conformance/node_modules/@modelcontextprotocol/conformance/dist/index.js`.

`list --requirements 2026-07-28` reports 69 required scenarios (37 server, 32 client) plus not-scored extensions. `list --requirements 2025-11-25` reports 48 required scenarios (30 server, 18 client). The client legs were not executed.

Direct official server run, before the adapter change, fixture `http://127.0.0.1:8765/mcp`, profile 2026-07-28:

```text
node ... server --url http://127.0.0.1:8765/mcp --requirements 2026-07-28 --output-dir /tmp/baseline-requirements-2026
```

2026-10-29T15:21:14Z – 15:21:17Z, exit 1. Check statuses: SUCCESS 73, FAILURE 88, SKIPPED 7, WARNING 8, INFO 1 (177). Official log: not-scored scenarios do not affect conformance (13 scenarios, 12 failing). Scored subset of those files: SUCCESS 52, FAILURE 48, SKIPPED 6, WARNING 5, INFO 1. Inconclusive: none emitted.

Adapter before the fix, one scenario, 2026-10-29T15:21:07Z – 15:21:08Z, exit 0:

The saved command contained `--spec-version 2026-07-28` and `--scenario tools-list`, and did not contain `--requirements`. The artifact field `requirements` was `2026-07-28` and `outcome` was `OFFICIAL_CONFORMANCE_PASS` (3/3 checks).

Adapter after the fix:

| Target | Selector | Revision | UTC | Exit | Outcome | Passed | Failed | Skipped | Informational |
|---|---|---|---|---|---|---|---|---|---|
| `targets/http/fixture-2026.yaml` | requirements | 2026-07-28 | 15:28:53Z – 15:28:57Z | 1 | `OFFICIAL_CONFORMANCE_FAILURE` | 73 | 88 | 7 | 9 |
| `targets/http/fixture-2026.yaml` `--scenario tools-list` | scenario | 2026-07-28 | 15:28:57Z – 15:28:58Z | 0 | `OFFICIAL_CONFORMANCE_PASS` | 3 | 0 | 0 | 0 |
| `targets/http/fixture-2025.yaml` | requirements | 2025-11-25 | 15:31:00Z – 15:31:02Z | 1 | `OFFICIAL_CONFORMANCE_FAILURE` | 34 | 28 | 0 | 6 |

Informational is WARNING plus INFO. It is not a pass, a skip, or an inconclusive. The 2025 server run was measured only after the fix. There is no paired before-adapter 2025 execution.

## Pyright strict mode

`pyproject.toml` lines 88–109 document six `reportUnknown*` suppressions. `uv run pyright` on this VM after the fix: 0 errors. The suppressions were not removed.
