"""The measurement-coverage ledger: every promised metric, and its real state.

The Private 0.3 methodology names more timing dimensions than the execution
actually measures. That gap is not a defect on its own — measuring a duration
requires a boundary, and several of these do not have one. What *would* be a
defect is letting the un-measured dimensions quietly disappear from a report, so
that a reader counts the timings they can see and concludes that is the whole
set.

So every promised metric is listed here, always, with an explicit state:

* ``MEASURED`` — a boundary exists, it was timed on a monotonic clock, and there
  are at least :data:`HEADLINE_MINIMUM_SAMPLES` valid samples behind the headline
  figure.
* ``PARTIALLY_MEASURED`` — something real was timed, and it is narrower than the
  metric's name implies: too few samples for a headline figure, one environment
  where the metric spans two, or first-party fixtures where the metric is about
  the pinned external targets.
* ``NOT_MEASURED`` — nobody looked. Never a failure, never a zero, and never
  omitted.
* ``NOT_APPLICABLE`` — the metric has no subject in this configuration.

Two rules decide the states, and both exist because the tempting shortcut is the
dishonest one:

**A metric is not measured because a nearby operation happened during a larger
timed command.** The battery subprocess starts a target, discovers its surface
and runs thirty-two scenarios inside one duration. That duration measures the
battery. It is not evidence about startup.

**Startup and discovery are never derived by subtraction.** Two durations whose
difference has the right units is not a measurement of the thing in between.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.benchmark.model import MIN_TIMING_SAMPLES, MeasurementState

#: Version of the ledger document, carried into every export.
COVERAGE_SCHEMA_VERSION = "1"

#: Valid measured samples required before a metric may quote a headline figure.
HEADLINE_MINIMUM_SAMPLES = MIN_TIMING_SAMPLES


class CoverageState(StrEnum):
    """What is actually known about one promised metric.

    A deliberately separate enum from :class:`MeasurementState`, which describes
    one artifact's timing summary. This describes a *promise*: the methodology
    named a dimension, and this says how far that naming was honoured.
    """

    MEASURED = "measured"
    PARTIALLY_MEASURED = "partially_measured"
    NOT_MEASURED = "not_measured"
    NOT_APPLICABLE = "not_applicable"


class MeasurementCategory(StrEnum):
    """What kind of work the metric times. Grouping for a reader, nothing more."""

    BUILD = "build"
    DISCOVERY = "discovery"
    EXECUTION = "execution"
    ANALYSIS = "analysis"
    GATE = "gate"
    CONTAINMENT = "containment"
    REPRODUCIBILITY = "reproducibility"


class MeasurementCoverageEntry(BaseModel):
    """One promised metric, and everything a reader needs to judge it."""

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    display_name: str
    category: MeasurementCategory
    state: CoverageState

    #: Valid measured samples behind the figure, excluding warm-ups. Zero
    #: whenever the state is NOT_MEASURED — an un-measured metric has no samples,
    #: and a non-zero count here would invite a reader to look for a number.
    sample_count: int = 0
    minimum_required_samples: int = HEADLINE_MINIMUM_SAMPLES
    warmup_count: int = 0

    #: The artifact this entry is derived from, relative to the benchmark output
    #: directory. ``None`` when nothing was written, which is the only honest
    #: value for a metric nobody measured.
    artifact_reference: str | None = None
    #: The boundary that was timed, in enough detail to argue with.
    measurement_method: str
    #: Where it was timed. A duration without one of these is not comparable to
    #: anything.
    environment: str
    #: What the figure does not establish. Never empty — every one of these reads
    #: stronger than the evidence behind it.
    limitation: str
    #: Why nobody measured it. Required whenever the state is not MEASURED.
    reason_not_measured: str | None = None

    @model_validator(mode="after")
    def _an_absent_measurement_states_why(self) -> MeasurementCoverageEntry:
        if self.state is not CoverageState.MEASURED and not self.reason_not_measured:
            raise ValueError(
                f"{self.metric_id} is {self.state.value} and records no reason. A gap "
                "without a reason reads as an oversight, and a reader cannot tell it "
                "from one."
            )
        if self.state is CoverageState.NOT_MEASURED and self.sample_count:
            raise ValueError(
                f"{self.metric_id} is not_measured and claims {self.sample_count} "
                "sample(s). Nobody looked; there is nothing to have sampled."
            )
        if (
            self.state is CoverageState.MEASURED
            and self.sample_count < self.minimum_required_samples
        ):
            raise ValueError(
                f"{self.metric_id} is measured with {self.sample_count} sample(s), below "
                f"the {self.minimum_required_samples} this benchmark requires before a "
                "headline figure. It is partially_measured, not measured."
            )
        if not self.limitation:
            raise ValueError(f"{self.metric_id} records no limitation")
        return self


#: Every metric the Private 0.3 methodology names as a timing dimension.
#:
#: The ledger must cover exactly this set. A metric added to the methodology and
#: not to this tuple would be measured and never reported; one removed from the
#: tuple and left in the methodology would be promised and never explained.
REQUIRED_METRIC_IDS: tuple[str, ...] = (
    "clean_image_build",
    "cached_image_build",
    "capability_discovery",
    "target_startup",
    "first_valid_request",
    "applicable_suite_runtime",
    "time_to_first_actionable_result",
    "replay",
    "minimization",
    "report_generation",
    "ci_gate",
    "cleanup",
    "containment_overhead_stdio",
    "containment_overhead_http_2025",
    "containment_overhead_http_2026",
    "cross_environment_result_signatures",
)


class MeasurementCoverage(BaseModel):
    """The whole ledger. Complete by construction, or it does not validate."""

    model_config = ConfigDict(extra="forbid")

    coverage_schema_version: str = COVERAGE_SCHEMA_VERSION
    entries: list[MeasurementCoverageEntry] = Field(default_factory=list)
    #: What the ledger as a whole does not establish.
    limitations: list[str] = Field(
        default_factory=lambda: [
            "A state of MEASURED describes the boundary named in measurement_method "
            "and nothing wider. It is never a claim that the whole operation a "
            "reader has in mind was timed.",
            "NOT_MEASURED is never a failure and never a zero. It records that "
            "nobody looked, which is a fact about this benchmark rather than about "
            "any implementation.",
            "No duration here was derived by subtracting one measurement from "
            "another. Where no boundary exists, the metric is not measured.",
        ]
    )

    @model_validator(mode="after")
    def _the_ledger_covers_every_promised_metric_exactly_once(self) -> MeasurementCoverage:
        seen = [entry.metric_id for entry in self.entries]
        duplicates = sorted({name for name in seen if seen.count(name) > 1})
        if duplicates:
            raise ValueError(f"the coverage ledger lists {duplicates} more than once")
        missing = sorted(set(REQUIRED_METRIC_IDS) - set(seen))
        if missing:
            raise ValueError(
                f"the coverage ledger omits {missing}. A promised metric absent from "
                "the ledger is a metric a reader cannot tell was never measured."
            )
        unknown = sorted(set(seen) - set(REQUIRED_METRIC_IDS))
        if unknown:
            raise ValueError(
                f"the coverage ledger names {unknown}, which the methodology does not "
                "promise. An unsupported metric presented beside supported ones "
                "borrows their credibility."
            )
        return self

    def entry(self, metric_id: str) -> MeasurementCoverageEntry:
        for candidate in self.entries:
            if candidate.metric_id == metric_id:
                return candidate
        raise KeyError(metric_id)

    @property
    def counts(self) -> dict[str, int]:
        return {
            state.value: sum(1 for entry in self.entries if entry.state is state)
            for state in CoverageState
        }


# --------------------------------------------------------------- static prose
#
# The parts of an entry that do not depend on what a run produced. Kept here so
# the derivation below decides only the state and the sample counts, and cannot
# accidentally reword a limitation into something weaker.


class _Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    category: MeasurementCategory
    measurement_method: str
    environment: str
    limitation: str


DECLARATIONS: dict[str, _Declaration] = {
    "clean_image_build": _Declaration(
        display_name="Clean image build",
        category=MeasurementCategory.BUILD,
        measurement_method=(
            "Wall duration of one `docker build` invocation per external target, "
            "timed on a monotonic clock by the builder and recorded on the build "
            "resolution entry."
        ),
        environment="The machine that ran `mcp-failure-lab external-build`.",
        limitation=(
            "One build per target, with no warm-up and no repetition, and the "
            "builder does not record whether the layer cache was cold. A single "
            "duration cannot distinguish a clean build from a cached one, so this "
            "is not a headline figure and no median is reported."
        ),
    ),
    "cached_image_build": _Declaration(
        display_name="Cached image build",
        category=MeasurementCategory.BUILD,
        measurement_method=(
            "Would require building each image a second time with the layer cache "
            "warm, immediately after the clean build, and recording both."
        ),
        environment="Not executed.",
        limitation=(
            "Absent entirely. The benchmark builds each image once, and a second "
            "build is not performed."
        ),
    ),
    "capability_discovery": _Declaration(
        display_name="Capability discovery",
        category=MeasurementCategory.DISCOVERY,
        measurement_method=(
            "Wall duration of one full CapabilityResolver.resolve() against a "
            "first-party stdio fixture: launch, initialize, tools/list and "
            "teardown, timed end to end on a monotonic clock with a fresh resolver "
            "per sample so the per-target cache cannot answer instead of the "
            "target. One warm-up excluded and recorded."
        ),
        environment=(
            "First-party reference fixture, executed on the host. Not the three "
            "pinned external targets."
        ),
        limitation=(
            "Measured against a fixture this repository wrote, not against the "
            "pinned external implementations. The benchmark reaches those through "
            "a battery subprocess that exposes no discovery boundary, and a "
            "discovery figure obtained by subtracting one duration from another "
            "would not be a measurement of discovery."
        ),
    ),
    "target_startup": _Declaration(
        display_name="Target startup",
        category=MeasurementCategory.EXECUTION,
        measurement_method=(
            "Would require timing the transport session's start() for each pinned "
            "target and carrying that duration out through the run result, the "
            "battery report and the benchmark sample."
        ),
        environment="Not executed.",
        limitation=(
            "Absent. The benchmark reaches each target through a battery "
            "subprocess whose duration covers launching, discovering and running "
            "thirty-two scenarios. That duration measures the battery."
        ),
    ),
    "first_valid_request": _Declaration(
        display_name="Time to first valid request",
        category=MeasurementCategory.EXECUTION,
        measurement_method=(
            "Would require a timestamped wire boundary between the target becoming "
            "ready and the first successful response, carried out through the run "
            "result."
        ),
        environment="Not executed.",
        limitation=(
            "Absent. The wire trace records message order rather than a "
            "readiness boundary, and the interval between a launch and a first "
            "answer is not recoverable from the durations that are kept."
        ),
    ),
    "applicable_suite_runtime": _Declaration(
        display_name="Applicable suite runtime",
        category=MeasurementCategory.EXECUTION,
        measurement_method=(
            "Sum of the per-scenario durations of one timed battery repetition, per "
            "target. Harness startup and dependency resolution are excluded because "
            "they are not the suite. A repetition containing any rejected sample "
            "yields no total at all."
        ),
        environment="The machine that ran `mcp-failure-lab benchmark run`.",
        limitation=(
            "Each target has a different applicable scenario set, so two targets' "
            "suite runtimes are not comparable as wholes. Durations between "
            "implementations written in different languages measure the languages "
            "and their runtimes as much as anything else."
        ),
    ),
    "time_to_first_actionable_result": _Declaration(
        display_name="Time to first actionable result",
        category=MeasurementCategory.EXECUTION,
        measurement_method=(
            "Cumulative scenario duration, in suite order, up to the first decided "
            "non-pass of the first correctness repetition."
        ),
        environment="The machine that ran `mcp-failure-lab benchmark run`.",
        limitation=(
            "One value from one repetition, not a sampled distribution, and a "
            "property of this suite's scenario ordering rather than of the target. "
            "Absent when the battery reached no actionable result, which is a "
            "legitimate outcome and not a zero."
        ),
    ),
    "replay": _Declaration(
        display_name="Replay",
        category=MeasurementCategory.ANALYSIS,
        measurement_method=(
            "Wall duration of one replay of a committed reproduction package, from "
            "loading the package to reaching a replay outcome, timed on a monotonic "
            "clock. One warm-up excluded and recorded."
        ),
        environment=("Approved first-party synthetic fixture package, executed on the host."),
        limitation=(
            "A first-party fixture package with a deliberate defect, not a "
            "third-party reproduction. Replay duration depends on the scenario "
            "being replayed and does not generalise across packages."
        ),
    ),
    "minimization": _Declaration(
        display_name="Minimisation",
        category=MeasurementCategory.ANALYSIS,
        measurement_method=(
            "Wall duration of one complete minimisation, from the reproducing "
            "failure to the reduced case whose signature still matches, timed on a "
            "monotonic clock. One warm-up excluded and recorded."
        ),
        environment=(
            "Approved first-party synthetic fixture with a deliberate defect, executed on the host."
        ),
        limitation=(
            "Minimisation cost is dominated by how many candidate reductions the "
            "search has to execute, which is a property of the failure. One "
            "fixture's figure does not transfer to another failure."
        ),
    ),
    "report_generation": _Declaration(
        display_name="Report generation",
        category=MeasurementCategory.ANALYSIS,
        measurement_method=(
            "Wall duration of serialising one loaded battery report to JSON, JUnit "
            "and SARIF, timed on a monotonic clock. A pure function of an "
            "in-memory report, with no target execution inside the boundary."
        ),
        environment="Host process, no subprocess and no network.",
        limitation=(
            "Scales with the number of runs in the report being serialised. The "
            "figure describes the report used for this measurement and is not a "
            "constant."
        ),
    ),
    "ci_gate": _Declaration(
        display_name="CI gate evaluation",
        category=MeasurementCategory.GATE,
        measurement_method=(
            "Wall duration of one `ci.gate.evaluate` call against a committed "
            "baseline and a loaded matrix report, timed on a monotonic clock. "
            "Evaluation only: the matrix execution that produces the report is a "
            "different measurement and is not inside this boundary."
        ),
        environment="Host process, no subprocess and no network.",
        limitation=(
            "Gate latency here excludes running the matrix, which dominates the "
            "wall time a CI job actually spends. Scales with the number of "
            "baseline entries compared."
        ),
    ),
    "cleanup": _Declaration(
        display_name="Cleanup",
        category=MeasurementCategory.EXECUTION,
        measurement_method=(
            "Wall duration of one transport session close() against a first-party "
            "stdio fixture — process-tree termination and reclamation — timed on a "
            "monotonic clock with the session started outside the boundary."
        ),
        environment=("First-party reference fixture, executed on the host, uncontained."),
        limitation=(
            "Host cleanup of a first-party fixture. Reclaiming a container is a "
            "different operation with a different cost, and this figure is not "
            "evidence about it."
        ),
    ),
    "containment_overhead_stdio": _Declaration(
        display_name="Containment overhead — stdio",
        category=MeasurementCategory.CONTAINMENT,
        measurement_method=(
            "Would compare the same stdio implementation host-side and contained, "
            "with the scenario, profile and implementation held constant, and "
            "report the ratio of the medians."
        ),
        environment="Not executed.",
        limitation=(
            "No contained twin of the stdio fixture exists in this matrix, so "
            "host-versus-contained cannot be measured with the implementation held "
            "constant. Comparing two different implementations would measure the "
            "implementations."
        ),
    ),
    "containment_overhead_http_2025": _Declaration(
        display_name="Containment overhead — Streamable HTTP (2025-11-25)",
        category=MeasurementCategory.CONTAINMENT,
        measurement_method=(
            "Would compare the same 2025-profile HTTP fixture host-side and "
            "contained, with the scenario held constant, and report the ratio of "
            "the medians."
        ),
        environment="Not executed.",
        limitation=(
            "The contained 2025 fixture issues session identifiers and its host "
            "twin does not, so the two sides differ by more than containment. "
            "Reporting the gap as containment overhead would attribute session "
            "issuance to the container."
        ),
    ),
    "containment_overhead_http_2026": _Declaration(
        display_name="Containment overhead — Streamable HTTP (2026-07-28)",
        category=MeasurementCategory.CONTAINMENT,
        measurement_method=(
            "The same 2026-profile HTTP fixture measured host-side and contained "
            "on the same scenario, one warm-up excluded, and the ratio of the two "
            "medians reported only when both sides reached the sample floor."
        ),
        environment="The machine that ran `mcp-failure-lab benchmark run`, with Docker.",
        limitation=(
            "This harness's containment, these fixtures, this machine. Per-scenario "
            "containers are started and torn down individually; a harness reusing "
            "one container across a suite would measure a different cost. Not a "
            "general statement about Docker."
        ),
    ),
    "cross_environment_result_signatures": _Declaration(
        display_name="Cross-environment result signatures",
        category=MeasurementCategory.REPRODUCIBILITY,
        measurement_method=(
            "`benchmark compare` reads two complete execution directories from "
            "different machines and compares identities only — target, scenario and "
            "capability fingerprints, negotiated profiles, classifications and "
            "complete result signatures. Durations are never compared across "
            "machines."
        ),
        environment=(
            "One environment executed. The comparison requires two, placed where "
            "`benchmark compare` can read both."
        ),
        limitation=(
            "Agreement is asserted over portable build inputs. Builder-local image "
            "IDs differ between storage drivers for identical bits and are not "
            "target drift."
        ),
    ),
}


# ------------------------------------------------------------------ derivation


def _read(path: Path) -> Any:
    """Load an artifact, returning ``None`` rather than raising.

    A missing or unreadable artifact makes its metric NOT_MEASURED, which is the
    honest reading: whatever the file was meant to record, this ledger cannot see
    it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _timing_named(timings: Any, predicate: Any) -> list[dict[str, Any]]:
    if not isinstance(timings, list):
        return []
    return [entry for entry in timings if isinstance(entry, dict) and predicate(entry)]


def _entry(
    metric_id: str,
    *,
    state: CoverageState,
    sample_count: int = 0,
    warmup_count: int = 0,
    artifact_reference: str | None = None,
    reason_not_measured: str | None = None,
    minimum_required_samples: int = HEADLINE_MINIMUM_SAMPLES,
) -> MeasurementCoverageEntry:
    declaration = DECLARATIONS[metric_id]
    return MeasurementCoverageEntry(
        metric_id=metric_id,
        display_name=declaration.display_name,
        category=declaration.category,
        state=state,
        sample_count=sample_count,
        minimum_required_samples=minimum_required_samples,
        warmup_count=warmup_count,
        artifact_reference=artifact_reference,
        measurement_method=declaration.measurement_method,
        environment=declaration.environment,
        limitation=declaration.limitation,
        reason_not_measured=reason_not_measured,
    )


def _clean_image_build(output_dir: Path) -> MeasurementCoverageEntry:
    payload = _read(output_dir / "build-resolution.json")
    entries = payload.get("entries") if isinstance(payload, dict) else None
    timed = [
        entry
        for entry in entries or []
        if isinstance(entry, dict) and isinstance(entry.get("build_duration_ms"), int | float)
    ]
    if not timed:
        return _entry(
            "clean_image_build",
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=(
                "no build resolution entry carrying a build duration was written "
                "beside these results, so no build was timed here"
            ),
        )
    return _entry(
        "clean_image_build",
        state=CoverageState.PARTIALLY_MEASURED,
        sample_count=len(timed),
        warmup_count=0,
        artifact_reference="build-resolution.json",
        reason_not_measured=(
            f"one build per target and {len(timed)} in total — below the "
            f"{HEADLINE_MINIMUM_SAMPLES} samples this benchmark requires before "
            "quoting a headline timing, with no warm-up and no record of whether "
            "the layer cache was cold. The durations are shown; no median is "
            "reported from them."
        ),
        minimum_required_samples=HEADLINE_MINIMUM_SAMPLES,
    )


def _from_operations(
    metric_id: str, operations: Any, *, artifact: str = "measured-operations.json"
) -> MeasurementCoverageEntry:
    """State one operation metric from what `benchmark measure` recorded."""
    record: dict[str, Any] | None = None
    if isinstance(operations, dict):
        for candidate in operations.get("operations", []):
            if isinstance(candidate, dict) and candidate.get("metric_id") == metric_id:
                record = candidate
                break
    if record is None:
        return _entry(
            metric_id,
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=(
                "this execution recorded no measurement for this boundary, so nothing was timed"
            ),
        )

    samples = record.get("samples_ms")
    count = len(samples) if isinstance(samples, list) else 0
    warmups = int(record.get("warmup_count") or 0)
    if record.get("state") != MeasurementState.MEASURED.value or count < HEADLINE_MINIMUM_SAMPLES:
        return _entry(
            metric_id,
            state=(CoverageState.PARTIALLY_MEASURED if count else CoverageState.NOT_MEASURED),
            sample_count=count,
            warmup_count=warmups,
            artifact_reference=artifact if count else None,
            reason_not_measured=str(
                record.get("insufficient_reason")
                or f"{count} valid sample(s), below the {HEADLINE_MINIMUM_SAMPLES} "
                "required before quoting a headline timing"
            ),
        )
    return _entry(
        metric_id,
        state=CoverageState.MEASURED,
        sample_count=count,
        warmup_count=warmups,
        artifact_reference=artifact,
    )


def _suite_runtime(output_dir: Path) -> MeasurementCoverageEntry:
    timings = _read(output_dir / "timings.json")
    summaries = _timing_named(
        timings, lambda entry: str(entry.get("name", "")).endswith("applicable suite runtime")
    )
    measured = [
        entry
        for entry in summaries
        if entry.get("state") == MeasurementState.MEASURED.value
        and len(entry.get("samples_ms") or []) >= HEADLINE_MINIMUM_SAMPLES
    ]
    if not measured:
        return _entry(
            "applicable_suite_runtime",
            state=CoverageState.PARTIALLY_MEASURED if summaries else CoverageState.NOT_MEASURED,
            sample_count=sum(len(entry.get("samples_ms") or []) for entry in summaries),
            warmup_count=1 if summaries else 0,
            artifact_reference="timings.json" if summaries else None,
            reason_not_measured=(
                "no target reached the sample floor for a suite total. A repetition "
                "containing any rejected sample yields no total, so the shortfall "
                "records rejected samples rather than a slower run."
            ),
        )
    return _entry(
        "applicable_suite_runtime",
        state=CoverageState.MEASURED,
        sample_count=sum(len(entry.get("samples_ms") or []) for entry in measured),
        warmup_count=1,
        artifact_reference="timings.json",
    )


def _first_actionable(output_dir: Path) -> MeasurementCoverageEntry:
    timings = _read(output_dir / "timings.json")
    summaries = _timing_named(
        timings,
        lambda entry: str(entry.get("name", "")).endswith("time to first actionable result"),
    )
    decided = [entry for entry in summaries if entry.get("value_ms") is not None]
    if not decided:
        return _entry(
            "time_to_first_actionable_result",
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=(
                "the battery reached no decided non-pass in the first repetition of "
                "any target, so there is no interval to report. A legitimate "
                "outcome, and not a zero."
            ),
        )
    return _entry(
        "time_to_first_actionable_result",
        state=CoverageState.PARTIALLY_MEASURED,
        sample_count=len(decided),
        warmup_count=0,
        artifact_reference="timings.json",
        reason_not_measured=(
            "one value per target from the first correctness repetition, not a "
            "sampled distribution. No warm-up, no median, and the figure moves with "
            "scenario ordering."
        ),
    )


#: Which overhead pair label in the artifact answers which coverage metric.
_OVERHEAD_LABELS: dict[str, str] = {
    "containment_overhead_stdio": "stdio",
    "containment_overhead_http_2025": "streamable_http (2025-11-25)",
    "containment_overhead_http_2026": "streamable_http (2026-07-28)",
}


def _containment_overhead(metric_id: str, output_dir: Path) -> MeasurementCoverageEntry:
    payload = _read(output_dir / "containment-overhead.json")
    label = _OVERHEAD_LABELS[metric_id]
    pair: dict[str, Any] | None = None
    if isinstance(payload, dict):
        for candidate in payload.get("pairs", []):
            if isinstance(candidate, dict) and candidate.get("transport") == label:
                pair = candidate
                break
    if pair is None:
        return _entry(
            metric_id,
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=(
                "no pair for this transport was recorded in this execution, so "
                "nothing host-versus-contained was measured"
            ),
        )

    if pair.get("state") != MeasurementState.MEASURED.value:
        return _entry(
            metric_id,
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=str(
                pair.get("reason")
                or pair.get("insufficient_reason")
                or "the pair produced no ratio"
            ),
        )

    host = pair.get("host") or {}
    contained = pair.get("contained") or {}
    samples = len(host.get("samples_ms") or []) + len(contained.get("samples_ms") or [])
    return _entry(
        metric_id,
        state=CoverageState.MEASURED,
        sample_count=samples,
        warmup_count=2,
        artifact_reference="containment-overhead.json",
        minimum_required_samples=HEADLINE_MINIMUM_SAMPLES * 2,
    )


def _cross_environment(output_dir: Path) -> MeasurementCoverageEntry:
    payload = _read(output_dir / "reproduction-results.json")
    second = (payload or {}).get("environment_b") if isinstance(payload, dict) else None
    state = (second or {}).get("state") if isinstance(second, dict) else None
    if state == MeasurementState.MEASURED.value:
        return _entry(
            "cross_environment_result_signatures",
            state=CoverageState.MEASURED,
            sample_count=HEADLINE_MINIMUM_SAMPLES,
            artifact_reference="reproduction-results.json",
        )
    return _entry(
        "cross_environment_result_signatures",
        state=CoverageState.PARTIALLY_MEASURED,
        sample_count=1,
        warmup_count=0,
        artifact_reference="reproduction-results.json",
        minimum_required_samples=2,
        reason_not_measured=(
            "one environment executed and recorded in full. The second "
            "environment's result artifact is not where `benchmark compare` can "
            "read it, so agreement across environments is reported as partially "
            "measured rather than inferred from a single machine."
        ),
    )


def build_coverage(output_dir: Path) -> MeasurementCoverage:
    """Derive the ledger from what one benchmark execution actually wrote.

    Every state comes from an artifact on disk. Nothing here is declared: a
    metric is measured because a file records samples for it, and the absence of
    that file makes the metric NOT_MEASURED rather than leaving it out.
    """
    operations = _read(output_dir / "measured-operations.json")

    entries = [
        _clean_image_build(output_dir),
        _entry(
            "cached_image_build",
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=(
                "the benchmark builds each image once. No cache-warm second build "
                "is executed, and the builder records no cache state, so there is "
                "nothing to report and nothing to infer."
            ),
        ),
        _from_operations("capability_discovery", operations),
        _entry(
            "target_startup",
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=(
                "no boundary exists. Each pinned target is reached through a "
                "battery subprocess whose single duration covers launch, discovery "
                "and thirty-two scenarios, and startup is not recoverable from it "
                "by subtraction."
            ),
        ),
        _entry(
            "first_valid_request",
            state=CoverageState.NOT_MEASURED,
            reason_not_measured=(
                "no boundary exists. The wire trace records message order rather "
                "than a readiness instant, so the interval between a launch and a "
                "first successful answer was never observed."
            ),
        ),
        _suite_runtime(output_dir),
        _first_actionable(output_dir),
        _from_operations("replay", operations),
        _from_operations("minimization", operations),
        _from_operations("report_generation", operations),
        _from_operations("ci_gate", operations),
        _from_operations("cleanup", operations),
        *(_containment_overhead(metric_id, output_dir) for metric_id in _OVERHEAD_LABELS),
        _cross_environment(output_dir),
    ]
    return MeasurementCoverage(entries=entries)
