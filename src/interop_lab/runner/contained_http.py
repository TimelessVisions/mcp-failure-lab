"""The contained-HTTP strategy: one compiled plan, executed inside the boundary.

The harness never opens a socket to the target. It compiles the scenario into a
probe plan, starts an isolated network, starts the target and the probe on it,
verifies that both containers actually have the isolation that was requested,
waits for the probe, and then interprets what the probe recorded.

Everything after "interprets" is shared code. The assertions are the same
specifications, evaluated by the same ``evaluate`` function; the classification
is the same ``classify``; the trace is the same ``WireTrace``. A contained run
that produced a `PASS` did so through the identical machinery that grades a
stdio run, which is the only way the two results are comparable at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.assertions.checks import evaluate
from interop_lab.containment.docker import (
    ContainmentFailedError,
    ContainmentUnavailableError,
    stated_limits,
)
from interop_lab.containment.docker import (
    probe as docker_probe,
)
from interop_lab.containment.model import (
    ContainmentOutcome,
    ContainmentRecord,
    ExecutionMode,
)
from interop_lab.containment.resolution import ImageResolutionError, ResolvedImage
from interop_lab.containment.sidecar import (
    DEFAULT_PROBE_DIGEST,
    DEFAULT_PROBE_IMAGE,
    PROBE_CPUS,
    PROBE_MEMORY_MB,
    PROBE_PIDS_LIMIT,
    PROBE_USER,
    SidecarResult,
    SidecarRun,
)
from interop_lab.events.model import Channel, Direction, WireEvent, WireTrace
from interop_lab.models.core import (
    AssertionResult,
    ProcessOutcome,
    TargetDefinition,
    TimeoutCause,
)
from interop_lab.probe.compile import PlanCompilationError, compile_plan
from interop_lab.probe.events import build_trace, responses_by_step, step_timed_out
from interop_lab.profiles.registry import ProtocolProfile
from interop_lab.runner.images import ExecutionContext
from interop_lab.runner.strategy import ExecutionOutcome
from interop_lab.scenarios.model import Scenario

TRANSPORT = "streamable_http"

#: Lifecycle facts recorded on the trace, in the order they happen, so a reader
#: can see *how* the evidence was obtained rather than only what it contains. A
#: contained run has a setup story — a network was created, two containers were
#: started, both were inspected, everything was reclaimed — and none of that is
#: visible in the traffic itself.
#:
#: Asserted against a real run in `tests/integration/test_sidecar_containment.py`,
#: so a stage that stops being emitted fails rather than quietly vanishing from
#: every artifact.
LIFECYCLE_STAGES = (
    "docker available",
    "isolated network created",
    "target container created",
    "target ready",
    "probe container created",
    "probe started",
    "probe completed",
    "target stopped",
    "probe stopped",
    "network removed",
    "cleanup verified",
)


def _target_accepted(result: SidecarResult) -> bool:
    """Whether the probe ever got a connection to the target."""
    return any(
        "target accepting connections" in str(event.get("note", "")) for event in result.events
    )


class ContainedHttpPlanStrategy:
    """Execute a scenario as a single plan inside the containment boundary."""

    name = "contained_http_plan"

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root or Path.cwd()

    async def execute(
        self,
        scenario: Scenario,
        target: TargetDefinition,
        *,
        run_id: str,
        profile: ProtocolProfile,
        context: ExecutionContext,
    ) -> ExecutionOutcome:
        spec = target.container
        assert spec is not None, "select_strategy guarantees a container spec here"

        started_at = datetime.now(UTC)
        lifecycle: list[WireEvent] = []

        def note(text: str, **fields: Any) -> None:
            lifecycle.append(_lifecycle_event(len(lifecycle), started_at, text, **fields))

        # Checked before Docker, for the same reason as the stdio path: a
        # locally built target with no build resolution is a configuration error
        # on any machine, and a daemon message would misdirect the fix.
        try:
            context.resolver.require_resolvable(target)
        except ImageResolutionError as exc:
            note(f"image resolution failed: {exc}")
            return ExecutionOutcome(
                trace=build_trace(run_id, [], started_at=started_at, lifecycle=lifecycle),
                process=ProcessOutcome(),
                harness_error=f"ImageResolutionError: {exc}",
                containment=self.record_for(
                    target,
                    ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
                    unavailable_reason=str(exc),
                ),
            )

        # Docker availability is a lifecycle fact, and its absence is an
        # environment failure — never a host fallback and never a PASS.
        available, reason, _ = await docker_probe()
        if not available:
            note(f"docker unavailable: {reason}")
            return self._unavailable(target, run_id, lifecycle, started_at, reason=reason)
        note("docker available")

        # Resolved before the plan is compiled and long before a container
        # exists. A local build with no valid resolution stops here, as the
        # environment failure it is — not as a skipped test and not as a verdict.
        try:
            resolved = await context.resolver.resolve(target)
        except ImageResolutionError as exc:
            note(f"image resolution failed: {exc}")
            return ExecutionOutcome(
                trace=build_trace(run_id, [], started_at=started_at, lifecycle=lifecycle),
                process=ProcessOutcome(),
                harness_error=f"ImageResolutionError: {exc}",
                containment=self.record_for(
                    target,
                    ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
                    unavailable_reason=str(exc),
                ),
            )
        note(
            "image resolved",
            source=resolved.source_kind.value,
            reference=resolved.launch_reference,
            verification=resolved.verification.value,
        )

        try:
            compiled = compile_plan(scenario, target, run_id=run_id)
        except PlanCompilationError as exc:
            note(f"plan compilation refused: {exc}")
            return ExecutionOutcome(
                trace=build_trace(run_id, [], started_at=started_at, lifecycle=lifecycle),
                process=ProcessOutcome(),
                harness_error=f"PlanCompilationError: {exc}",
                containment=self.record_for(
                    target, ContainmentOutcome.CONTAINMENT_FAILED, unavailable_reason=str(exc)
                ),
            )

        run = SidecarRun(
            spec,
            run_id=run_id,
            launch_command=target.launch_command,
            env=target.env,
            repo_root=context.repo_root,
            resolved_target=resolved,
        )

        try:
            result = await run.execute(compiled.plan.to_json_dict())
        except (ContainmentUnavailableError, ContainmentFailedError) as exc:
            note(f"containment failed: {exc}")
            return ExecutionOutcome(
                trace=build_trace(run_id, [], started_at=started_at, lifecycle=lifecycle),
                process=ProcessOutcome(),
                harness_error=f"{type(exc).__name__}: {exc}",
                containment=self.record_for(
                    target, ContainmentOutcome.CONTAINMENT_FAILED, unavailable_reason=str(exc)
                ),
            )

        note(
            "isolated network created",
            network=result.network_observed.get("name"),
            internal=result.network_observed.get("internal"),
        )
        note("target container created", container=run.target_name)
        # Readiness is the probe's observation, not the harness's: the harness
        # never opens a socket to the target, so it has no way to know when the
        # target began accepting connections except by being told.
        note(
            "target ready" if _target_accepted(result) else "target never accepted a connection",
            observed_by="probe",
        )
        note("probe container created", container=run.probe_name)
        note("probe started")
        note(f"probe completed: {result.outcome}", detail=result.detail[:200])
        note("target stopped")
        note("probe stopped")
        note(
            "network removed" if result.cleanup.clean else "network removal incomplete",
            cleanup=result.cleanup.as_dict(),
        )
        note(
            "cleanup verified" if result.cleanup.clean else "cleanup FAILED",
            leaked=result.cleanup.leaked,
        )

        trace = build_trace(
            run_id,
            result.events,
            started_at=started_at,
            lifecycle=lifecycle,
            fault_by_step=compiled.fault_by_step or None,
        )

        containment = self.containment_from(target, result)
        process = process_outcome_for(result)

        # A run whose containment was not obtained says nothing about the
        # target, so its assertions are not graded at all: grading them would
        # attribute the harness's own failure to someone else's code.
        if not containment.is_usable_evidence:
            return ExecutionOutcome(
                trace=trace,
                process=process,
                containment=containment,
                faults_applied=compiled.faults_applied,
                harness_error=(
                    f"containment {containment.outcome.value}: "
                    + (containment.unavailable_reason or "; ".join(containment.weaknesses))
                ),
                extra_artifacts=self._artifacts(target, result, compiled.plan.to_json_dict()),
            )

        if result.harness_error is not None:
            return ExecutionOutcome(
                trace=trace,
                process=process,
                containment=containment,
                faults_applied=compiled.faults_applied,
                harness_error=result.harness_error,
                extra_artifacts=self._artifacts(target, result, compiled.plan.to_json_dict()),
            )

        assertions, timed_out, cause, expected_timeout = self.grade(scenario, compiled, trace)

        outcome = ExecutionOutcome(
            trace=trace,
            process=process,
            assertions=assertions,
            faults_applied=compiled.faults_applied,
            containment=containment,
            timed_out=timed_out,
            timeout_cause=cause,
            # The target lives in a container the probe cannot inspect. Claiming
            # to know whether its process was still alive at the deadline would
            # be an inference dressed as an observation, so it is left false and
            # a silent contained target is reported as TIMEOUT rather than HANG.
            process_alive_at_timeout=False,
            expected_timeout=expected_timeout,
            extra_artifacts=self._artifacts(target, result, compiled.plan.to_json_dict()),
        )
        if result.outcome not in ("completed", "setup_incomplete"):
            outcome.harness_error = f"probe {result.outcome}: {result.detail}"
        elif result.outcome == "setup_incomplete":
            outcome.harness_error = f"setup did not complete: {result.detail}"
        return outcome

    # ---------------------------------------------------------------- grading

    def grade(
        self, scenario: Scenario, compiled: Any, trace: WireTrace
    ) -> tuple[list[AssertionResult], bool, TimeoutCause | None, bool]:
        """Map probe events back to scenario steps and apply the same assertions.

        The mapping is by step id rather than by position: a dropped step and a
        step that produced no response are different situations, and positional
        matching would silently shift every later assertion onto the wrong event.
        """
        responses = responses_by_step(trace)
        assertions: list[AssertionResult] = []
        timed_out = False
        cause: TimeoutCause | None = None
        expected_timeout = False

        for step in scenario.steps:
            event = responses.get(step.step_id)
            request_id = compiled.request_ids.get(step.step_id)

            dropped = step.step_id in compiled.dropped_steps
            failed, raw_cause, transport_error = step_timed_out(trace, step.step_id)
            step_timed_out_here = dropped or failed or event is None

            if step_timed_out_here:
                if dropped:
                    # A harness-injected drop is the harness's deadline, not the
                    # target's silence. Attributing it to the target would blame
                    # a blameless implementation.
                    cause = TimeoutCause.HARNESS_DEADLINE
                elif transport_error is not None:
                    cause = TimeoutCause.TARGET_NO_RESPONSE
                elif raw_cause is not None:
                    cause = _timeout_cause(raw_cause)
                else:
                    cause = TimeoutCause.TARGET_NO_RESPONSE
                if step.timeout_is_expected:
                    expected_timeout = True
                else:
                    timed_out = True

            for spec in step.assertions:
                assertions.append(
                    evaluate(
                        spec,
                        event=event,
                        request_id=request_id,
                        timed_out=step_timed_out_here,
                        timeout_expected=step.timeout_is_expected,
                    )
                )

            if timed_out:
                break  # A hung target invalidates every later step.

        return assertions, timed_out, cause, expected_timeout

    # ----------------------------------------------------------- containment

    def containment_from(
        self, target: TargetDefinition, result: SidecarResult
    ) -> ContainmentRecord:
        weaknesses = [*result.target_weaknesses, *result.probe_weaknesses]

        if result.outcome == "containment_failed":
            return self.record_for(
                target,
                ContainmentOutcome.CONTAINMENT_FAILED,
                unavailable_reason=result.detail,
                resolved_image=result.resolved_target,
            )
        if result.outcome == "target_image_identity_mismatch":
            # The container was confined perfectly and is running the wrong
            # bits. Recorded as containment that cannot be used as evidence,
            # which is what makes the executor report HARNESS_ERROR.
            return self.record_for(
                target,
                ContainmentOutcome.CONTAINMENT_WEAKER_THAN_REQUESTED,
                weaknesses=weaknesses or [result.detail],
                observed=result.target_observed,
                resolved_image=result.resolved_target,
            )
        if weaknesses:
            return self.record_for(
                target,
                ContainmentOutcome.CONTAINMENT_WEAKER_THAN_REQUESTED,
                weaknesses=weaknesses,
                observed=result.target_observed,
                resolved_image=result.resolved_target,
            )
        return self.record_for(
            target,
            ContainmentOutcome.CONTAINED,
            observed=result.target_observed,
            resolved_image=result.resolved_target,
        )

    def record_for(
        self,
        target: TargetDefinition,
        outcome: ContainmentOutcome,
        *,
        weaknesses: list[str] | None = None,
        unavailable_reason: str | None = None,
        observed: Any = None,
        resolved_image: ResolvedImage | None = None,
    ) -> ContainmentRecord:
        spec = target.container
        return ContainmentRecord(
            mode=ExecutionMode.CONTAINER,
            provenance=target.provenance,
            outcome=outcome,
            requested=spec,
            observed=observed,
            resolved_image=resolved_image,
            weaknesses=weaknesses or [],
            unavailable_reason=unavailable_reason,
            stated_limits=stated_limits(spec) if spec is not None else [],
        )

    def _unavailable(
        self,
        target: TargetDefinition,
        run_id: str,
        lifecycle: list[WireEvent],
        started_at: datetime,
        *,
        reason: str,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            trace=build_trace(run_id, [], started_at=started_at, lifecycle=lifecycle),
            process=ProcessOutcome(),
            containment=self.record_for(
                target,
                ContainmentOutcome.CONTAINMENT_UNAVAILABLE,
                unavailable_reason=(
                    f"containment was requested but {reason}. The harness does not fall "
                    "back to host execution, and a contained HTTP target has no host path "
                    "to fall back to in any case."
                ),
            ),
            harness_error=f"containment unavailable: {reason}",
        )

    def _artifacts(
        self, target: TargetDefinition, result: SidecarResult, plan: dict[str, Any]
    ) -> dict[str, object]:
        """Evidence about how the run was executed, persisted beside the trace.

        Four separate records, not two. Requested and observed are different
        claims for *each* container, and a single "containment" blob would let a
        reader assume the flags that were passed describe the isolation that
        applied. The probe gets the same treatment as the target because it is
        the process that parses the untrusted bytes.
        """
        return {
            "probe_plan.json": plan,
            "sidecar.json": {
                "probe_outcome": result.outcome,
                "probe_detail": result.detail,
                "probe_result": result.probe_result,
                "probe_identity": (
                    result.probe_identity.as_dict() if result.probe_identity else None
                ),
                "target_requested_containment": (
                    target.container.model_dump(mode="json", exclude_none=True)
                    if target.container
                    else None
                ),
                "target_observed_containment": (
                    result.target_observed.model_dump(mode="json", exclude_none=True)
                    if result.target_observed
                    else None
                ),
                "target_weaknesses": result.target_weaknesses,
                "target_resolved_image": (
                    result.resolved_target.model_dump(mode="json", exclude_none=True)
                    if result.resolved_target
                    else None
                ),
                "probe_requested_containment": {
                    "image_source": {
                        "kind": "registry",
                        "repository": DEFAULT_PROBE_IMAGE,
                        "repository_digest": DEFAULT_PROBE_DIGEST,
                    },
                    "user": PROBE_USER,
                    "memory_mb": PROBE_MEMORY_MB,
                    "cpus": PROBE_CPUS,
                    "pids_limit": PROBE_PIDS_LIMIT,
                    "read_only_root": True,
                    "drop_all_capabilities": True,
                    "no_new_privileges": True,
                    "published_host_port": None,
                },
                "probe_observed_containment": (
                    result.probe_observed.model_dump(mode="json", exclude_none=True)
                    if result.probe_observed
                    else None
                ),
                "probe_weaknesses": result.probe_weaknesses,
                "network_observed": result.network_observed,
                "output_channel": result.output_channel,
                "cleanup": result.cleanup.as_dict(),
            },
        }


def process_outcome_for(result: SidecarResult) -> ProcessOutcome:
    """The target's process outcome, as far as a contained run can know it.

    Cleanup is folded in here rather than reported separately, because
    ``classify`` already treats ``cleanup_ok`` as decisive: a run that leaked a
    container or a network leaves the machine in a state the next run inherits,
    so its own evidence cannot be trusted either.
    """
    outcome = ProcessOutcome()
    outcome.cleanup_ok = result.cleanup.clean
    if not result.cleanup.clean:
        outcome.cleanup_detail = result.cleanup.detail()
    return outcome


def _timeout_cause(raw: str) -> TimeoutCause:
    try:
        return TimeoutCause(raw)
    except ValueError:
        return TimeoutCause.TARGET_NO_RESPONSE


def _lifecycle_event(seq: int, started_at: datetime, note: str, **fields: Any) -> WireEvent:
    detail = "; ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, [], {}))
    return WireEvent(
        seq=seq,
        monotonic_ns=0,
        wall_clock=started_at.isoformat(),
        direction=Direction.LIFECYCLE,
        channel=Channel.PROCESS,
        transport=TRANSPORT,
        note=f"{note} ({detail})" if detail else note,
    )
