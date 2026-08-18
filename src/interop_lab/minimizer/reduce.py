"""Automatic failure reduction.

Given a scenario that fails against a target, find the smallest scenario that
still fails the same way. Reduction is staged from coarse to fine, and a
candidate is only accepted if it reproduces the original *failure signature* —
the classification together with the identities of the assertions that failed.

Matching on the classification alone is not enough, and getting this wrong
produces confidently wrong reproductions: a reduction that deletes a tool name
turns a "missing required member" violation into an "unknown tool" error, which
carries the same SPEC_VIOLATION label while no longer demonstrating the defect.

A minimised reproduction is only declared valid when it reproduces that same
signature on three consecutive independent runs.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any

from interop_lab.models.core import AssertionStatus, Classification, RunResult, TargetDefinition
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.scenarios.model import Scenario, Step

#: Number of consecutive confirming runs required before calling a repro stable.
STABILITY_RUNS = 3

#: What "the same failure" means: the classification plus the identities of the
#: assertions that failed, in order.
FailureSignature = tuple[Classification, tuple[str, ...]]


def signature_of(result: RunResult) -> FailureSignature:
    failing = tuple(a.assertion_id for a in result.assertions if a.status is AssertionStatus.FAIL)
    return (result.classification, failing)


@dataclass
class MinimizationReport:
    scenario_id: str
    target_id: str
    classification: Classification

    original_event_count: int
    minimized_event_count: int
    original_body_bytes: int
    minimized_body_bytes: int
    original_header_override_bytes: int
    minimized_header_override_bytes: int
    original_mutation_surface_bytes: int
    minimized_mutation_surface_bytes: int

    candidate_runs: int = 0
    duration_s: float = 0.0
    stable: bool = False
    stability_runs: int = 0
    stages_applied: list[str] = field(default_factory=list)
    #: Assertion ids that failed in the original run and still fail in the
    #: minimised one. Recorded so a reviewer can confirm the reduction did not
    #: drift to a different defect that happens to share the classification.
    preserved_failing_assertions: list[str] = field(default_factory=list)

    @property
    def already_minimal(self) -> bool:
        """True when nothing could be removed.

        Reported explicitly so an already-minimal reproduction is never dressed
        up as a successful reduction.
        """
        return not self.stages_applied and (
            self.minimized_mutation_surface_bytes == self.original_mutation_surface_bytes
            and self.minimized_event_count == self.original_event_count
        )

    @property
    def reduced(self) -> bool:
        return (
            self.minimized_mutation_surface_bytes < self.original_mutation_surface_bytes
            or self.minimized_event_count < self.original_event_count
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "target_id": self.target_id,
            "classification": self.classification.value,
            "original_event_count": self.original_event_count,
            "minimized_event_count": self.minimized_event_count,
            "original_body_bytes": self.original_body_bytes,
            "minimized_body_bytes": self.minimized_body_bytes,
            "original_header_override_bytes": self.original_header_override_bytes,
            "minimized_header_override_bytes": self.minimized_header_override_bytes,
            "original_mutation_surface_bytes": self.original_mutation_surface_bytes,
            "minimized_mutation_surface_bytes": self.minimized_mutation_surface_bytes,
            "candidate_runs": self.candidate_runs,
            "duration_s": round(self.duration_s, 3),
            "stability_runs": self.stability_runs,
            "stable": self.stable,
            "stages_applied": self.stages_applied,
            "preserved_failing_assertions": self.preserved_failing_assertions,
            "already_minimal": self.already_minimal,
            "reduced": self.reduced,
        }


def body_bytes(scenario: Scenario) -> int:
    """Bytes the scenario puts in request bodies."""
    import json

    total = 0
    for step in scenario.steps:
        total += len(json.dumps(step.params, sort_keys=True))
        if step.raw_payload:
            total += len(step.raw_payload)
    return total


def header_override_bytes(scenario: Scenario) -> int:
    """Bytes the scenario puts in explicit HTTP header overrides."""
    import json

    return sum(
        len(json.dumps(step.http_header_overrides, sort_keys=True))
        for step in scenario.steps
        if step.http_header_overrides
    )


def mutation_surface_bytes(scenario: Scenario) -> int:
    """Everything the minimiser is allowed to touch, measured together.

    Reported alongside the body and header figures so a reduction in one cannot
    be presented as a reduction overall.
    """
    return body_bytes(scenario) + header_override_bytes(scenario)


def shrink_value(value: Any) -> Any | None:
    """Return a strictly smaller version of ``value``, or ``None`` if minimal."""
    if isinstance(value, str) and len(value) > 1:
        return value[: len(value) // 2]
    if isinstance(value, list) and value:
        return value[: len(value) // 2]
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value not in (0, 1):
        return 0 if abs(value) <= 1 else value // 2
    if isinstance(value, float) and value != 0.0:
        return 0.0
    if isinstance(value, dict) and value:
        keys = sorted(value)
        reduced = dict(value)
        reduced.pop(keys[-1])
        return reduced
    return None


def _walk_paths(payload: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(payload, dict):
        for key in sorted(payload):
            path = f"{prefix}.{key}" if prefix else key
            paths.append(path)
            paths.extend(_walk_paths(payload[key], path))
    return paths


def _get_path(payload: Any, path: str) -> Any:
    cursor = payload
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _set_path(payload: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    updated = copy.deepcopy(payload)
    cursor: Any = updated
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return updated
        cursor = cursor[part]
    if isinstance(cursor, dict):
        cursor[parts[-1]] = value
    return updated


def _drop_path(payload: dict[str, Any], path: str) -> dict[str, Any]:
    updated = copy.deepcopy(payload)
    cursor: Any = updated
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return updated
        cursor = cursor[part]
    if isinstance(cursor, dict):
        cursor.pop(parts[-1], None)
    return updated


class Minimizer:
    """Delta-debugging style reducer over the declarative scenario structure."""

    def __init__(self, executor: ScenarioExecutor, target: TargetDefinition) -> None:
        self.executor = executor
        self.target = target
        self._runs = 0

    async def _signature(self, scenario: Scenario) -> FailureSignature:
        self._runs += 1
        result, _ = await self.executor.run(scenario, self.target, persist=False)
        return signature_of(result)

    async def _reproduces(self, scenario: Scenario, expected: FailureSignature) -> bool:
        """A candidate is only accepted if it fails *the same way*.

        Matching on classification alone is not sufficient. A reduction can
        easily reach the same label by a different route — for example, deleting
        a tool name turns a "missing required member" violation into an "unknown
        tool" error, which is still SPEC_VIOLATION but no longer demonstrates the
        original defect. Comparing the failing assertion identities keeps the
        minimised reproduction faithful to the finding it came from.
        """
        return await self._signature(scenario) == expected

    async def minimize(
        self, scenario: Scenario, expected: Classification
    ) -> tuple[Scenario, MinimizationReport]:
        started = time.perf_counter()
        self._runs = 0

        # Capture the full failure signature from the unreduced scenario; every
        # candidate is judged against this, not against the label alone.
        target_signature = await self._signature(scenario)

        report = MinimizationReport(
            scenario_id=scenario.scenario_id,
            target_id=self.target.target_id,
            classification=expected,
            original_event_count=len(scenario.steps),
            minimized_event_count=len(scenario.steps),
            original_body_bytes=body_bytes(scenario),
            minimized_body_bytes=body_bytes(scenario),
            original_header_override_bytes=header_override_bytes(scenario),
            minimized_header_override_bytes=header_override_bytes(scenario),
            original_mutation_surface_bytes=mutation_surface_bytes(scenario),
            minimized_mutation_surface_bytes=mutation_surface_bytes(scenario),
        )

        current = scenario

        # Stage 1 — delete optional steps, largest index first so earlier steps
        # keep their meaning while later ones are trimmed.
        removed_any = False
        index = len(current.steps) - 1
        while index >= 0:
            if not current.steps[index].optional:
                index -= 1
                continue
            candidate_steps: list[Step] = [s for i, s in enumerate(current.steps) if i != index]
            if candidate_steps and await self._reproduces(
                current.with_steps(candidate_steps), target_signature
            ):
                current = current.with_steps(candidate_steps)
                removed_any = True
            index -= 1
        if removed_any:
            report.stages_applied.append("remove_optional_steps")

        # Stage 2 — drop parameter fields that are not needed to reproduce.
        dropped_any = False
        for step_index, step in enumerate(current.steps):
            for path in _walk_paths(step.params):
                if _get_path(step.params, path) is None:
                    continue
                candidate_params = _drop_path(step.params, path)
                if candidate_params == step.params:
                    continue
                candidate_steps = list(current.steps)
                candidate_steps[step_index] = step.model_copy(update={"params": candidate_params})
                if await self._reproduces(current.with_steps(candidate_steps), target_signature):
                    current = current.with_steps(candidate_steps)
                    step = current.steps[step_index]
                    dropped_any = True
        if dropped_any:
            report.stages_applied.append("remove_payload_fields")

        # Stage 2b — drop HTTP header overrides the scenario marked reducible.
        #
        # Only explicitly-nominated headers are candidates. A header override is
        # normally the condition under test, and deleting it yields a scenario
        # whose assertions still fail — for a completely different reason — so
        # the failure signature is preserved while the reproduction becomes a
        # reproduction of something else. The signature check cannot detect that,
        # so constitutive headers are excluded by construction rather than by
        # hoping the check catches it.
        header_dropped = False
        for step_index, step in enumerate(current.steps):
            for header in sorted(step.reducible_headers):
                step = current.steps[step_index]
                if header not in step.http_header_overrides:
                    continue
                candidate_headers = {
                    k: v for k, v in step.http_header_overrides.items() if k != header
                }
                candidate_steps = list(current.steps)
                candidate_steps[step_index] = step.model_copy(
                    update={"http_header_overrides": candidate_headers}
                )
                if await self._reproduces(current.with_steps(candidate_steps), target_signature):
                    current = current.with_steps(candidate_steps)
                    header_dropped = True
        if header_dropped:
            report.stages_applied.append("remove_header_overrides")

        # Stage 3 — shrink remaining values toward their minimum.
        shrunk_any = False
        for step_index, step in enumerate(current.steps):
            for path in _walk_paths(step.params):
                for _ in range(8):  # bounded: each pass at least halves the value
                    step = current.steps[step_index]
                    value = _get_path(step.params, path)
                    smaller = shrink_value(value)
                    if smaller is None or smaller == value:
                        break
                    candidate_params = _set_path(step.params, path, smaller)
                    candidate_steps = list(current.steps)
                    candidate_steps[step_index] = step.model_copy(
                        update={"params": candidate_params}
                    )
                    if await self._reproduces(
                        current.with_steps(candidate_steps), target_signature
                    ):
                        current = current.with_steps(candidate_steps)
                        shrunk_any = True
                    else:
                        break
        if shrunk_any:
            report.stages_applied.append("shrink_payload_values")

        # Stability: the minimised scenario must reproduce N consecutive times.
        confirmations = 0
        for _ in range(STABILITY_RUNS):
            if await self._reproduces(current, target_signature):
                confirmations += 1
            else:
                break

        report.minimized_event_count = len(current.steps)
        report.minimized_body_bytes = body_bytes(current)
        report.minimized_header_override_bytes = header_override_bytes(current)
        report.minimized_mutation_surface_bytes = mutation_surface_bytes(current)
        report.candidate_runs = self._runs
        report.duration_s = time.perf_counter() - started
        report.stability_runs = confirmations
        report.stable = confirmations == STABILITY_RUNS
        report.preserved_failing_assertions = list(target_signature[1])

        return current, report
