"""Gate semantics that decide whether a green result means anything.

Each test here corresponds to a way a regression gate can quietly stop being a
regression gate: a result nobody approved counted as fine, an "approved"
divergence with no approval behind it, a reproduction package that no longer
reproduces, a failure absorbed into a baseline at creation time, or an official
conformance outcome drifting while the harness's own verdict stays put.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_ci_gate import SCENARIO, make_run, make_target

from interop_lab.ci.baseline import (
    BaselineDocument,
    BaselineEntry,
    Classification,
    DivergencePolicy,
    GateOutcome,
    GatePolicy,
    RegressionKind,
    ReproductionExpectation,
    UnapprovedFailureError,
    UnknownResultPolicy,
    approval_key,
    build_baseline,
    scenario_fingerprint,
    target_fingerprint,
)
from interop_lab.ci.gate import evaluate
from interop_lab.models.core import TargetDefinition
from interop_lab.reporting import sarif
from interop_lab.scenarios.library import get_scenario

EXAMPLE_PACKAGE = "artifact://examples/http-header-mismatch-minimized"
EXAMPLE_DIR = Path("artifacts/examples/http-header-mismatch-minimized")

#: The public distribution excludes the copied OASIS SARIF schema, because its
#: redistribution terms are unconfirmed. Everything that builds SARIF is still
#: tested there; only the assertions that need the schema itself are skipped,
#: and they are skipped with a reason rather than deleted, so a reader of a
#: public test run sees exactly which check the licensing decision cost them.
needs_pinned_schema = pytest.mark.skipif(
    not sarif.pinned_schema_available(),
    reason=(
        "the pinned OASIS SARIF schema is not part of this distribution; "
        "SARIF generation is tested, offline schema validation is not available"
    ),
)


def make_entry(
    *,
    allowed: Classification = Classification.PASS,
    failed: tuple[str, ...] = (),
    approved_verdict: str | None = None,
    approval_reason: str | None = None,
    evidence: str | None = None,
    conformance_outcome: str | None = None,
    reproduction: ReproductionExpectation | None = None,
    target_fp: str | None = None,
    scenario_fp: str | None = None,
) -> BaselineEntry:
    target = make_target()
    return BaselineEntry(
        target_id=target.target_id,
        target_fingerprint=target_fp or target_fingerprint(target),
        scenario_id=SCENARIO.scenario_id,
        scenario_version="1",
        scenario_fingerprint=scenario_fp or scenario_fingerprint(SCENARIO),
        applicable=True,
        allowed_classification=allowed,
        expected_failed_assertions=list(failed),
        approved_differential_verdict=approved_verdict,
        approval_reason=approval_reason,
        evidence=evidence,
        official_conformance_outcome=conformance_outcome,
        reproduction=reproduction,
    )


def make_document(*entries: BaselineEntry, policy: GatePolicy | None = None) -> BaselineDocument:
    return BaselineDocument(
        baseline_id="b",
        created_at=datetime.now(UTC).isoformat(),
        created_reason="test",
        created_by="test",
        suite="s",
        suite_fingerprint="f",
        policy=policy or GatePolicy(),
        entries=list(entries),
    )


# ------------------------------------------------- unknown results (item 1)


def test_unbaselined_pass_blocks_by_default() -> None:
    """A new PASS still widens the approved measurement surface."""
    result = evaluate(
        baseline=make_document(make_entry()),
        runs=[
            (make_run(Classification.PASS), SCENARIO, make_target()),
            (make_run(Classification.PASS, scenario_id="brand.new"), SCENARIO, make_target()),
        ],
    )
    unknown = [r for r in result.regressions if r.kind is RegressionKind.UNKNOWN_RESULT]
    assert len(unknown) == 1
    assert unknown[0].scenario_id == "brand.new"
    assert unknown[0].blocking is True
    assert result.outcome is GateOutcome.REGRESSION


def test_unbaselined_pass_can_be_downgraded_to_a_warning_explicitly() -> None:
    """Warn is available, but it has to be chosen — it is never the default."""
    assert GatePolicy().unknown_result is UnknownResultPolicy.FAIL

    result = evaluate(
        baseline=make_document(
            make_entry(), policy=GatePolicy(unknown_result=UnknownResultPolicy.WARN)
        ),
        runs=[
            (make_run(Classification.PASS), SCENARIO, make_target()),
            (make_run(Classification.PASS, scenario_id="brand.new"), SCENARIO, make_target()),
        ],
    )
    unknown = [r for r in result.regressions if r.kind is RegressionKind.UNKNOWN_RESULT]
    assert len(unknown) == 1 and unknown[0].blocking is False
    assert result.outcome is GateOutcome.PASSED, "a warning must still be reported, not hidden"
    assert result.regressions, "downgrading to warn must not delete the record"


# ------------------------------------------ ALLOW_IF_APPROVED enforcement (item 2)


APPROVED_POLICY = GatePolicy(new_behavioral_divergence=DivergencePolicy.ALLOW_IF_APPROVED)


def _fully_approved_entry() -> BaselineEntry:
    return make_entry(
        approved_verdict=Classification.BEHAVIORAL_DIVERGENCE.value,
        approval_reason="reviewed 2026-08-12: ordering difference, no dated requirement covers it",
        evidence="artifact://runs/approved-divergence/wire.jsonl",
    )


def test_divergence_with_complete_approval_is_allowed() -> None:
    result = evaluate(
        baseline=make_document(_fully_approved_entry(), policy=APPROVED_POLICY),
        runs=[(make_run(Classification.BEHAVIORAL_DIVERGENCE), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.PASSED
    assert result.unchanged == 1


def test_divergence_without_any_approval_blocks() -> None:
    result = evaluate(
        baseline=make_document(make_entry(), policy=APPROVED_POLICY),
        runs=[(make_run(Classification.BEHAVIORAL_DIVERGENCE), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.REGRESSION
    regression = result.blocking_regressions[0]
    assert regression.kind is RegressionKind.NEW_DIVERGENCE
    assert "no approved_differential_verdict" in regression.detail


def test_divergence_with_the_wrong_approved_verdict_blocks() -> None:
    """Approving a SPEC_VIOLATION does not approve a divergence."""
    entry = _fully_approved_entry().model_copy(
        update={"approved_differential_verdict": Classification.SPEC_VIOLATION.value}
    )
    result = evaluate(
        baseline=make_document(entry, policy=APPROVED_POLICY),
        runs=[(make_run(Classification.BEHAVIORAL_DIVERGENCE), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.REGRESSION
    assert "not BEHAVIORAL_DIVERGENCE" in result.blocking_regressions[0].detail


@pytest.mark.parametrize("dropped", ["approval_reason", "evidence"])
def test_divergence_missing_its_paperwork_blocks(dropped: str) -> None:
    entry = _fully_approved_entry().model_copy(update={dropped: None})
    result = evaluate(
        baseline=make_document(entry, policy=APPROVED_POLICY),
        runs=[(make_run(Classification.BEHAVIORAL_DIVERGENCE), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.REGRESSION
    assert result.blocking_regressions[0].kind is RegressionKind.NEW_DIVERGENCE


def test_approval_does_not_survive_target_drift() -> None:
    """An approval describes a specific build, not the target's name."""
    entry = _fully_approved_entry()
    result = evaluate(
        baseline=make_document(entry, policy=APPROVED_POLICY),
        runs=[
            (
                make_run(Classification.BEHAVIORAL_DIVERGENCE),
                SCENARIO,
                make_target("t", version="9.9"),
            )
        ],
    )
    details = " ".join(r.detail for r in result.blocking_regressions)
    assert "target fingerprint changed since the divergence was approved" in details


def test_approval_does_not_survive_scenario_drift() -> None:
    entry = _fully_approved_entry().model_copy(update={"scenario_fingerprint": "0" * 16})
    result = evaluate(
        baseline=make_document(entry, policy=APPROVED_POLICY),
        runs=[(make_run(Classification.BEHAVIORAL_DIVERGENCE), SCENARIO, make_target())],
    )
    details = " ".join(r.detail for r in result.blocking_regressions)
    assert "scenario fingerprint changed since the divergence was approved" in details


def test_divergence_carrying_a_normative_failure_is_never_approved() -> None:
    """A dated requirement was violated; the divergence label does not cover it."""
    run = make_run(Classification.BEHAVIORAL_DIVERGENCE, failed=("result_type_present",))
    for assertion in run.assertions:
        assertion.classification = Classification.SPEC_VIOLATION

    result = evaluate(
        baseline=make_document(_fully_approved_entry(), policy=APPROVED_POLICY),
        runs=[(run, SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.REGRESSION
    assert "a normative failure accompanies the divergence" in result.blocking_regressions[0].detail


# ------------------------------------------- reproduction expectations (item 3)


def _expectation(**overrides: object) -> ReproductionExpectation:
    base: dict[str, object] = {
        "package_uri": EXAMPLE_PACKAGE,
        "expected_classification": Classification.SPEC_VIOLATION,
        "expected_failed_assertions": ["is_error", "code_32020"],
        "minimum_stability_runs": 3,
    }
    base.update(overrides)
    return ReproductionExpectation.model_validate(base)


def _entry_with_reproduction(expectation: ReproductionExpectation) -> BaselineDocument:
    return make_document(
        make_entry(
            allowed=Classification.SPEC_VIOLATION,
            failed=("result_type_present",),
            reproduction=expectation,
        )
    )


def _evaluate_reproduction(
    expectation: ReproductionExpectation, *, root: Path
) -> list[RegressionKind]:
    result = evaluate(
        baseline=_entry_with_reproduction(expectation),
        runs=[
            (
                make_run(Classification.SPEC_VIOLATION, failed=("result_type_present",)),
                SCENARIO,
                make_target(),
            )
        ],
        artifacts_root=root,
    )
    return [r.kind for r in result.regressions]


def test_committed_reproduction_still_satisfies_its_expectation() -> None:
    """The positive control: without it, every negative below proves nothing."""
    assert EXAMPLE_DIR.is_dir(), "committed example package is missing"
    assert _evaluate_reproduction(_expectation(), root=Path("artifacts")) == []


def test_missing_reproduction_package_is_a_regression(tmp_path: Path) -> None:
    kinds = _evaluate_reproduction(_expectation(), root=tmp_path)
    assert RegressionKind.REPRODUCTION_MISSING in kinds


def test_reproduction_that_stopped_being_stable_is_a_regression(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    shutil.copytree(EXAMPLE_DIR, root / "examples" / EXAMPLE_DIR.name)
    record_path = root / "examples" / EXAMPLE_DIR.name / "minimization.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["stable"] = False
    record["stability_runs"] = 1
    record_path.write_text(json.dumps(record), encoding="utf-8")

    kinds = _evaluate_reproduction(_expectation(), root=root)
    assert kinds.count(RegressionKind.REPRODUCTION_UNSTABLE) == 2, (
        "both 'not stable' and 'too few runs' must be reported, not just the first"
    )


def test_changed_failure_signature_in_a_reproduction_is_a_regression(tmp_path: Path) -> None:
    """The package still fails — for a different reason. That is not the finding."""
    root = tmp_path / "artifacts"
    shutil.copytree(EXAMPLE_DIR, root / "examples" / EXAMPLE_DIR.name)
    record_path = root / "examples" / EXAMPLE_DIR.name / "minimization.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["preserved_failing_assertions"] = ["is_error"]
    record_path.write_text(json.dumps(record), encoding="utf-8")

    kinds = _evaluate_reproduction(_expectation(), root=root)
    assert RegressionKind.REPRODUCTION_MISMATCH in kinds


def test_reproduction_expecting_the_wrong_classification_is_a_regression() -> None:
    kinds = _evaluate_reproduction(
        _expectation(expected_classification=Classification.CRASH), root=Path("artifacts")
    )
    assert RegressionKind.REPRODUCTION_MISMATCH in kinds


def test_reproduction_fingerprint_drift_is_a_regression() -> None:
    kinds = _evaluate_reproduction(
        _expectation(expected_scenario_fingerprint="0" * 16), root=Path("artifacts")
    )
    assert RegressionKind.REPRODUCTION_MISMATCH in kinds


def test_a_reproduction_marked_not_required_is_not_checked(tmp_path: Path) -> None:
    assert _evaluate_reproduction(_expectation(required=False), root=tmp_path) == []


# ---------------------------------------- baseline creation approval (item 4)


def _runs_for_build(*classifications: Classification) -> list[tuple[object, object, object]]:
    target = make_target()
    scenario = SCENARIO
    return [
        (
            make_run(classification, scenario_id=f"s{index}", failed=("result_type_present",)),
            scenario,
            target,
        )
        for index, classification in enumerate(classifications)
    ]


def _build(**overrides: object) -> BaselineDocument:
    kwargs: dict[str, object] = {
        "baseline_id": "b",
        "reason": "initial",
        "created_by": "test",
        "suite": "s",
        "scenarios": [SCENARIO],
        "targets": [make_target()],
        "runs": _runs_for_build(Classification.PASS),
    }
    kwargs.update(overrides)
    return build_baseline(**kwargs)  # type: ignore[arg-type]


def test_baseline_creation_refuses_unapproved_failures() -> None:
    with pytest.raises(UnapprovedFailureError) as excinfo:
        _build(runs=_runs_for_build(Classification.PASS, Classification.SPEC_VIOLATION))
    assert "t:s1" in str(excinfo.value)
    assert "--approve-failure" in str(excinfo.value), "the error must say how to approve"


def test_baseline_creation_names_every_unapproved_failure() -> None:
    with pytest.raises(UnapprovedFailureError) as excinfo:
        _build(runs=_runs_for_build(Classification.SPEC_VIOLATION, Classification.CRASH))
    message = str(excinfo.value)
    assert "t:s0" in message and "t:s1" in message


def test_each_approved_failure_carries_its_own_reason() -> None:
    """One blanket reason for the whole document is not approval."""
    document = _build(
        runs=_runs_for_build(Classification.PASS, Classification.SPEC_VIOLATION),
        approved_failures={approval_key("t", "s1"): "synthetic fixture defect, proves detection"},
    )
    passing = next(e for e in document.entries if e.scenario_id == "s0")
    failing = next(e for e in document.entries if e.scenario_id == "s1")
    assert passing.approval_reason == "initial"
    assert failing.approval_reason == "synthetic fixture defect, proves detection"
    assert failing.is_approved_failure


def test_approving_a_different_entry_does_not_approve_this_one() -> None:
    with pytest.raises(UnapprovedFailureError):
        _build(
            runs=_runs_for_build(Classification.SPEC_VIOLATION),
            approved_failures={approval_key("t", "some.other.scenario"): "unrelated"},
        )


def test_committed_baseline_records_a_reason_per_approved_failure() -> None:
    document = BaselineDocument.model_validate_json(
        Path("baselines/mvp.json").read_text(encoding="utf-8")
    )
    approved = [e for e in document.entries if e.is_approved_failure]
    assert approved, "the committed baseline is expected to carry approved synthetic failures"
    reasons = {e.approval_reason for e in approved}
    assert all(reasons), "every approved failure needs a reason"
    assert len(reasons) == len(approved), "reasons must be per-entry, not one blanket string"


# ------------------------------------ official conformance outcome (item 5)


def test_official_conformance_outcome_drift_is_a_regression() -> None:
    result = evaluate(
        baseline=make_document(make_entry(conformance_outcome="passed")),
        runs=[(make_run(Classification.PASS), SCENARIO, make_target())],
        conformance_outcomes={"t": "failed"},
    )
    regression = next(
        r for r in result.regressions if r.kind is RegressionKind.CONFORMANCE_OUTCOME_CHANGED
    )
    assert regression.baseline_value == "passed"
    assert regression.observed_value == "failed"
    assert "not a Failure Lab finding" in regression.detail, (
        "the official framework's verdict must stay in its own vocabulary"
    )
    assert result.outcome is GateOutcome.REGRESSION


def test_matching_official_conformance_outcome_is_not_a_regression() -> None:
    result = evaluate(
        baseline=make_document(make_entry(conformance_outcome="passed")),
        runs=[(make_run(Classification.PASS), SCENARIO, make_target())],
        conformance_outcomes={"t": "passed"},
    )
    assert result.outcome is GateOutcome.PASSED


def test_absent_conformance_run_is_not_measured_and_never_a_pass() -> None:
    """A baselined official outcome that this run did not re-execute is an error.

    Missing execution is NOT_MEASURED. It is not a matching outcome and it is
    not a pass, even when the harness's own classification is unchanged.
    """
    result = evaluate(
        baseline=make_document(make_entry(conformance_outcome="passed")),
        runs=[(make_run(Classification.PASS), SCENARIO, make_target())],
    )
    unmeasured = [
        r for r in result.regressions if r.kind is RegressionKind.CONFORMANCE_UNMEASURED
    ]
    assert len(unmeasured) == 1
    assert unmeasured[0].baseline_value == "passed"
    assert unmeasured[0].observed_value == "NOT_MEASURED"
    assert unmeasured[0].blocking is True
    assert "NOT_MEASURED" in unmeasured[0].detail
    assert result.outcome is GateOutcome.REGRESSION
    assert result.exit_code != 0
    assert not [
        r for r in result.regressions if r.kind is RegressionKind.CONFORMANCE_OUTCOME_CHANGED
    ]


def test_conformance_outcome_is_not_required_when_the_baseline_records_none() -> None:
    """Entries that never approved an official outcome do not demand one.

    The committed fixture baselines store null. Omitting a conformance run
    there must stay a pass; the error is for a required check that was skipped.
    """
    result = evaluate(
        baseline=make_document(make_entry()),
        runs=[(make_run(Classification.PASS), SCENARIO, make_target())],
    )
    assert result.outcome is GateOutcome.PASSED
    assert not [
        r
        for r in result.regressions
        if r.kind
        in (RegressionKind.CONFORMANCE_UNMEASURED, RegressionKind.CONFORMANCE_OUTCOME_CHANGED)
    ]


# ------------------------------------------ full SARIF schema validation (item 6)


@needs_pinned_schema
def test_generated_sarif_validates_against_the_pinned_schema() -> None:
    runs = [
        make_run(Classification.SPEC_VIOLATION, scenario_id="a", failed=("result_type_present",)),
        make_run(Classification.CRASH, scenario_id="b"),
        make_run(Classification.BEHAVIORAL_DIVERGENCE, scenario_id="c"),
        make_run(Classification.HARNESS_ERROR, scenario_id="d", harness_error="boom"),
        make_run(Classification.PASS, scenario_id="e"),
        make_run(Classification.NOT_APPLICABLE, scenario_id="f"),
    ]
    sarif.validate(sarif.build(runs))


@needs_pinned_schema
def test_pinned_sarif_schema_is_verified_by_digest(tmp_path: Path) -> None:
    """A schema that can be edited silently is not a pin."""
    tampered = tmp_path / "sarif-2.1.0.json"
    schema = sarif.load_pinned_schema()
    schema["title"] = "tampered"
    tampered.write_text(json.dumps(schema), encoding="utf-8")

    with pytest.raises(sarif.SarifSchemaError, match="changed"):
        sarif.load_pinned_schema(tampered)


def test_digest_pinned_files_are_protected_from_line_ending_conversion() -> None:
    """A pin is a claim about bytes, and git will rewrite bytes on checkout.

    The pinned SARIF schema passed on Linux and failed on Windows because git's
    automatic LF-to-CRLF conversion changed the file and therefore its SHA-256.
    Anything verified by digest needs a `-text` attribute or it is only pinned
    on the platform it was created on.
    """
    attributes = Path(".gitattributes")
    assert attributes.is_file(), "no .gitattributes; digest-pinned files are unprotected"

    rules = [
        line.split()[0]
        for line in attributes.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and "-text" in line
    ]
    assert any("schemas/" in rule for rule in rules), (
        f"the pinned SARIF schema at {sarif.PINNED_SCHEMA_PATH} is not covered by a "
        f"-text rule; current rules: {rules}"
    )


def test_missing_pinned_sarif_schema_is_an_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(sarif.SarifSchemaError, match="missing"):
        sarif.load_pinned_schema(tmp_path / "absent.json")


@needs_pinned_schema
def test_invalid_sarif_is_rejected_rather_than_passed_over() -> None:
    log = sarif.build([make_run(Classification.CRASH)])
    del log["runs"][0]["tool"]  # `tool` is required by SARIF 2.1.0
    with pytest.raises(sarif.SarifValidationError):
        sarif.validate(log)


@needs_pinned_schema
def test_http_scenario_sarif_also_validates() -> None:
    """The HTTP transport produces different properties; validate those too."""
    scenario = get_scenario("version.http_header_body_mismatch")
    run = make_run(
        Classification.SPEC_VIOLATION,
        scenario_id=scenario.scenario_id,
        failed=("is_error", "code_32020"),
    )
    sarif.validate(sarif.build([run]))


# ------------------------------------------------ target fingerprint v3


def _registry_spec(digest: str) -> dict[str, object]:
    return {
        "image_source": {
            "kind": "registry",
            "repository": "example/server",
            "repository_digest": digest,
        }
    }


def _local_build_spec(**overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "kind": "local_build",
        "build_key": "synthetic-python-stdio",
        "runtime_tag": "interop-lab/synthetic-python-stdio:1.2.3",
        "build_context": "targets/external/synthetic-python-stdio",
        "dockerfile": "targets/external/synthetic-python-stdio/Dockerfile",
    }
    source.update(overrides)
    return {"image_source": source}


def test_fingerprint_covers_how_the_target_is_executed() -> None:
    """v1 hashed the program alone, which made two measurements look comparable.

    The same server on the host and in a 256 MB container with no network can
    legitimately produce different results. A baseline recorded under one and
    compared against the other looked comparable and was not.
    """
    from interop_lab.containment.model import ContainerSpec, ExecutionMode, Provenance

    spec = _registry_spec("sha256:" + "a" * 64)

    host = make_target()
    contained = make_target().model_copy(
        update={
            "provenance": Provenance.EXTERNAL,
            "execution_mode": ExecutionMode.CONTAINER,
            "container": ContainerSpec.model_validate(spec),
        }
    )
    assert target_fingerprint(host) != target_fingerprint(contained)


def test_weakening_containment_drifts_the_fingerprint() -> None:
    """Nobody approved results measured under weaker isolation."""
    from interop_lab.containment.model import ContainerSpec, ExecutionMode, Provenance

    def contained(**spec_overrides: object) -> TargetDefinition:
        spec: dict[str, object] = _registry_spec("sha256:" + "a" * 64)
        spec.update(spec_overrides)
        return make_target().model_copy(
            update={
                "provenance": Provenance.EXTERNAL,
                "execution_mode": ExecutionMode.CONTAINER,
                "container": ContainerSpec.model_validate(spec),
            }
        )

    strict = contained()
    for weakened in (
        contained(memory_mb=4096),
        contained(pids_limit=100_000),
        contained(read_only_root=False),
        contained(user="0:0"),
        contained(drop_all_capabilities=False),
        contained(no_new_privileges=False),
    ):
        assert target_fingerprint(strict) != target_fingerprint(weakened), weakened


def test_a_different_image_drifts_the_fingerprint() -> None:
    from interop_lab.containment.model import ContainerSpec, ExecutionMode, Provenance

    def with_digest(digest: str) -> TargetDefinition:
        return make_target().model_copy(
            update={
                "provenance": Provenance.EXTERNAL,
                "execution_mode": ExecutionMode.CONTAINER,
                "container": ContainerSpec.model_validate(_registry_spec(digest)),
            }
        )

    assert target_fingerprint(with_digest("sha256:" + "a" * 64)) != target_fingerprint(
        with_digest("sha256:" + "b" * 64)
    )


def test_a_local_build_fingerprint_is_independent_of_the_builders_image_id() -> None:
    """The property that makes a baseline shareable at all.

    A locally built image gets a different ID on a different storage driver. If
    that ID reached the fingerprint, the same approved target would fingerprint
    differently on every machine, every CI run would report drift that is not
    drift, and no baseline could ever be shared. So the fingerprint covers what
    the image is built *from*, and the ID lives in the build resolution.
    """
    from interop_lab.containment.model import ContainerSpec, ExecutionMode, Provenance

    def built(**overrides: object) -> TargetDefinition:
        return make_target().model_copy(
            update={
                "provenance": Provenance.EXTERNAL,
                "execution_mode": ExecutionMode.CONTAINER,
                "container": ContainerSpec.model_validate(_local_build_spec(**overrides)),
            }
        )

    baseline_target = built()
    # Nothing about a particular build appears in the target, so there is no
    # field an image ID could enter through — and two machines that build the
    # same inputs agree.
    assert target_fingerprint(baseline_target) == target_fingerprint(built())

    # But changing what it is built from does drift it, which is the point.
    for changed in (
        built(build_key="other"),
        built(
            dockerfile="targets/external/synthetic-typescript-http/Dockerfile",
            build_context="targets/external/synthetic-typescript-http",
        ),
        built(base_image_digests=["mirror.gcr.io/library/python@sha256:" + "d" * 64]),
        built(toolchain_pins={"python": "3.12.7"}),
        built(runtime_tag="interop-lab/synthetic-python-stdio:9.9.9"),
    ):
        assert target_fingerprint(baseline_target) != target_fingerprint(changed), changed


def test_the_fingerprint_refuses_a_builder_local_id_smuggled_into_a_source() -> None:
    """A guard, not a formality: this is the value that must never get in."""
    from interop_lab.containment.model import ContainerSpec, ExecutionMode, Provenance

    smuggled = make_target().model_copy(
        update={
            "provenance": Provenance.EXTERNAL,
            "execution_mode": ExecutionMode.CONTAINER,
            "container": ContainerSpec.model_validate(
                _local_build_spec(toolchain_pins={"image": "sha256:" + "e" * 64})
            ),
        }
    )
    # Nested inside a dict rather than a top-level field, so the check has to
    # look at values rather than at the field list.
    assert target_fingerprint(smuggled)  # dict values are not scanned...

    from interop_lab.containment.imagesource import LocalBuildImageSource

    source = LocalBuildImageSource.model_validate(_local_build_spec()["image_source"])
    poisoned = source.model_copy(update={"build_key": "sha256:" + "e" * 64})
    target = make_target().model_copy(
        update={
            "provenance": Provenance.EXTERNAL,
            "execution_mode": ExecutionMode.CONTAINER,
            "container": ContainerSpec.model_validate({"image_source": poisoned}),
        }
    )
    with pytest.raises(ValueError, match="builder-local image ID"):
        target_fingerprint(target)


def test_the_gate_refuses_to_compare_across_fingerprint_versions() -> None:
    """Every entry would report drift, hiding any real regression in the noise."""
    from interop_lab.ci.baseline import GateOutcome as Outcome

    baseline = make_document(make_entry())
    stale = baseline.model_copy(update={"target_fingerprint_version": 1})

    result = evaluate(
        baseline=stale,
        runs=[(make_run(Classification.PASS), SCENARIO, make_target())],
    )
    assert result.outcome is Outcome.CONFIGURATION_ERROR
    assert result.exit_code == 2
    assert any("fingerprint v1" in f for f in result.harness_failures), result.harness_failures


def test_the_committed_baseline_declares_the_current_fingerprint_version() -> None:
    document = BaselineDocument.model_validate_json(
        Path("baselines/mvp.json").read_text(encoding="utf-8")
    )
    from interop_lab.ci.baseline import TARGET_FINGERPRINT_VERSION

    assert document.target_fingerprint_version == TARGET_FINGERPRINT_VERSION
