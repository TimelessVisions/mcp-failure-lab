"""Turning valid samples into category observations and private artifacts.

Two rules govern everything here, and both are about what the numbers are *not*
allowed to say.

**No overall ranking.** There is no aggregate score, no best implementation and
no ordering of targets. Every function that produces a per-target figure produces
it inside one category, with that category's own denominator and its own
limitations. A reader who wants to compare two targets on stateful lifecycle can;
a reader who wants one number saying which target is better cannot get one from
this module, because thirty-two scenarios cannot support one.

**The denominator is stated, never implied.** ``applicable`` is the denominator.
``NOT_APPLICABLE`` sits outside it — a scenario that could not apply is not a
scenario the target failed, and folding it in turns "this target speaks stdio"
into a lower score. ``NOT_MEASURED`` sits outside it too, in the other direction:
nobody looked, and a gap in coverage is not a defect in someone's server.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.benchmark.model import (
    BenchmarkCategory,
    BenchmarkDefinition,
    BenchmarkOutputs,
    BenchmarkSample,
    CategoryObservation,
)
from interop_lab.models.core import Classification

#: Category-level limitations attached wherever the category is reported.
CATEGORY_LIMITATIONS: dict[BenchmarkCategory, tuple[str, ...]] = {
    BenchmarkCategory.ENVIRONMENT_AND_BUILD_IDENTITY: (
        "Builder-local image IDs differ between storage drivers and are not target "
        "drift. Only the portable build inputs are compared across environments.",
    ),
    BenchmarkCategory.VALID_BASELINE_BEHAVIOR: (
        "A passing baseline means the target answered the exchanges this battery "
        "sends. It is not a statement that the implementation is correct.",
    ),
    BenchmarkCategory.JSONRPC_REQUEST_INTEGRITY: (
        "Hostile inputs are drawn from a fixed, hand-written set. Absence of a "
        "finding is absence of a finding from this set, not from the space.",
    ),
    BenchmarkCategory.STREAMABLE_HTTP_SEMANTICS: (
        "Long-lived SSE consumption, Last-Event-ID resumption and server-initiated "
        "requests over a retained stream are not implemented by this harness and are "
        "therefore neither observed nor claimed.",
    ),
    BenchmarkCategory.STATEFUL_LIFECYCLE_BEHAVIOR: (
        "2025-11-25 makes session identifiers optional. A server that issues none "
        "has nothing for these scenarios to be about, and they are NOT_APPLICABLE "
        "rather than failed.",
    ),
    BenchmarkCategory.TIMING_AND_RECOVERY: (
        "Deadlines are harness-side. A timeout records that no answer arrived within "
        "the deadline this project chose, not that the target is slow.",
    ),
    BenchmarkCategory.PROCESS_CONTAINMENT_AND_CLEANUP: (
        "Containment is this harness's, and a cleanup failure is a fact about the "
        "process tree the harness could reclaim — attributing it to the target "
        "requires evidence this category does not carry on its own.",
    ),
    BenchmarkCategory.NORMATIVE_FAILURES: (
        "Every failure here cites a dated requirement. A requirement absent from a "
        "target's negotiated profile is never asserted against it.",
    ),
    BenchmarkCategory.BEHAVIORAL_DIVERGENCES: (
        "A divergence is not a defect. These are cases where implementations differ "
        "and no dated requirement decides between them.",
    ),
    BenchmarkCategory.SECURITY_RELEVANT_OBSERVATIONS: (
        "Security-relevant is not vulnerable. These counts are observations; the "
        "confirmed count is separate and is reached by a person working a result up, "
        "never by this harness.",
    ),
    BenchmarkCategory.APPLICABILITY_AND_NOT_MEASURED_SURFACE: (
        "This category exists to make the un-measured surface visible. It is the "
        "largest honest statement the benchmark makes.",
    ),
    BenchmarkCategory.REPRODUCTION_AND_MINIMIZATION: (
        "Minimisation preserves the failure signature — classification and failing "
        "assertion identities. It does not establish that the reduced case is the "
        "smallest one that could.",
    ),
    BenchmarkCategory.CI_GATE_PERFORMANCE: (
        "Gate latency is measured against this repository's own baselines and "
        "fixtures. It transfers to another matrix only as an order of magnitude.",
    ),
    BenchmarkCategory.TIME_TO_FIRST_ACTIONABLE_RESULT: (
        "Time to the first non-pass the battery reaches, which depends on scenario "
        "ordering. It is a property of this suite, not of the target.",
    ),
}


def observe_category(
    category: BenchmarkCategory,
    samples: list[BenchmarkSample],
    *,
    target_key: str | None = None,
    not_measured: int = 0,
    extra_limitations: list[str] | None = None,
) -> CategoryObservation:
    """Count one category's results, with the denominator kept honest.

    ``stable_pass`` and ``stable_non_pass`` are counted over *scenarios*, not over
    samples: a scenario measured three times contributes one result, and only when
    all three repetitions agreed. A scenario whose repetitions disagreed is
    counted as inconclusive here rather than as whichever outcome happened to
    appear most, which would be a majority vote dressed as a measurement.
    """
    observation = CategoryObservation(
        category=category,
        target_key=target_key,
        not_measured=not_measured,
        limitations=[*CATEGORY_LIMITATIONS.get(category, ()), *(extra_limitations or [])],
    )

    by_scenario: dict[str, list[BenchmarkSample]] = defaultdict(list)
    for sample in samples:
        if sample.is_warmup or sample.scenario_id is None:
            continue
        by_scenario[sample.scenario_id].append(sample)

    for repetitions in by_scenario.values():
        valid = [s for s in repetitions if s.valid]
        if not valid:
            # Every repetition was rejected. Nothing was measured here, and it is
            # emphatically not a failure of the target.
            observation.harness_error += 1
            continue

        classifications = {s.classification for s in valid}
        digests = {s.result_signature_digest for s in valid}
        first = valid[0].classification

        if first is Classification.NOT_APPLICABLE:
            observation.not_applicable += 1
            continue

        # Applicable: the denominator every rate is computed against.
        observation.applicable_scenarios += 1
        observation.executed_scenarios += 1

        if any(s.dimensions.security_relevant for s in valid):
            observation.security_relevant_observations += 1
        if any(s.dimensions.is_confirmed_vulnerability for s in valid):
            observation.confirmed_private_vulnerabilities += 1

        if len(classifications) > 1 or len(digests) > 1:
            # Repetitions disagreed. Not a result about the target.
            observation.inconclusive += 1
            continue

        if first is Classification.INCONCLUSIVE:
            observation.inconclusive += 1
        elif first is Classification.HARNESS_ERROR:
            observation.harness_error += 1
        elif first is Classification.PASS:
            observation.stable_pass += 1
        else:
            observation.stable_non_pass += 1
            if first is Classification.SPEC_VIOLATION:
                observation.spec_violation += 1
            elif first is Classification.BEHAVIORAL_DIVERGENCE:
                observation.behavioral_divergence += 1
            elif first is Classification.EXPECTED_DIFFERENCE:
                observation.expected_difference += 1

    return observation


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    """Write one artifact deterministically.

    Sorted keys and a trailing newline: two executions that produced the same
    content must produce the same bytes, or the manifest's hashes record
    formatting rather than content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_manifest(
    output_dir: Path,
    *,
    definition: BenchmarkDefinition,
    harness_commit: str | None = None,
    problems: list[str] | None = None,
) -> BenchmarkOutputs:
    """Hash every artifact this execution wrote.

    Every file under the output directory, discovered rather than listed, so an
    artifact someone adds later cannot silently escape the manifest.

    ``harness_commit`` is the commit this execution ran at, supplied by the
    caller from the working tree that produced the outputs. It is deliberately
    *not* taken from the definition: the definition names the commit it was
    pinned at, a later execution legitimately runs at a newer one, and copying
    the pinned value here made the manifest name a commit that had not produced
    the results.
    """
    outputs = BenchmarkOutputs(
        generated_at=datetime.now(UTC).isoformat(),
        harness_commit=harness_commit,
        definition_harness_commit=definition.harness_commit,
        problems=list(problems or []),
        limitations=[
            "Private. These results compare third-party implementations and are not "
            "published, in whole or in part.",
            "No overall ranking exists in these outputs, and none can be derived from "
            "them honestly: the categories have different denominators and different "
            "evidence behind them.",
            "Correctness and timing are separate measurements. Timing figures from a "
            "GitHub-hosted runner are subject to shared-runner variance and are not "
            "comparable across machines.",
            *definition.limitations,
        ],
    )
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        outputs.artifacts[str(path.relative_to(output_dir))] = sha256_file(path)
    return outputs


def verify_manifest(output_dir: Path, manifest: BenchmarkOutputs) -> list[str]:
    """Re-hash every artifact the manifest names. Returns the disagreements."""
    problems: list[str] = []
    for relative, expected in sorted(manifest.artifacts.items()):
        path = output_dir / relative
        if not path.is_file():
            problems.append(f"{relative}: named in the manifest and absent from disk")
            continue
        observed = sha256_file(path)
        if observed != expected:
            problems.append(
                f"{relative}: manifest records {expected}, the file on disk hashes to {observed}"
            )
    on_disk = {
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    for extra in sorted(on_disk - set(manifest.artifacts)):
        problems.append(f"{extra}: present on disk and absent from the manifest")
    return problems
