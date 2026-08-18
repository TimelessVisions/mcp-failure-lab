"""Scenario execution against a single target.

Produces a ``RunResult`` and a ``WireTrace``. The executor never decides that a
target is defective on its own — it collects evidence and hands classification
to ``classify``, which keeps the failure vocabulary in one place.

*How* the evidence is collected is a strategy (see ``runner.strategy``): a live
interactive session for stdio and host HTTP, a compiled plan inside a probe
sidecar for contained HTTP. The workflow here is identical either way —
applicability, execute, classify, record runtime, persist — because the point of
the strategy boundary is that only the evidence-gathering differs.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.artifacts.runtime import build_runtime_record
from interop_lab.capabilities.model import (
    CapabilityResolutionStatus,
    ResolvedTargetCapabilities,
)
from interop_lab.capabilities.resolve import (
    CapabilityResolver,
    apply_declared_fallback,
    unresolved,
)
from interop_lab.events.model import WireTrace
from interop_lab.models.core import (
    AssertionResult,
    AssertionStatus,
    Classification,
    RunResult,
    RunStatus,
    TargetDefinition,
    TimeoutCause,
    ToolDeclarationPolicy,
)
from interop_lab.profiles.registry import get_profile
from interop_lab.results.derive import derive_dimensions
from interop_lab.runner.images import ExecutionContext
from interop_lab.runner.strategy import (
    ExecutionOutcome,
    ScenarioExecutionStrategy,
    StrategySelectionError,
    select_strategy,
)
from interop_lab.scenarios.model import Scenario


def new_run_id(scenario_id: str, target_id: str) -> str:
    return f"{scenario_id}__{target_id}__{uuid.uuid4().hex[:8]}"


def transport_feature_gap(
    required: dict[str, Any], capabilities: ResolvedTargetCapabilities
) -> str | None:
    """Why the observed transport cannot support this scenario, or ``None``.

    The reason is quoted from the observation rather than composed from the
    requirement, because a NOT_APPLICABLE whose reason nobody measured is
    indistinguishable from one that was invented.
    """
    observed = capabilities.observed_transport_features
    if observed is None:
        return (
            f"the scenario requires observed transport features {sorted(required)}, and no "
            f"transport observation was made against this target (capability resolution "
            f"is {capabilities.status.value}). Whether the target has them is unknown, "
            "which is not the same as knowing it does not."
        )

    gaps: list[str] = []
    for name, wanted in sorted(required.items()):
        actual = getattr(observed, name, None)
        if actual == wanted:
            continue
        gaps.append(
            f"{name} was observed as {actual!r}, and the scenario needs {wanted!r}"
            if actual is not None
            else f"{name} was not observed at all, and the scenario needs {wanted!r}"
        )
    if not gaps:
        return None
    return "; ".join(gaps)


@dataclass(frozen=True)
class SafeInvocationVerdict:
    """Whether an approved invocation may be performed, and why not if not."""

    blocked: bool
    classification: Classification = Classification.PASS
    reason: str = ""


def safe_invocation_verdict(
    target: TargetDefinition, capabilities: ResolvedTargetCapabilities
) -> SafeInvocationVerdict:
    """Confirm the approved invocation against what the target actually advertises.

    Four outcomes, and each is a different statement:

    * No contract on the target. NOT_APPLICABLE — nobody approved a call here,
      which is a fact about this repository and not about the server.
    * Capabilities unusable. INCONCLUSIVE, never NOT_APPLICABLE: reporting a skip
      would state that the target lacks the tool, and nobody looked.
    * Tool absent from the observed surface. NOT_APPLICABLE, with the surface
      quoted, because that reason *was* observed.
    * Tool present with a different input schema. INCONCLUSIVE. The arguments in
      the contract were written for a contract the target no longer has, and
      sending them anyway would produce a failure attributable to this project.
    """
    invocation = target.safe_tool_invocation
    if invocation is None:
        return SafeInvocationVerdict(
            blocked=True,
            classification=Classification.NOT_APPLICABLE,
            reason=(
                f"target {target.target_id!r} declares no approved safe tool invocation, "
                "and this harness does not synthesise a call from an advertised schema"
            ),
        )

    if not capabilities.is_usable:
        return SafeInvocationVerdict(
            blocked=True,
            classification=Classification.INCONCLUSIVE,
            reason=(
                f"capability resolution is {capabilities.status.value}: "
                f"{capabilities.status_detail or 'no detail'}. Whether the target still "
                f"advertises {invocation.tool_name!r} with the approved input schema was "
                "not observed, so the approved call is not made and no verdict about the "
                "target is available."
            ),
        )

    observed = {tool.name: tool for tool in capabilities.tools}
    tool = observed.get(invocation.tool_name)
    if tool is None:
        return SafeInvocationVerdict(
            blocked=True,
            classification=Classification.NOT_APPLICABLE,
            reason=(
                f"target does not advertise the approved safe tool "
                f"{invocation.tool_name!r} (advertised: {capabilities.tool_names})"
            ),
        )

    if not invocation.schema_agrees(tool.input_schema_sha256):
        return SafeInvocationVerdict(
            blocked=True,
            classification=Classification.INCONCLUSIVE,
            reason=(
                f"tool {invocation.tool_name!r} is advertised with input schema "
                f"{tool.input_schema_sha256}, and the approved invocation was written "
                f"against {invocation.expected_input_schema_sha256}. The approved "
                "arguments describe a contract this target no longer has, so the call is "
                "not made: a failure produced by sending them would be attributable to "
                "this repository rather than to the server."
            ),
        )

    return SafeInvocationVerdict(blocked=False)


def bind_safe_invocation(scenario: Scenario, target: TargetDefinition) -> Scenario:
    """Fill every safe-invocation step with the target's approved call."""
    invocation = target.safe_tool_invocation
    assert invocation is not None, "safe_invocation_verdict guarantees one here"

    bound = [
        step.model_copy(
            update={
                "params": {
                    "name": invocation.tool_name,
                    "arguments": dict(invocation.arguments),
                }
            }
        )
        if step.use_safe_tool_invocation
        else step
        for step in scenario.steps
    ]
    return scenario.with_steps(bound)


def classify(
    assertions: list[AssertionResult],
    *,
    timed_out: bool,
    timeout_cause: TimeoutCause | None,
    process_alive_at_timeout: bool,
    exit_code: int | None,
    killed_by_harness: bool,
    cleanup_ok: bool,
    expected_timeout: bool,
) -> tuple[Classification, TimeoutCause | None]:
    """Reduce evidence to a single primary classification.

    Ordering matters: a crash explains a missing response, so process outcomes
    are considered before assertion failures.
    """
    if not cleanup_ok:
        return Classification.CLEANUP_FAILURE, timeout_cause

    # A target that exited non-zero without being killed by us has crashed,
    # regardless of what the assertions saw.
    if exit_code is not None and exit_code != 0 and not killed_by_harness:
        return Classification.CRASH, timeout_cause

    if timed_out and not expected_timeout:
        # Distinguish "still running but silent" from "gone".
        if process_alive_at_timeout:
            return Classification.HANG, timeout_cause or TimeoutCause.TARGET_NO_RESPONSE
        return Classification.TIMEOUT, timeout_cause or TimeoutCause.TRANSPORT_DEADLINE

    failures = [a for a in assertions if a.status is AssertionStatus.FAIL]
    if failures:
        # The first failing assertion's own mapping wins.
        first = failures[0]
        return (first.classification or Classification.SPEC_VIOLATION), timeout_cause

    if any(a.status is AssertionStatus.INCONCLUSIVE for a in assertions):
        return Classification.INCONCLUSIVE, timeout_cause

    if not assertions:
        return Classification.INCONCLUSIVE, timeout_cause

    return Classification.PASS, timeout_cause


class ScenarioExecutor:
    """Executes one scenario against one target, over whichever path applies."""

    def __init__(
        self,
        *,
        artifacts_dir: Path,
        seed: int = 0,
        strategy: ScenarioExecutionStrategy | None = None,
        capability_resolver: CapabilityResolver | None = None,
        capabilities: ResolvedTargetCapabilities | None = None,
        context: ExecutionContext | None = None,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.seed = seed
        #: How a target's image is resolved, passed to every strategy. Defaulted
        #: rather than required so a host-only or stubbed caller needs no
        #: ceremony; the default carries no build resolution, so a locally built
        #: target reaches a named error instead of a surprise.
        self.context = context or ExecutionContext.create()
        #: Overriding the strategy is for tests that need a stub. Production
        #: callers leave it unset so routing stays a property of the target,
        #: never of the caller.
        self._strategy_override = strategy
        #: Resolved once per target and reused across the matrix, so a battery of
        #: thirty scenarios starts one discovery container per target rather than
        #: thirty.
        self._capability_resolver = capability_resolver or CapabilityResolver(
            artifacts_dir=artifacts_dir, context=self.context
        )
        #: A caller may supply capabilities directly — a unit test with a stub
        #: strategy has no target to discover. Supplying them is an explicit act;
        #: nothing here invents them.
        self._capability_override = capabilities

    def strategy_for(self, target: TargetDefinition) -> ScenarioExecutionStrategy:
        return self._strategy_override or select_strategy(target)

    async def capabilities_for(self, target: TargetDefinition) -> ResolvedTargetCapabilities:
        """What this target advertises, discovered once and cached."""
        if self._capability_override is not None:
            return self._capability_override
        if self._strategy_override is not None:
            # A stubbed strategy is not the target, so discovering through it
            # would record whatever the stub returns as the target's surface.
            # Reported as not attempted rather than guessed at.
            return unresolved(
                target,
                status=CapabilityResolutionStatus.NOT_ATTEMPTED,
                detail=(
                    "the executor was constructed with a strategy override, so no "
                    "discovery was run against the real target"
                ),
            )
        return await self._capability_resolver.resolve(target)

    async def run(
        self, scenario: Scenario, target: TargetDefinition, *, persist: bool = True
    ) -> tuple[RunResult, WireTrace]:
        profile = get_profile(target.protocol_profile)
        run_id = new_run_id(scenario.scenario_id, target.target_id)
        started = datetime.now(UTC)
        t0 = time.perf_counter()

        # Cheap gates first. Profile, transport and role are properties of the
        # target definition and cannot change by asking the target, so deciding
        # them before starting a container avoids paying for a discovery run
        # whose answer could not affect the outcome.
        declarative = scenario.definition.applicability.reason_not_applicable_declaratively(
            spec_version=profile.spec_version,
            profile_id=profile.profile_id,
            transport=target.transport,
            role=target.role,
        )
        if declarative is not None:
            result = self._skeleton(run_id, scenario, target, profile.spec_version, started, t0)
            result.status = RunStatus.NOT_APPLICABLE
            result.classification = Classification.NOT_APPLICABLE
            result.not_applicable_reason = declarative
            return result, WireTrace(run_id=run_id)

        # Tool prerequisites are decided from what the target advertises, never
        # from what its file declares. `expected_tools` is an expectation that
        # can be wrong in both directions, and has been: one target declared an
        # empty list while advertising a full surface, another declared a tool
        # its pinned release does not register.
        capabilities = await self.capabilities_for(target)
        capabilities = apply_declared_fallback(capabilities, target)
        result_capabilities = capabilities
        requires_tools = scenario.definition.applicability.requires_tools

        # A declaration that disagrees with the target is a fact about the target
        # file, never about the target. Under `fail` it stops the run as the
        # configuration error it is; it is never allowed to change which tools
        # the applicability gate below sees.
        if (
            capabilities.is_usable
            and capabilities.has_declaration_mismatch
            and target.tool_declaration_policy is ToolDeclarationPolicy.FAIL
        ):
            result = self._skeleton(run_id, scenario, target, profile.spec_version, started, t0)
            result.status = RunStatus.HARNESS_ERROR
            result.classification = Classification.HARNESS_ERROR
            result.harness_error = (
                f"target {target.target_id!r} declares tools that disagree with what it "
                f"advertises, and its policy is fail. Declared but not advertised: "
                f"{capabilities.declared_but_not_advertised}. Advertised but not "
                f"declared: {capabilities.advertised_but_not_declared}. This is a "
                "target-file error, not a finding about the implementation."
            )
            result.resolved_capabilities = result_capabilities
            return result, WireTrace(run_id=run_id)

        if requires_tools and not capabilities.is_usable:
            # The prerequisite cannot be checked. Reporting NOT_APPLICABLE here
            # would invent a reason ("the target lacks the tool") that was never
            # observed, and PASS would be worse. INCONCLUSIVE is what this is.
            result = self._skeleton(run_id, scenario, target, profile.spec_version, started, t0)
            result.status = RunStatus.HARNESS_ERROR
            result.classification = Classification.INCONCLUSIVE
            result.harness_error = (
                f"capability resolution is {capabilities.status.value}: "
                f"{capabilities.status_detail or 'no detail'}. The scenario requires "
                f"{requires_tools}, and whether the target advertises them was not "
                "observed, so no verdict about the target is available."
            )
            result.resolved_capabilities = result_capabilities
            return result, WireTrace(run_id=run_id)

        not_applicable = scenario.definition.applicability.reason_not_applicable(
            spec_version=profile.spec_version,
            profile_id=profile.profile_id,
            transport=target.transport,
            role=target.role,
            available_tools=capabilities.tool_names,
        )
        if not_applicable is not None:
            result = self._skeleton(run_id, scenario, target, profile.spec_version, started, t0)
            result.status = RunStatus.NOT_APPLICABLE
            result.classification = Classification.NOT_APPLICABLE
            result.not_applicable_reason = not_applicable
            result.resolved_capabilities = result_capabilities
            return result, WireTrace(run_id=run_id)

        required_features = scenario.definition.applicability.requires_transport_features
        if required_features:
            reason = transport_feature_gap(required_features, capabilities)
            if reason is not None:
                result = self._skeleton(run_id, scenario, target, profile.spec_version, started, t0)
                result.resolved_capabilities = result_capabilities
                if capabilities.observed_transport_features is None:
                    # Nothing was observed, so "the target does not do this" is
                    # not something anyone established.
                    result.status = RunStatus.HARNESS_ERROR
                    result.classification = Classification.INCONCLUSIVE
                    result.harness_error = reason
                else:
                    result.status = RunStatus.NOT_APPLICABLE
                    result.classification = Classification.NOT_APPLICABLE
                    result.not_applicable_reason = reason
                return result, WireTrace(run_id=run_id)

        if scenario.definition.applicability.requires_safe_tool_invocation:
            verdict = safe_invocation_verdict(target, capabilities)
            if verdict.blocked:
                result = self._skeleton(run_id, scenario, target, profile.spec_version, started, t0)
                result.resolved_capabilities = result_capabilities
                if verdict.classification is Classification.NOT_APPLICABLE:
                    result.status = RunStatus.NOT_APPLICABLE
                    result.classification = Classification.NOT_APPLICABLE
                    result.not_applicable_reason = verdict.reason
                else:
                    result.status = RunStatus.HARNESS_ERROR
                    result.classification = verdict.classification
                    result.harness_error = verdict.reason
                return result, WireTrace(run_id=run_id)
            # Concrete arguments from here on, so the trace, the compiled plan
            # and the reproduction all record the call that was actually made.
            scenario = bind_safe_invocation(scenario, target)

        try:
            strategy = self.strategy_for(target)
            outcome = await strategy.execute(
                scenario, target, run_id=run_id, profile=profile, context=self.context
            )
        except StrategySelectionError as exc:
            # No strategy, and deliberately no substitute. Running the scenario
            # some other way would produce evidence about a configuration nobody
            # asked to measure.
            result = self._skeleton(run_id, scenario, target, profile.spec_version, started, t0)
            result.status = RunStatus.HARNESS_ERROR
            result.classification = Classification.HARNESS_ERROR
            result.harness_error = f"StrategySelectionError: {exc}"
            return result, WireTrace(run_id=run_id)

        finished = datetime.now(UTC)
        duration_ms = (time.perf_counter() - t0) * 1000

        result = RunResult(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.definition.version,
            target_id=target.target_id,
            protocol_profile=target.protocol_profile,
            spec_version=profile.spec_version,
            transport=target.transport,
            seed=self.seed,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=duration_ms,
            assertions=outcome.assertions,
            process=outcome.process,
            resolved_target=target,
            faults_applied=outcome.faults_applied,
            execution_strategy=strategy.name,
        )
        result.containment = outcome.containment
        # Carried on every completed run, not only the ones a prerequisite
        # filtered: the fingerprint is what lets a later reader see that an
        # upstream tool-surface change happened between two baselines.
        result.resolved_capabilities = result_capabilities

        # A run whose requested containment was not obtained is a harness or
        # environment failure, never a verdict about the target. Grading its
        # assertions would attribute the harness's own failure to someone else's
        # code — and a PASS here would be the worst possible outcome, because it
        # would claim isolation that never existed.
        containment = result.containment
        if containment is not None and not containment.is_usable_evidence:
            result.status = RunStatus.HARNESS_ERROR
            result.classification = Classification.HARNESS_ERROR
            result.harness_error = outcome.harness_error or (
                f"containment {containment.outcome.value}: "
                + (containment.unavailable_reason or "; ".join(containment.weaknesses))
            )
        elif outcome.harness_error is not None:
            result.status = RunStatus.HARNESS_ERROR
            result.classification = Classification.HARNESS_ERROR
            result.harness_error = outcome.harness_error
        else:
            classification, cause = classify(
                outcome.assertions,
                timed_out=outcome.timed_out,
                timeout_cause=outcome.timeout_cause,
                process_alive_at_timeout=outcome.process_alive_at_timeout,
                exit_code=outcome.process.exit_code,
                killed_by_harness=outcome.process.killed_by_harness,
                cleanup_ok=outcome.process.cleanup_ok,
                expected_timeout=outcome.expected_timeout,
            )
            result.classification = classification
            result.timeout_cause = cause if outcome.timed_out or outcome.expected_timeout else None

        # Derived last, from the classification the branches above settled on, so
        # a run whose containment failed or whose harness errored carries no
        # security dimension at all. Those results decided nothing about the
        # target, and a security reading of a non-verdict would be a finding
        # invented out of a missing measurement.
        result.dimensions = derive_dimensions(
            scenario,
            classification=result.classification,
            assertions=result.assertions,
            usable_evidence=result.is_usable_evidence,
        )

        result.runtime = build_runtime_record(
            protocol_profile=target.protocol_profile,
            transport=target.transport.value,
            language=target.language,
            declared_runtime_version=target.runtime_version,
            target_package=target.package_name,
            declared_package_version=target.version,
            target_commit_sha=target.commit_sha,
        )
        result.environment_warnings = result.runtime.warnings()

        if persist:
            self._persist(run_id, result, outcome.trace, outcome)
        # Always an artifact URI: the artifact must resolve on another machine.
        result.trace_path = f"artifact://runs/{run_id}/wire.jsonl"

        return result, outcome.trace

    def _skeleton(
        self,
        run_id: str,
        scenario: Scenario,
        target: TargetDefinition,
        spec_version: str,
        started: datetime,
        t0: float,
    ) -> RunResult:
        return RunResult(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.definition.version,
            target_id=target.target_id,
            protocol_profile=target.protocol_profile,
            spec_version=spec_version,
            transport=target.transport,
            seed=self.seed,
            started_at=started.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=(time.perf_counter() - t0) * 1000,
            resolved_target=target,
        )

    def _persist(
        self,
        run_id: str,
        result: RunResult,
        trace: WireTrace,
        outcome: ExecutionOutcome | None = None,
    ) -> Path:
        run_dir = self.artifacts_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        trace_path = run_dir / "wire.jsonl"
        trace_path.write_text(trace.to_jsonl(), encoding="utf-8")
        (run_dir / "run.json").write_text(
            result.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
        )
        # Strategy-shaped evidence: the resolved probe plan, the probe's
        # identity, the observed network. Persisted beside the trace because
        # "what the target did" and "how we were able to see it" are both part
        # of the record, and only one of them fits the typed models.
        for name, payload in (outcome.extra_artifacts if outcome else {}).items():
            (run_dir / name).write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
        return trace_path
