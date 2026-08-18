"""Assembling the viewer dataset from a verified benchmark execution.

Everything here is a translation, never a computation. Counts come from the
category observations the benchmark already wrote; stability comes from
comparing the signature digests the benchmark already recorded; a denominator is
carried, never re-derived. Where an artifact says nothing, the dataset says
NOT_MEASURED and states why, because the one thing a viewer must never do is
produce a number the benchmark did not produce.

The scenario prose — purpose, normative citation, applicability prerequisites —
comes from the harness's own scenario library rather than from the benchmark
outputs, which pin fingerprints and not text. That is only sound if the library
still describes the scenarios the run measured, so every scenario's fingerprint
is recomputed and compared, and a disagreement refuses the export instead of
labelling one scenario's results with another's description.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interop_lab.benchmark.coverage import MeasurementCoverage, build_coverage
from interop_lab.benchmark.model import BenchmarkDefinition
from interop_lab.naming import PRODUCT_NAME
from interop_lab.viewer.fixture import is_synthetic
from interop_lab.viewer.model import (
    VIEWER_SCHEMA_VERSION,
    AssertionSignatureView,
    BenchmarkIdentity,
    CapabilitySurface,
    CategorySummary,
    CellState,
    ContainmentEvidence,
    ContainmentFact,
    ContainmentOverheadPair,
    DatasetProvenance,
    DisclosureControls,
    EvidenceKind,
    LimitationEntry,
    MatrixCell,
    MethodologySection,
    OverviewTotals,
    ProjectSummary,
    RepetitionResult,
    ReproductionSummary,
    SignatureRecord,
    TimingCard,
    ToolSurface,
    ViewerDataset,
    ViewerScenario,
    ViewerTarget,
    VisibilityMode,
)
from interop_lab.viewer.source import BenchmarkSource, BenchmarkSourceError

#: The banner a non-private build wears. Exact strings, asserted by the frontend
#: tests and by the privacy tests, so a wording change cannot quietly soften them.
PUBLIC_SAFE_EYEBROW = "PUBLIC-SAFE SYNTHETIC EVIDENCE"
#: The same evidence before the public-safe transform. A private build of the
#: fixture is synthetic without being public-safe, and claiming otherwise would
#: mislabel the one dataset the guided-mode tests run against.
SYNTHETIC_EYEBROW = "SYNTHETIC EVIDENCE"
SYNTHETIC_DISCLAIMER = (
    "This demonstration uses invented implementations and synthetic measurements. "
    "No third-party benchmark result is displayed."
)
#: Used when a public-safe document really is a redaction of real measurements,
#: which is a different claim and must not borrow the synthetic wording.
PUBLIC_SAFE_ANONYMISED_EYEBROW = "PUBLIC-SAFE ANONYMISED EVIDENCE"
ANONYMISED_DISCLAIMER = (
    "Anonymised real measurements. Third-party identities, versions and "
    "fingerprints are withheld; the results themselves were measured."
)

#: What the project is, in the terms the repository already uses. Committed here
#: rather than written in the frontend so the one place a reader meets a claim
#: about this project is a place the Python tests can hold to it.
PROJECT = ProjectSummary(
    name=PRODUCT_NAME,
    tagline=(
        "Version-aware hostile protocol testing, differential comparison, replay "
        "and automatic failure reduction for Model Context Protocol servers."
    ),
    problem=(
        "Two MCP servers can both answer a happy-path conformance run and still "
        "disagree about what happens when the wire is malformed, the profile is "
        "older than the requirement, the transport is aborted mid-body or the "
        "process is killed. Those disagreements are where interoperability "
        "actually fails, and a pass/fail suite reports none of them."
    ),
    approach=[
        "Scenarios are gated to dated specification revisions. A 2026-07-28 "
        "requirement is never asserted against a 2025-11-25 target; it is "
        "NOT_APPLICABLE with the reason recorded.",
        "Every normative assertion cites the dated requirement behind it. An "
        "assertion without a citation is an opinion, and the test suite enforces "
        "that.",
        "A divergence is not a defect. Implementations differing where no dated "
        "requirement decides between them is recorded as BEHAVIORAL_DIVERGENCE.",
        "Stability means complete result-signature equality across repetitions — "
        "classification, failing assertion identities, process outcome, cleanup, "
        "containment and fingerprints — never a repeated label.",
        "Failures are reduced automatically to the smallest case that preserves "
        "the failure signature, and packaged so someone else can re-run them.",
    ],
    not_claimed=[
        "No overall ranking, no aggregate score, no best implementation. The "
        "categories have different denominators and different evidence behind "
        "them, and a single number would be read as though they did not.",
        "Absence of a finding is absence of a finding from this battery. It is "
        "never evidence of correctness.",
        "A security-relevant observation is not a vulnerability. Practical "
        "exploitability is a separate, human-reached status that no execution "
        "path writes.",
        "Timing figures are not comparable across machines, and a duration "
        "comparison between implementations written in different languages "
        "measures the languages as much as anything else.",
    ],
)

#: Classification strings that are results rather than states.
_CLASSIFICATION_STATES = {state.value for state in CellState}


class ViewerExportError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def _cell_state(classification: str | None) -> CellState:
    if classification is None:
        return CellState.NOT_MEASURED
    if classification in _CLASSIFICATION_STATES:
        return CellState(classification)
    return CellState.INCONCLUSIVE


def _targets(
    definition: BenchmarkDefinition,
    capability_records: dict[str, dict[str, Any]],
) -> list[ViewerTarget]:
    targets: list[ViewerTarget] = []
    for pinned in definition.targets:
        record = capability_records.get(pinned.target_key) or {}
        surface = record.get("capabilities") or {}
        targets.append(
            ViewerTarget(
                target_key=pinned.target_key,
                display_name=pinned.target_key,
                target_id=pinned.target_id,
                target_file=pinned.target_file,
                source_version=pinned.source_version,
                source_commit=pinned.source_commit,
                build_context=pinned.build_context,
                language=pinned.language,
                transport=pinned.transport,
                declared_profile=pinned.declared_profile,
                negotiated_profile=surface.get("protocol_profile_negotiated"),
                relationship=pinned.relationship,
                target_fingerprint=pinned.target_fingerprint,
                portable_identity_fingerprint=pinned.portable_identity_fingerprint,
                dockerfile_sha256=pinned.dockerfile_sha256,
                lock_sha256=pinned.lock_sha256,
                source_archive_sha256=pinned.source_archive_sha256,
                capability_fingerprint=record.get("capability_fingerprint"),
                official_conformance=pinned.official_conformance.value,
                official_conformance_detail=pinned.official_conformance_detail,
                limitations=[
                    "Every figure attributed to this target is valid for this pinned "
                    "version, this built image, the profile it negotiated and the "
                    "containment recorded beside it. It generalises to nothing else.",
                ],
            )
        )
    return targets


def _scenarios(definition: BenchmarkDefinition) -> tuple[list[ViewerScenario], list[str]]:
    """Join the pinned scenario set to the library's prose, fingerprint-checked."""
    from interop_lab.ci.baseline import scenario_fingerprint
    from interop_lab.scenarios.library import ALL_SCENARIOS

    problems: list[str] = []
    scenarios: list[ViewerScenario] = []
    for pinned in definition.scenarios:
        live = ALL_SCENARIOS.get(pinned.scenario_id)
        if live is None:
            problems.append(
                f"{pinned.scenario_id}: pinned by the benchmark and absent from this "
                "harness's scenario library, so its description cannot be shown "
                "beside its results"
            )
            continue
        observed = scenario_fingerprint(live)
        if observed != pinned.scenario_fingerprint:
            problems.append(
                f"{pinned.scenario_id}: the benchmark pinned fingerprint "
                f"{pinned.scenario_fingerprint} and this harness computes {observed}. "
                "The scenario changed after the run, so describing these results "
                "with the current text would label one scenario's results with "
                "another's description."
            )
            continue

        definition_ = live.definition
        scenarios.append(
            ViewerScenario(
                scenario_id=pinned.scenario_id,
                version=pinned.version,
                title=definition_.title,
                category=pinned.category.value,
                scenario_category=definition_.category.value,
                nature=definition_.nature.value,
                purpose=definition_.purpose,
                expected_outcome=definition_.expected_outcome,
                normative_source=definition_.normative_source,
                rationale=definition_.rationale,
                severity=definition_.severity.value,
                security_category=(
                    definition_.security_category.value
                    if definition_.security_category is not None
                    else None
                ),
                scenario_fingerprint=pinned.scenario_fingerprint,
                requires_observed_surface=pinned.requires_observed_surface,
                applicability_profiles=list(definition_.applicability.profiles),
                applicability_transports=[
                    transport.value for transport in definition_.applicability.transports
                ],
                applicability_min_spec_version=definition_.applicability.min_spec_version,
                applicability_max_spec_version=definition_.applicability.max_spec_version,
                requires_tools=list(definition_.applicability.requires_tools),
                requires_transport_features={
                    key: str(value)
                    for key, value in definition_.applicability.requires_transport_features.items()
                },
                requires_safe_tool_invocation=(
                    definition_.applicability.requires_safe_tool_invocation
                ),
                assertion_ids=sorted(
                    {assertion.assertion_id for step in live.steps for assertion in step.assertions}
                ),
                limitations=[
                    "The official conformance framework's verdict on this scenario, "
                    "where one exists, is recorded in a separate vocabulary and is "
                    "never merged into these classifications.",
                ],
            )
        )
    return scenarios, problems


def _categories(source: BenchmarkSource) -> list[CategorySummary]:
    from interop_lab.benchmark.reporting import CATEGORY_LIMITATIONS

    security_dimension = {
        "security_relevant_observations": (
            "Security-relevant is not vulnerable. Every observation here carries "
            "vulnerability_status, which states what has actually been established."
        ),
    }
    summaries: list[CategorySummary] = []
    for raw in source.json("category-observations.json") or []:
        category = str(raw.get("category"))
        summaries.append(
            CategorySummary(
                category=category,
                target_key=raw.get("target_key"),
                applicable_scenarios=int(raw.get("applicable_scenarios", 0)),
                executed_scenarios=int(raw.get("executed_scenarios", 0)),
                stable_pass=int(raw.get("stable_pass", 0)),
                stable_non_pass=int(raw.get("stable_non_pass", 0)),
                inconclusive=int(raw.get("inconclusive", 0)),
                not_applicable=int(raw.get("not_applicable", 0)),
                not_measured=int(raw.get("not_measured", 0)),
                expected_difference=int(raw.get("expected_difference", 0)),
                behavioral_divergence=int(raw.get("behavioral_divergence", 0)),
                spec_violation=int(raw.get("spec_violation", 0)),
                harness_error=int(raw.get("harness_error", 0)),
                security_relevant_observations=int(raw.get("security_relevant_observations", 0)),
                confirmed_private_vulnerabilities=int(
                    raw.get("confirmed_private_vulnerabilities", 0)
                ),
                security_dimension=security_dimension.get(category, ""),
                limitations=list(raw.get("limitations") or [])
                or [
                    text
                    for key, texts in CATEGORY_LIMITATIONS.items()
                    if key.value == category
                    for text in texts
                ],
            )
        )
    return summaries


def _overview(definition: BenchmarkDefinition, categories: list[CategorySummary]) -> OverviewTotals:
    """Totals, summed from the per-category counts and never from raw samples.

    Summed rather than recomputed because the category observations already
    applied the rule that decides these numbers: a scenario counts once, and only
    when its repetitions agreed on a complete signature. Recounting from samples
    here would produce a second, laxer definition of "stable" living beside the
    strict one.
    """
    totals = OverviewTotals(
        target_count=len(definition.targets),
        languages=sorted({target.language for target in definition.targets}),
        transports=sorted(definition.transports),
        profiles=sorted(definition.profiles),
        scenario_count=len(definition.scenarios),
        correctness_repetitions=definition.correctness_repetitions,
    )
    for summary in categories:
        totals.applicable_results += summary.applicable_scenarios
        totals.stable_pass += summary.stable_pass
        totals.stable_non_pass += summary.stable_non_pass
        totals.inconclusive += summary.inconclusive
        totals.not_applicable += summary.not_applicable
        totals.not_measured += summary.not_measured
        totals.harness_error += summary.harness_error
        totals.security_relevant_observations += summary.security_relevant_observations
        totals.confirmed_private_vulnerabilities += summary.confirmed_private_vulnerabilities
    return totals


def _signatures(source: BenchmarkSource) -> list[SignatureRecord]:
    records: list[SignatureRecord] = []
    for raw in source.json("result-signatures.json") or []:
        signature = raw.get("signature") or {}
        dimensions = signature.get("dimensions") or {}
        records.append(
            SignatureRecord(
                target_key=str(raw.get("target_key")),
                scenario_id=str(raw.get("scenario_id")),
                repetition=int(raw.get("repetition", 0)),
                digest=str(raw.get("digest")),
                signature_version=int(signature.get("signature_version", 1)),
                classification=str(signature.get("classification")),
                failed_assertion_ids=list(signature.get("failed_assertion_ids") or []),
                assertions=[
                    AssertionSignatureView(
                        assertion_id=str(entry.get("assertion_id")),
                        status=str(entry.get("status")),
                        classification=entry.get("classification"),
                        security_relevant=bool(entry.get("security_relevant", False)),
                    )
                    for entry in signature.get("assertions") or []
                ],
                timeout_cause=signature.get("timeout_cause"),
                exit_code=signature.get("exit_code"),
                signal=signature.get("signal"),
                killed_by_harness=bool(signature.get("killed_by_harness", False)),
                cleanup_ok=bool(signature.get("cleanup_ok", True)),
                containment_outcome=signature.get("containment_outcome"),
                containment_usable=bool(signature.get("containment_usable", True)),
                target_fingerprint=signature.get("target_fingerprint"),
                scenario_fingerprint=signature.get("scenario_fingerprint"),
                capability_fingerprint=signature.get("capability_fingerprint"),
                security_relevant=bool(dimensions.get("security_relevant", False)),
                security_category=dimensions.get("security_category"),
                vulnerability_status=str(dimensions.get("vulnerability_status", "not_assessed")),
                normative=bool(dimensions.get("normative", False)),
                first_meaningful_difference=signature.get("first_meaningful_difference"),
            )
        )
    return records


def _matrix(
    source: BenchmarkSource,
    definition: BenchmarkDefinition,
    categories: list[CategorySummary],
) -> list[MatrixCell]:
    scenario_by_id = {scenario.scenario_id: scenario for scenario in definition.scenarios}
    target_by_key = {target.target_key: target for target in definition.targets}
    denominators = {
        (summary.target_key, summary.category): summary.applicable_scenarios
        for summary in categories
    }
    negotiated: dict[str, str | None] = {}
    for record in source.json("capability-results.json") or []:
        surface = record.get("capabilities") or {}
        negotiated[str(record.get("target_key"))] = surface.get("protocol_profile_negotiated")

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in source.json("classification-results.json") or []:
        grouped[(str(raw.get("target_key")), str(raw.get("scenario_id")))].append(raw)

    digests: dict[tuple[str, str], list[str]] = defaultdict(list)
    for raw in source.json("result-signatures.json") or []:
        digests[(str(raw.get("target_key")), str(raw.get("scenario_id")))].append(
            str(raw.get("digest"))
        )

    cells: list[MatrixCell] = []
    for target in definition.targets:
        for scenario in definition.scenarios:
            key = (target.target_key, scenario.scenario_id)
            repetitions = sorted(
                grouped.get(key, []), key=lambda raw: int(raw.get("repetition", 0))
            )
            category = scenario.category.value
            observed_digests = digests.get(key, [])

            if not repetitions:
                cells.append(
                    MatrixCell(
                        target_key=target.target_key,
                        scenario_id=scenario.scenario_id,
                        category=category,
                        state=CellState.NOT_MEASURED,
                        applicable_denominator=denominators.get((target.target_key, category), 0),
                        applicable=False,
                        repetitions=0,
                        valid_repetitions=0,
                        profile=negotiated.get(target.target_key) or target.declared_profile,
                        transport=target.transport,
                        limitations=[
                            "No sample exists for this pair in this execution. Nobody "
                            "looked; this is not a result about the target.",
                        ],
                    )
                )
                continue

            valid = [raw for raw in repetitions if raw.get("valid_sample")]
            classifications = {raw.get("classification") for raw in valid}
            dimensions = [raw.get("dimensions") or {} for raw in valid]
            first = valid[0] if valid else repetitions[0]
            distinct_digests = sorted(set(observed_digests))

            if not valid:
                state = CellState.HARNESS_ERROR
                applicable = False
            elif len(classifications) > 1 or len(distinct_digests) > 1:
                # Repetitions disagreed. Not a result about the target, and never
                # resolved to whichever outcome happened to appear most often.
                state = CellState.INCONCLUSIVE
                applicable = True
            else:
                state = _cell_state(first.get("classification"))
                applicable = state is not CellState.NOT_APPLICABLE

            stable: bool | None = None
            if len(observed_digests) >= 2:
                stable = len(distinct_digests) == 1

            pinned_scenario = scenario_by_id[scenario.scenario_id]
            cells.append(
                MatrixCell(
                    target_key=target.target_key,
                    scenario_id=scenario.scenario_id,
                    category=category,
                    state=state,
                    applicable_denominator=denominators.get((target.target_key, category), 0),
                    applicable=applicable and state is not CellState.HARNESS_ERROR,
                    repetitions=len(repetitions),
                    valid_repetitions=len(valid),
                    signature_stable=stable,
                    signature_digests=distinct_digests,
                    repetition_results=[
                        RepetitionResult(
                            repetition=int(raw.get("repetition", 0)),
                            classification=raw.get("classification"),
                            valid_sample=bool(raw.get("valid_sample", False)),
                            rejections=[
                                str(detail) for detail in raw.get("rejection_detail") or []
                            ],
                            result_signature_digest=None,
                        )
                        for raw in repetitions
                    ],
                    profile=negotiated.get(target.target_key)
                    or target_by_key[target.target_key].declared_profile,
                    transport=target.transport,
                    security_relevant=any(d.get("security_relevant") for d in dimensions),
                    security_category=next(
                        (
                            d.get("security_category")
                            for d in dimensions
                            if d.get("security_category")
                        ),
                        None,
                    ),
                    vulnerability_status=next(
                        (
                            str(d.get("vulnerability_status"))
                            for d in dimensions
                            if d.get("vulnerability_status")
                        ),
                        "not_assessed",
                    ),
                    normative=any(d.get("normative") for d in dimensions),
                    limitations=_cell_limitations(state, pinned_scenario.requires_observed_surface),
                )
            )
    return cells


def _cell_limitations(state: CellState, requires_observed_surface: bool) -> list[str]:
    limitations = [
        "Valid for this pinned target version, this built image, the negotiated "
        "profile and the containment recorded beside it.",
    ]
    if state is CellState.NOT_APPLICABLE:
        limitations.append(
            "Outside every denominator. A scenario that could not apply is not a "
            "scenario the target failed."
            + (
                " Applicability here rested on what the target advertised, not on "
                "what its definition claimed."
                if requires_observed_surface
                else ""
            )
        )
    if state is CellState.INCONCLUSIVE:
        limitations.append(
            "The repetitions disagreed. This is a fact about this harness rather "
            "than about the target, and it is never resolved to a majority."
        )
    if state is CellState.HARNESS_ERROR:
        limitations.append(
            "Every repetition was rejected. The harness or the environment failed, "
            "and this is never a verdict about the target."
        )
    return limitations


def _capabilities(
    source: BenchmarkSource, signatures: list[SignatureRecord]
) -> list[CapabilitySurface]:
    observed_fingerprints: dict[str, set[str]] = defaultdict(set)
    for record in signatures:
        if record.capability_fingerprint:
            observed_fingerprints[record.target_key].add(record.capability_fingerprint)

    surfaces: list[CapabilitySurface] = []
    for record in source.json("capability-results.json") or []:
        target_key = str(record.get("target_key"))
        surface = record.get("capabilities") or {}
        tools = [
            ToolSurface(
                name=str(tool.get("name")),
                input_schema_fingerprint=tool.get("input_schema_hash"),
                output_schema_fingerprint=tool.get("output_schema_hash"),
            )
            for tool in surface.get("tools") or []
        ]
        declared = [str(name) for name in surface.get("declared_tools") or []]
        observed_names = [tool.name for tool in tools]
        differences: list[str] = []
        if declared and sorted(declared) != sorted(observed_names):
            for name in sorted(set(declared) - set(observed_names)):
                differences.append(f"declared and not observed: {name}")
            for name in sorted(set(observed_names) - set(declared)):
                differences.append(f"observed and not declared: {name}")

        seen = observed_fingerprints.get(target_key, set())
        surfaces.append(
            CapabilitySurface(
                target_key=target_key,
                status=str(surface.get("status") or "not_attempted"),
                status_detail=surface.get("status_detail"),
                negotiated_profile=surface.get("protocol_profile_negotiated"),
                offered_profile=surface.get("protocol_profile_offered"),
                capability_fingerprint=record.get("capability_fingerprint"),
                server_capabilities={
                    key: str(value)
                    for key, value in (surface.get("server_capabilities") or {}).items()
                },
                tools=tools,
                declared_tools=declared,
                advertises_resources=bool(surface.get("advertises_resources", False)),
                advertises_prompts=bool(surface.get("advertises_prompts", False)),
                advertises_logging=bool(surface.get("advertises_logging", False)),
                advertises_completions=bool(surface.get("advertises_completions", False)),
                observed_transport_features={
                    key: str(value)
                    for key, value in (surface.get("observed_transport_features") or {}).items()
                    if value is not None
                },
                declared_versus_observed=differences,
                fingerprint_stable_across_repetitions=(len(seen) == 1 if seen else None),
                limitations=list(surface.get("limitations") or [])
                or [
                    "One surface, observed once and held to by every later sample. "
                    "A surface that moved mid-execution rejects the samples on both "
                    "sides of the move rather than being averaged.",
                ],
            )
        )
    return surfaces


_CONTAINMENT_FACTS: tuple[tuple[str, str, str], ...] = (
    ("user", "user", "user"),
    ("network", "network", "network_mode"),
    ("read-only root", "read_only_root", "read_only_root"),
    ("capabilities dropped", "drop_all_capabilities", "cap_drop"),
    ("no-new-privileges", "no_new_privileges", "security_opt"),
    ("memory", "memory_mb", "memory_limit_bytes"),
    ("CPU", "cpus", "nano_cpus"),
    ("PID limit", "pids_limit", "pids_limit"),
)


def _containment(source: BenchmarkSource) -> list[ContainmentEvidence]:
    evidence: list[ContainmentEvidence] = []
    for record in source.json("containment-results.json") or []:
        containment = record.get("containment")
        target_key = str(record.get("target_key"))
        if not containment:
            evidence.append(
                ContainmentEvidence(
                    target_key=target_key,
                    mode="unrecorded",
                    provenance="unrecorded",
                    outcome="not_measured",
                    usable=False,
                    unavailable_reason=(
                        "this execution recorded no containment record for this "
                        "target, so what was requested and what was obtained are "
                        "both unknown here"
                    ),
                    limitations=[
                        "Absent evidence, not weak containment. Nothing here says "
                        "the target ran uncontained.",
                    ],
                )
            )
            continue

        requested = containment.get("requested") or {}
        observed = containment.get("observed") or {}
        resolved = containment.get("resolved_image") or {}
        facts: list[ContainmentFact] = []
        for label, requested_key, observed_key in _CONTAINMENT_FACTS:
            requested_value = requested.get(requested_key)
            observed_value = observed.get(observed_key)
            facts.append(
                ContainmentFact(
                    name=label,
                    requested=None if requested_value is None else str(requested_value),
                    observed=None if observed_value is None else str(observed_value),
                    # Deliberately absent rather than False when either side is
                    # unrecorded: "we did not observe this" and "this disagreed"
                    # are different facts and a boolean cannot hold both.
                    agrees=None
                    if requested_value is None or observed_value is None
                    else _facts_agree(requested_key, requested_value, observed_value),
                )
            )
        evidence.append(
            ContainmentEvidence(
                target_key=target_key,
                mode=str(containment.get("mode")),
                provenance=str(containment.get("provenance")),
                outcome=str(containment.get("outcome")),
                usable=str(containment.get("outcome")) in {"enforced", "verified"},
                image_source_kind=(requested.get("image_source") or {}).get("kind"),
                portable_build_identity=observed.get("build_identity"),
                builder_local_image_identity=observed.get("observed_image_id")
                or resolved.get("launch_reference"),
                facts=facts,
                cleanup_ok=None,
                weaknesses=[str(text) for text in containment.get("weaknesses") or []],
                unavailable_reason=containment.get("unavailable_reason"),
                limitations=[str(text) for text in containment.get("stated_limits") or []]
                or [
                    "Requested and observed are recorded separately and never "
                    "merged. A builder-local image identity is this builder's name "
                    "for these bits and is not portable.",
                ],
            )
        )
    return evidence


def _facts_agree(key: str, requested: Any, observed: Any) -> bool:
    """Whether one requested restriction was actually applied.

    Three of these compare values in different units on the two sides — megabytes
    against bytes, CPUs against nano-CPUs, a boolean against a list of flags — so
    the comparison is per-field rather than an equality that would report
    disagreement on every correctly applied limit.
    """
    if key == "memory_mb":
        return int(observed) == int(requested) * 1024 * 1024
    if key == "cpus":
        return int(observed) == int(float(requested) * 1_000_000_000)
    if key == "drop_all_capabilities":
        return bool(requested) is ("ALL" in [str(item).upper() for item in observed or []])
    if key == "no_new_privileges":
        flags = [str(item).lower() for item in observed or []]
        return bool(requested) is any("no-new-privileges" in flag for flag in flags)
    if key == "network":
        return str(observed) == str(requested)
    return str(observed) == str(requested)


def _timing_card_from_summary(raw: dict[str, Any], *, environment: str) -> TimingCard:
    samples = [float(value) for value in raw.get("samples_ms") or []]
    return TimingCard(
        name=str(raw.get("name")),
        unit=str(raw.get("unit", "ms")),
        state=str(raw.get("state", "not_measured")),
        sample_count=len(samples),
        warmup_count=int(raw.get("warmup_count", 0)),
        samples_ms=samples,
        median_ms=raw.get("median_ms"),
        min_ms=raw.get("min_ms"),
        max_ms=raw.get("max_ms"),
        single_value_ms=raw.get("value_ms"),
        rejected_samples=int(raw.get("rejected_samples", 0)),
        environment=environment,
        insufficient_reason=raw.get("insufficient_reason"),
        limitations=[str(text) for text in raw.get("limitations") or []],
    )


def _timings(source: BenchmarkSource, coverage: MeasurementCoverage) -> list[TimingCard]:
    """Every timing the execution produced, plus a card for every metric it did not.

    The second half is the point. A screen showing only the timings that exist
    invites a reader to count them and conclude that is the whole set, so every
    NOT_MEASURED metric in the coverage ledger gets a card of its own carrying
    its reason.
    """
    environment_record = source.json("environment.json") or {}
    environment = " · ".join(
        str(environment_record.get(key))
        for key in ("os", "machine", "python", "docker_engine_version")
        if environment_record.get(key)
    )

    cards: list[TimingCard] = []
    for raw in source.json("timings.json") or []:
        if isinstance(raw, dict):
            cards.append(_timing_card_from_summary(raw, environment=environment))

    for raw in (source.json("measured-operations.json") or {}).get("operations") or []:
        samples = [float(value) for value in raw.get("samples_ms") or []]
        cards.append(
            TimingCard(
                name=str(raw.get("display_name")),
                metric_id=str(raw.get("metric_id")),
                unit=str(raw.get("unit", "ms")),
                state=str(raw.get("state", "not_measured")),
                sample_count=len(samples),
                warmup_count=int(raw.get("warmup_count", 0)),
                samples_ms=samples,
                warmup_samples_ms=[float(v) for v in raw.get("warmup_samples_ms") or []],
                median_ms=raw.get("median_ms"),
                min_ms=raw.get("min_ms"),
                max_ms=raw.get("max_ms"),
                environment=str(raw.get("environment") or environment),
                insufficient_reason=raw.get("insufficient_reason"),
                limitations=[str(text) for text in raw.get("limitations") or []],
            )
        )

    for entry in coverage.entries:
        if entry.state.value != "not_measured":
            continue
        cards.append(
            TimingCard(
                name=entry.display_name,
                metric_id=entry.metric_id,
                state="not_measured",
                sample_count=0,
                environment=entry.environment,
                insufficient_reason=entry.reason_not_measured,
                limitations=[entry.limitation],
            )
        )
    return cards


def _containment_overhead(source: BenchmarkSource) -> list[ContainmentOverheadPair]:
    payload = source.json("containment-overhead.json") or {}
    environment_record = payload.get("environment") or {}
    environment = str(environment_record.get("os") or "")
    pairs: list[ContainmentOverheadPair] = []
    for raw in payload.get("pairs") or []:
        host = raw.get("host")
        contained = raw.get("contained")
        pairs.append(
            ContainmentOverheadPair(
                transport=str(raw.get("transport")),
                state=str(raw.get("state", "not_measured")),
                scenario=raw.get("scenario"),
                host=_timing_card_from_summary(host, environment=environment) if host else None,
                contained=(
                    _timing_card_from_summary(contained, environment=environment)
                    if contained
                    else None
                ),
                overhead_ratio=raw.get("overhead_ratio"),
                overhead_absolute_ms=raw.get("overhead_absolute_ms"),
                reason=raw.get("reason") or raw.get("insufficient_reason"),
                limitations=[str(text) for text in raw.get("limitations") or []],
            )
        )
    return pairs


def _methodology(definition: BenchmarkDefinition) -> list[MethodologySection]:
    """The rules, as a first-class view rather than a footer.

    Every one of these is a rule a reader would otherwise have to infer from the
    shape of a table, and inferring them wrongly is how a category-level
    observation becomes "this SDK is worse".
    """
    return [
        MethodologySection(
            section_id="denominator",
            title="The denominator",
            body=[
                "`applicable` is the denominator for every rate. It is stated on "
                "every cell rather than implied by a table's width.",
                "`NOT_APPLICABLE` sits outside it. A scenario that could not apply "
                "is not a scenario the target failed, and folding it in turns "
                '"this target speaks stdio" into a lower score.',
                "`NOT_MEASURED` sits outside it in the other direction. Nobody "
                "looked, and a gap in coverage is not a defect in someone's server.",
                "`INCONCLUSIVE` is a fact about this harness. A scenario whose "
                "repetitions disagreed is counted inconclusive, never as whichever "
                "outcome appeared most often.",
            ],
        ),
        MethodologySection(
            section_id="no-ranking",
            title="No ranking",
            body=[
                "There is no best implementation here, no safest one, no most "
                "compliant one and no aggregate score.",
                f"{len(definition.scenarios)} scenarios against {len(definition.targets)} "
                "implementations cannot support one, and a single number would be "
                "read as though it could.",
                "Rows are ordered alphabetically. They are not comparable as wholes: "
                "each target has a different applicable set, because applicability "
                "depends on the transport it speaks and the surface it advertises.",
                "More green cells does not mean better. It frequently means a "
                "narrower applicable set.",
            ],
        ),
        MethodologySection(
            section_id="security-dimension",
            title="Security relevance is a separate dimension",
            body=[
                "The primary classification says what the target did. Security "
                "relevance says what it means for exposure. They are independent, "
                "and a result can be a normative failure and security-relevant at "
                "once.",
                "Three counts are separate and must not be conflated: normative "
                "failures, security-relevant observations, and confirmed private "
                "vulnerabilities.",
                "The third is reached by a person working a result up. No execution "
                "path in this harness writes it.",
            ],
        ),
        MethodologySection(
            section_id="result-signature",
            title="What makes two runs the same result",
            body=[
                "Stability requires equality of a complete result signature, not a "
                "repeated label. Two runs can both report SPEC_VIOLATION while "
                "different assertions failed.",
                "The signature carries the classification, the failing assertion "
                "identities, each assertion's own classification, the timeout "
                "cause, exit code, signal, whether the harness did the killing, "
                "cleanup and containment outcomes, the target, scenario and "
                "capability fingerprints, and the result dimensions.",
                "It deliberately excludes durations, timestamps and run "
                "identifiers. A signature that moved on those would report "
                "instability on every run and mean nothing.",
            ],
        ),
        MethodologySection(
            section_id="timing",
            title="Timing methodology",
            body=[
                "Monotonic clocks throughout. Median, minimum, maximum and every "
                "individual sample; no confidence interval and no significance "
                "claim.",
                f"One warm-up excluded and recorded, {definition.timing_measured_runs} "
                "measured samples. A metric below that floor reports its samples "
                "and no headline figure.",
                "Image build time is never combined with suite execution time, and "
                "no duration is obtained by subtracting one measurement from "
                "another.",
                "Timing values are never compared across machines. A shared runner "
                "varies between runs for reasons that have nothing to do with the "
                "code under measurement.",
            ],
        ),
        MethodologySection(
            section_id="containment",
            title="Containment scope",
            body=[
                "Requested and observed are recorded separately and never merged. "
                "A restriction that was asked for and not applied is the case this "
                "pair exists to surface.",
                "A run without usable containment is not evidence about the target, "
                "and its samples are rejected rather than reported with a caveat.",
                "Builder-local image identifiers differ between storage drivers for "
                "identical bits. They are not target drift, and cross-environment "
                "agreement is asserted over the portable build inputs instead.",
            ],
        ),
        MethodologySection(
            section_id="official-conformance",
            title="The official conformance boundary",
            body=[
                "The official conformance framework is a baseline, not a "
                "competitor. Its outcomes are recorded in a separate vocabulary and "
                "never merged into these classifications.",
                "It was not executed against the pinned targets here. Running it "
                "would require publishing a host port or building a contained "
                "runner, and weakening containment to obtain a conformance number "
                "would invalidate every other result measured under it.",
            ],
        ),
        MethodologySection(
            section_id="cross-environment",
            title="Cross-environment status",
            body=[
                "One environment executed and recorded in full.",
                "Agreement across environments is asserted over identities — "
                "fingerprints, negotiated profiles, classifications and complete "
                "result signatures — and never over durations.",
                "Until a second environment's artifacts are placed where "
                "`benchmark compare` can read them, cross-environment agreement is "
                "partially measured rather than inferred from a single machine.",
            ],
        ),
        MethodologySection(
            section_id="privacy",
            title="Privacy status",
            body=[
                "These results concern software this project does not own. They "
                "are private, they are not published in whole or in part, and no "
                "maintainer has been contacted.",
                "A public-safe dataset excludes third-party identities, versions, "
                "finding identifiers, raw traces and reproduction payloads, and is "
                "scanned against a denylist generated from the private inventories "
                "before it may be built.",
                "Anonymisation alone is not treated as sufficient. Language, exact "
                "version, transport and result can re-identify a target in "
                "combination, so anonymised labels standing in for real identities "
                "require explicit owner approval.",
            ],
        ),
    ]


def _limitations(definition: BenchmarkDefinition, source: BenchmarkSource) -> list[LimitationEntry]:
    entries = [LimitationEntry(scope="benchmark", text=text) for text in definition.limitations]
    entries += [
        LimitationEntry(scope="execution", text=text) for text in source.outputs.limitations
    ]
    entries.append(
        LimitationEntry(
            scope="viewer",
            text=(
                "This viewer displays values read from benchmark artifacts whose "
                "hashes the execution manifest names. It computes no result of its "
                "own, and a value absent from those artifacts is shown as "
                "NOT_MEASURED rather than derived."
            ),
        )
    )
    return entries


def _reproduction(repo_root: Path, *, private: bool) -> list[ReproductionSummary]:
    """Reproduction packages, described by their shape and never their contents.

    The synthetic example packages are first-party fixtures with defects this
    repository authored, and describing them exposes nothing about anyone else.
    Private finding packages are counted and characterised — status, stability,
    minimisation — and never opened: the payload is the part that must not travel,
    and a summary that quoted one would defeat every other control here.
    """
    import json as _json

    summaries: list[ReproductionSummary] = []

    examples = repo_root / "artifacts" / "examples"
    for directory in sorted(p for p in examples.glob("*") if p.is_dir()):
        minimization_path = directory / "minimization.json"
        minimization: dict[str, Any] = {}
        if minimization_path.is_file():
            try:
                minimization = _json.loads(minimization_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                minimization = {}
        summaries.append(
            ReproductionSummary(
                reference=f"artifacts/examples/{directory.name}",
                status="packaged",
                stability_runs=int(minimization.get("stability_runs", 0) or 0),
                minimization_status=(
                    "already_minimal"
                    if minimization.get("already_minimal")
                    else "reduced"
                    if minimization.get("reduced")
                    else "not_measured"
                ),
                original_event_count=minimization.get("original_event_count"),
                minimized_event_count=minimization.get("minimized_event_count"),
                original_body_bytes=minimization.get("original_body_bytes"),
                minimized_body_bytes=minimization.get("minimized_body_bytes"),
                preserved_signature=bool(minimization.get("stable")) if minimization else None,
                replay_requirements=[
                    "The package carries the portable build inputs, never the "
                    "original machine's image identifier. A locally built target "
                    "must be rebuilt on the replaying machine.",
                ],
                synthetic=True,
                limitations=[
                    "A first-party fixture with a deliberate defect. It is a test of "
                    "this harness, not a finding about anyone's implementation.",
                    "Minimisation preserves the failure signature. It does not "
                    "establish that the reduced case is the smallest one that could.",
                ],
            )
        )

    if not private:
        return summaries

    findings = repo_root / "artifacts" / "private-findings"
    for directory in sorted(p for p in findings.glob("*") if p.is_dir()):
        minimization_path = directory / "minimization.json"
        minimization = {}
        if minimization_path.is_file():
            try:
                minimization = _json.loads(minimization_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                minimization = {}
        summaries.append(
            ReproductionSummary(
                reference=directory.name,
                status="private_finding_package",
                stability_runs=int(minimization.get("stability_runs", 0) or 0),
                minimization_status=(
                    "already_minimal"
                    if minimization.get("already_minimal")
                    else "reduced"
                    if minimization.get("reduced")
                    else "not_measured"
                ),
                original_event_count=minimization.get("original_event_count"),
                minimized_event_count=minimization.get("minimized_event_count"),
                original_body_bytes=minimization.get("original_body_bytes"),
                minimized_body_bytes=minimization.get("minimized_body_bytes"),
                preserved_signature=bool(minimization.get("stable")) if minimization else None,
                replay_requirements=[
                    "Private. The payload is not carried into this dataset in "
                    "either mode, and is never carried into a build.",
                ],
                synthetic=False,
                limitations=[
                    "Undisclosed. No maintainer has been contacted and nothing here "
                    "may be published.",
                ],
            )
        )
    return summaries


def _provenance(definition: BenchmarkDefinition, *, synthetic: bool) -> DatasetProvenance:
    """State separately what defined this document and what filled it in.

    Built here, in the private assembly path, so that both modes derive their
    labelling from the same place. The public-safe transform narrows it; it does
    not get to invent a different story about where the evidence came from.
    """
    lineage = (
        f"Generated from the Benchmark {definition.benchmark_schema_version} schema "
        "and scenario library."
    )
    if synthetic:
        return DatasetProvenance(
            schema_lineage=lineage,
            viewer_schema_version=VIEWER_SCHEMA_VERSION,
            benchmark_schema_version=definition.benchmark_schema_version,
            harness_version=definition.harness_version,
            evidence_kind=EvidenceKind.SYNTHETIC,
            evidence_note=(
                "Every implementation, result and measurement in this document is "
                "invented. The scenario library and the schema are real; nothing "
                "that was measured is."
            ),
            # Nothing is withheld, and saying otherwise would suggest these rows
            # are a masked version of results that exist somewhere.
            benchmark_identity_withheld=False,
            benchmark_identity_note=(
                "No private benchmark identity is withheld, because no private "
                "execution produced this document. These rows are not an "
                "anonymised version of any real benchmark result."
            ),
            # Not the public-safe wording yet: this is the private assembly
            # path, and a private build of synthetic evidence is synthetic
            # without being public-safe. The redaction adds the prefix when the
            # document actually becomes the public-safe one.
            eyebrow=SYNTHETIC_EYEBROW,
            disclaimer=SYNTHETIC_DISCLAIMER,
        )
    return DatasetProvenance(
        schema_lineage=lineage,
        viewer_schema_version=VIEWER_SCHEMA_VERSION,
        benchmark_schema_version=definition.benchmark_schema_version,
        harness_version=definition.harness_version,
        evidence_kind=EvidenceKind.REAL_MEASUREMENT,
        evidence_note=(
            "Measured against real, pinned third-party implementations under "
            "containment. Owner-authorized review only."
        ),
        benchmark_identity_withheld=False,
        benchmark_identity_note=(
            "This document is the private benchmark's own report and names it."
        ),
        eyebrow=definition.benchmark_name,
        disclaimer=None,
    )


def build_dataset(
    source: BenchmarkSource,
    *,
    repo_root: Path,
    mode: VisibilityMode = VisibilityMode.PRIVATE,
) -> ViewerDataset:
    """Translate one verified execution into the private viewer dataset.

    Always the private dataset. Public-safe is produced by redacting this one, so
    there is exactly one assembly path and the sanitised document cannot acquire
    a field the private one never had.
    """
    definition = source.definition
    scenarios, scenario_problems = _scenarios(definition)
    if scenario_problems:
        raise ViewerExportError(scenario_problems)

    capability_records = {
        str(record.get("target_key")): record
        for record in source.json("capability-results.json") or []
    }
    categories = _categories(source)
    signatures = _signatures(source)

    coverage_payload = source.json("measurement-coverage.json")
    coverage = (
        MeasurementCoverage.model_validate(coverage_payload)
        if coverage_payload is not None
        else build_coverage(source.root)
    )

    return ViewerDataset(
        schema_version=VIEWER_SCHEMA_VERSION,
        visibility_mode=VisibilityMode.PRIVATE,
        generated_at=datetime.now(UTC).isoformat(),
        generated_from_harness_commit=source.outputs.harness_commit,
        generated_from_benchmark_manifest_hash=source.manifest_digest,
        provenance=_provenance(definition, synthetic=is_synthetic(source.root)),
        benchmark=BenchmarkIdentity(
            benchmark_name=definition.benchmark_name,
            benchmark_schema_version=definition.benchmark_schema_version,
            harness_version=definition.harness_version,
            definition_harness_commit=source.outputs.definition_harness_commit,
            execution_harness_commit=source.outputs.harness_commit,
            generated_at=source.outputs.generated_at,
            visibility=source.outputs.visibility,
            correctness_repetitions=definition.correctness_repetitions,
            timing_warmup_runs=definition.timing_warmup_runs,
            timing_measured_runs=definition.timing_measured_runs,
            profiles=sorted(definition.profiles),
            transports=sorted(definition.transports),
        ),
        project=PROJECT,
        overview=_overview(definition, categories),
        targets=_targets(definition, capability_records),
        scenarios=scenarios,
        matrix=_matrix(source, definition, categories),
        categories=categories,
        capabilities=_capabilities(source, signatures),
        result_signatures=signatures,
        containment=_containment(source),
        timings=_timings(source, coverage),
        containment_overhead=_containment_overhead(source),
        measurement_coverage=coverage,
        reproduction=_reproduction(repo_root, private=True),
        methodology=_methodology(definition),
        limitations=_limitations(definition, source),
        disclosure_controls=DisclosureControls(
            visibility_mode=VisibilityMode.PRIVATE,
            publishable=False,
            notes=[
                "Private full-evidence dataset. Owner-authorized local review only, "
                "never bundled into a public build and never deployed.",
            ],
        ),
    )


__all__ = [
    "PROJECT",
    "BenchmarkSourceError",
    "ViewerExportError",
    "build_dataset",
]
