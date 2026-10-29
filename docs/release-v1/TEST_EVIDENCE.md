# Test evidence — Track A conformance requirements

Environment: Linux cloud VM, Python 3.12.3, uv 0.8.17, Node v22.23.4, npm 10.9.9, pytest 9.1.1, ruff 0.16.2, pyright 1.1.411. Docker is not installed. No `INTEROP_STRICT_EXTERNAL`. Working directory: repository root. Dependencies: `uv sync --all-extras --dev`, `npm ci --prefix tools/conformance`, `npm ci --prefix targets/typescript`.

Inconclusive is recorded only when a tool emits that bucket. Pytest did not. The conformance CLI did not emit an inconclusive status. WARNING and INFO are informational, not passes and not skips.

## Baseline pytest (before any code change)

SHA `d89ae1a79f2f0e4d632a7df4978dc324bcf303b4`.

```text
2026-10-29T15:20:02Z
uv run pytest -rs --tb=line --junitxml=/tmp/baseline-pytest-junit.xml
2026-10-29T15:20:38Z
```

Exit 0. Pytest summary: `980 passed, 50 skipped in 35.40s`. Junit: tests=1030 (includes skipped), failures=0, errors=0, skipped=50. Passed excluding skips: 980. Failed: 0. Inconclusive: 0.

## Characterization (failing tests first)

Commit `7419791d2e6dcfff4d1c60f019dbe2ce1abb86e2` adds `tests/unit/test_conformance_requirements.py` and does not change the adapter.

```text
2026-10-29T15:26:33Z
uv run pytest tests/unit/test_conformance_requirements.py --tb=line -q
```

Exit 1. 6 failed, 0 passed.

- `test_unscoped_run_invokes_frozen_requirements_not_spec_version` — command contained `--spec-version` and not `--requirements`.
- `test_requirements_exit_zero_is_a_pass_even_when_an_unscored_check_fails` — `selector` was absent.
- `test_scenario_probe_is_not_a_requirements_pass` — `selector` was absent (`None == 'scenario'`).
- `test_scenario_probe_does_not_treat_a_zero_exit_as_a_pass_over_a_failed_check` — same missing selector. The outcome assertion was not reached.
- `test_command_builder_never_mixes_requirements_with_spec_version` — `ImportError: build_server_command`.
- `test_skipped_and_informational_checks_are_counted_apart_from_passes` — `ConformanceCheck` had no `status`.

## Negative control already observed on the old adapter

```text
2026-10-29T15:21:07Z
uv run mcp-failure-lab conformance -t targets/http/fixture-2026.yaml --scenario tools-list --artifacts /tmp/baseline-adapter-scenario
```

Exit 0. Saved command included `--spec-version` and `--scenario tools-list`, not `--requirements`. `requirements` field `2026-07-28`. Outcome `OFFICIAL_CONFORMANCE_PASS`. Checks 3 passed, 0 failed, total 3. That pass was a single scenario.

Direct pinned CLI, not the adapter, same clock window:

```text
2026-10-29T15:21:14Z
node tools/conformance/node_modules/@modelcontextprotocol/conformance/dist/index.js server --url http://127.0.0.1:8765/mcp --requirements 2026-07-28 --output-dir /tmp/baseline-requirements-2026
2026-10-29T15:21:17Z
```

Exit 1. Official totals: 73 passed, 88 failed. Not scored: 13 scenarios, 12 failing, "do not affect conformance." Check-file statuses: SUCCESS 73, FAILURE 88, SKIPPED 7, WARNING 8, INFO 1. Scored files only: SUCCESS 52, FAILURE 48, SKIPPED 6, WARNING 5, INFO 1. Not-scored files only: FAILURE 40, SUCCESS 21, WARNING 3, SKIPPED 1. Inconclusive: 0.

`list --requirements` counts, same binary: 2026-07-28 has 69 required scenarios (37 server + 32 client); 2025-11-25 has 48 (30 server + 18 client). Client scenarios were not run.

## After the fix

```text
2026-10-29T15:28:15Z
uv run pytest -rs --tb=line --junitxml=/tmp/after-pytest-junit.xml
2026-10-29T15:28:48Z
```

Exit 0. `986 passed, 50 skipped in 32.21s`. Junit tests=1036, failures=0, errors=0, skipped=50. Delta from baseline: +6 tests, all in `tests/unit/test_conformance_requirements.py`. Skip reasons unchanged: 34 docker-or-image, 7 pinned image absent, 4 SARIF schema, 4 private inventories, 1 external targets.

```text
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
```

Ruff passed. Pyright: 0 errors, 0 warnings.

Live CLI, fixed adapter:

```text
2026-10-29T15:28:53Z
uv run mcp-failure-lab conformance -t targets/http/fixture-2026.yaml --artifacts /tmp/after-requirements
```

Exit 1. `selector=requirements`, `verdict_source=official_exit_code`, revision `2026-07-28`, outcome `OFFICIAL_CONFORMANCE_FAILURE`. Checks: 73 passed, 88 failed, 7 skipped, 9 informational, total 177. Command flags include `--requirements 2026-07-28` and do not include `--spec-version` or `--scenario`.

```text
2026-10-29T15:28:57Z
uv run mcp-failure-lab conformance -t targets/http/fixture-2026.yaml --scenario tools-list --artifacts /tmp/after-scenario
```

Exit 0. `selector=scenario`, `verdict_source=check_counts`, outcome `OFFICIAL_CONFORMANCE_PASS`. Checks 3 passed, 0 failed, 0 skipped, 0 informational. Command has `--scenario tools-list` and `--spec-version 2026-07-28`, and not `--requirements`. The CLI prints that this is not a frozen requirement-set result.

```text
2026-10-29T15:31:00Z
uv run mcp-failure-lab conformance -t targets/http/fixture-2025.yaml --artifacts /tmp/after-requirements-2025
2026-10-29T15:31:02Z
```

Exit 1. `selector=requirements`, revision `2025-11-25`, outcome `OFFICIAL_CONFORMANCE_FAILURE`. Checks: 34 passed, 28 failed, 0 skipped, 6 informational, total 68. No paired before-fix 2025 run was captured.

## Negative controls encoded as tests

- Unscoped run with official exit 1 and a SUCCESS check is `OFFICIAL_CONFORMANCE_FAILURE`, not a pass.
- Unscoped run with official exit 0 and one FAILURE check is `OFFICIAL_CONFORMANCE_PASS`, because the pinned CLI's exit code already excludes not-scored failures. The failure is still counted in `scenarios_failed`.
- Scenario run with exit 0 and a FAILURE check is `OFFICIAL_CONFORMANCE_FAILURE`. A zero exit does not hide a failed check on a probe.
- `build_server_command` never emits `--requirements` together with `--spec-version` or `--scenario`.

## Not verified

- GitHub Actions on this branch (workflow file changed; no run exists yet).
- Client conformance legs.
- Docker containment tests (41 skips).
- Conformance `0.2.0-alpha.12` beyond `--help` and the flag-mix rejection.
- Python SDK 2.3.0 and TypeScript SDK 1.32.2.
- A fixture that passes every scored scenario and fails only a not-scored scenario. The exit-code rule is unit-tested with a fake process, not with a live server built for that case.
- Independent re-run by a different worker.
