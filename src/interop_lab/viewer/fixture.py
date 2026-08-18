"""A synthetic benchmark execution, for building and testing the viewer.

CI cannot use the private Benchmark 0.3 output and must not try. So the public
path is built from evidence that has no subject: three invented implementations
that do not exist, measured against this repository's own scenario library, with
outcomes assigned by a deterministic rule rather than by anything anyone ran.

This is not a mock of the *contract*. The directory it writes is a real benchmark
output — real scenario fingerprints from the live library, real result
signatures with real digests, a manifest whose hashes verify — so the exporter's
verification runs against it in full rather than being switched off for the
synthetic case. What is synthetic is the subject matter, and only that.

The marker file `synthetic-fixture.json` is what lets the export command produce
a public-safe dataset without owner approval: there is no third party here to
re-identify, because there is no third party.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.benchmark.coverage import build_coverage
from interop_lab.benchmark.documents import (
    benchmark_junit,
    benchmark_sarif,
    executive_summary,
    limitations_document,
    methodology_document,
)
from interop_lab.benchmark.model import (
    BenchmarkCategory,
    BenchmarkDefinition,
    BenchmarkSample,
    ConformanceBoundary,
    MeasurementState,
    PinnedTarget,
    SampleRejection,
)
from interop_lab.benchmark.plan import DEFINITION_LIMITATIONS, pin_scenarios
from interop_lab.benchmark.reporting import build_manifest, observe_category, write_json
from interop_lab.benchmark.validation import summarise_timing
from interop_lab.models.core import AssertionStatus, Classification
from interop_lab.models.dimensions import (
    DEFAULT_SECURITY_LIMITATIONS,
    ResultDimensions,
    SecurityCategory,
    VulnerabilityStatus,
)
from interop_lab.profiles.registry import Transport
from interop_lab.results.signature import AssertionSignature, ResultSignature
from interop_lab.scenarios.library import ALL_SCENARIOS

SYNTHETIC_MARKER = "synthetic-fixture.json"

#: Three invented implementations. The names are deliberately not near any real
#: project's, and the languages and transports exist so the applicability rules
#: have something to be about.
SYNTHETIC_TARGETS: tuple[dict[str, str], ...] = (
    {
        "target_key": "fixture-aurora",
        "target_id": "fixture.aurora",
        "language": "python",
        "transport": "stdio",
        "relationship": "first_party",
        "source_version": "0.0.0-synthetic",
    },
    {
        "target_key": "fixture-borealis",
        "target_id": "fixture.borealis",
        "language": "typescript",
        "transport": "streamable_http",
        "relationship": "first_party",
        "source_version": "0.0.0-synthetic",
    },
    {
        "target_key": "fixture-cirrus",
        "target_id": "fixture.cirrus",
        "language": "go",
        "transport": "stdio",
        "relationship": "first_party",
        "source_version": "0.0.0-synthetic",
    },
)

PROFILE = "mcp-2025-11-25"

#: The six boundaries `benchmark measure` times, with invented durations.
#:
#: Listed here so the fixture exercises exactly the metric ids the coverage
#: ledger binds to. A fixture naming a seventh would render a card the ledger
#: has no entry for, which is the inconsistency this whole checkpoint is about.
SYNTHETIC_OPERATIONS: tuple[tuple[str, str, str, float], ...] = (
    (
        "capability_discovery",
        "Capability discovery",
        "CapabilityResolver.resolve() end to end",
        24.0,
    ),
    ("cleanup", "Cleanup", "StdioSession.close()", 0.18),
    (
        "report_generation",
        "Report generation",
        "RunReport to JSON, JUnit and SARIF",
        0.26,
    ),
    ("ci_gate", "CI gate evaluation", "ci.gate.evaluate()", 0.15),
    ("minimization", "Minimisation", "Minimizer.minimize()", 158.0),
    ("replay", "Replay", "replay() over a written package", 49.0),
)


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _outcome(target_key: str, scenario_id: str) -> str:
    """Deterministically assign one synthetic outcome.

    Deterministic so two runs of the generator produce byte-identical artifacts,
    which is what lets CI compare a build against a previous one at all. The
    distribution is chosen to exercise every state the viewer must render —
    including the awkward ones: a scenario whose repetitions disagree, and a
    scenario every repetition of which was rejected.
    """
    bucket = int(_digest(target_key, scenario_id)[:8], 16) % 100
    if bucket < 6:
        return "spec_violation"
    if bucket < 11:
        return "behavioral_divergence"
    if bucket < 14:
        return "expected_difference"
    if bucket < 17:
        return "unstable"
    if bucket < 19:
        return "harness_error"
    if bucket < 22:
        return "inconclusive"
    return "pass"


def _applies(scenario_id: str, transport: str) -> bool:
    scenario = ALL_SCENARIOS[scenario_id]
    transports = scenario.definition.applicability.transports
    if not transports:
        return True
    return Transport(transport) in transports


def _dimensions(scenario_id: str, classification: Classification) -> ResultDimensions:
    scenario = ALL_SCENARIOS[scenario_id]
    category = scenario.definition.security_category
    normative = any(
        assertion.normative_source is not None
        for step in scenario.steps
        for assertion in step.assertions
    )
    if category is None or classification is Classification.PASS:
        return ResultDimensions(normative=normative)
    return ResultDimensions(
        normative=normative,
        security_relevant=True,
        security_category=SecurityCategory(category.value),
        vulnerability_status=VulnerabilityStatus.UNCONFIRMED,
        security_limitations=list(DEFAULT_SECURITY_LIMITATIONS),
    )


def _signature(
    *,
    scenario_id: str,
    classification: Classification,
    dimensions: ResultDimensions,
    target_fingerprint: str,
    scenario_fingerprint: str,
    capability_fingerprint: str,
    variant: int,
) -> ResultSignature:
    failed = (
        [f"{scenario_id}.assert_{variant}"]
        if classification not in (Classification.PASS, Classification.NOT_APPLICABLE)
        else []
    )
    return ResultSignature(
        scenario_id=scenario_id,
        classification=classification,
        failed_assertion_ids=failed,
        assertions=[
            AssertionSignature(
                assertion_id=f"{scenario_id}.assert_{index}",
                status=(
                    AssertionStatus.FAIL
                    if f"{scenario_id}.assert_{index}" in failed
                    else AssertionStatus.PASS
                ),
                classification=classification
                if f"{scenario_id}.assert_{index}" in failed
                else None,
                security_relevant=dimensions.security_relevant
                and f"{scenario_id}.assert_{index}" in failed,
            )
            for index in (0, 1)
        ],
        exit_code=0,
        cleanup_ok=True,
        containment_outcome="enforced",
        containment_usable=True,
        target_fingerprint=target_fingerprint,
        scenario_fingerprint=scenario_fingerprint,
        capability_fingerprint=capability_fingerprint,
        dimensions=dimensions,
    )


def _definition() -> BenchmarkDefinition:
    scenarios = pin_scenarios()
    targets = [
        PinnedTarget(
            target_key=entry["target_key"],
            target_id=entry["target_id"],
            target_file=f"targets/synthetic/{entry['target_key']}.yaml",
            language=entry["language"],
            transport=entry["transport"],
            declared_profile=PROFILE,
            relationship=entry["relationship"],
            source_version=entry["source_version"],
            source_commit=_digest(entry["target_key"], "commit")[:40],
            target_fingerprint=_digest(entry["target_key"], "target")[:16],
            portable_identity_fingerprint=_digest(entry["target_key"], "portable"),
            build_context=f"targets/synthetic/{entry['target_key']}",
            dockerfile_sha256=_digest(entry["target_key"], "dockerfile"),
            lock_sha256=_digest(entry["target_key"], "lock"),
            source_archive_sha256=f"sha256:{_digest(entry['target_key'], 'archive')}",
            official_conformance=ConformanceBoundary.NOT_MEASURED,
            official_conformance_detail=(
                "Synthetic fixture. The official conformance framework was not run, "
                "and there is nothing here it could be run against."
            ),
        )
        for entry in SYNTHETIC_TARGETS
    ]
    return BenchmarkDefinition(
        harness_commit=_digest("synthetic", "harness")[:40],
        targets=targets,
        scenarios=scenarios,
        profiles=[PROFILE],
        transports=sorted({entry["transport"] for entry in SYNTHETIC_TARGETS}),
        limitations=[
            "Synthetic fixture evidence. These three implementations do not exist, "
            "and no figure here is a measurement of any real software.",
            *DEFINITION_LIMITATIONS,
        ],
    )


def _samples(definition: BenchmarkDefinition) -> dict[str, list[BenchmarkSample]]:
    per_target: dict[str, list[BenchmarkSample]] = {}
    for target in definition.targets:
        capability_fingerprint = _digest(target.target_key, "capabilities")[:16]
        collected: list[BenchmarkSample] = []
        for scenario in definition.scenarios:
            outcome = _outcome(target.target_key, scenario.scenario_id)
            applicable = _applies(scenario.scenario_id, target.transport)
            for repetition in (1, 2, 3):
                if not applicable:
                    classification = Classification.NOT_APPLICABLE
                elif outcome == "pass":
                    classification = Classification.PASS
                elif outcome == "spec_violation":
                    classification = Classification.SPEC_VIOLATION
                elif outcome == "behavioral_divergence":
                    classification = Classification.BEHAVIORAL_DIVERGENCE
                elif outcome == "expected_difference":
                    classification = Classification.EXPECTED_DIFFERENCE
                elif outcome == "inconclusive":
                    classification = Classification.INCONCLUSIVE
                elif outcome == "harness_error":
                    classification = Classification.HARNESS_ERROR
                else:  # unstable: the repetitions genuinely disagree
                    classification = (
                        Classification.SPEC_VIOLATION
                        if repetition == 1
                        else Classification.BEHAVIORAL_DIVERGENCE
                    )

                dimensions = _dimensions(scenario.scenario_id, classification)
                sample = BenchmarkSample(
                    sample_id=f"{target.target_key}:{scenario.scenario_id}:{repetition}",
                    category=scenario.category,
                    target_key=target.target_key,
                    scenario_id=scenario.scenario_id,
                    repetition=repetition,
                    duration_ms=float(
                        20 + int(_digest(target.target_key, scenario.scenario_id)[:4], 16) % 180
                    ),
                    exit_code=0,
                    observed_artifacts=["synthetic-report.json"],
                    target_fingerprint=target.target_fingerprint,
                    scenario_fingerprint=scenario.scenario_fingerprint,
                    capability_fingerprint=capability_fingerprint,
                    classification=classification,
                    dimensions=dimensions,
                )
                signature = _signature(
                    scenario_id=scenario.scenario_id,
                    classification=classification,
                    dimensions=dimensions,
                    target_fingerprint=target.target_fingerprint or "",
                    scenario_fingerprint=scenario.scenario_fingerprint,
                    capability_fingerprint=capability_fingerprint,
                    variant=0 if classification is not Classification.BEHAVIORAL_DIVERGENCE else 1,
                )
                sample.result_signature = signature
                sample.result_signature_digest = signature.digest
                if classification is Classification.HARNESS_ERROR:
                    sample.rejections.append(SampleRejection.HARNESS_ERROR)
                    sample.rejection_detail.append(
                        "the harness or the environment failed. Never a verdict about "
                        "the target, and never a valid timing sample either."
                    )
                collected.append(sample)
        per_target[target.target_key] = collected
    return per_target


def _capability_record(target: PinnedTarget) -> dict[str, Any]:
    fingerprint = _digest(target.target_key, "capabilities")[:16]
    tools = [f"synthetic_{name}" for name in ("echo", "add", "sleep", "describe")]
    return {
        "target_key": target.target_key,
        "capability_fingerprint": fingerprint,
        "capabilities": {
            "target_id": target.target_id,
            "status": "resolved",
            "status_detail": None,
            "protocol_profile_offered": PROFILE,
            "protocol_profile_negotiated": PROFILE,
            "server_capabilities": {"tools": {"listChanged": False}},
            "tools": [
                {
                    "name": name,
                    "input_schema_hash": _digest(target.target_key, name, "in")[:16],
                    "output_schema_hash": _digest(target.target_key, name, "out")[:16],
                }
                for name in tools
            ],
            "declared_tools": tools[:3],
            "advertises_resources": target.language == "typescript",
            "advertises_prompts": False,
            "advertises_logging": True,
            "advertises_completions": False,
            "observed_transport_features": {
                "transport": target.transport,
                "initialization_completed": True,
                "session_identifier_issued": target.transport == "streamable_http",
                "accepted_protocol_version": PROFILE,
            },
            "limitations": [
                "Synthetic surface. Nothing was discovered, because there is nothing "
                "here to discover it from.",
            ],
        },
    }


def _containment_record(target: PinnedTarget) -> dict[str, Any]:
    return {
        "target_key": target.target_key,
        "containment": {
            "mode": "container",
            "provenance": "local_build",
            "outcome": "enforced",
            "requested": {
                "image_source": {"kind": "local_build"},
                "network": "none",
                "memory_mb": 512,
                "cpus": 1.0,
                "pids_limit": 128,
                "read_only_root": True,
                "user": "65534:65534",
                "drop_all_capabilities": True,
                "no_new_privileges": True,
            },
            "observed": {
                "network_mode": "none",
                "memory_limit_bytes": 512 * 1024 * 1024,
                "nano_cpus": 1_000_000_000,
                "pids_limit": 128,
                "read_only_root": True,
                "user": "65534:65534",
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "published_host_port": None,
                "build_identity": _digest(target.target_key, "portable"),
                "observed_image_id": f"sha256:{_digest(target.target_key, 'image')}",
            },
            "weaknesses": [],
            "stated_limits": [
                "Synthetic containment record. No container was started.",
            ],
        },
    }


def write_fixture(out: Path, *, repo_root: Path) -> Path:
    """Write a complete synthetic benchmark output directory. Returns its path."""
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.rglob("*"):
        if stale.is_file():
            stale.unlink()

    definition = _definition()
    per_target = _samples(definition)
    all_samples = [sample for samples in per_target.values() for sample in samples]

    write_json(out / "benchmark-definition.json", definition.model_dump(mode="json"))
    write_json(
        out / "target-matrix.json",
        [target.model_dump(mode="json") for target in definition.targets],
    )
    write_json(
        out / "scenario-matrix.json",
        [scenario.model_dump(mode="json") for scenario in definition.scenarios],
    )
    write_json(
        out / SYNTHETIC_MARKER,
        {
            "synthetic": True,
            "reason": (
                "Every target in this execution is invented and every outcome is "
                "assigned by a deterministic rule. There is no third party here, so "
                "a public-safe export from this directory has nobody to re-identify."
            ),
            "generator": "interop_lab.viewer.fixture",
        },
    )
    write_json(
        out / "environment.json",
        {
            "os": "synthetic",
            "release": "synthetic",
            "machine": "synthetic",
            "python": "3.12",
            "runner_identity": "synthetic-fixture",
            "docker_engine_version": None,
            "storage_driver": None,
            "timing_caveat": (
                "Synthetic durations. They are not measurements and are comparable "
                "to nothing, including each other."
            ),
        },
    )
    write_json(
        out / "build-resolution.json",
        {
            "source": "synthetic",
            "present": False,
            "note": "No build was performed. No image exists.",
            "entries": [],
            "duration_note": "No build was timed, so no build duration is recorded.",
        },
    )

    applicability: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []
    signatures: list[dict[str, Any]] = []
    for target_key, samples in per_target.items():
        for sample in samples:
            applicability.append(
                {
                    "target_key": target_key,
                    "scenario_id": sample.scenario_id,
                    "repetition": sample.repetition,
                    "classification": (
                        sample.classification.value if sample.classification else None
                    ),
                    "applicable": sample.classification is not Classification.NOT_APPLICABLE,
                    "valid_sample": sample.valid,
                    "rejections": [rejection.value for rejection in sample.rejections],
                }
            )
            classifications.append(
                {
                    "target_key": target_key,
                    "scenario_id": sample.scenario_id,
                    "repetition": sample.repetition,
                    "classification": (
                        sample.classification.value if sample.classification else None
                    ),
                    "dimensions": sample.dimensions.model_dump(mode="json"),
                    "valid_sample": sample.valid,
                    "rejection_detail": sample.rejection_detail,
                }
            )
            if sample.result_signature is not None and sample.valid:
                signatures.append(
                    {
                        "target_key": target_key,
                        "scenario_id": sample.scenario_id,
                        "repetition": sample.repetition,
                        "digest": sample.result_signature_digest,
                        "signature": sample.result_signature.model_dump(mode="json"),
                    }
                )

    write_json(out / "applicability.json", applicability)
    write_json(out / "classification-results.json", classifications)
    write_json(out / "result-signatures.json", signatures)
    write_json(
        out / "capability-results.json",
        [_capability_record(target) for target in definition.targets],
    )
    write_json(
        out / "containment-results.json",
        [_containment_record(target) for target in definition.targets],
    )

    observations: list[dict[str, Any]] = []
    for category in BenchmarkCategory:
        for target in definition.targets:
            samples = [
                sample for sample in per_target[target.target_key] if sample.category is category
            ]
            if not samples:
                continue
            observations.append(
                observe_category(category, samples, target_key=target.target_key).model_dump(
                    mode="json"
                )
            )
    write_json(out / "category-observations.json", observations)

    timings: list[dict[str, Any]] = []
    for target in definition.targets:
        totals = [
            BenchmarkSample(
                sample_id=f"{target.target_key}:suite:{repetition}",
                category=BenchmarkCategory.VALID_BASELINE_BEHAVIOR,
                target_key=target.target_key,
                repetition=repetition,
                duration_ms=float(
                    900 + int(_digest(target.target_key, str(repetition))[:4], 16) % 300
                ),
            )
            for repetition in range(1, 6)
        ]
        timings.append(
            summarise_timing(
                f"{target.target_key}: applicable suite runtime",
                totals,
                minimum_samples=5,
                limitations=[
                    "Synthetic durations. Not a measurement of anything.",
                ],
            ).model_dump(mode="json")
        )
        timings.append(
            {
                "name": f"{target.target_key}: time to first actionable result",
                "unit": "ms",
                "state": MeasurementState.MEASURED.value,
                "value_ms": float(120 + int(_digest(target.target_key, "ttfar")[:3], 16) % 400),
                "limitations": [
                    "Cumulative scenario time to the first decided non-pass, in suite "
                    "order. A property of this suite's ordering, not of the target.",
                ],
            }
        )
    write_json(out / "timings.json", timings)

    write_json(
        out / "containment-overhead.json",
        {
            "environment": {"os": "synthetic"},
            "scenarios_held_constant": ["baseline.tools_list", "baseline.http_tools_list"],
            "scenario_note": "Held constant within each pair, which is the comparison.",
            "pairs": [
                {
                    "transport": "stdio",
                    "state": MeasurementState.NOT_MEASURED.value,
                    "reason": (
                        "no contained twin of the stdio fixture exists in this matrix, "
                        "so host-versus-contained cannot be measured with the "
                        "implementation held constant"
                    ),
                    "limitations": ["Synthetic fixture."],
                },
                {
                    "transport": "streamable_http (2025-11-25)",
                    "state": MeasurementState.NOT_MEASURED.value,
                    "reason": (
                        "the contained 2025 fixture issues session identifiers and its "
                        "host twin does not, so the difference between them is not "
                        "containment alone"
                    ),
                    "limitations": ["Synthetic fixture."],
                },
                {
                    "transport": "streamable_http (2026-07-28)",
                    "state": MeasurementState.MEASURED.value,
                    "scenario": "baseline.http_tools_list",
                    "host_target": "synthetic host fixture",
                    "contained_target": "synthetic contained fixture",
                    "host": {
                        "name": "streamable_http (2026-07-28): host",
                        "state": MeasurementState.MEASURED.value,
                        "samples_ms": [41.0, 43.0, 44.0, 45.0, 48.0],
                        "median_ms": 44.0,
                        "min_ms": 41.0,
                        "max_ms": 48.0,
                        "limitations": ["Synthetic durations."],
                    },
                    "contained": {
                        "name": "streamable_http (2026-07-28): contained",
                        "state": MeasurementState.MEASURED.value,
                        "samples_ms": [88.0, 91.0, 93.0, 95.0, 99.0],
                        "median_ms": 93.0,
                        "min_ms": 88.0,
                        "max_ms": 99.0,
                        "limitations": ["Synthetic durations."],
                    },
                    "overhead_ratio": 2.11,
                    "overhead_absolute_ms": 49.0,
                    "limitations": [
                        "Same implementation, same scenario, same profile on both "
                        "sides. Synthetic durations.",
                    ],
                },
            ],
        },
    )

    write_json(
        out / "measured-operations.json",
        {
            "minimum_samples": 5,
            "clock": "time.perf_counter (monotonic)",
            "operations": [
                {
                    "metric_id": metric_id,
                    "display_name": display,
                    "unit": "ms",
                    "state": MeasurementState.MEASURED.value,
                    "boundary": boundary,
                    "environment": "Synthetic fixture, no execution performed.",
                    "warmup_count": 1,
                    "warmup_samples_ms": [round(base * 1.4, 3)],
                    "samples_ms": [
                        round(base * factor, 3) for factor in (0.92, 0.97, 1.0, 1.05, 1.12)
                    ],
                    "median_ms": base,
                    "min_ms": round(base * 0.92, 3),
                    "max_ms": round(base * 1.12, 3),
                    "limitations": ["Synthetic durations. Not a measurement."],
                }
                for metric_id, display, boundary, base in SYNTHETIC_OPERATIONS
            ],
            "limitations": [
                "Synthetic fixture. Every duration here is generated, and none of "
                "them is a measurement of anything.",
            ],
        },
    )

    write_json(
        out / "reproduction-results.json",
        {
            "environment_a": {
                "identity": "synthetic-fixture",
                "state": MeasurementState.MEASURED.value,
                "compared": ["target fingerprints", "scenario fingerprints"],
            },
            "environment_b": {
                "state": MeasurementState.NOT_MEASURED.value,
                "reason": (
                    "This execution ran in one environment. Cross-environment "
                    "reproducibility is reported as NOT_MEASURED rather than inferred "
                    "from a single machine."
                ),
            },
            "timing_note": "Timing values are never compared across machines.",
        },
    )
    write_json(
        out / "conformance-boundary.json",
        {
            "note": ("Recorded separately and never merged into a Failure Lab classification."),
            "per_target": [
                {
                    "target_key": target.target_key,
                    "official_conformance": target.official_conformance.value,
                    "detail": target.official_conformance_detail,
                }
                for target in definition.targets
            ],
        },
    )

    (out / "junit.xml").write_text(
        benchmark_junit(all_samples, suite_name=definition.benchmark_name), encoding="utf-8"
    )
    write_json(out / "sarif.json", benchmark_sarif(all_samples, definition=definition))
    for name, body in (
        ("methodology.md", methodology_document()),
        ("limitations.md", limitations_document(definition)),
        ("executive-summary.md", executive_summary(definition, observations, [])),
    ):
        (out / name).write_text(body, encoding="utf-8")

    write_json(
        out / "measurement-coverage.json",
        build_coverage(out).model_dump(mode="json"),
    )

    outputs = build_manifest(
        out,
        definition=definition,
        harness_commit=definition.harness_commit,
        problems=[],
    )
    # A synthetic execution is not a private third-party measurement, and saying
    # it is would train a reader to ignore the word.
    outputs.generated_at = datetime.now(UTC).isoformat()
    write_json(out / "manifest.json", outputs.model_dump(mode="json"))
    return out


def is_synthetic(root: Path) -> bool:
    return (root / SYNTHETIC_MARKER).is_file()
