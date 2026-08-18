"""Running the external battery in CI, and validating that it really ran.

The previous version of this lived as an inline Python block in a workflow, and
it launched ``mcp-failure-lab run`` with ``check=False`` while discarding the
returned process entirely. That is the specific failure this module exists to
prevent: a fast configuration error — a bad flag, an unreadable target file, a
CLI that exits 2 before touching a container — produces no report, takes half a
second, and is indistinguishable from a successful measurement if nobody looks
at the exit code.

So every repetition is recorded as evidence in its own right: what the command
returned, how long it took, what it wrote, and the hash of what it wrote. A run
that produced nothing is a failure of this job, never a quiet zero.

Exit codes are checked against a closed set. ``0`` means the battery ran and
nothing failed; ``1`` means it ran and something did, which is a legitimate
outcome for an external target and must not fail the job. Anything else is the
CLI reporting that it never got as far as measuring, and is refused.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.containment.resolution import (
    DEFAULT_RESOLUTION_PATH,
    BuildResolutionIndex,
    ImageResolutionError,
)
from interop_lab.external.identity import derive_portable_identity
from interop_lab.external.images import ExternalImage, load_images
from interop_lab.models.core import RunResult
from interop_lab.naming import CLI_COMMAND, HARNESS_VERSION
from interop_lab.results.signature import ResultSignature, signature_for

#: Exit codes that mean the battery actually executed.
#:
#: 0 — ran, nothing failed.
#: 1 — ran, something failed. A legitimate outcome against a third-party target
#:     and deliberately not a job failure: refusing it would mean the matrix
#:     could only stay green while every external implementation stayed perfect.
ALLOWED_EXIT_CODES = frozenset({0, 1})

#: Captured output is evidence, not a log. Bounded so a target that prints a
#: megabyte of diagnostics cannot turn an evidence manifest into a dump.
OUTPUT_CAPTURE_LIMIT = 8_000


def bounded_capture(text: str, limit: int = OUTPUT_CAPTURE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…[truncated, {len(text) - limit} more characters]"


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExternalRunRecord(BaseModel):
    """One repetition of the battery against one target."""

    model_config = ConfigDict(extra="forbid")

    target_key: str
    target_file: str
    repetition: int

    exit_code: int | None = None
    duration_ms: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""

    report_path: str | None = None
    report_sha256: str | None = None
    report_parsed: bool = False

    classifications: dict[str, int] = Field(default_factory=dict)
    #: scenario id → classification, for comparing repetitions.
    per_scenario_classification: dict[str, str] = Field(default_factory=dict)
    #: scenario id → the full typed signature. A classification alone cannot
    #: distinguish two runs that failed different assertions under the same label,
    #: or one killed by the harness from one that crashed on its own.
    per_scenario_result_signature: dict[str, ResultSignature] = Field(default_factory=dict)
    #: scenario id → the digest of the signature above, stored rather than left to
    #: be recomputed by every reader — and checked against its own contents, so a
    #: record edited after the fact fails to agree with itself.
    per_scenario_result_signature_digest: dict[str, str] = Field(default_factory=dict)

    capability_status: str | None = None
    capability_fingerprint: str | None = None
    negotiated_profile: str | None = None
    observed_tools: list[str] = Field(default_factory=list)

    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def recompute_capability_fingerprint(capabilities: dict[str, Any]) -> tuple[str | None, str | None]:
    """Re-derive a serialized capability record's digest. Returns ``(digest, error)``.

    Deliberately routed through ``ResolvedTargetCapabilities`` rather than
    re-implementing the hash here. A second implementation of a fingerprint is a
    second definition of what a capability surface *is*, and the two would agree
    right up until the day the real one changed — at which point this job would
    certify agreement with a rule nobody else follows.

    The recorded digest is stripped before validation so the model recomputes
    from the contents instead of accepting what the report claims. Comparing the
    two is the whole point: a report whose capability record was edited after the
    fact has a digest that no longer describes its own contents.
    """
    from interop_lab.capabilities.model import ResolvedTargetCapabilities

    contents = {k: v for k, v in capabilities.items() if k != "capability_fingerprint"}
    try:
        record = ResolvedTargetCapabilities.model_validate(contents)
    except Exception as exc:  # a hostile or truncated record, not a target defect
        return None, f"{type(exc).__name__}: {exc}"
    if not record.is_usable:
        return None, None
    return record.fingerprint().digest, None


def _record_signature(record: ExternalRunRecord, scenario: str, run: dict[str, Any]) -> None:
    """Build and store one run's signature, refusing a report it cannot be built from.

    The signature is computed here from the serialized run rather than trusted
    from the report, and the digest is stored beside it. A report whose signature
    cannot be reconstructed is not a weaker measurement — it is a report this job
    cannot check at all, and it is refused rather than summarised.
    """
    try:
        parsed = RunResult.model_validate(run)
    except Exception as exc:
        record.problems.append(
            f"{scenario}: the run does not load into the typed model, so its result "
            f"signature cannot be computed and its stability cannot be checked: "
            f"{type(exc).__name__}: {exc}"
        )
        return

    # The target and scenario fingerprints are deliberately absent here: they need
    # the target definition and the scenario object, which this report does not
    # carry. They are compared across repetitions at the target level instead,
    # where the definitions are in hand. Inventing them from a partial view would
    # produce values that look authoritative and describe something else.
    signature = signature_for(parsed)
    record.per_scenario_result_signature[scenario] = signature
    record.per_scenario_result_signature_digest[scenario] = signature.digest


def validate_report(record: ExternalRunRecord, report: dict[str, Any]) -> None:
    """Check one report, appending every problem found rather than the first.

    Appending rather than raising: a job that stops at the first problem makes
    the next one invisible until someone fixes this one and runs again.
    """
    runs = report.get("runs")
    if not isinstance(runs, list) or not runs:
        record.problems.append("report contains no runs")
        return

    for run in runs:
        scenario = run.get("scenario_id", "<unknown>")
        classification = run.get("classification")
        if not isinstance(classification, str):
            record.problems.append(f"{scenario}: run has no classification")
            continue

        record.per_scenario_classification[scenario] = classification
        _record_signature(record, scenario, run)

        if classification == "HARNESS_ERROR":
            # The harness or the environment failed. Never the target, and never
            # something this job may report as a successful measurement.
            record.problems.append(
                f"{scenario}: HARNESS_ERROR — {run.get('harness_error') or 'no detail'}"
            )
            continue

        if classification == "NOT_APPLICABLE":
            if not run.get("not_applicable_reason"):
                record.problems.append(
                    f"{scenario}: NOT_APPLICABLE with no reason. A skip whose reason is "
                    "not recorded cannot be distinguished from one that was invented."
                )
            continue

        containment = run.get("containment") or {}
        outcome = containment.get("outcome")
        if outcome != "contained":
            record.problems.append(f"{scenario}: containment outcome {outcome!r}, not contained")
        if containment.get("weaknesses"):
            record.problems.append(
                f"{scenario}: containment weaknesses {containment['weaknesses']}"
            )

    record.classifications = dict(
        sorted(Counter(record.per_scenario_classification.values()).items())
    )

    # Capability evidence, taken from the first run that carries it. Every run
    # of a target in one report shares a resolution, so the first is enough.
    for run in runs:
        capabilities = run.get("resolved_capabilities")
        if not isinstance(capabilities, dict):
            continue
        record.capability_status = capabilities.get("status")
        record.capability_fingerprint = capabilities.get("capability_fingerprint")
        record.negotiated_profile = capabilities.get("protocol_profile_negotiated")
        record.observed_tools = sorted(
            tool.get("name", "")
            for tool in (capabilities.get("tools") or [])
            if isinstance(tool, dict)
        )

        # The tool *names* alone were what this job used to check, and names
        # cannot show a tool that kept its name while changing its input schema
        # — the drift that silently invalidates an applicability decision. The
        # digest covers the schemas, the server capability object, the negotiated
        # profile and the observed transport features, so it moves when any of
        # them does.
        recomputed, error = recompute_capability_fingerprint(capabilities)
        if error is not None:
            record.problems.append(
                f"the capability record does not load, so its fingerprint cannot be "
                f"recomputed and nothing in it can be checked: {error}"
            )
        elif record.capability_status == "resolved":
            if not record.capability_fingerprint:
                record.problems.append(
                    "capability status is resolved but no capability fingerprint was "
                    "recorded. Tool names alone do not establish schema identity, so an "
                    "applicability decision in this report cannot be re-checked."
                )
            elif recomputed is None:
                record.problems.append(
                    "a resolved capability record produced no recomputed fingerprint"
                )
            elif recomputed != record.capability_fingerprint:
                record.problems.append(
                    f"capability fingerprint {record.capability_fingerprint} does not "
                    f"match the record it describes, which hashes to {recomputed}"
                )
        break

    if record.capability_status is None:
        record.problems.append(
            "no run carried a resolved capability record, so no applicability "
            "decision in this report can be checked against what the target advertises"
        )


def run_once(
    image: ExternalImage,
    *,
    repetition: int,
    repo_root: Path,
    reports_dir: Path,
    artifacts_root: Path,
    resolution: Path = DEFAULT_RESOLUTION_PATH,
) -> ExternalRunRecord:
    """Execute the battery once and record what actually happened."""
    report_path = reports_dir / f"{image.key}-{repetition}.json"
    record = ExternalRunRecord(
        target_key=image.key,
        target_file=image.target_file,
        repetition=repetition,
        report_path=str(report_path.relative_to(repo_root))
        if report_path.is_relative_to(repo_root)
        else report_path.name,
    )

    command = [
        "uv",
        "run",
        CLI_COMMAND,
        "run",
        "-t",
        image.target_file,
        "--suite",
        "all",
        "--json",
        str(report_path),
        "--artifacts",
        str(artifacts_root / f"{image.key}-{repetition}"),
        # Named explicitly rather than left to the default path. Every external
        # target is locally built, so a run without this measures nothing — and
        # passing it makes the manifest say which build was measured.
        "--resolution",
        str(resolution),
    ]

    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    record.duration_ms = (time.perf_counter() - started) * 1000.0
    record.exit_code = completed.returncode
    record.stdout_tail = bounded_capture(completed.stdout)
    record.stderr_tail = bounded_capture(completed.stderr)

    if completed.returncode not in ALLOWED_EXIT_CODES:
        record.problems.append(
            f"exit code {completed.returncode} is outside the allowed set "
            f"{sorted(ALLOWED_EXIT_CODES)}. The CLI reports 0 or 1 once it has run the "
            "battery; anything else means it stopped before measuring, and a report "
            "written by an earlier run must not be read as this one's result."
        )

    record.report_sha256 = _sha256_file(report_path)
    if not report_path.is_file():
        record.problems.append(
            f"exit code {completed.returncode} but no report at {report_path}. "
            "A run that produced nothing is not a measurement."
        )
        return record

    try:
        report = json.loads(report_path.read_text())
    except json.JSONDecodeError as exc:
        record.problems.append(f"report is not valid JSON: {exc}")
        return record

    record.report_parsed = True
    validate_report(record, report)
    return record


class TargetEvidence(BaseModel):
    """Everything one external target produced in this job."""

    model_config = ConfigDict(extra="forbid")

    target_key: str
    target_id: str | None = None
    target_file: str
    source_repository: str | None = None
    relationship: str | None = None
    source_version: str | None = None
    source_commit: str | None = None
    language: str | None = None
    transport: str | None = None
    declared_profile: str | None = None
    negotiated_profile: str | None = None

    image_name: str
    #: How the image is identified at all. Every external target is local_build;
    #: the field is recorded so a reader never has to infer it.
    image_source_kind: str | None = None
    build_key: str | None = None
    runtime_tag: str | None = None
    #: The portable half: what the image was built from, reproducible anywhere.
    portable_identity_fingerprint: str | None = None
    #: The non-portable half: what this builder produced, with the facts that
    #: say what kind of identifier it is. Never presented as cross-machine.
    resolved_local_image_id: str | None = None
    oci_manifest_digest: str | None = None
    builder: str | None = None
    storage_driver: str | None = None
    resolution_manifest_sha256: str | None = None
    dockerfile_sha256: str | None = None
    #: Hash of a lockfile found in the build context, when there is one.
    lock_sha256: str | None = None
    #: What the target file itself declares, which is the authority for
    #: ecosystems whose closure never lands in the build context — a Go module's
    #: go.sum lives inside the verified module zip, not beside the Dockerfile.
    declared_lock_sha256: str | None = None
    source_archive_sha256: str | None = None
    module_sum: str | None = None

    capability_status: str | None = None
    capability_fingerprint: str | None = None
    observed_tools: list[str] = Field(default_factory=list)
    declared_tools: list[str] = Field(default_factory=list)
    declared_but_not_advertised: list[str] = Field(default_factory=list)
    advertised_but_not_declared: list[str] = Field(default_factory=list)

    applicable_scenarios: int = 0
    repetitions: int = 0
    classifications: dict[str, int] = Field(default_factory=dict)
    stable_across_repetitions: bool = True
    instability: list[str] = Field(default_factory=list)
    #: Whether every repetition observed the same capability surface. Tracked
    #: apart from classification stability because they fail for different
    #: reasons and mean different things: classifications can wobble while the
    #: surface holds still, and a surface that moves between two repetitions of
    #: one job invalidates the applicability decisions of both.
    capability_fingerprint_stable: bool = True
    capability_instability: list[str] = Field(default_factory=list)

    runs: list[ExternalRunRecord] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExternalEvidenceManifest(BaseModel):
    """The private, machine-readable record of one external-matrix execution.

    Deliberately classifications and identity only. No wire traces: a
    third-party trace is evidence about someone else's software, it stays under
    `artifacts/private-findings/`, and it is never carried out of the repository
    by an ordinary CI artifact.
    """

    model_config = ConfigDict(extra="forbid")

    harness_version: str = HARNESS_VERSION
    harness_commit: str | None = None
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: Which builder produced the images these results describe, and what kind
    #: of image identifier its storage driver emits.
    builder: str | None = None
    storage_driver: str | None = None
    architecture: str | None = None
    resolution_manifest_sha256: str | None = None
    repetitions: int = 0
    targets: list[TargetEvidence] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    #: What this manifest does not establish.
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Classifications only. No third-party wire trace is included here, by "
            "design; the traces stay local.",
            "A clean pass means the target did not diverge on the scenarios that "
            "applied, not that it is correct.",
            "Applicable-scenario counts are bounded by the battery, which is narrow.",
            "resolved_local_image_id identifies these bits on this builder only. "
            "Cross-driver stability of a local image identifier has not been "
            "established by this repository, and is not claimed here.",
        ]
    )

    @property
    def ok(self) -> bool:
        return not self.problems


def check_stability(evidence: TargetEvidence) -> None:
    """A result observed once is not a stable claim, and a repeated label is not one either.

    Four things have to hold before this job may say "stable across repetitions",
    and the previous version checked the first:

    1. **The same scenario set.** A scenario present in one repetition and absent
       from another is a change in what was measured, whatever the results say.
    2. **The same classification per scenario.** Compared per scenario rather than
       as summary counts: two runs can produce the same tally while disagreeing
       about which scenario failed.
    3. **The same failed assertion identities.** A stable classification with a
       changed assertion signature is *instability*. Two runs both reporting
       SPEC_VIOLATION while different assertions failed are two different findings
       wearing one label, and reporting them as a stable result is the specific
       claim this function exists to stop.
    4. **The same result-signature digest.** Which additionally covers the
       assertion-level classifications, the timeout cause, the exit code and
       signal, whether the harness did the killing, cleanup, containment and the
       capability fingerprint.

    A missing signature, or one that cannot be recomputed, is instability too —
    not a weaker claim. The job cannot check what it cannot rebuild, and an
    unverifiable signature must not be reported as an agreeing one.
    """
    parsed = [run for run in evidence.runs if run.report_parsed]
    if len(parsed) < 2:
        return

    def unstable(detail: str) -> None:
        evidence.stable_across_repetitions = False
        evidence.instability.append(detail)

    # (1) The measured set itself.
    scenario_sets = [frozenset(run.per_scenario_classification) for run in parsed]
    every_scenario = sorted(set().union(*scenario_sets))
    for run, seen in zip(parsed, scenario_sets, strict=True):
        missing = sorted(set(every_scenario) - seen)
        if missing:
            unstable(
                f"repetition {run.repetition} did not report {missing}, which other "
                "repetitions did. A scenario that was measured once and not again is a "
                "change in what was measured."
            )

    reference = parsed[0]
    for scenario in every_scenario:
        # (2) Classification.
        classifications = {run.per_scenario_classification.get(scenario) for run in parsed}
        if len(classifications) > 1:
            unstable(f"{scenario}: {sorted(str(value) for value in classifications)}")
            continue

        # (3) and (4). Both rest on a signature existing and agreeing with itself,
        # so the absence of one is reported as its own problem rather than as
        # silent agreement.
        signatures: list[ResultSignature] = []
        for run in parsed:
            signature = run.per_scenario_result_signature.get(scenario)
            stored_digest = run.per_scenario_result_signature_digest.get(scenario)
            if signature is None or stored_digest is None:
                unstable(
                    f"{scenario}: repetition {run.repetition} carries no result "
                    "signature, so a repeated classification cannot be read as a "
                    "repeated result"
                )
                continue
            if signature.digest != stored_digest:
                unstable(
                    f"{scenario}: repetition {run.repetition} stores signature digest "
                    f"{stored_digest} while its contents hash to {signature.digest}. "
                    "The record and its digest disagree, so one of them was edited."
                )
                continue
            signatures.append(signature)

        if len(signatures) != len(parsed):
            continue

        baseline = signatures[0]
        for run, signature in zip(parsed, signatures, strict=True):
            if signature.digest == baseline.digest:
                continue
            changes = baseline.describe_difference(signature)
            unstable(
                f"{scenario}: repetition {run.repetition} has the same classification "
                f"as repetition {reference.repetition} and a different result "
                f"signature — {'; '.join(changes) or 'no field-level difference found'}"
            )

    if evidence.stable_across_repetitions:
        evidence.classifications = dict(
            sorted(Counter(reference.per_scenario_classification.values()).items())
        )


def check_capability_stability(evidence: TargetEvidence) -> None:
    """Take the fingerprint from the first valid repetition, and hold the rest to it.

    A capability surface that differs between two repetitions of the same job,
    against the same image, is capability drift — and this job has no baseline to
    classify it against. So it is recorded as instability rather than absorbed:
    the alternative is a manifest that reports one fingerprint and quietly does
    not mention that the other repetitions disagreed with it.
    """
    parsed = [run for run in evidence.runs if run.report_parsed]
    first = next((run for run in parsed if run.capability_fingerprint), None)
    if first is None:
        return

    evidence.capability_fingerprint = first.capability_fingerprint
    for run in parsed:
        if run.capability_fingerprint == first.capability_fingerprint:
            continue
        evidence.capability_fingerprint_stable = False
        evidence.capability_instability.append(
            f"repetition {run.repetition} observed capability fingerprint "
            f"{run.capability_fingerprint or 'none'}, repetition {first.repetition} "
            f"observed {first.capability_fingerprint}"
        )


def _target_metadata(image: ExternalImage, repo_root: Path) -> dict[str, Any]:
    """Read the target's declared identity, through the typed model.

    Every value here is a field someone wrote down deliberately. An earlier
    version guessed `relationship` by searching the target's prose for the word
    "independent", and labelled the official TypeScript server independent
    because its notes say it is *not* the independent slot. The matrix's central
    claim is "one official and one independent implementation"; it does not rest
    on a substring search.
    """
    target = image.target(repo_root)
    supply_chain = target.supply_chain
    return {
        "target_id": target.target_id,
        "language": target.language,
        "transport": target.transport.value,
        "declared_profile": target.protocol_profile,
        "source_version": target.version,
        "source_commit": target.commit_sha,
        "source_repository": target.package_name,
        "relationship": target.relationship.value,
        "declared_tools": list(target.expected_tools),
        "source_archive_sha256": supply_chain.source_archive_sha256 if supply_chain else None,
        "declared_lock_sha256": supply_chain.lock_sha256 if supply_chain else None,
        "module_sum": supply_chain.module_sum if supply_chain else None,
    }


def _target_fingerprint(image: ExternalImage, repo_root: Path) -> str:
    from interop_lab.ci.baseline import target_fingerprint

    return target_fingerprint(image.target(repo_root))


def build_manifest(
    *,
    repo_root: Path,
    reports_dir: Path,
    artifacts_root: Path,
    repetitions: int,
    harness_commit: str | None = None,
    resolution: Path = DEFAULT_RESOLUTION_PATH,
) -> ExternalEvidenceManifest:
    """Run every external target `repetitions` times and record the evidence."""
    manifest = ExternalEvidenceManifest(repetitions=repetitions, harness_commit=harness_commit)
    reports_dir.mkdir(parents=True, exist_ok=True)

    index = BuildResolutionIndex.load(resolution)
    manifest.builder = index.manifest.builder
    manifest.storage_driver = index.manifest.storage_driver
    manifest.architecture = index.manifest.architecture
    manifest.resolution_manifest_sha256 = index.manifest_sha256

    for image in load_images(repo_root):
        metadata = _target_metadata(image, repo_root)
        source = image.source(repo_root)
        evidence = TargetEvidence(
            target_key=image.key,
            target_file=image.target_file,
            image_name=source.image_name,
            image_source_kind=source.kind.value,
            build_key=source.build_key,
            runtime_tag=source.runtime_tag,
            builder=index.manifest.builder,
            storage_driver=index.manifest.storage_driver,
            resolution_manifest_sha256=index.manifest_sha256,
            repetitions=repetitions,
            **metadata,
        )

        # The portable identity is recomputed from the committed inputs rather
        # than copied out of the resolution: copying would make the manifest
        # agree with itself by construction and prove nothing.
        try:
            portable = derive_portable_identity(image.target(repo_root), repo_root=repo_root)
            evidence.portable_identity_fingerprint = portable.fingerprint
            resolved = index.resolve_local_build(
                source,
                target_id=metadata["target_id"],
                target_fingerprint=_target_fingerprint(image, repo_root),
                portable_identity=portable,
            )
            evidence.resolved_local_image_id = resolved.expected_local_image_id
            entry = index.manifest.entry_for(metadata["target_id"])
            evidence.oci_manifest_digest = entry.oci_manifest_digest if entry else None
        except (OSError, ValueError, ImageResolutionError) as exc:
            manifest.problems.append(f"{image.key}: build resolution unusable: {exc}")

        evidence.dockerfile_sha256 = _sha256_file(repo_root / source.dockerfile)
        for lock_name in ("package-lock.json", "requirements.lock.txt", "go.sum"):
            digest = _sha256_file(repo_root / source.build_context / lock_name)
            if digest is not None:
                evidence.lock_sha256 = digest
                break

        for repetition in range(1, repetitions + 1):
            record = run_once(
                image,
                repetition=repetition,
                repo_root=repo_root,
                reports_dir=reports_dir,
                artifacts_root=artifacts_root,
                resolution=resolution,
            )
            evidence.runs.append(record)
            manifest.problems.extend(
                f"{image.key} run {repetition}: {problem}" for problem in record.problems
            )

        check_capability_stability(evidence)
        if not evidence.capability_fingerprint_stable:
            manifest.problems.extend(
                f"{image.key}: capability surface moved between repetitions — {entry}"
                for entry in evidence.capability_instability
            )

        first_parsed = next((run for run in evidence.runs if run.report_parsed), None)
        if first_parsed is not None:
            evidence.capability_status = first_parsed.capability_status
            evidence.negotiated_profile = first_parsed.negotiated_profile
            evidence.observed_tools = first_parsed.observed_tools
            evidence.classifications = first_parsed.classifications
            evidence.applicable_scenarios = sum(
                count
                for name, count in first_parsed.classifications.items()
                if name != "NOT_APPLICABLE"
            )
            declared = set(evidence.declared_tools)
            observed = set(evidence.observed_tools)
            evidence.declared_but_not_advertised = sorted(declared - observed)
            evidence.advertised_but_not_declared = sorted(observed - declared)

        check_stability(evidence)
        if not evidence.stable_across_repetitions:
            manifest.problems.extend(
                f"{image.key}: unstable across repetitions — {entry}"
                for entry in evidence.instability
            )

        manifest.targets.append(evidence)

    return manifest
