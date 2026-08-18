"""Direct measurement of the harness operations that have a clean boundary.

Several timing dimensions the methodology names were previously absent from the
outputs, and the honest reason differs per dimension. Some have no boundary at
all — target startup happens inside a battery subprocess whose single duration
covers thirty-two scenarios, and no amount of arithmetic recovers it. Others have
a perfectly clean boundary that simply nobody had timed: a resolver call, a
session teardown, a serialisation, a gate evaluation, a replay, a minimisation.

This module times the second group and nothing else. Each operation is a real
call at a real boundary, entered and left inside one ``time.perf_counter``
window, with everything that is *not* the operation — building the input,
starting the process, writing the artifact — outside it. Where that separation
could not be achieved honestly, the operation is not here, and
:mod:`interop_lab.benchmark.coverage` reports the metric NOT_MEASURED.

Two properties are deliberate:

**First-party fixtures only.** Every measurement here runs against code this
repository wrote. None of it touches a pinned external target, so none of these
figures is a statement about anyone else's implementation, and the coverage
ledger says so for every one of them.

**Failure degrades, never propagates.** An operation that raises produces a
NOT_MEASURED record carrying the exception, because a benchmark that aborted
because a timing probe failed would lose the correctness results it exists for.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.benchmark.model import MIN_TIMING_SAMPLES, MeasurementState

#: The fixtures every measurement here runs against. First-party, committed, and
#: chosen because their defects are ones this repository put there deliberately.
REFERENCE_TARGET = "targets/fixtures/reference.yaml"
FAULTY_TARGET = "targets/fixtures/faulty-result-type.yaml"
FAULTY_SCENARIO = "version.result_type_required"
GATE_SUITE = "smoke"


class OperationMeasurement(BaseModel):
    """One boundary, timed, with everything needed to judge the number.

    Mirrors :class:`~interop_lab.benchmark.model.TimingSummary` deliberately —
    median, range and every individual sample, and no confidence interval — but
    carries the metric id so the coverage ledger can bind an entry to it without
    matching on a display string.
    """

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    display_name: str
    unit: str = "ms"
    state: MeasurementState = MeasurementState.MEASURED
    boundary: str
    environment: str

    warmup_count: int = 0
    #: Warm-up durations, recorded rather than discarded so the exclusion is
    #: visible in the artifact instead of being a claim in a document.
    warmup_samples_ms: list[float] = Field(default_factory=list)
    samples_ms: list[float] = Field(default_factory=list)
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    #: Set when the operation could not be measured, or measured enough times.
    insufficient_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class MeasuredOperations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_samples: int = MIN_TIMING_SAMPLES
    clock: str = "time.perf_counter (monotonic)"
    operations: list[OperationMeasurement] = Field(default_factory=list)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Every operation here runs against first-party fixtures. None of these "
            "figures is a measurement of, or a statement about, any pinned external "
            "implementation.",
            "Each figure describes the boundary named in its `boundary` field and "
            "nothing wider. No duration here was obtained by subtracting one "
            "measurement from another.",
            "Median with the full range and every individual sample. No confidence "
            "interval and no significance claim: a handful of samples cannot "
            "support either.",
        ]
    )


async def _timed(operation: Callable[[], Awaitable[None]]) -> float:
    started = time.perf_counter()
    await operation()
    return (time.perf_counter() - started) * 1000.0


def _summarise(measurement: OperationMeasurement, *, minimum_samples: int) -> OperationMeasurement:
    values = sorted(measurement.samples_ms)
    measurement.samples_ms = values
    if len(values) < minimum_samples:
        measurement.state = MeasurementState.NOT_MEASURED
        measurement.insufficient_reason = (
            measurement.insufficient_reason
            or f"{len(values)} valid sample(s), and this benchmark requires "
            f"{minimum_samples} before quoting a headline timing. The samples are "
            "recorded; no median is reported from them."
        )
        return measurement
    measurement.state = MeasurementState.MEASURED
    measurement.median_ms = statistics.median(values)
    measurement.min_ms = values[0]
    measurement.max_ms = values[-1]
    return measurement


async def _sample_loop(
    measurement: OperationMeasurement,
    *,
    setup: Callable[[], Awaitable[Callable[[], Awaitable[None]]]],
    repetitions: int,
    warmups: int,
    minimum_samples: int,
) -> OperationMeasurement:
    """Run one operation ``warmups + repetitions`` times, timing only the operation.

    ``setup`` prepares one iteration and returns the callable to time. Everything
    it does — building an input, launching a fixture, loading a package — is
    outside the window by construction, which is the whole reason the boundary is
    expressed this way rather than as a flag inside the timed function.
    """
    measurement.warmup_count = warmups
    try:
        for index in range(warmups + repetitions):
            operation = await setup()
            duration = await _timed(operation)
            if index < warmups:
                measurement.warmup_samples_ms.append(duration)
            else:
                measurement.samples_ms.append(duration)
    except Exception as exc:  # a probe failing must not take the benchmark down
        measurement.insufficient_reason = (
            f"the operation raised {type(exc).__name__}: {exc}. Nothing was "
            "measured, and this is a fact about this environment rather than "
            "about any implementation."
        )
    return _summarise(measurement, minimum_samples=minimum_samples)


# ------------------------------------------------------------------ operations


async def _measure_discovery_and_cleanup(
    *, repo_root: Path, repetitions: int, warmups: int, minimum_samples: int
) -> list[OperationMeasurement]:
    """Capability discovery and session teardown, at two separate boundaries.

    Discovery is timed as one whole ``resolve()`` — launch, initialize,
    tools/list, teardown — with a *fresh* resolver each sample so its per-target
    cache cannot answer in place of the target. Cleanup is timed separately, with
    the session started outside the window, because the two questions are
    different and a single duration covering both would answer neither.
    """
    from interop_lab.capabilities.resolve import CapabilityResolver
    from interop_lab.config.loader import load_target
    from interop_lab.transports.stdio.session import StdioSession

    target = load_target(repo_root / REFERENCE_TARGET)

    discovery = OperationMeasurement(
        metric_id="capability_discovery",
        display_name="Capability discovery",
        boundary=(
            "CapabilityResolver.resolve() end to end, with a fresh resolver per "
            "sample; the target definition is loaded outside the window."
        ),
        environment="First-party stdio reference fixture, host execution.",
        limitations=[
            "Covers launch, initialize, tools/list and teardown together. It is "
            "not a startup measurement and startup is not recoverable from it.",
            "A fixture this repository wrote, not a pinned external target.",
        ],
    )

    async def discovery_setup() -> Callable[[], Awaitable[None]]:
        resolver = CapabilityResolver()

        async def operation() -> None:
            await resolver.resolve(target)

        return operation

    await _sample_loop(
        discovery,
        setup=discovery_setup,
        repetitions=repetitions,
        warmups=warmups,
        minimum_samples=minimum_samples,
    )

    cleanup = OperationMeasurement(
        metric_id="cleanup",
        display_name="Cleanup",
        boundary=(
            "StdioSession.close() — process-tree termination and reclamation — "
            "with start() outside the window."
        ),
        environment="First-party stdio reference fixture, host execution, uncontained.",
        limitations=[
            "Host cleanup of a first-party fixture. Reclaiming a container is a "
            "different operation and this figure is not evidence about it.",
        ],
    )

    async def cleanup_setup() -> Callable[[], Awaitable[None]]:
        session = StdioSession(target, run_id="coverage.cleanup")
        await session.start()

        async def operation() -> None:
            await session.close()

        return operation

    await _sample_loop(
        cleanup,
        setup=cleanup_setup,
        repetitions=repetitions,
        warmups=warmups,
        minimum_samples=minimum_samples,
    )
    return [discovery, cleanup]


async def _measure_report_generation(
    *, repo_root: Path, work_dir: Path, repetitions: int, warmups: int, minimum_samples: int
) -> OperationMeasurement:
    """Serialising one report to JSON, JUnit and SARIF.

    The runs are executed once, outside every window. What is timed is the pure
    function from an in-memory report to its three serialised forms, which is the
    only part of "report generation" that is not target execution wearing a
    different name.
    """
    from interop_lab.config.loader import load_target
    from interop_lab.reporting import junit, sarif
    from interop_lab.reporting.render import RunReport
    from interop_lab.runner.executor import ScenarioExecutor
    from interop_lab.scenarios.library import get_suite

    measurement = OperationMeasurement(
        metric_id="report_generation",
        display_name="Report generation",
        boundary=(
            "RunReport -> JSON, JUnit and SARIF. A pure function of an in-memory "
            "report; the runs behind it are executed once, outside the window."
        ),
        environment="Host process, no subprocess and no network.",
        limitations=[
            "Scales with the number of runs serialised. The figure describes the "
            "report used here and is not a constant.",
        ],
    )

    target = load_target(repo_root / REFERENCE_TARGET)
    executor = ScenarioExecutor(artifacts_dir=work_dir / "report-generation", seed=0)
    runs = []
    for scenario in get_suite(GATE_SUITE):
        run, _ = await executor.run(scenario, target, persist=False)
        runs.append(run)
    report = RunReport(suite=GATE_SUITE, runs=runs)

    async def setup() -> Callable[[], Awaitable[None]]:
        async def operation() -> None:
            report.model_dump_json(indent=2)
            junit.to_string(report.runs, suite_name=report.suite)
            sarif.build(report.runs)

        return operation

    return await _sample_loop(
        measurement,
        setup=setup,
        repetitions=repetitions,
        warmups=warmups,
        minimum_samples=minimum_samples,
    )


async def _measure_ci_gate(
    *, repo_root: Path, work_dir: Path, repetitions: int, warmups: int, minimum_samples: int
) -> OperationMeasurement:
    """One gate evaluation against a baseline every entry of which matches.

    The baseline is built from the very runs it is then compared against. That is
    not circular for a *latency* measurement — it is what makes it representative:
    a baseline whose entries did not match would short-circuit most of the
    comparison and time a gate nobody runs.
    """
    from interop_lab.ci.baseline import build_baseline
    from interop_lab.ci.gate import evaluate
    from interop_lab.config.loader import load_target
    from interop_lab.runner.executor import ScenarioExecutor
    from interop_lab.scenarios.library import get_suite

    measurement = OperationMeasurement(
        metric_id="ci_gate",
        display_name="CI gate evaluation",
        boundary=(
            "ci.gate.evaluate() against a loaded baseline and loaded runs. The "
            "matrix execution that produces the runs is outside the window."
        ),
        environment="Host process, no subprocess and no network.",
        limitations=[
            "Excludes running the matrix, which dominates the wall time a CI job "
            "actually spends. Scales with the number of baseline entries.",
        ],
    )

    target = load_target(repo_root / REFERENCE_TARGET)
    scenarios = get_suite(GATE_SUITE)
    executor = ScenarioExecutor(artifacts_dir=work_dir / "ci-gate", seed=0)
    runs = []
    for scenario in scenarios:
        run, _ = await executor.run(scenario, target, persist=False)
        runs.append((run, scenario, target))

    baseline = build_baseline(
        baseline_id="coverage-ci-gate",
        reason="Built inside a latency measurement; never written to disk and never approved.",
        created_by="mcp-failure-lab benchmark measure",
        suite=GATE_SUITE,
        scenarios=scenarios,
        targets=[target],
        runs=runs,
    )

    async def setup() -> Callable[[], Awaitable[None]]:
        async def operation() -> None:
            evaluate(baseline=baseline, runs=runs, artifacts_root=work_dir / "ci-gate")

        return operation

    return await _sample_loop(
        measurement,
        setup=setup,
        repetitions=repetitions,
        warmups=warmups,
        minimum_samples=minimum_samples,
    )


async def _measure_minimization_and_replay(
    *, repo_root: Path, work_dir: Path, repetitions: int, warmups: int, minimum_samples: int
) -> list[OperationMeasurement]:
    """Minimisation and replay, against an approved synthetic fixture.

    The fixture's defect is one this repository put there on purpose, which is
    what makes measuring it legitimate: a third-party failure reduced repeatedly
    to produce a timing figure would be executing someone else's defect six times
    for a number.
    """
    from interop_lab.config.loader import load_target
    from interop_lab.minimizer.reduce import Minimizer
    from interop_lab.models.core import Classification
    from interop_lab.replay.repro import ReplayOutcome, replay, write_repro
    from interop_lab.runner.executor import ScenarioExecutor
    from interop_lab.scenarios.library import get_scenario

    target = load_target(repo_root / FAULTY_TARGET)
    scenario = get_scenario(FAULTY_SCENARIO)

    minimization = OperationMeasurement(
        metric_id="minimization",
        display_name="Minimisation",
        boundary=(
            "Minimizer.minimize() from the reproducing failure to the reduced "
            "case whose signature still matches. The reproducing run that "
            "establishes the expected classification is outside the window."
        ),
        environment="Approved first-party synthetic fixture with a deliberate defect.",
        limitations=[
            "Cost is dominated by how many candidate reductions the search has to "
            "execute, which is a property of the failure rather than of the tool.",
        ],
    )

    async def minimization_setup() -> Callable[[], Awaitable[None]]:
        executor = ScenarioExecutor(artifacts_dir=work_dir / "minimization", seed=0)
        first, _ = await executor.run(scenario, target, persist=False)
        expected = first.classification

        async def operation() -> None:
            await Minimizer(executor, target).minimize(scenario, expected)

        return operation

    await _sample_loop(
        minimization,
        setup=minimization_setup,
        repetitions=repetitions,
        warmups=warmups,
        minimum_samples=minimum_samples,
    )

    replay_measurement = OperationMeasurement(
        metric_id="replay",
        display_name="Replay",
        boundary=(
            "replay() over a written reproduction package, from loading the "
            "package to a replay outcome. Writing the package is outside the window."
        ),
        environment="Approved first-party synthetic reproduction package.",
        limitations=[
            "Replay duration depends on the scenario being replayed and does not "
            "generalise across packages.",
        ],
    )

    package_root = work_dir / "replay"
    executor = ScenarioExecutor(artifacts_dir=package_root, seed=0)
    run, trace = await executor.run(scenario, target)
    if run.classification in (Classification.PASS, Classification.NOT_APPLICABLE):
        replay_measurement.insufficient_reason = (
            f"the fixture returned {run.classification.value}, so there is no "
            "reproduction to replay and nothing was timed"
        )
        _summarise(replay_measurement, minimum_samples=minimum_samples)
        return [minimization, replay_measurement]

    repro_path = (
        write_repro(
            package_root,
            finding_id="COVERAGE-REPLAY",
            scenario=scenario,
            target=target,
            run=run,
            trace=trace,
            seed=0,
        )
        / "repro.yaml"
    )

    # Confirm, once and outside every window, that the package actually
    # reproduces. A replay that fails to load returns in microseconds, and five
    # samples of that would be a fast, precise measurement of nothing.
    verification = await replay(repro_path, artifacts_dir=package_root / "verify")
    if verification.outcome is not ReplayOutcome.REPRODUCED:
        replay_measurement.insufficient_reason = (
            f"the package did not reproduce ({verification.outcome.value}: "
            f"{verification.detail}), so no replay was timed. Timing a replay that "
            "did not replay would measure the failure path."
        )
        _summarise(replay_measurement, minimum_samples=minimum_samples)
        return [minimization, replay_measurement]

    async def replay_setup() -> Callable[[], Awaitable[None]]:
        async def operation() -> None:
            await replay(repro_path, artifacts_dir=package_root / "replays")

        return operation

    await _sample_loop(
        replay_measurement,
        setup=replay_setup,
        repetitions=repetitions,
        warmups=warmups,
        minimum_samples=minimum_samples,
    )
    return [minimization, replay_measurement]


def measure_operations(
    *,
    repo_root: Path,
    work_dir: Path,
    repetitions: int = MIN_TIMING_SAMPLES,
    warmups: int = 1,
) -> MeasuredOperations:
    """Time every operation that has a clean boundary. Never raises.

    A failure anywhere degrades that operation to NOT_MEASURED with the reason
    recorded, and leaves the others alone. The benchmark this runs inside exists
    for its correctness results, and losing those because a timing probe could
    not start a fixture would be the wrong trade by a wide margin.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    minimum = MIN_TIMING_SAMPLES
    document = MeasuredOperations(minimum_samples=minimum)

    async def drive() -> list[OperationMeasurement]:
        collected: list[OperationMeasurement] = []
        collected.extend(
            await _measure_discovery_and_cleanup(
                repo_root=repo_root,
                repetitions=repetitions,
                warmups=warmups,
                minimum_samples=minimum,
            )
        )
        collected.append(
            await _measure_report_generation(
                repo_root=repo_root,
                work_dir=work_dir,
                repetitions=repetitions,
                warmups=warmups,
                minimum_samples=minimum,
            )
        )
        collected.append(
            await _measure_ci_gate(
                repo_root=repo_root,
                work_dir=work_dir,
                repetitions=repetitions,
                warmups=warmups,
                minimum_samples=minimum,
            )
        )
        collected.extend(
            await _measure_minimization_and_replay(
                repo_root=repo_root,
                work_dir=work_dir,
                repetitions=repetitions,
                warmups=warmups,
                minimum_samples=minimum,
            )
        )
        return collected

    try:
        document.operations = asyncio.run(drive())
    except Exception as exc:  # the whole probe failed; the benchmark continues
        document.operations = [
            OperationMeasurement(
                metric_id=metric_id,
                display_name=metric_id.replace("_", " ").title(),
                state=MeasurementState.NOT_MEASURED,
                boundary="not reached",
                environment="not reached",
                insufficient_reason=(
                    f"the measurement driver raised {type(exc).__name__}: {exc} "
                    "before this operation ran"
                ),
            )
            for metric_id in (
                "capability_discovery",
                "cleanup",
                "report_generation",
                "ci_gate",
                "minimization",
                "replay",
            )
        ]
    return document


def as_dict(document: MeasuredOperations) -> dict[str, Any]:
    return document.model_dump(mode="json")
