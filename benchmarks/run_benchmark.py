"""Benchmark 0.2 — time-to-value measurement for the Failure Lab harness.

Measures how long the harness takes to do its own work, against real targets,
so that scheduling and pricing rest on a number rather than an impression.

It deliberately does **not** rank implementations against each other. Timing is
recorded per target because a contained external server is genuinely slower than
an in-process fixture and pretending otherwise would make every estimate wrong;
it is not recorded to say one implementation is better than another.

Results are written to a git-ignored directory. Nothing here is published.

Usage:
    uv run python benchmarks/run_benchmark.py --out benchmarks/private-results
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BENCHMARK_VERSION = "0.2"

#: Repeats per step. Three is enough to expose a wildly unstable step without
#: turning the benchmark into something nobody runs.
REPEATS = 3


@dataclass
class Step:
    """One measured operation."""

    name: str
    argv: list[str]
    #: What a reader is entitled to conclude from this number.
    means: str
    #: Why it might not transfer to another machine or another target.
    caveat: str
    #: Exit codes that mean the work happened. The gate legitimately exits 1 on a
    #: regression, so "0 only" would reject a correct measurement.
    accepted_exit_codes: frozenset[int] = frozenset({0})
    #: Text that must appear, because an exit code alone does not prove the work
    #: was done — a command can exit 0 having skipped everything.
    required_output: tuple[str, ...] = ()
    #: Text that must not appear. A run that fell back, errored, or reported
    #: containment failure is not the operation being timed.
    forbidden_output: tuple[str, ...] = ()
    samples: list[float] = field(default_factory=list)
    #: Rejected samples, kept with their reasons. A step that rejected half its
    #: samples is not a step whose median should be quoted.
    rejected: list[str] = field(default_factory=list)

    @property
    def median_s(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def is_quotable(self) -> bool:
        """Whether this step's median may be reported at all."""
        return len(self.samples) >= 3 and not self.rejected

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": " ".join(self.argv),
            "accepted_samples": len(self.samples),
            "rejected_samples": len(self.rejected),
            "rejections": self.rejected,
            "quotable": self.is_quotable,
            "median_s": round(self.median_s, 2) if self.samples else None,
            "min_s": round(min(self.samples), 2) if self.samples else None,
            "max_s": round(max(self.samples), 2) if self.samples else None,
            "samples": [round(s, 2) for s in self.samples],
            "means": self.means,
            "caveat": self.caveat,
        }


CLI = ["uv", "run", "interop-lab"]

EXTERNAL = "targets/external/mcp-server-time.yaml"
FIXTURE = "targets/fixtures/reference.yaml"
CONTAINED_FIXTURE = "targets/fixtures/reference-contained.yaml"


def steps() -> list[Step]:
    return [
        Step(
            name="time_to_first_finding_external",
            argv=[*CLI, "run", "-t", EXTERNAL, "--scenario", "wire.unknown_method"],
            # `run` exits 1 when it finds a failure. For a hostile scenario
            # that is the work succeeding: exit 0 here would mean the finding
            # was not reproduced.
            accepted_exit_codes=frozenset({1}),
            required_output=("SPEC_VIOLATION",),
            forbidden_output=("HARNESS_ERROR", "containment_unavailable"),
            means=(
                "Wall clock from an empty terminal to a classified, cited finding "
                "against a third-party server running in a container."
            ),
            caveat=(
                "The image must already be built and present. A cold build adds "
                "roughly 17s and a registry pull adds more."
            ),
        ),
        Step(
            name="applicable_suite_external",
            argv=[*CLI, "run", "-t", EXTERNAL, "--suite", "core"],
            accepted_exit_codes=frozenset({0, 1}),
            required_output=("Summary:",),
            forbidden_output=("HARNESS_ERROR", "containment_unavailable"),
            means="A full applicable-scenario pass over one contained external target.",
            caveat="Dominated by container start cost, which is per-scenario today.",
        ),
        Step(
            name="full_matrix_external",
            argv=[*CLI, "run", "-t", EXTERNAL, "--suite", "all"],
            accepted_exit_codes=frozenset({0, 1}),
            required_output=("Summary:", "NOT_APPLICABLE"),
            forbidden_output=("HARNESS_ERROR", "containment_unavailable"),
            means=(
                "Every scenario in the registry, including the ones correctly "
                "reported NOT_APPLICABLE for this target's transport and profile."
            ),
            caveat=(
                "NOT_APPLICABLE scenarios cost almost nothing, so this is far "
                "cheaper than the scenario count suggests."
            ),
        ),
        Step(
            name="full_matrix_local_fixture_host",
            argv=[*CLI, "run", "-t", FIXTURE, "--suite", "all"],
            required_output=("Summary:",),
            forbidden_output=("HARNESS_ERROR",),
            means="The same scenarios against an in-process fixture on the host.",
            caveat=(
                "Fixture-scale. Quoting this as a review time would be dishonest: it "
                "measures a program written to be fast to start."
            ),
        ),
        Step(
            name="full_matrix_local_fixture_contained",
            argv=[*CLI, "run", "-t", CONTAINED_FIXTURE, "--suite", "smoke"],
            required_output=("PASS",),
            forbidden_output=("HARNESS_ERROR", "containment_unavailable"),
            means="The cost containment itself adds, with the target held constant.",
            caveat="Compare against the host fixture row, not against the external row.",
        ),
        Step(
            name="release_gate",
            argv=[
                *CLI,
                "ci",
                "--baseline",
                "baselines/mvp.json",
                "--matrix",
                "targets/mvp-matrix.yaml",
                "--suite",
                "suites/release_gate/mvp.yaml",
            ],
            required_output=("gate passed",),
            means="The number that matters for CI adoption: gate latency on every push.",
            caveat="Twenty runs across four local fixtures. A client matrix will be larger.",
        ),
        Step(
            name="minimize_and_package_external",
            argv=[
                *CLI,
                "minimize",
                "-t",
                EXTERNAL,
                "--scenario",
                "wire.unknown_method",
                "--finding-id",
                "BENCH-TEMP",
            ],
            required_output=("reproduction package:", "stable across"),
            means=(
                "From a known failure to a portable reproduction package, including "
                "the three stability runs that make it a finding rather than a report."
            ),
            caveat=(
                "This scenario is already minimal, so no reduction stages run. A "
                "reducible failure costs more."
            ),
        ),
    ]


class RejectedSampleError(Exception):
    """A measurement that cannot be counted, with the reason it was rejected."""


def measure(step: Step, *, repeats: int) -> None:
    """Time a step, rejecting any sample that did not measure what it claims to.

    A benchmark that averages in a crashed run reports a number that is faster
    than the work and describes nothing. Timing is only meaningful when the
    command did the thing being timed, so every sample is validated before it is
    recorded, and a rejected sample is kept with its reason rather than dropped
    silently — a step that rejected half its samples is not a step whose median
    should be quoted.
    """
    for _ in range(repeats):
        start = time.perf_counter()
        completed = subprocess.run(
            step.argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        elapsed = time.perf_counter() - start

        try:
            validate_sample(step, completed)
        except RejectedSampleError as exc:
            step.rejected.append(f"{exc} (after {elapsed:.2f}s)")
            continue
        step.samples.append(elapsed)


def validate_sample(step: Step, completed: subprocess.CompletedProcess[str]) -> None:
    """Reject a sample that did not do the work, or did the wrong work."""
    output = (completed.stdout or "") + (completed.stderr or "")

    if completed.returncode not in step.accepted_exit_codes:
        raise RejectedSampleError(
            f"exit {completed.returncode}, expected one of {sorted(step.accepted_exit_codes)}"
        )

    # An exit code alone is not proof the work happened: a command can exit 0
    # having skipped everything.
    for marker in step.required_output:
        if marker not in output:
            raise RejectedSampleError(f"output does not contain {marker!r}")

    for marker in step.forbidden_output:
        if marker in output:
            raise RejectedSampleError(f"output contains {marker!r}")


def environment() -> dict[str, object]:
    docker_version = None
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        docker_version = result.stdout.strip() or None
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "docker_server": docker_version,
        "cpu_count": __import__("os").cpu_count(),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/private-results"))
    parser.add_argument("--repeats", type=int, default=REPEATS)
    args = parser.parse_args(argv[1:])

    measured = steps()
    for step in measured:
        print(f"measuring {step.name} ...", flush=True)
        measure(step, repeats=args.repeats)
        if step.samples:
            note = "" if step.is_quotable else "  [NOT QUOTABLE]"
            print(f"  median {step.median_s:.2f}s{note}", flush=True)
        else:
            print("  no valid samples", flush=True)
        for reason in step.rejected:
            print(f"  rejected: {reason}", flush=True)

    payload = {
        "benchmark_version": BENCHMARK_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repeats": args.repeats,
        "environment": environment(),
        "publication_status": "PRIVATE — not for publication",
        "scope": (
            "Measures this harness's own latency. Contains no comparative claim "
            "about any third-party implementation."
        ),
        "steps": [step.as_dict() for step in measured],
    }

    unquotable = [s.name for s in measured if not s.is_quotable]
    payload["unquotable_steps"] = unquotable
    payload["all_steps_quotable"] = not unquotable

    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / f"benchmark-{BENCHMARK_VERSION}.json"
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {destination}")
    if unquotable:
        # Reported as a failure rather than a footnote: a median computed from
        # rejected or missing samples is a number that describes nothing, and
        # quoting it is how a benchmark becomes a lie with a decimal point.
        print(
            "\nREJECTED — these steps have no quotable median: " + ", ".join(unquotable),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
