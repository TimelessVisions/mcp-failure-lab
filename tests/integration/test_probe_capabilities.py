"""What the contained probe can actually do, exercised against a real target.

Every capability listed in `docs/containment.md` is asserted here against a
container, because a capability that is implemented but never exercised is a
claim rather than a feature — and the ones most likely to be quietly broken are
exactly the ones no scenario happens to use yet: the aborts, the byte-exact
bodies, connection reuse.

Each test drives the probe through a plan built from the typed contract, so a
schema change breaks these instead of leaving them asserting a shape nothing
produces.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from interop_lab.containment import docker
from interop_lab.containment.resolution import ResolvedImage, resolve_registry_source
from interop_lab.containment.sidecar import SidecarResult, SidecarRun
from interop_lab.models.core import TargetDefinition
from interop_lab.probe.plan import (
    ProbeAbort,
    ProbeBodyMode,
    ProbeCapture,
    ProbeCaptureSource,
    ProbeMethod,
    ProbePhase,
    ProbePlan,
    ProbeStep,
    ProbeSubstitution,
    ProbeSubstitutionTarget,
    ProbeVariable,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CONTAINED_2026 = Path("targets/http/fixture-2026-contained.yaml")
CONTAINED_2025 = Path("targets/http/fixture-2025-contained.yaml")
IMAGE = (
    "mirror.gcr.io/library/python@sha256:"
    "229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
)

TOOLS_LIST: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    },
}
ROUTING_HEADERS = {
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2026-07-28",
    "Mcp-Method": "tools/list",
}


def _resolved(target: TargetDefinition) -> ResolvedImage:
    """The HTTP fixtures run a registry-pinned image, so nothing is built."""
    spec = target.container
    assert spec is not None
    source = spec.registry_source
    assert source is not None
    return resolve_registry_source(source)


def _docker_ready() -> bool:
    available, _, _ = asyncio.run(docker.probe())
    if not available:
        return False
    result = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)
    return result.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason="docker or the pinned base image is unavailable"
)


def run_plan(
    steps: list[ProbeStep], *, target_path: Path = CONTAINED_2026, **plan_kwargs: Any
) -> SidecarResult:
    target = TargetDefinition.model_validate(yaml.safe_load(target_path.read_text()))
    assert target.container is not None
    plan = ProbePlan(
        run_id="pytest-capabilities",
        protocol_profile=target.protocol_profile,
        timeout_s=10.0,
        ready_timeout_s=60.0,
        scenario_steps=steps,
        **plan_kwargs,
    )
    run = SidecarRun(
        target.container,
        run_id="pytest-capabilities",
        launch_command=target.launch_command,
        env=target.env,
        repo_root=Path.cwd(),
        resolved_target=_resolved(target),
    )
    result = asyncio.run(run.execute(plan.to_json_dict()))
    assert result.cleanup.clean, result.cleanup.leaked
    return result


def inbound(result: SidecarResult, step_id: str) -> dict[str, Any]:
    for event in result.events:
        if event.get("direction") == "inbound" and event.get("step_id") == step_id:
            return event
    raise AssertionError(f"no inbound event for {step_id}: {result.events}")


def lifecycle_notes(result: SidecarResult, step_id: str) -> list[str]:
    return [
        str(event.get("note", ""))
        for event in result.events
        if event.get("direction") == "lifecycle" and event.get("step_id") == step_id
    ]


def post(step_id: str, **overrides: Any) -> ProbeStep:
    base: dict[str, Any] = {
        "step_id": step_id,
        "phase": ProbePhase.SCENARIO,
        "method": ProbeMethod.POST,
        "path": "/mcp",
        "headers": dict(ROUTING_HEADERS),
        "body_mode": ProbeBodyMode.JSON,
        "json_body": dict(TOOLS_LIST),
    }
    base.update(overrides)
    return ProbeStep.model_validate(base)


# ------------------------------------------------------------------- methods


@requires_docker
def test_post_get_and_delete_all_reach_the_target() -> None:
    """Three methods, three answers. GET and DELETE are otherwise unexercised."""
    result = run_plan(
        [
            post("post"),
            ProbeStep(
                step_id="get",
                phase=ProbePhase.SCENARIO,
                method=ProbeMethod.GET,
                path="/mcp",
            ),
            ProbeStep(
                step_id="delete",
                phase=ProbePhase.SCENARIO,
                method=ProbeMethod.DELETE,
                path="/mcp",
            ),
        ]
    )
    assert result.succeeded, f"{result.outcome}: {result.detail}"
    assert inbound(result, "post")["status"] == 200
    # This fixture offers no SSE stream and says so; what matters here is that a
    # GET was issued and its answer recorded, not which status it chose.
    assert inbound(result, "get")["status"] == 405
    assert inbound(result, "get")["method"] == "GET"
    assert inbound(result, "delete")["method"] == "DELETE"


@requires_docker
def test_an_exact_path_is_used_verbatim() -> None:
    result = run_plan([post("exact", path="/mcp?trace=1")])
    assert inbound(result, "exact")["path"] == "/mcp?trace=1"


# -------------------------------------------------------------------- bodies


@requires_docker
def test_an_empty_body_is_sent_as_an_empty_body() -> None:
    result = run_plan([post("empty", body_mode=ProbeBodyMode.NONE, json_body=None)])
    outbound = next(
        e for e in result.events if e.get("direction") == "outbound" and e.get("step_id") == "empty"
    )
    assert outbound["body_bytes"] == 0
    # The target answered rather than hanging on a bodyless POST.
    assert inbound(result, "empty")["status"] in (400, 200)


@requires_docker
def test_malformed_json_arrives_malformed() -> None:
    """A JSON round trip anywhere in the path would repair this."""
    payload = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{'
    result = run_plan(
        [post("malformed", body_mode=ProbeBodyMode.TEXT, json_body=None, text_body=payload)]
    )
    outbound = next(
        e
        for e in result.events
        if e.get("direction") == "outbound" and e.get("step_id") == "malformed"
    )
    assert outbound["body"] == payload
    assert outbound["body_bytes"] == len(payload.encode())
    answer = inbound(result, "malformed")
    assert answer["status"] == 400
    assert answer["message"]["error"]["code"] == -32700


@requires_docker
def test_raw_bytes_that_are_not_valid_utf8_survive_the_trip() -> None:
    """Base64 is the only mode that can carry a payload text cannot represent."""
    raw = b'{"jsonrpc":"2.0",\xff\xfe"id":1}'
    result = run_plan(
        [
            post(
                "raw",
                body_mode=ProbeBodyMode.BASE64,
                json_body=None,
                base64_body=base64.b64encode(raw).decode(),
            )
        ]
    )
    outbound = next(
        e for e in result.events if e.get("direction") == "outbound" and e.get("step_id") == "raw"
    )
    # The byte count is the claim; the text rendering is lossy by construction.
    assert outbound["body_bytes"] == len(raw)
    assert inbound(result, "raw")["status"] == 400


# ------------------------------------------------------ bounds and truncation


@requires_docker
def test_a_bounded_read_marks_the_body_truncated() -> None:
    """A hostile target must not be able to exhaust the sidecar by replying."""
    result = run_plan([post("capped", response_body_limit_bytes=16)])
    answer = inbound(result, "capped")
    assert answer["body_truncated"] is True
    assert answer["body_bytes"] <= 16


@requires_docker
def test_an_untruncated_body_is_not_marked_truncated() -> None:
    """The negative half: the flag has to mean something."""
    result = run_plan([post("whole")])
    assert inbound(result, "whole")["body_truncated"] is False


# ------------------------------------------------------------------- aborts


@requires_docker
def test_a_client_abort_after_the_request_is_recorded_and_survivable() -> None:
    """The target must not be wedged by a client that vanishes mid-exchange."""
    result = run_plan([post("abort", abort=ProbeAbort.AFTER_REQUEST), post("after")])
    assert result.succeeded, f"{result.outcome}: {result.detail}"
    assert any("aborted after request" in note for note in lifecycle_notes(result, "abort"))
    # The property that matters: the next request is answered normally.
    assert inbound(result, "after")["status"] == 200


@requires_docker
def test_a_client_abort_after_headers_records_the_status_but_no_body() -> None:
    result = run_plan([post("headers_only", abort=ProbeAbort.AFTER_HEADERS), post("after")])
    answer = inbound(result, "headers_only")
    assert answer["status"] == 200
    assert answer["body_bytes"] == 0
    assert answer["body_truncated"] is True
    assert answer["first_byte_ms"] is not None
    assert inbound(result, "after")["status"] == 200


@requires_docker
def test_a_client_abort_part_way_through_the_body_keeps_what_arrived() -> None:
    result = run_plan([post("partial", abort=ProbeAbort.AFTER_PARTIAL_BODY), post("after")])
    answer = inbound(result, "partial")
    assert answer["body_truncated"] is True
    assert 0 < answer["body_bytes"] < inbound(result, "after")["body_bytes"]


# -------------------------------------------------------------------- timing


@requires_docker
def test_a_delay_before_a_request_is_honoured() -> None:
    result = run_plan([post("prompt"), post("delayed", delay_before_ms=600.0)])
    prompt = next(
        e
        for e in result.events
        if e.get("direction") == "outbound" and e.get("step_id") == "prompt"
    )
    delayed = next(
        e
        for e in result.events
        if e.get("direction") == "outbound" and e.get("step_id") == "delayed"
    )
    assert delayed["at_ms"] - prompt["at_ms"] >= 500.0


@requires_docker
def test_a_slow_client_body_still_completes() -> None:
    """Chunked sending exercises the target's tolerance of a slow client."""
    result = run_plan([post("slow_send", body_chunk_delay_ms=250.0)])
    assert inbound(result, "slow_send")["status"] == 200


@requires_docker
def test_both_timing_measurements_are_captured() -> None:
    """One elapsed figure cannot separate a stalled header from a stalled body."""
    answer = inbound(run_plan([post("timed")]), "timed")
    assert answer["first_byte_ms"] is not None
    assert answer["body_complete_ms"] is not None
    assert answer["body_complete_ms"] >= answer["first_byte_ms"]


# --------------------------------------------------------------- connections


@requires_docker
def test_a_connection_is_reused_when_a_step_asks_for_one() -> None:
    result = run_plan(
        [
            post("first", keep_connection_open=True),
            post("second", reuse_connection=True),
        ]
    )
    assert result.succeeded, f"{result.outcome}: {result.detail}"
    second = next(
        e
        for e in result.events
        if e.get("direction") == "outbound" and e.get("step_id") == "second"
    )
    assert second["reused_connection"] is True
    assert inbound(result, "second")["status"] == 200


@requires_docker
def test_a_fresh_connection_is_opened_when_a_step_does_not_ask_for_reuse() -> None:
    result = run_plan([post("first", keep_connection_open=True), post("second")])
    second = next(
        e
        for e in result.events
        if e.get("direction") == "outbound" and e.get("step_id") == "second"
    )
    assert second["reused_connection"] is False


# ------------------------------------------------------- capture and reuse


@requires_docker
def test_a_header_is_captured_and_substituted_into_a_later_request() -> None:
    """The generic mechanism behind the 2025 session lifecycle.

    Exercised here on its own, against a target that genuinely rejects the later
    request without the captured value — so a substitution that silently did
    nothing could not pass.
    """
    target = TargetDefinition.model_validate(yaml.safe_load(CONTAINED_2025.read_text()))
    assert target.container is not None

    initialize = ProbeStep(
        step_id="init",
        phase=ProbePhase.SETUP,
        method=ProbeMethod.POST,
        path="/mcp",
        headers={"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"},
        body_mode=ProbeBodyMode.JSON,
        json_body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
        },
        captures=[
            ProbeCapture(
                variable="session",
                source=ProbeCaptureSource.RESPONSE_HEADER,
                header="Mcp-Session-Id",
                required=True,
            )
        ],
    )
    with_session = ProbeStep(
        step_id="with_session",
        phase=ProbePhase.SCENARIO,
        method=ProbeMethod.POST,
        path="/mcp",
        headers={"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"},
        body_mode=ProbeBodyMode.JSON,
        json_body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        substitutions=[
            ProbeSubstitution(
                variable="session",
                target=ProbeSubstitutionTarget.HEADER,
                header="Mcp-Session-Id",
                required=True,
            )
        ],
    )

    plan = ProbePlan(
        run_id="pytest-capture",
        protocol_profile="mcp-2025-11-25",
        timeout_s=10.0,
        ready_timeout_s=60.0,
        variables=[ProbeVariable(name="session", required=True)],
        setup_steps=[initialize],
        scenario_steps=[with_session],
    )
    run = SidecarRun(
        target.container,
        run_id="pytest-capture",
        launch_command=target.launch_command,
        env=target.env,
        repo_root=Path.cwd(),
        resolved_target=_resolved(target),
    )
    result = asyncio.run(run.execute(plan.to_json_dict()))

    assert result.succeeded, f"{result.outcome}: {result.detail}"
    assert result.probe_result["captured_variables"] == ["session"]
    # 200 rather than the 400 an unsubstituted request would receive.
    assert inbound(result, "with_session")["status"] == 200


@requires_docker
def test_a_required_capture_that_finds_nothing_stops_the_plan() -> None:
    """Running the scenario anyway would blame the target for missing setup."""
    target = TargetDefinition.model_validate(yaml.safe_load(CONTAINED_2026.read_text()))
    assert target.container is not None

    plan = ProbePlan(
        run_id="pytest-missing-capture",
        protocol_profile="mcp-2026-07-28",
        timeout_s=10.0,
        ready_timeout_s=60.0,
        variables=[ProbeVariable(name="absent", required=True)],
        setup_steps=[
            ProbeStep(
                step_id="setup",
                phase=ProbePhase.SETUP,
                method=ProbeMethod.POST,
                path="/mcp",
                headers=dict(ROUTING_HEADERS),
                body_mode=ProbeBodyMode.JSON,
                json_body=dict(TOOLS_LIST),
                captures=[
                    ProbeCapture(
                        variable="absent",
                        source=ProbeCaptureSource.RESPONSE_HEADER,
                        header="X-Header-This-Fixture-Never-Sends",
                        required=False,
                    )
                ],
            )
        ],
        scenario_steps=[post("never_runs")],
    )
    run = SidecarRun(
        target.container,
        run_id="pytest-missing-capture",
        launch_command=target.launch_command,
        env=target.env,
        repo_root=Path.cwd(),
        resolved_target=_resolved(target),
    )
    result = asyncio.run(run.execute(plan.to_json_dict()))

    assert result.outcome == "setup_incomplete", f"{result.outcome}: {result.detail}"
    assert "absent" in result.detail
    scenario_events = [e for e in result.events if e.get("phase") == "scenario"]
    assert scenario_events == [], "scenario traffic ran despite incomplete setup"
