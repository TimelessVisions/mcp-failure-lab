"""The pinned official conformance CLI is executed, not mocked.

Missing node or the pinned package is a failure. A skip would record an
unmeasured run as a pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interop_lab.artifacts.portability import validate_payload
from interop_lab.config.loader import load_target
from interop_lab.conformance.adapter import (
    CLI_ENTRY,
    ConformanceAdapter,
    is_available,
    unavailable_reason,
)
from interop_lab.conformance.models import (
    CONFORMANCE_VERSION,
    ConformanceOutcome,
    ConformanceSelector,
)
from interop_lab.transports.http.session import StreamableHttpSession

PINNED_VERSION = "0.2.0-alpha.11"


@pytest.mark.integration
async def test_pinned_official_cli_runs_against_the_2026_fixture(tmp_path: Path) -> None:
    if not is_available():
        pytest.fail(
            "the pinned official conformance CLI must be installed before the "
            f"suite runs: {unavailable_reason()}"
        )
    package_json = CLI_ENTRY.parents[1] / "package.json"
    installed = json.loads(package_json.read_text(encoding="utf-8"))
    if installed.get("version") != PINNED_VERSION:
        pytest.fail(
            f"expected {CONFORMANCE_VERSION} at {package_json}, found {installed.get('version')!r}"
        )

    target = load_target("targets/http/fixture-2026.yaml")
    adapter = ConformanceAdapter(output_root=tmp_path)
    async with StreamableHttpSession(target, run_id="conformance-live") as session:
        assert session.base_url is not None
        run = await adapter.run_server(
            target=target,
            url=session.base_url,
            scenario="tools-list",
            timeout_s=60,
        )

    assert run.selector is ConformanceSelector.SCENARIO
    assert run.outcome is ConformanceOutcome.OFFICIAL_CONFORMANCE_PASS
    assert run.exit_code == 0
    assert any(check.check_id == "tools-list" for check in run.checks)
    dumped = run.model_dump(mode="json")
    assert "requirements" not in dumped
    _assert_tree_portable(tmp_path, dumped)


def _assert_tree_portable(root: Path, summary: object) -> None:
    problems = validate_payload(summary, context="conformance-summary")
    assert not problems, problems
    for checks in root.rglob("checks.json"):
        payload = json.loads(checks.read_text(encoding="utf-8"))
        problems = validate_payload(payload, context=checks.name)
        assert not problems, problems
