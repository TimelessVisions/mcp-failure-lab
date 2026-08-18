"""Validate every committed example artifact against its schema.

Committed examples are the only evidence a reviewer can inspect without running
the harness, so they must stay valid, complete, and safe as the models evolve.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

from interop_lab.artifacts.examples import STABLE_TIMESTAMP
from interop_lab.events.model import WireTrace
from interop_lab.models.core import Classification, DifferentialResult, TargetDefinition
from interop_lab.reporting.render import RunReport
from interop_lab.scenarios.model import Scenario

EXAMPLES_ROOT = Path("artifacts/examples")

REQUIRED_FILES = (
    "report.json",
    "wire.jsonl",
    "normalized-wire.jsonl",
    "finding.json",
    "differential.json",
    "minimization.json",
    "repro.yaml",
    "README.md",
    "run.sh",
    "run.ps1",
)

#: Patterns that must never appear in a committed artifact.
FORBIDDEN = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)

#: Shapes that only a live run produces. An ISO timestamp, a UUID, or an
#: ephemeral high port in a committed capability record means something volatile
#: escaped scrubbing and every future regeneration will diff.
PER_RUN_VALUE_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    # An ephemeral port. Five digits from 10000 up; hex digests cannot match,
    # because a word boundary cannot fall inside a run of hex characters.
    re.compile(r"\b[1-6]\d{4}\b"),
)


def example_dirs() -> list[Path]:
    if not EXAMPLES_ROOT.is_dir():
        return []
    return sorted(p for p in EXAMPLES_ROOT.iterdir() if p.is_dir())


def test_at_least_one_example_is_committed() -> None:
    assert example_dirs(), "no reviewable example artifact is committed"


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_example_has_every_required_file(example: Path) -> None:
    missing = [name for name in REQUIRED_FILES if not (example / name).is_file()]
    assert not missing, f"{example.name} is missing {missing}"


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_report_validates_against_the_model(example: Path) -> None:
    report = RunReport.model_validate_json((example / "report.json").read_text(encoding="utf-8"))
    assert report.runs
    for run in report.runs:
        assert run.resolved_target is not None
        assert run.resolved_target.version
        assert run.spec_version


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_differential_validates_against_the_model(example: Path) -> None:
    diff = DifferentialResult.model_validate_json(
        (example / "differential.json").read_text(encoding="utf-8")
    )
    assert diff.left_target and diff.right_target


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_traces_are_valid_jsonl(example: Path) -> None:
    for name in ("wire.jsonl", "normalized-wire.jsonl"):
        text = (example / name).read_text(encoding="utf-8")
        trace = WireTrace.from_jsonl(example.name, text)
        assert trace.events, f"{name} is empty"


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_repro_reloads_into_typed_models(example: Path) -> None:
    raw = yaml.safe_load((example / "repro.yaml").read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(raw["scenario"])
    target = TargetDefinition.model_validate(raw["target"])
    Classification(raw["expected_classification"])
    assert scenario.steps
    assert target.version not in ("main", "latest")


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_finding_states_its_limitation(example: Path) -> None:
    finding = json.loads((example / "finding.json").read_text(encoding="utf-8"))
    for field in ("fact", "normative_basis", "interpretation", "limitation", "reproduction"):
        assert finding.get(field), f"{field} is missing from finding.json"
    # A synthetic fixture must say so, loudly.
    assert "fixture" in finding["limitation"].lower()
    assert finding["public_safety"] == "private"


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_minimization_records_the_preserved_signature(example: Path) -> None:
    data = json.loads((example / "minimization.json").read_text(encoding="utf-8"))
    assert data["stable"] is True
    assert data["stability_runs"] == 3
    assert data["preserved_failing_assertions"]


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_capability_record_is_byte_stable(example: Path) -> None:
    """A committed capability record must carry no field that changes per run.

    Capability resolution was added to run evidence after these examples were
    first committed, and it brought a wall-clock `discovered_at` with it. That
    single field made `examples regenerate --check` fail on every invocation,
    because two regenerations minutes apart differed by a timestamp and nothing
    else. The check could not then distinguish a real behaviour change from the
    clock advancing, which is the whole point of committing the examples.

    This test fails if any future capability field reintroduces per-run
    variance, rather than waiting for CI to fail on a diff nobody can read.
    """
    report = json.loads((example / "report.json").read_text(encoding="utf-8"))
    records = [
        run["resolved_capabilities"]
        for run in report["runs"]
        if run.get("resolved_capabilities") is not None
    ]
    assert records, f"{example.name} committed no capability record to check"

    for record in records:
        assert record["discovered_at"] == STABLE_TIMESTAMP, (
            "discovered_at is wall-clock and must be scrubbed in a committed example"
        )
        # Derived from the target id, not from the run, so it is reproducible.
        assert record["discovery_run_id"].endswith(record["target_id"])
        # Relative by construction: an absolute path would stop resolving on any
        # machine that is not the one that generated the example.
        assert not PurePosixPath(record["source_trace_uri"]).is_absolute()
        # Content-derived, so identical across runs and still verifiable.
        for tool in record["tools"]:
            digest = tool.get("input_schema_sha256")
            assert digest is None or re.fullmatch(r"[0-9a-f]{64}", digest)

    encoded = json.dumps(records, sort_keys=True)
    for pattern in PER_RUN_VALUE_PATTERNS:
        assert not pattern.search(encoded), (
            f"capability record carries a per-run value matching {pattern.pattern}"
        )


@pytest.mark.parametrize("example", example_dirs(), ids=lambda p: p.name)
def test_example_contains_no_secrets_and_no_remote_hosts(example: Path) -> None:
    for path in sorted(example.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in FORBIDDEN:
            assert not pattern.search(text), f"{path} matches forbidden pattern {pattern.pattern}"
        # Only loopback may appear as a target URL.
        for match in re.findall(r"http://([^/\"\s]+)", text):
            host = match.split(":")[0]
            assert host in ("127.0.0.1", "localhost"), f"{path} references non-local host {host}"
