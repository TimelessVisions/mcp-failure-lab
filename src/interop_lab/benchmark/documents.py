"""The prose artifacts, and the integrity reports.

Three documents and two machine-readable integrity reports, and the division is
about audience. ``methodology.md`` and ``limitations.md`` say what the numbers
mean and what they do not; ``executive-summary.md`` is the one a non-engineer
reads, which is exactly why it is written in FACT / SOURCE / EVIDENCE /
INTERPRETATION / LIMITATION and contains no ranking language at all.

The JUnit and SARIF outputs report the benchmark's **own execution integrity** —
whether each sample was valid — rather than the targets' conformance. A dashboard
showing a rejected sample as green would defeat the entire rejection design.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from io import StringIO
from typing import Any
from xml.etree import ElementTree as ET

from interop_lab.benchmark.model import BenchmarkDefinition, BenchmarkSample
from interop_lab.naming import HARNESS_VERSION, PRODUCT_NAME

METHODOLOGY_SOURCE = "benchmarks/methodology/private-0.3.md"


def methodology_document() -> str:
    """A pointer, plus the rules a reader must not have to go looking for.

    Deliberately not a copy of the committed methodology: two copies of a
    methodology diverge, and the one in the output directory would be the stale
    half exactly when it mattered.
    """
    return f"""# Methodology — Private Benchmark 0.3

The full methodology is committed at `{METHODOLOGY_SOURCE}`. This file states the
rules a reader of these artifacts must not have to go looking for.

## No overall ranking

There is no best SDK here, no safest implementation, no most compliant target and
no aggregate score. Thirty-two scenarios against three implementations cannot
support one. Every figure belongs to one category, with that category's own
denominator and its own limitations.

## The denominator

`applicable` is the denominator. `NOT_APPLICABLE` is outside it — a scenario that
could not apply is not a scenario the target failed. `NOT_MEASURED` is outside it
too, and is never a failure: nobody looked.

`INCONCLUSIVE` is a fact about this harness, not about the target. A scenario
whose repetitions disagreed is counted inconclusive, never as whichever outcome
appeared most often.

## Stability

A repeated classification is not a repeated result. Stability requires equality of
a complete result signature: classification, failed assertion identities,
assertion-level classifications, timeout cause, exit code, signal,
`killed_by_harness`, cleanup and containment outcomes, target/scenario/capability
fingerprints, and the result dimensions. Durations and timestamps are excluded.

## Security relevance

Orthogonal to the primary classification. A result may be a `SPEC_VIOLATION` and
security-relevant at once, and recording only one of the two destroys the other.

Three counts are separate and must not be conflated: normative failures,
security-relevant observations, and confirmed private vulnerabilities. The third
is reached by a person working a result up. **No execution path writes it.**

## Timing

Median, minimum, maximum and every individual sample. No confidence intervals, no
significance claims. One warm-up excluded and recorded. A category with fewer than
five valid samples reports insufficient samples and no headline figure.

Image build time is never combined with suite execution time.
"""


def limitations_document(definition: BenchmarkDefinition) -> str:
    lines = [
        "# Limitations — Private Benchmark 0.3",
        "",
        "Everything below is a reason a number here does not mean what it might",
        "appear to mean. None of it is boilerplate.",
        "",
        "## What this benchmark does not establish",
        "",
    ]
    lines += [f"- {limitation}" for limitation in definition.limitations]
    lines += [
        "",
        "## Scope of any single figure",
        "",
        "Any number here is valid for the pinned target version, the built image, the",
        "negotiated profile, the transport, the containment configuration and the",
        "environment recorded beside it. It generalises to nothing else.",
        "",
        "Absence of a finding is absence of a finding **from this battery**. It is",
        "never evidence of correctness.",
        "",
        "## Official conformance",
        "",
        "Recorded separately per target and never merged into a Failure Lab",
        "classification. No host port was published and no containment was weakened to",
        "obtain a conformance number: a conformance result bought by invalidating every",
        "other result measured under containment is not worth having.",
        "",
        "## Cross-environment",
        "",
        "Builder-local image IDs differ between storage drivers for identical bits and",
        "are not target drift. Cross-environment agreement is asserted over the portable",
        "build inputs. Timing values are never compared across machines.",
        "",
        "## Third-party results",
        "",
        "These outputs concern software this project does not own. They are private,",
        "they are not published in whole or in part, and no maintainer has been",
        "contacted. See `docs/responsible-disclosure.md`.",
    ]
    return "\n".join(lines) + "\n"


def executive_summary(
    definition: BenchmarkDefinition,
    observations: list[dict[str, Any]],
    problems: list[str],
) -> str:
    """FACT / SOURCE / EVIDENCE / INTERPRETATION / LIMITATION, with no ranking language.

    The structure is not decoration. Each block forces the sentence that the
    previous one invites a reader to skip: a fact without its source is an
    assertion, and an interpretation without its limitation is the sentence that
    gets quoted out of context.
    """
    totals = Counter()
    for observation in observations:
        for key in (
            "applicable_scenarios",
            "stable_pass",
            "stable_non_pass",
            "inconclusive",
            "not_applicable",
            "security_relevant_observations",
            "confirmed_private_vulnerabilities",
            "spec_violation",
            "behavioral_divergence",
        ):
            totals[key] += int(observation.get(key, 0))

    per_target: dict[str, Counter[str]] = {}
    for observation in observations:
        key = observation.get("target_key") or "unattributed"
        bucket = per_target.setdefault(key, Counter())
        for field in (
            "applicable_scenarios",
            "stable_pass",
            "stable_non_pass",
            "inconclusive",
            "not_applicable",
            "security_relevant_observations",
        ):
            bucket[field] += int(observation.get(field, 0))

    lines = [
        "# Executive summary — Private Benchmark 0.3",
        "",
        f"**Generated:** {datetime.now(UTC).isoformat()} · **Status:** PRIVATE · "
        "**Not for publication**",
        "",
        "---",
        "",
        "## FACT",
        "",
        f"{len(definition.targets)} pinned third-party implementations across "
        f"{len({t.language for t in definition.targets})} languages and "
        f"{len(definition.transports)} transports were measured against "
        f"{len(definition.scenarios)} scenarios, "
        f"{definition.correctness_repetitions} independent repetitions each.",
        "",
        f"Across all categories: {totals['applicable_scenarios']} applicable scenario "
        f"results, of which {totals['stable_pass']} were stable passes and "
        f"{totals['stable_non_pass']} were stable non-pass results. "
        f"{totals['not_applicable']} were not applicable and are outside every "
        f"denominator. {totals['inconclusive']} were inconclusive.",
        "",
        f"Security-relevant observations: {totals['security_relevant_observations']}. "
        f"Confirmed private vulnerabilities: "
        f"{totals['confirmed_private_vulnerabilities']}.",
        "",
        "## SOURCE",
        "",
        f"- Definition pinned at harness commit "
        f"`{definition.harness_commit or 'unrecorded'}`, benchmark schema "
        f"{definition.benchmark_schema_version}. The commit this execution *ran* at "
        "is in `manifest.json`; a run legitimately happens at a later commit than the "
        "pin, and the two are recorded separately so neither stands in for the other.",
        "- Every target pinned by version, commit, target fingerprint, portable build",
        "  identity, Dockerfile hash and dependency-lock hash. See",
        "  `target-matrix.json` and `benchmark-definition.json`.",
        "- Normative assertions cite dated requirements from the profile each target",
        "  actually negotiated. A requirement absent from a negotiated revision is never",
        "  asserted against it.",
        "",
        "## EVIDENCE",
        "",
        "| Target | Applicable | Stable pass | Stable non-pass | Inconclusive | "
        "Not applicable | Security-relevant |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(per_target):
        bucket = per_target[key]
        lines.append(
            f"| {key} | {bucket['applicable_scenarios']} | {bucket['stable_pass']} | "
            f"{bucket['stable_non_pass']} | {bucket['inconclusive']} | "
            f"{bucket['not_applicable']} | {bucket['security_relevant_observations']} |"
        )

    lines += [
        "",
        "The table is ordered alphabetically. **It is not a ranking**, the columns do",
        "not combine into a score, and the rows are not comparable as wholes: each",
        "target has a different applicable set, because applicability depends on the",
        "transport it speaks and the surface it advertises.",
        "",
        "Per-scenario classifications, result signatures and their digests are in",
        "`classification-results.json` and `result-signatures.json`. Every rejected",
        "sample and its reason is in `applicability.json`.",
        "",
        "## INTERPRETATION",
        "",
        "Stability here means complete result-signature equality across "
        f"{definition.correctness_repetitions} repetitions — not a repeated label. A",
        "result whose repetitions agreed on a classification but disagreed on which",
        "assertions failed is reported inconclusive, not stable.",
        "",
        "A `BEHAVIORAL_DIVERGENCE` is not a defect. It records that implementations",
        "differ where no dated requirement decides between them.",
        "",
        "A security-relevant observation is not a vulnerability. Every one produced",
        "here carries `vulnerability_status: unconfirmed`, which states that the result",
        "touches a security property and that nothing about exploiting it has been",
        "established.",
        "",
        "## LIMITATION",
        "",
    ]
    lines += [f"- {limitation}" for limitation in definition.limitations]
    lines += [
        "- Absence of a finding is absence of a finding from this battery, never",
        "  evidence of correctness.",
        "",
    ]
    if problems:
        lines += [
            "## EXECUTION PROBLEMS",
            "",
            f"{len(problems)} problem(s) were recorded during this execution. They are",
            "listed in full in `manifest.json`. A benchmark that hid them would be",
            "reporting a cleaner run than the one that happened.",
            "",
        ]
        lines += [f"- {problem}" for problem in problems[:20]]
        if len(problems) > 20:
            lines.append(f"- …and {len(problems) - 20} more, in `manifest.json`.")
        lines.append("")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ integrity reports


def benchmark_junit(samples: list[BenchmarkSample], *, suite_name: str) -> str:
    """One test case per sample, reporting whether the *sample* was valid.

    This is execution integrity, not conformance. A `SPEC_VIOLATION` against a
    third-party target is a perfectly valid sample and passes here; a rejected
    sample fails here whatever classification it carried.
    """
    failures = sum(1 for sample in samples if not sample.valid)
    total_time = sum(sample.duration_ms for sample in samples) / 1000

    attributes = {
        "name": suite_name,
        "tests": str(len(samples)),
        "failures": str(failures),
        "errors": "0",
        "skipped": "0",
        "time": f"{total_time:.3f}",
    }
    suites = ET.Element("testsuites", attributes)
    suite = ET.SubElement(suites, "testsuite", attributes)

    for sample in samples:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": sample.target_key or "benchmark",
                "name": f"{sample.scenario_id or sample.sample_id} #{sample.repetition}",
                "time": f"{sample.duration_ms / 1000:.3f}",
            },
        )
        properties = ET.SubElement(case, "properties")
        values = {
            "category": sample.category.value,
            "classification": (sample.classification.value if sample.classification else "none"),
            "is_warmup": "true" if sample.is_warmup else "false",
            "security_relevant": "true" if sample.dimensions.security_relevant else "false",
            "vulnerability_status": sample.dimensions.vulnerability_status.value,
            "result_signature_digest": sample.result_signature_digest or "none",
        }
        for name, value in values.items():
            ET.SubElement(properties, "property", {"name": name, "value": value})

        if not sample.valid:
            failure = ET.SubElement(
                case,
                "failure",
                {
                    "type": ",".join(r.value for r in sample.rejections),
                    "message": (
                        "sample rejected; it contributes to no median and no classification summary"
                    ),
                },
            )
            failure.text = "\n".join(sample.rejection_detail)

    ET.indent(suites, space="  ")
    buffer = StringIO()
    ET.ElementTree(suites).write(buffer, encoding="unicode", xml_declaration=True)
    return buffer.getvalue()


def benchmark_sarif(
    samples: list[BenchmarkSample], *, definition: BenchmarkDefinition
) -> dict[str, Any]:
    """Normative findings the benchmark reached, with both dimensions preserved.

    Only `SPEC_VIOLATION` results become SARIF results. A divergence is not a
    defect and does not belong in a findings feed; a rejected sample is not a
    finding about anyone's server either, and is reported through JUnit instead.
    """
    from interop_lab.models.core import Classification

    reportable = [
        sample
        for sample in samples
        if sample.valid
        and not sample.is_warmup
        and sample.classification is Classification.SPEC_VIOLATION
    ]
    # One result per target x scenario, not one per repetition: three repetitions
    # of one finding are one finding, and emitting three would treble it in any
    # dashboard that counts.
    seen: set[tuple[str | None, str | None]] = set()
    results: list[dict[str, Any]] = []
    for sample in reportable:
        key = (sample.target_key, sample.scenario_id)
        if key in seen:
            continue
        seen.add(key)
        dimensions = sample.dimensions
        results.append(
            {
                "ruleId": "MCP-SPEC-VIOLATION",
                "level": "error",
                "message": {
                    "text": (
                        f"SPEC_VIOLATION on {sample.target_key} in {sample.scenario_id}, "
                        f"stable across {definition.correctness_repetitions} repetitions"
                    )
                },
                "partialFingerprints": {
                    "interopLabResultSignature": sample.result_signature_digest or "none"
                },
                "properties": {
                    "targetKey": sample.target_key,
                    "scenarioId": sample.scenario_id,
                    "category": sample.category.value,
                    "normative": dimensions.normative,
                    "securityRelevant": dimensions.security_relevant,
                    "securityCategory": (
                        dimensions.security_category.value if dimensions.security_category else None
                    ),
                    "vulnerabilityStatus": dimensions.vulnerability_status.value,
                    "securityLimitations": list(dimensions.security_limitations),
                    "failedAssertions": (
                        sample.result_signature.failed_assertion_ids
                        if sample.result_signature
                        else []
                    ),
                    "limitations": (
                        "Private. Valid only for the pinned target version, built image, "
                        "negotiated profile and containment recorded in the benchmark "
                        "definition. A security-relevant result is not a vulnerability "
                        "claim; vulnerabilityStatus states what has been established."
                    ),
                },
            }
        )

    return {
        "$schema": (
            "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/"
            "schema/sarif-schema-2.1.0.json"
        ),
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": f"{PRODUCT_NAME} — {definition.benchmark_name}",
                        "version": HARNESS_VERSION,
                        "informationUri": "https://github.com/TimelessVisions/mcp-failure-lab",
                        "rules": [
                            {
                                "id": "MCP-SPEC-VIOLATION",
                                "name": "MCPSPECVIOLATION",
                                "shortDescription": {
                                    "text": (
                                        "The target did not meet a requirement of the "
                                        "dated profile it negotiated."
                                    )
                                },
                                "defaultConfiguration": {"level": "error"},
                            }
                        ],
                    }
                },
                "results": results,
                "invocations": [{"executionSuccessful": True}],
            }
        ],
    }
