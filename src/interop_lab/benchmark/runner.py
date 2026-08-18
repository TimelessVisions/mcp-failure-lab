"""Executing a pinned benchmark definition and collecting samples.

Correctness and timing are collected by two separate passes, and the separation
is deliberate rather than tidy. A correctness repetition exists to be compared
against its siblings by complete result signature; a timing repetition exists to
contribute a duration. Merging them would mean either timing the correctness
comparison or comparing signatures across runs that were shaped for speed, and
both produce numbers that answer neither question.

Timing uses ``time.perf_counter`` throughout — a monotonic clock. Wall-clock time
is subject to adjustment, and two samples measured across one would differ for a
reason that is not the operation.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from interop_lab.benchmark.model import (
    BenchmarkCategory,
    BenchmarkDefinition,
    BenchmarkSample,
    PinnedScenario,
    PinnedTarget,
)
from interop_lab.benchmark.validation import validate_sample
from interop_lab.models.core import Classification, RunResult
from interop_lab.naming import CLI_COMMAND
from interop_lab.results.signature import signature_for


@dataclass
class CommandOutcome:
    """What one subprocess actually did. Never inferred from a green exit."""

    exit_code: int
    duration_ms: float
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)


def run_command(argv: Sequence[str], *, cwd: Path, timeout_s: float) -> CommandOutcome:
    """Run one command on a monotonic clock, recording what it returned.

    A timeout is reported as an outcome with a sentinel exit code rather than
    raised: the sample is then rejected by the ordinary rules, with its duration
    intact, instead of taking the whole benchmark down.
    """
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return CommandOutcome(
            exit_code=124,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            stderr=f"the command exceeded the pinned bound of {timeout_s}s",
        )
    return CommandOutcome(
        exit_code=completed.returncode,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def tracked_tree_is_clean(repo_root: Path) -> bool:
    """Whether the tracked tree is byte-identical to the commit under test.

    Checked around every sample rather than once at the start. A benchmark that
    mutated its own inputs halfway through measured something the commit does not
    describe, and only a per-sample check can say which samples that affected.
    """
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def _runs_from_report(report_path: Path) -> tuple[list[RunResult], list[str]]:
    """Load a battery report's runs, returning what loaded and what did not.

    Both halves are returned rather than the first. A run that does not load is
    not a run that is absent: something wrote a report this harness cannot read,
    and dropping it silently would shrink a repetition's scenario set without
    anyone noticing — which is precisely the "scenario measured once and not
    again" instability the signature comparison exists to catch, hidden at the
    point where it would have been caught.
    """
    if not report_path.is_file():
        return [], [f"{report_path.name}: no report was written"]
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{report_path.name}: unreadable report: {exc}"]

    runs: list[RunResult] = []
    problems: list[str] = []
    for index, raw in enumerate(payload.get("runs", [])):
        try:
            runs.append(RunResult.model_validate(raw))
        except Exception as exc:  # a truncated or hand-edited report, not a target fault
            scenario = raw.get("scenario_id", f"<run {index}>") if isinstance(raw, dict) else index
            problems.append(
                f"{report_path.name}: run {scenario} does not load into the typed "
                f"model, so no sample can be built from it: {type(exc).__name__}: {exc}"
            )
    return runs, problems


def samples_from_report(
    report_path: Path,
    *,
    definition: BenchmarkDefinition,
    target: PinnedTarget,
    scenarios: dict[str, PinnedScenario],
    repetition: int,
    outcome: CommandOutcome,
    tracked_clean: bool,
    expected_capability_fingerprint: str | None = None,
    is_warmup: bool = False,
) -> tuple[list[BenchmarkSample], list[str]]:
    """Turn one battery report into one validated sample per scenario.

    The scenario fingerprint compared here is the one the definition pinned. It
    is not recomputed from the report: a report does not carry the scenario
    object, and a fingerprint derived from a partial view would look authoritative
    while describing something else. The consequence is that a scenario absent
    from the definition produces no sample at all, which is the honest outcome —
    the benchmark cannot say anything about a scenario it did not pin.
    """
    collected: list[BenchmarkSample] = []
    runs, problems = _runs_from_report(report_path)
    for run in runs:
        pinned = scenarios.get(run.scenario_id)
        if pinned is None:
            continue

        capabilities = run.resolved_capabilities
        sample = BenchmarkSample(
            sample_id=f"{target.target_key}:{run.scenario_id}:{repetition}",
            category=pinned.category,
            target_key=target.target_key,
            scenario_id=run.scenario_id,
            repetition=repetition,
            is_warmup=is_warmup,
            duration_ms=run.duration_ms,
            exit_code=outcome.exit_code,
            observed_artifacts=[str(report_path.name)],
            target_fingerprint=target.target_fingerprint,
            scenario_fingerprint=pinned.scenario_fingerprint,
            capability_fingerprint=(
                capabilities.capability_fingerprint if capabilities is not None else None
            ),
            classification=run.classification,
            dimensions=run.dimensions,
            containment_usable=run.is_usable_evidence,
            cleanup_ok=run.process.cleanup_ok,
            tracked_tree_clean=tracked_clean,
        )
        signature = signature_for(
            run,
            target_fingerprint=target.target_fingerprint,
            scenario_fingerprint=pinned.scenario_fingerprint,
        )
        sample.result_signature = signature
        sample.result_signature_digest = signature.digest

        collected.append(
            validate_sample(
                sample,
                definition,
                expected_target_fingerprint=target.target_fingerprint,
                expected_scenario_fingerprint=pinned.scenario_fingerprint,
                expected_capability_fingerprint=expected_capability_fingerprint,
            )
        )
    return collected, problems


def battery_argv(
    target: PinnedTarget,
    *,
    report_path: Path,
    artifacts_dir: Path,
    resolution: Path,
) -> list[str]:
    """The battery invocation, with the build resolution named explicitly.

    Named rather than defaulted: every external target is locally built, so a run
    without it measures nothing — the identity of the image to run is produced by
    the build and exists nowhere else.
    """
    return [
        "uv",
        "run",
        CLI_COMMAND,
        "run",
        "-t",
        target.target_file,
        "--suite",
        "all",
        "--json",
        str(report_path),
        "--artifacts",
        str(artifacts_dir),
        "--resolution",
        str(resolution),
    ]


def surfaces_from_report(report_path: Path) -> tuple[Any | None, Any | None]:
    """The capability surface and containment record one repetition observed.

    Both are already inside every battery report and neither survived into the
    benchmark outputs, so the private results recorded that a fingerprint existed
    without recording what it was a fingerprint *of*. A reader could see that a
    surface had been observed and not what the target advertised, and could see a
    containment outcome without the requested-versus-observed pair behind it.

    Taken from the first repetition that carries them. Later repetitions are held
    to the same capability fingerprint by the sample validation, so a surface that
    moved rejects those samples rather than silently replacing this record.

    Returns ``(None, None)`` for an unreadable report rather than raising: the
    samples are built by :func:`samples_from_report`, which reports that problem
    already, and raising here would turn one recorded problem into a crash.
    """
    runs, _ = _runs_from_report(report_path)
    capabilities = next(
        (run.resolved_capabilities for run in runs if run.resolved_capabilities), None
    )
    containment = next((run.containment for run in runs if run.containment), None)
    return capabilities, containment


@dataclass
class TargetExecution:
    """Everything one target produced across correctness and timing passes."""

    target: PinnedTarget
    correctness_samples: list[BenchmarkSample] = field(default_factory=list)
    suite_timing_samples: list[BenchmarkSample] = field(default_factory=list)
    capability_fingerprint: str | None = None
    #: The full observed surface behind ``capability_fingerprint``, and the
    #: containment record behind the samples' containment outcome. Both are
    #: recorded so a report can show what was observed rather than only that
    #: something was.
    capability_record: Any | None = None
    containment_record: Any | None = None
    problems: list[str] = field(default_factory=list)


def first_actionable_result_ms(samples: list[BenchmarkSample]) -> float | None:
    """Cumulative time to the first decided non-pass, in scenario order.

    A property of this suite's ordering rather than of the target, and the
    reporting layer says so. Returns ``None`` when the battery reached no
    actionable result at all, which is a legitimate outcome and not a zero.
    """
    elapsed = 0.0
    for sample in samples:
        if not sample.valid:
            continue
        elapsed += sample.duration_ms
        if sample.classification in (
            Classification.PASS,
            Classification.NOT_APPLICABLE,
            Classification.INCONCLUSIVE,
            Classification.HARNESS_ERROR,
        ):
            continue
        return elapsed
    return None


def execute_target(
    target: PinnedTarget,
    *,
    definition: BenchmarkDefinition,
    scenarios: dict[str, PinnedScenario],
    repo_root: Path,
    work_dir: Path,
    resolution: Path,
) -> TargetExecution:
    """Run the correctness repetitions and the timing repetitions for one target.

    One warm-up precedes the timing pass and is recorded with ``is_warmup`` set
    rather than discarded, so the exclusion is visible in the artifacts instead of
    being a claim in a document.
    """
    execution = TargetExecution(target=target)
    work_dir.mkdir(parents=True, exist_ok=True)

    def one_pass(repetition: int, *, label: str, is_warmup: bool = False) -> list[BenchmarkSample]:
        report_path = work_dir / f"{target.target_key}-{label}-{repetition}.json"
        artifacts_dir = work_dir / f"{target.target_key}-{label}-{repetition}"
        outcome = run_command(
            battery_argv(
                target,
                report_path=report_path,
                artifacts_dir=artifacts_dir,
                resolution=resolution,
            ),
            cwd=repo_root,
            timeout_s=definition.timing_bounds.per_sample_timeout_s,
        )
        clean = tracked_tree_is_clean(repo_root)
        if not clean:
            execution.problems.append(
                f"{target.target_key} {label} {repetition}: the tracked tree changed "
                "during this sample"
            )
        produced, report_problems = samples_from_report(
            report_path,
            definition=definition,
            target=target,
            scenarios=scenarios,
            repetition=repetition,
            outcome=outcome,
            tracked_clean=clean,
            expected_capability_fingerprint=execution.capability_fingerprint,
            is_warmup=is_warmup,
        )
        execution.problems.extend(
            f"{target.target_key} {label} {repetition}: {problem}" for problem in report_problems
        )
        if not produced:
            execution.problems.append(
                f"{target.target_key} {label} {repetition}: the battery produced no "
                f"usable report (exit {outcome.exit_code}). A run that measured nothing "
                "is not a measurement."
            )
        # The first surface observed in this execution becomes the one every later
        # sample is held to. Taken from a run rather than declared: a capability
        # fingerprint that moved mid-execution invalidates the applicability
        # decisions on both sides of the move.
        if execution.capability_fingerprint is None:
            for sample in produced:
                if sample.capability_fingerprint:
                    execution.capability_fingerprint = sample.capability_fingerprint
                    break
        if execution.capability_record is None or execution.containment_record is None:
            capabilities, containment = surfaces_from_report(report_path)
            execution.capability_record = execution.capability_record or capabilities
            execution.containment_record = execution.containment_record or containment
        return produced

    for repetition in range(1, definition.correctness_repetitions + 1):
        execution.correctness_samples.extend(one_pass(repetition, label="correctness"))

    for repetition in range(1, definition.timing_warmup_runs + 1):
        execution.suite_timing_samples.extend(one_pass(repetition, label="warmup", is_warmup=True))
    for repetition in range(1, definition.timing_measured_runs + 1):
        execution.suite_timing_samples.extend(one_pass(repetition, label="timing"))

    return execution


def suite_duration_samples(execution: TargetExecution) -> list[BenchmarkSample]:
    """One synthetic sample per timing repetition, holding the suite's total.

    Built by summing the per-scenario durations of a repetition rather than by
    timing the subprocess: the subprocess figure includes ``uv`` resolving an
    environment and the CLI starting, which are this harness's startup rather
    than the suite's execution. A repetition with any rejected sample yields no
    suite figure at all — a total assembled from a partial suite is not a total.
    """
    by_repetition: dict[int, list[BenchmarkSample]] = {}
    for sample in execution.suite_timing_samples:
        if sample.is_warmup:
            continue
        by_repetition.setdefault(sample.repetition, []).append(sample)

    totals: list[BenchmarkSample] = []
    for repetition, samples in sorted(by_repetition.items()):
        if not samples or any(not s.valid for s in samples):
            continue
        totals.append(
            BenchmarkSample(
                sample_id=f"{execution.target.target_key}:suite:{repetition}",
                category=BenchmarkCategory.VALID_BASELINE_BEHAVIOR,
                target_key=execution.target.target_key,
                repetition=repetition,
                duration_ms=sum(s.duration_ms for s in samples),
            )
        )
    return totals


def as_json(value: Any) -> Any:
    """Serialize pydantic models and plain values alike, for artifact writing."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [as_json(item) for item in value]
    if isinstance(value, dict):
        return {key: as_json(item) for key, item in value.items()}
    return value
