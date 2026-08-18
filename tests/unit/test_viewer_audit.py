"""`viewer audit` — the check that a private dataset actually holds real evidence.

`scan` asks whether a public document leaked something. This asks the opposite
question of a private one, and it exists because of a specific way the private
viewer path could have stayed broken: an export that succeeds, a build that
renders, a job that goes green, and a dataset that came from the synthetic
fixture the whole time.

The distinguishing check is therefore not "does this look populated" — the
fixture looks populated, deliberately, because it is a real benchmark output.
It is whether the evidence is a *measurement*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from interop_lab.cli.main import app
from interop_lab.viewer.export import build_dataset
from interop_lab.viewer.fixture import write_fixture
from interop_lab.viewer.model import EvidenceKind, VisibilityMode
from interop_lab.viewer.redaction import to_public_safe
from interop_lab.viewer.source import load_source

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture(scope="module")
def private_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("audit")
    output = write_fixture(root / "benchmark", repo_root=REPO_ROOT)
    dataset = build_dataset(load_source(output), repo_root=REPO_ROOT)
    path = root / "private.json"
    path.write_text(json.dumps(dataset.model_dump(mode="json")), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def public_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("audit-public")
    output = write_fixture(root / "benchmark", repo_root=REPO_ROOT)
    dataset = to_public_safe(
        build_dataset(load_source(output), repo_root=REPO_ROOT),
        anonymization_approved=False,
        approval_reference="synthetic fixture evidence: no third party to anonymise",
    )
    path = root / "public.json"
    path.write_text(json.dumps(dataset.model_dump(mode="json")), encoding="utf-8")
    return path


def test_audit_describes_a_dataset_without_judging_it(private_path: Path) -> None:
    result = runner.invoke(app, ["viewer", "audit", str(private_path)])
    assert result.exit_code == 0
    for dimension in (
        "target identities",
        "languages",
        "transports",
        "capability fingerprints",
        "classification results",
        "result signatures",
        "containment evidence",
        "timing evidence",
        "measurement coverage",
    ):
        assert dimension in result.output


def test_require_private_refuses_a_synthetic_dataset(private_path: Path) -> None:
    """The check the private-viewer job depends on.

    Every dimension is present in the fixture — it is a real benchmark output
    with invented subjects — so the only thing separating it from a real run is
    that nothing was measured. If this passed, a green private-viewer job would
    prove nothing at all.
    """
    result = runner.invoke(app, ["viewer", "audit", str(private_path), "--require-private"])
    assert result.exit_code == 1
    assert "synthetic fixture evidence" in result.output


def test_require_private_refuses_a_public_safe_dataset(public_path: Path) -> None:
    result = runner.invoke(app, ["viewer", "audit", str(public_path), "--require-private"])
    assert result.exit_code == 1
    assert "public-safe" in result.output


def test_require_private_accepts_a_dataset_from_real_measurement(
    private_path: Path, tmp_path: Path
) -> None:
    """A real run differs from the fixture in exactly one recorded way.

    Constructed by flipping that one field rather than by hand-writing a
    dataset, so the test cannot pass because the fixture it built happened to
    omit something the audit does not check.
    """
    payload = json.loads(private_path.read_text(encoding="utf-8"))
    payload["provenance"]["evidence_kind"] = EvidenceKind.REAL_MEASUREMENT.value
    payload["provenance"]["evidence_note"] = "Measured against real implementations."
    payload["provenance"]["eyebrow"] = "MCP Interoperability Benchmark — Private 0.3"
    payload["provenance"]["disclaimer"] = None

    path = tmp_path / "real.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(app, ["viewer", "audit", str(path), "--require-private"])
    assert result.exit_code == 0, result.output
    assert "real private evidence" in result.output


def test_require_private_names_each_missing_dimension(private_path: Path, tmp_path: Path) -> None:
    """An empty shape renders without error, which is why this is checked."""
    payload = json.loads(private_path.read_text(encoding="utf-8"))
    payload["provenance"]["evidence_kind"] = EvidenceKind.REAL_MEASUREMENT.value
    payload["provenance"]["disclaimer"] = None
    payload["containment"] = []
    payload["result_signatures"] = []
    for target in payload["targets"]:
        target["capability_fingerprint"] = None

    path = tmp_path / "hollow.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(app, ["viewer", "audit", str(path), "--require-private"])
    assert result.exit_code == 1
    assert "containment evidence" in result.output
    assert "result signatures" in result.output
    assert "capability fingerprints" in result.output


def test_audit_refuses_a_document_that_is_not_a_viewer_dataset(tmp_path: Path) -> None:
    path = tmp_path / "not-a-dataset.json"
    path.write_text('{"hello": "world"}', encoding="utf-8")

    result = runner.invoke(app, ["viewer", "audit", str(path)])
    assert result.exit_code == 2


def test_audit_reports_the_visibility_it_found(private_path: Path, public_path: Path) -> None:
    private_result = runner.invoke(app, ["viewer", "audit", str(private_path)])
    assert VisibilityMode.PRIVATE.value in private_result.output

    public_result = runner.invoke(app, ["viewer", "audit", str(public_path)])
    assert VisibilityMode.PUBLIC_SAFE.value in public_result.output
    # A public-safe document has had the private benchmark's name removed, and
    # the audit must say so rather than printing an empty field.
    assert "(withheld)" in public_result.output
