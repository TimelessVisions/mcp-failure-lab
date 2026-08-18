"""Integration tests for the Streamable HTTP transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interop_lab.config.loader import load_target
from interop_lab.differential.compare import compare
from interop_lab.events.model import Channel, Direction
from interop_lab.minimizer.reduce import Minimizer, signature_of
from interop_lab.models.core import AssertionStatus, Classification, DifferentialVerdict
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.scenarios.library import get_scenario
from interop_lab.transports.http.session import UnauthorizedTargetError, assert_local

pytestmark = pytest.mark.integration

HTTP_2026 = Path("targets/http/fixture-2026.yaml")
HTTP_2025 = Path("targets/http/fixture-2025.yaml")
FAULTY_HEADERS = Path("targets/http/faulty-header-mismatch.yaml")
SLOW_BODY = Path("targets/http/slow-body.yaml")
RESET = Path("targets/http/connection-reset.yaml")


@pytest.fixture
def executor(tmp_path: Path) -> ScenarioExecutor:
    return ScenarioExecutor(artifacts_dir=tmp_path, seed=0)


# --------------------------------------------------------------- containment


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "https://8.8.8.8/mcp",
        "http://169.254.169.254/latest/meta-data",
        "http://[2001:db8::1]/mcp",
        "ftp://127.0.0.1/mcp",
    ],
)
def test_non_local_targets_are_refused(url: str) -> None:
    """Hostile scenarios must never be able to leave the machine."""
    with pytest.raises(UnauthorizedTargetError):
        assert_local(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/mcp", "http://localhost:1/mcp"])
def test_local_targets_are_permitted(url: str) -> None:
    assert_local(url)  # must not raise


# ------------------------------------------------------------------ baseline


async def test_http_baseline_passes_on_both_profiles(executor: ScenarioExecutor) -> None:
    scenario = get_scenario("baseline.http_tools_list")
    for path in (HTTP_2026, HTTP_2025):
        result, trace = await executor.run(scenario, load_target(path))
        assert result.classification is Classification.PASS, path
        assert trace.responses()


async def test_http_capture_records_status_and_timing(executor: ScenarioExecutor) -> None:
    _, trace = await executor.run(get_scenario("baseline.http_tools_list"), load_target(HTTP_2026))
    inbound = [e for e in trace.events if e.direction is Direction.INBOUND]
    assert inbound
    headers = inbound[0].headers or {}
    assert headers.get(":status") == "200"
    assert ":first_byte_ms" in headers
    assert ":body_complete_ms" in headers
    assert inbound[0].raw_sha256

    outbound = [e for e in trace.events if e.direction is Direction.OUTBOUND]
    assert outbound
    request_headers = outbound[0].headers or {}
    assert request_headers.get(":method") == "POST"
    # Routing headers are derived from the body, so they agree by construction.
    assert request_headers.get("Mcp-Method") == "tools/list"


async def test_lifecycle_events_are_recorded(executor: ScenarioExecutor) -> None:
    _, trace = await executor.run(get_scenario("baseline.http_tools_list"), load_target(HTTP_2026))
    notes = " ".join(e.note or "" for e in trace.events)
    assert "server ready at" in notes
    assert "connection opened" in notes
    assert "connection closed" in notes


# ------------------------------------------------------- header/body mismatch


async def test_header_body_mismatch_is_rejected_by_a_conforming_fixture(
    executor: ScenarioExecutor,
) -> None:
    result, _ = await executor.run(
        get_scenario("version.http_header_body_mismatch"), load_target(HTTP_2026)
    )
    assert result.classification is Classification.PASS


async def test_header_body_mismatch_is_detected_on_the_faulty_fixture(
    executor: ScenarioExecutor,
) -> None:
    result, trace = await executor.run(
        get_scenario("version.http_header_body_mismatch"), load_target(FAULTY_HEADERS)
    )
    assert result.classification is Classification.SPEC_VIOLATION
    failed = [a.assertion_id for a in result.assertions if a.status is AssertionStatus.FAIL]
    assert "code_32020" in failed
    # Evidence must point at a real captured event.
    evidence = [a.evidence_seq for a in result.assertions if a.status is AssertionStatus.FAIL]
    assert any(evidence)
    # The mismatch really was on the wire.
    outbound = [e for e in trace.events if e.direction is Direction.OUTBOUND]
    assert (outbound[0].headers or {}).get("Mcp-Method") == "tools/call"
    assert (outbound[0].message or {}).get("method") == "tools/list"


# ---------------------------------------------------------------- timing


async def test_slow_body_is_distinguished_from_a_header_timeout(
    executor: ScenarioExecutor,
) -> None:
    result, trace = await executor.run(
        get_scenario("timing.http_slow_body"), load_target(SLOW_BODY)
    )
    assert result.classification in (Classification.HANG, Classification.TIMEOUT)
    notes = " ".join(e.note or "" for e in trace.events)
    # The whole point: headers arrived, the body did not.
    assert "body-completion timeout" in notes
    assert "headers arrived at" in notes


async def test_prompt_target_completes_the_body(executor: ScenarioExecutor) -> None:
    result, _ = await executor.run(get_scenario("timing.http_slow_body"), load_target(HTTP_2026))
    assert result.classification is Classification.PASS


async def test_dropped_http_request_is_not_blamed_on_the_target(
    executor: ScenarioExecutor,
) -> None:
    result, _ = await executor.run(
        get_scenario("timing.http_dropped_request"), load_target(HTTP_2026)
    )
    assert result.classification is Classification.PASS
    assert "drop_first_http_request" in result.faults_applied


# --------------------------------------------------------------- recovery


async def test_connection_reset_is_detected(executor: ScenarioExecutor) -> None:
    result, trace = await executor.run(
        get_scenario("recovery.http_connection_reset"), load_target(RESET)
    )
    assert result.classification is Classification.RECOVERY_FAILURE
    notes = " ".join(e.note or "" for e in trace.events)
    assert "reset" in notes or "connection" in notes


async def test_conforming_target_survives_the_recovery_scenario(
    executor: ScenarioExecutor,
) -> None:
    result, _ = await executor.run(
        get_scenario("recovery.http_connection_reset"), load_target(HTTP_2026)
    )
    assert result.classification is Classification.PASS


async def test_http_process_is_reaped(executor: ScenarioExecutor) -> None:
    result, _ = await executor.run(get_scenario("baseline.http_tools_list"), load_target(HTTP_2026))
    assert result.process.cleanup_ok
    assert result.process.exit_code is not None


# --------------------------------------------------- differential + minimise


async def test_http_differential_finds_the_mismatch(executor: ScenarioExecutor) -> None:
    scenario = get_scenario("version.http_header_body_mismatch")
    good_run, good_trace = await executor.run(scenario, load_target(HTTP_2026))
    bad_run, bad_trace = await executor.run(scenario, load_target(FAULTY_HEADERS))
    diff = compare(good_run, good_trace, bad_run, bad_trace)
    assert diff.verdict is DifferentialVerdict.NORMATIVE_DIVERGENCE


async def test_http_minimization_preserves_the_constitutive_header(
    executor: ScenarioExecutor,
) -> None:
    """Regression: the header creating the mismatch must survive minimisation.

    Deleting it leaves a scenario whose assertions still fail — because a
    conforming response is not an error — so the failure *signature* is
    unchanged while the reproduction no longer demonstrates the defect.
    Signature matching alone cannot catch this, so header overrides are only
    reducible when a scenario explicitly nominates them.
    """
    scenario = get_scenario("version.http_header_body_mismatch")
    target = load_target(FAULTY_HEADERS)

    baseline, _ = await executor.run(scenario, target)
    original = signature_of(baseline)

    reduced, report = await Minimizer(executor, target).minimize(scenario, baseline.classification)
    final, _ = await executor.run(reduced, target)

    assert signature_of(final) == original
    assert report.stable
    assert reduced.steps[0].http_header_overrides == {"Mcp-Method": "tools/call"}


async def test_http_repro_package_contains_no_absolute_secrets(
    executor: ScenarioExecutor, tmp_path: Path
) -> None:
    """A reproduction must be safe to hand to someone else."""
    from interop_lab.replay.repro import write_repro

    scenario = get_scenario("version.http_header_body_mismatch")
    target = load_target(FAULTY_HEADERS)
    run, trace = await executor.run(scenario, target)

    repro_dir = write_repro(
        tmp_path, finding_id="T", scenario=scenario, target=target, run=run, trace=trace, seed=0
    )
    blob = "\n".join(p.read_text(encoding="utf-8") for p in repro_dir.rglob("*") if p.is_file())
    for forbidden in ("ghp_", "sk-", "BEGIN RSA PRIVATE KEY", "Authorization: Bearer "):
        assert forbidden not in blob


async def test_http_trace_serialises_and_reloads(executor: ScenarioExecutor) -> None:
    _, trace = await executor.run(get_scenario("baseline.http_tools_list"), load_target(HTTP_2026))
    text = trace.to_jsonl()
    for line in text.splitlines():
        json.loads(line)
    assert Channel.HTTP.value in text


async def test_normalized_traces_are_deterministic_across_runs(
    executor: ScenarioExecutor,
) -> None:
    """Two runs of the same scenario differ raw but must normalise identically.

    Ephemeral ports and timestamps change every run. If they survive
    normalisation, every comparison shows spurious differences and the
    differential engine becomes useless.
    """
    from interop_lab.normalization.normalize import normalize_trace

    scenario = get_scenario("baseline.http_tools_list")
    target = load_target(HTTP_2026)

    _, first = await executor.run(scenario, target)
    _, second = await executor.run(scenario, target)

    # Different ephemeral ports, so the raw traces genuinely differ.
    assert first.to_jsonl() != second.to_jsonl()

    assert normalize_trace(first).to_jsonl() == normalize_trace(second).to_jsonl()


async def test_normalization_keeps_normative_headers_visible(
    executor: ScenarioExecutor,
) -> None:
    """Determinism must not be bought by blinding the comparison."""
    from interop_lab.normalization.normalize import normalize_trace

    scenario = get_scenario("version.http_header_body_mismatch")
    _, good = await executor.run(scenario, load_target(HTTP_2026))
    _, bad = await executor.run(scenario, load_target(FAULTY_HEADERS))

    good_norm = normalize_trace(good).to_jsonl()
    bad_norm = normalize_trace(bad).to_jsonl()

    # The mismatching header and the status difference must both survive.
    assert "tools/call" in good_norm and "tools/call" in bad_norm
    assert '"MCP-Protocol-Version":"2026-07-28"' in good_norm
    assert good_norm != bad_norm, "a real behavioural difference was normalised away"
    assert "-32020" in good_norm, "the normative error code must survive normalisation"


async def test_run_artifacts_use_artifact_uris(executor: ScenarioExecutor) -> None:
    from interop_lab.artifacts.portability import validate_payload

    result, _ = await executor.run(get_scenario("baseline.http_tools_list"), load_target(HTTP_2026))
    assert result.trace_path is not None
    assert result.trace_path.startswith("artifact://")
    assert not validate_payload(result.model_dump(mode="json"), context="run")


async def test_run_records_observed_runtime(executor: ScenarioExecutor) -> None:
    result, _ = await executor.run(get_scenario("baseline.http_tools_list"), load_target(HTTP_2026))
    assert result.runtime is not None
    assert result.runtime.observed.os
    assert result.runtime.observed.python_version
    assert result.runtime.observed.dependency_lock_sha256
