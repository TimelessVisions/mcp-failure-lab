"""``mcp-failure-lab ci`` must hand the gate an official outcome it measured."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_ci_gate import SCENARIO, make_run, make_target
from test_ci_gate_hardening import make_document, make_entry

from interop_lab.ci.baseline import GateOutcome, RegressionKind
from interop_lab.ci.gate import evaluate
from interop_lab.ci.official import official_outcomes_for_baseline
from interop_lab.config.loader import load_target
from interop_lab.conformance.models import ConformanceOutcome, ConformanceRun, ConformanceSelector
from interop_lab.models.core import Classification, TargetDefinition


class _Session:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.base_url: str | None = "http://127.0.0.1:9/mcp"

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Adapter:
    def __init__(self, *, output_root: Path) -> None:
        self.output_root = output_root

    async def run_server(
        self,
        *,
        target: TargetDefinition,
        url: str,
        scenario: str | None = None,
        timeout_s: float = 180.0,
    ) -> ConformanceRun:
        del url, timeout_s
        assert scenario is None
        return ConformanceRun(
            revision="2026-07-28",
            selector=ConformanceSelector.REQUIREMENTS,
            target_id=target.target_id,
            url="http://127.0.0.1:9/mcp",
            command=["node"],
            outcome=ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE,
        )


def _patch(monkeypatch: pytest.MonkeyPatch, *, available: bool = True) -> None:
    monkeypatch.setattr("interop_lab.ci.official.StreamableHttpSession", _Session)
    monkeypatch.setattr("interop_lab.ci.official.ConformanceAdapter", _Adapter)
    monkeypatch.setattr("interop_lab.ci.official.is_available", lambda: available)


@pytest.mark.asyncio
async def test_a_required_http_target_is_measured_and_the_gate_sees_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = load_target("targets/http/fixture-2026.yaml")
    entry = make_entry(conformance_outcome="OFFICIAL_CONFORMANCE_PASS").model_copy(
        update={"target_id": target.target_id}
    )
    document = make_document(entry)
    _patch(monkeypatch)
    runs = [(make_run(Classification.PASS, target_id=target.target_id), SCENARIO, target)]

    outcomes = await official_outcomes_for_baseline(document, runs, artifacts=tmp_path)

    assert outcomes == {target.target_id: ConformanceOutcome.OFFICIAL_CONFORMANCE_FAILURE.value}
    result = evaluate(baseline=document, runs=runs, conformance_outcomes=outcomes)
    assert result.outcome is GateOutcome.REGRESSION
    changed = next(
        r for r in result.regressions if r.kind is RegressionKind.CONFORMANCE_OUTCOME_CHANGED
    )
    assert changed.observed_value == "OFFICIAL_CONFORMANCE_FAILURE"
    assert changed.blocking is True


@pytest.mark.asyncio
async def test_a_stdio_target_is_not_measured_and_the_gate_does_not_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch(monkeypatch)
    target = make_target()
    document = make_document(make_entry(conformance_outcome="OFFICIAL_CONFORMANCE_PASS"))
    runs = [(make_run(Classification.PASS), SCENARIO, target)]

    outcomes = await official_outcomes_for_baseline(document, runs, artifacts=tmp_path)

    assert outcomes == {}
    result = evaluate(baseline=document, runs=runs, conformance_outcomes=outcomes)
    assert result.outcome is GateOutcome.REGRESSION
    assert any(r.kind is RegressionKind.CONFORMANCE_UNMEASURED for r in result.regressions)


@pytest.mark.asyncio
async def test_a_missing_cli_leaves_the_required_outcome_unmeasured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = load_target("targets/http/fixture-2026.yaml")
    entry = make_entry(conformance_outcome="OFFICIAL_CONFORMANCE_PASS").model_copy(
        update={"target_id": target.target_id}
    )
    _patch(monkeypatch, available=False)
    outcomes = await official_outcomes_for_baseline(
        make_document(entry),
        [(make_run(Classification.PASS, target_id=target.target_id), SCENARIO, target)],
        artifacts=tmp_path,
    )
    assert outcomes == {}


@pytest.mark.asyncio
async def test_entries_without_an_official_outcome_do_not_launch_the_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launched = False

    class _Boom:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal launched
            launched = True

    monkeypatch.setattr("interop_lab.ci.official.ConformanceAdapter", _Boom)
    monkeypatch.setattr("interop_lab.ci.official.is_available", lambda: True)
    target = load_target("targets/http/fixture-2026.yaml")
    outcomes = await official_outcomes_for_baseline(
        make_document(make_entry()),
        [(make_run(Classification.PASS, target_id=target.target_id), SCENARIO, target)],
        artifacts=tmp_path,
    )
    assert outcomes == {}
    assert launched is False
