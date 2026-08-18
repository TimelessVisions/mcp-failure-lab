"""The public-safe dataset must survive a scan it did not write.

The redaction is a hand-maintained list of fields, and a hand-maintained list
acquires a gap. So these tests do not check that the transform ran; they check
that nothing identifying survived it, using a denylist generated from the private
inventories rather than from the transform's own idea of what it removed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from interop_lab.viewer.export import (
    PUBLIC_SAFE_EYEBROW,
    SYNTHETIC_DISCLAIMER,
    SYNTHETIC_EYEBROW,
    build_dataset,
)
from interop_lab.viewer.fixture import write_fixture
from interop_lab.viewer.model import EvidenceKind, ViewerDataset, VisibilityMode
from interop_lab.viewer.redaction import Denylist, build_denylist, scan_payload, to_public_safe
from interop_lab.viewer.source import load_source

REPO_ROOT = Path(__file__).resolve().parents[2]


def private_inventories_present() -> bool:
    """Whether this tree carries the inventories the denylist is generated from.

    The denylist is built from the pinned external target definitions and the
    private finding packages. A public export withholds both, so there is
    nothing for it to be generated from and these four tests have no subject.

    The rest of this module keeps running there, and it is the part that
    matters publicly: it asserts what the public-safe document does and does not
    contain, which is a property of the redaction rather than of the private
    inventories behind it.
    """
    return (REPO_ROOT / "targets" / "external").is_dir() and (
        REPO_ROOT / "artifacts" / "private-findings"
    ).is_dir()


needs_private_inventories = pytest.mark.skipif(
    not private_inventories_present(),
    reason="the private inventories the denylist is generated from are not in this tree",
)


@pytest.fixture(scope="module")
def private_dataset(tmp_path_factory: pytest.TempPathFactory) -> ViewerDataset:
    output = write_fixture(tmp_path_factory.mktemp("benchmark"), repo_root=REPO_ROOT)
    return build_dataset(load_source(output), repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def public_dataset(private_dataset: ViewerDataset) -> ViewerDataset:
    return to_public_safe(
        private_dataset,
        anonymization_approved=False,
        approval_reference="synthetic fixture evidence: no third party to anonymise",
    )


@pytest.fixture(scope="module")
def denylist() -> Denylist:
    return build_denylist(REPO_ROOT)


@needs_private_inventories
def test_the_denylist_is_generated_and_not_empty(denylist: Denylist) -> None:
    assert denylist.terms
    origins = {term.origin.split(":")[0] for term in denylist.terms}
    assert any("target definition" in origin for origin in origins)


@needs_private_inventories
def test_the_denylist_names_the_pinned_third_party_targets(denylist: Denylist) -> None:
    values = " ".join(denylist.values).lower()
    for expected in ("mcp-go-everything", "server-everything", "mcp-server-time"):
        assert expected in values, f"{expected} is not on the denylist"


@needs_private_inventories
def test_the_denylist_names_every_private_finding_package(denylist: Denylist) -> None:
    packages = sorted(
        path.name
        for path in (REPO_ROOT / "artifacts" / "private-findings").glob("*")
        if path.is_dir()
    )
    assert packages, "the repository must carry private finding packages to scan for"
    for package in packages:
        assert package in denylist.values


def test_the_denylist_excludes_this_harness_own_vocabulary(denylist: Denylist) -> None:
    """A scanner that flags its own methodology prose is one people stop reading."""
    values = {value.lower() for value in denylist.values}
    for vocabulary in ("not_measured", "spec_violation", "container", "mcp-2025-11-25"):
        assert vocabulary not in values


def test_the_public_dataset_carries_no_pinned_identity(public_dataset: ViewerDataset) -> None:
    for target in public_dataset.targets:
        assert target.target_id is None
        assert target.target_file is None
        assert target.source_version is None
        assert target.source_commit is None
        assert target.build_context is None
        assert target.display_name.startswith("Implementation ")


def test_the_public_dataset_carries_no_fingerprint_or_build_hash(
    public_dataset: ViewerDataset,
) -> None:
    """Every one of these is a value a guess can be confirmed against."""
    for target in public_dataset.targets:
        assert target.target_fingerprint is None
        assert target.portable_identity_fingerprint is None
        assert target.dockerfile_sha256 is None
        assert target.lock_sha256 is None
        assert target.source_archive_sha256 is None
        assert target.capability_fingerprint is None
    for record in public_dataset.result_signatures:
        assert record.target_fingerprint is None
        assert record.capability_fingerprint is None
    for surface in public_dataset.capabilities:
        assert surface.capability_fingerprint is None


def test_the_public_dataset_carries_no_observed_tool_names(public_dataset: ViewerDataset) -> None:
    for surface in public_dataset.capabilities:
        for tool in surface.tools:
            assert tool.name.startswith("tool-")
            # The structural fact survives; the name does not.
            assert tool.input_schema_fingerprint


def test_the_public_dataset_carries_no_private_finding_package(
    public_dataset: ViewerDataset,
) -> None:
    """A private finding's existence is disclosure, so it is absent rather than masked."""
    for summary in public_dataset.reproduction:
        assert summary.synthetic
        assert summary.status != "private_finding_package"


def test_the_public_dataset_carries_no_environment_or_harness_identity(
    public_dataset: ViewerDataset,
) -> None:
    assert public_dataset.generated_from_harness_commit is None
    # The commit travels in three places. Dropping one of them would be a
    # redaction that reads as complete and is not.
    assert public_dataset.benchmark.definition_harness_commit is None
    assert public_dataset.benchmark.execution_harness_commit is None
    for card in public_dataset.timings:
        assert card.environment == "environment withheld"


def test_no_commit_shaped_value_survives_into_the_public_dataset(
    public_dataset: ViewerDataset,
) -> None:
    """A structural check, because a field list maintained by hand acquires gaps.

    The field-by-field assertions above are only as complete as somebody's
    memory. This one looks for the *shape* of a commit anywhere in the document,
    and it is what caught `benchmark.definition_harness_commit` when the
    field-by-field version passed.
    """
    import re

    payload = json.dumps(public_dataset.model_dump(mode="json"))
    assert re.search(r"\b[0-9a-f]{40}\b", payload) is None


def test_the_public_dataset_is_clean_against_the_generated_denylist(
    public_dataset: ViewerDataset, denylist: Denylist
) -> None:
    findings = scan_payload(public_dataset.model_dump(mode="json"), denylist)
    assert not findings, "\n".join(
        f"{finding.term} at {finding.location} (from {finding.origin})" for finding in findings
    )


@needs_private_inventories
def test_the_private_dataset_is_not_clean_which_is_why_it_is_private(
    private_dataset: ViewerDataset, denylist: Denylist
) -> None:
    """A control on the control.

    If the private dataset also scanned clean, the previous test would be
    passing because the denylist matches nothing rather than because the
    redaction works.
    """
    findings = scan_payload(private_dataset.model_dump(mode="json"), denylist)
    assert findings, (
        "the private dataset scanned clean against the private denylist, which "
        "means the leak scan is not actually testing anything"
    )


def test_the_public_dataset_states_that_it_is_not_publishable(
    public_dataset: ViewerDataset,
) -> None:
    controls = public_dataset.disclosure_controls
    assert controls.visibility_mode is VisibilityMode.PUBLIC_SAFE
    assert controls.publishable is False
    assert controls.redactions_applied
    assert any("Anonymised, not unidentifiable" in note for note in controls.notes)


def test_the_public_dataset_does_not_ship_the_denylist_itself(
    public_dataset: ViewerDataset,
) -> None:
    """Shipping the terms a scan looks for would put every one in the bundle."""
    payload = json.dumps(public_dataset.model_dump(mode="json"))
    assert "denylist_terms" not in payload
    assert isinstance(public_dataset.disclosure_controls.denylist_term_count, int)


def test_the_public_dataset_does_not_carry_the_private_benchmark_name(
    public_dataset: ViewerDataset,
) -> None:
    """The defect this closes: an invented matrix under a real execution's title.

    The public-safe build displayed ``benchmark_name`` from the private
    definition, so a synthetic demonstration presented itself as the report of
    a private benchmark against real third parties.
    """
    assert public_dataset.benchmark.benchmark_name is None
    payload = json.dumps(public_dataset.model_dump(mode="json"))
    assert "Private 0.3" not in payload
    # And the document reports its own visibility, not the source benchmark's.
    assert public_dataset.benchmark.visibility == VisibilityMode.PUBLIC_SAFE.value


def test_the_public_dataset_says_its_evidence_is_synthetic(
    public_dataset: ViewerDataset,
) -> None:
    provenance = public_dataset.provenance
    assert provenance.eyebrow == PUBLIC_SAFE_EYEBROW
    assert provenance.disclaimer == SYNTHETIC_DISCLAIMER
    assert provenance.evidence_kind is EvidenceKind.SYNTHETIC
    assert "invented" in provenance.evidence_note


def test_the_public_dataset_does_not_imply_it_is_an_anonymised_real_one(
    public_dataset: ViewerDataset,
) -> None:
    """Nothing is withheld here, because no private execution produced it.

    Claiming a withheld identity would suggest these rows are a masked version
    of results that exist — a stronger and false claim.
    """
    provenance = public_dataset.provenance
    assert provenance.benchmark_identity_withheld is False
    assert "not an anonymised version" in provenance.benchmark_identity_note


def test_schema_provenance_is_stated_apart_from_data_provenance(
    public_dataset: ViewerDataset,
) -> None:
    """The schema really is shared. The measurements are not. Both, separately."""
    provenance = public_dataset.provenance
    assert provenance.schema_lineage == (
        "Generated from the Benchmark 0.3 schema and scenario library."
    )
    assert provenance.benchmark_schema_version == "0.3"
    assert provenance.evidence_kind is EvidenceKind.SYNTHETIC


def test_a_private_synthetic_build_is_not_labelled_public_safe(
    private_dataset: ViewerDataset,
) -> None:
    """Synthetic and public-safe are different properties.

    The private export of the fixture is the dataset the guided-mode tests run
    against. It is synthetic, and it is not public-safe; the eyebrow must not
    claim the second on the strength of the first.
    """
    assert private_dataset.provenance.evidence_kind is EvidenceKind.SYNTHETIC
    assert private_dataset.provenance.eyebrow == SYNTHETIC_EYEBROW
    assert private_dataset.visibility_mode is VisibilityMode.PRIVATE


def test_a_public_safe_dataset_cannot_be_built_carrying_the_private_name(
    public_dataset: ViewerDataset,
) -> None:
    """The rule is enforced by the model, not only by the transform.

    A redaction step is a list somebody maintains by hand, and a list maintained
    by hand acquires a gap. This is the check that refuses the document itself,
    whichever code path assembled it.
    """
    payload = public_dataset.model_dump(mode="json")
    payload["benchmark"]["benchmark_name"] = "MCP Interoperability Benchmark — Private 0.3"

    with pytest.raises(ValidationError) as caught:
        ViewerDataset.model_validate(payload)
    assert "private benchmark" in str(caught.value)


def test_a_public_safe_dataset_must_carry_a_disclaimer(
    public_dataset: ViewerDataset,
) -> None:
    payload = public_dataset.model_dump(mode="json")
    payload["provenance"]["disclaimer"] = None

    with pytest.raises(ValidationError) as caught:
        ViewerDataset.model_validate(payload)
    assert "disclaimer" in str(caught.value)


def test_a_synthetic_dataset_may_not_claim_a_withheld_identity(
    public_dataset: ViewerDataset,
) -> None:
    """Suggesting an invented matrix is a masked real one is the worse error."""
    payload = public_dataset.model_dump(mode="json")
    payload["provenance"]["benchmark_identity_withheld"] = True

    with pytest.raises(ValidationError) as caught:
        ViewerDataset.model_validate(payload)
    assert "anonymised version of real results" in str(caught.value)


def test_public_safe_from_real_evidence_requires_owner_approval(tmp_path: Path) -> None:
    """Replacing a name is not the same as making a row anonymous.

    Language, transport and result narrow the field in combination — there are
    not many independent Go MCP servers — so a public-safe export from real
    third-party evidence is a decision the owner makes, not one an export makes.
    """
    from typer.testing import CliRunner

    from interop_lab.cli.main import app

    output = write_fixture(tmp_path / "benchmark", repo_root=REPO_ROOT)
    (output / "synthetic-fixture.json").unlink()
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].pop("synthetic-fixture.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = CliRunner().invoke(
        app,
        [
            "viewer",
            "export",
            "--benchmark",
            str(output),
            "--mode",
            "public-safe",
            "--out",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 3
    assert not (tmp_path / "out" / "dataset.json").exists()
