"""What containment costs, measured with everything else held constant.

The one way to get this wrong is to compare two different external targets and
call the difference "containment overhead". A Go binary in a `scratch` image and
a Node server are different programs; the gap between them measures the programs,
the runtimes and the languages, and containment is somewhere inside that number
with no way to extract it.

So this measures **first-party fixtures only**, and the same fixture on both
sides: the same code, the same scenario, the same profile, the same assertions —
one run on the host and one contained. Then the difference is attributable,
because nothing else moved.

That restriction is also why the figure is honest about what it transfers to. It
is the cost of *this harness's* containment for *these* fixtures on *this*
machine. It is not a general statement about Docker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from interop_lab.benchmark.model import (
    BenchmarkCategory,
    BenchmarkSample,
    MeasurementState,
    TimingSummary,
)
from interop_lab.benchmark.runner import CommandOutcome, run_command
from interop_lab.benchmark.validation import summarise_timing
from interop_lab.naming import CLI_COMMAND


@dataclass(frozen=True)
class FixturePair:
    """One implementation to measure host-side and contained.

    The scenario is per-pair, not global. ``baseline.tools_list`` is restricted to
    stdio and ``baseline.http_tools_list`` to Streamable HTTP, so a single global
    scenario would be NOT_APPLICABLE on one side of the matrix and produce no
    samples at all — which reads in the artifact as "containment overhead was not
    measured" rather than as "the scenario did not apply".

    What has to be constant is the scenario *within* a pair, because that is the
    comparison. Across pairs it is irrelevant: two pairs are never compared.
    """

    label: str
    host_target: str
    contained_target: str
    scenario: str
    #: Why this pair cannot yield an overhead figure, when it cannot.
    #:
    #: Set rather than the pair being deleted, so the gap appears in the artifact
    #: with its reason. A pair quietly removed is a measurement a reader cannot
    #: tell was never attempted.
    not_comparable_reason: str | None = None

    @property
    def comparable(self) -> bool:
        """Whether the two sides differ only in containment."""
        return self.not_comparable_reason is None and self.host_target != self.contained_target


#: Fixture pairs: the same implementation, one host and one contained.
FIXTURE_PAIRS: tuple[FixturePair, ...] = (
    FixturePair(
        label="stdio",
        host_target="targets/python/sdk-server.yaml",
        contained_target="targets/python/sdk-server.yaml",
        scenario="baseline.tools_list",
        not_comparable_reason=(
            "no contained twin of the stdio fixture exists in this matrix, so "
            "host-versus-contained cannot be measured with the implementation held "
            "constant"
        ),
    ),
    FixturePair(
        label="streamable_http (2025-11-25)",
        host_target="targets/http/fixture-2025.yaml",
        contained_target="targets/http/fixture-2025-contained.yaml",
        scenario="baseline.http_tools_list",
        not_comparable_reason=(
            "the contained 2025 fixture runs with INTEROP_FIXTURE_SESSIONS=1 and its "
            "host twin does not, so the contained side issues session identifiers and "
            "does strictly more work. The difference between them is therefore not "
            "containment alone, and reporting it as containment overhead would "
            "attribute session issuance to the container"
        ),
    ),
    FixturePair(
        label="streamable_http (2026-07-28)",
        host_target="targets/http/fixture-2026.yaml",
        contained_target="targets/http/fixture-2026-contained.yaml",
        scenario="baseline.http_tools_list",
    ),
)

#: The scenarios used, for the artifact's record. Deliberately the plainest ones
#: in the battery: a scenario that injects faults or waits on a deadline would put
#: its own timing into the comparison.
OVERHEAD_SCENARIOS: tuple[str, ...] = tuple(dict.fromkeys(pair.scenario for pair in FIXTURE_PAIRS))


@dataclass
class OverheadPair:
    """One implementation measured host-side and contained."""

    transport: str
    host_target: str
    contained_target: str
    scenario: str = ""
    host_samples: list[BenchmarkSample] = field(default_factory=list)
    contained_samples: list[BenchmarkSample] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: Set when the two sides are not the same fixture, which makes the
    #: comparison meaningless rather than merely imprecise.
    comparable: bool = True


def _measure(
    target_file: str,
    *,
    scenario: str,
    repo_root: Path,
    work_dir: Path,
    label: str,
    repetitions: int,
    warmups: int,
    timeout_s: float,
) -> tuple[list[BenchmarkSample], list[str]]:
    samples: list[BenchmarkSample] = []
    problems: list[str] = []
    for index in range(1, warmups + repetitions + 1):
        is_warmup = index <= warmups
        report = work_dir / f"overhead-{label}-{index}.json"
        outcome: CommandOutcome = run_command(
            [
                "uv",
                "run",
                CLI_COMMAND,
                "run",
                "-t",
                target_file,
                "--scenario",
                scenario,
                "--json",
                str(report),
                "--artifacts",
                str(work_dir / f"overhead-{label}-{index}"),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
        )
        if outcome.exit_code not in (0, 1):
            problems.append(
                f"{label} run {index}: exit {outcome.exit_code}; the CLI stopped before "
                "measuring, so this is not a sample"
            )
            continue

        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
            duration = float(payload["runs"][0]["duration_ms"])
            classification = payload["runs"][0]["classification"]
        except (OSError, KeyError, IndexError, ValueError) as exc:
            problems.append(f"{label} run {index}: unusable report: {exc}")
            continue

        if classification != "PASS":
            # A failing or skipped run took a different path through the code, so
            # its duration is not comparable with a passing one on the other side.
            # NOT_APPLICABLE is the case worth naming: it means the scenario does
            # not apply to this fixture at all, which is a configuration error in
            # the pair rather than a measurement.
            problems.append(
                f"{label} run {index}: classification {classification}, not PASS"
                + (
                    f". Scenario {scenario!r} does not apply to this fixture, so this "
                    "pair measures nothing; the pair's scenario is wrong."
                    if classification == "NOT_APPLICABLE"
                    else ". A run that failed took a different path and its duration is "
                    "not comparable."
                )
            )
            continue

        samples.append(
            BenchmarkSample(
                sample_id=f"overhead:{label}:{index}",
                category=BenchmarkCategory.PROCESS_CONTAINMENT_AND_CLEANUP,
                repetition=index,
                is_warmup=is_warmup,
                duration_ms=duration,
                exit_code=outcome.exit_code,
            )
        )
    return samples, problems


def measure_overhead(
    *,
    repo_root: Path,
    work_dir: Path,
    repetitions: int = 5,
    warmups: int = 1,
    timeout_s: float = 300.0,
) -> list[OverheadPair]:
    """Measure every fixture pair, host against contained."""
    work_dir.mkdir(parents=True, exist_ok=True)
    pairs: list[OverheadPair] = []

    for fixture in FIXTURE_PAIRS:
        pair = OverheadPair(
            transport=fixture.label,
            host_target=fixture.host_target,
            contained_target=fixture.contained_target,
            scenario=fixture.scenario,
        )
        if not fixture.comparable:
            # Reported NOT_MEASURED with its reason rather than estimated. A pair
            # whose sides differ in anything but containment measures that
            # difference, and putting a number on it would attribute the wrong
            # cause to the right arithmetic.
            pair.comparable = False
            pair.problems.append(
                f"{fixture.label}: {fixture.not_comparable_reason}. Reported "
                "NOT_MEASURED rather than estimated."
            )
            pairs.append(pair)
            continue

        slug = fixture.label.split()[0]
        host, host_problems = _measure(
            fixture.host_target,
            scenario=fixture.scenario,
            repo_root=repo_root,
            work_dir=work_dir,
            label=f"{slug}-host",
            repetitions=repetitions,
            warmups=warmups,
            timeout_s=timeout_s,
        )
        contained, contained_problems = _measure(
            fixture.contained_target,
            scenario=fixture.scenario,
            repo_root=repo_root,
            work_dir=work_dir,
            label=f"{slug}-contained",
            repetitions=repetitions,
            warmups=warmups,
            timeout_s=timeout_s,
        )
        pair.host_samples = host
        pair.contained_samples = contained
        pair.problems = [*host_problems, *contained_problems]
        pairs.append(pair)

    return pairs


def summarise(pair: OverheadPair, *, minimum_samples: int) -> dict[str, object]:
    """One pair's figures, with the ratio present only when both sides support it."""
    limits = [
        "Same implementation, same scenario, same profile, on both sides. The only "
        "difference is containment, which is what makes the ratio attributable.",
        "This harness's containment, these fixtures, this machine. Not a general "
        "statement about Docker, and not transferable to another harness.",
        "Per-scenario containers are started and torn down individually. A harness "
        "reusing one container across a suite would measure a different cost.",
    ]

    if not pair.comparable:
        return {
            "transport": pair.transport,
            "state": MeasurementState.NOT_MEASURED.value,
            "reason": pair.problems[0] if pair.problems else "not comparable",
            "limitations": limits,
        }

    host = summarise_timing(
        f"{pair.transport}: host", pair.host_samples, minimum_samples=minimum_samples
    )
    contained = summarise_timing(
        f"{pair.transport}: contained", pair.contained_samples, minimum_samples=minimum_samples
    )

    ratio: float | None = None
    absolute_ms: float | None = None
    if host.median_ms and contained.median_ms:
        ratio = round(contained.median_ms / host.median_ms, 2)
        absolute_ms = round(contained.median_ms - host.median_ms, 1)

    return {
        "transport": pair.transport,
        "scenario": pair.scenario,
        "state": (
            MeasurementState.MEASURED.value
            if ratio is not None
            else MeasurementState.NOT_MEASURED.value
        ),
        "host_target": pair.host_target,
        "contained_target": pair.contained_target,
        "host": host.model_dump(mode="json"),
        "contained": contained.model_dump(mode="json"),
        "overhead_ratio": ratio,
        "overhead_absolute_ms": absolute_ms,
        "insufficient_reason": (
            None
            if ratio is not None
            else "one or both sides produced too few valid samples for a median, so no "
            "ratio is reported rather than one computed from what survived"
        ),
        "problems": pair.problems,
        "limitations": limits,
    }


def overhead_timings(pairs: list[OverheadPair], *, minimum_samples: int) -> list[TimingSummary]:
    """Every side of every pair, as flat timing summaries for the timings artifact."""
    summaries: list[TimingSummary] = []
    for pair in pairs:
        if not pair.comparable:
            summaries.append(
                TimingSummary(
                    name=f"containment overhead ({pair.transport})",
                    state=MeasurementState.NOT_MEASURED,
                    insufficient_reason=pair.problems[0] if pair.problems else "not comparable",
                )
            )
            continue
        summaries.append(
            summarise_timing(
                f"{pair.transport}: host, uncontained",
                pair.host_samples,
                minimum_samples=minimum_samples,
            )
        )
        summaries.append(
            summarise_timing(
                f"{pair.transport}: contained",
                pair.contained_samples,
                minimum_samples=minimum_samples,
            )
        )
    return summaries
