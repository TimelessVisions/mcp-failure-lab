"""Reading a benchmark output directory, and refusing to read a broken one.

The viewer's whole claim is that every value on a screen came from a benchmark
artifact. That claim is worth exactly as much as the check behind it, so the
checks here are total and they refuse rather than warn:

* every file named by the manifest is re-hashed and must match;
* every file on disk must be named by the manifest — an artifact nobody recorded
  is an artifact nobody can attribute, and rendering it would put unverified
  content on a screen that says everything is verified;
* a manifest recording execution problems is refused, because those problems are
  the run telling you its own results are incomplete;
* every artifact the benchmark schema describes is validated against it.

The failure mode this prevents is specific. A directory assembled by hand, or
half-copied, or produced by a run that recorded problems, looks exactly like a
good one to a loader that only reads what it needs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from interop_lab.benchmark.model import BenchmarkDefinition, BenchmarkOutputs
from interop_lab.benchmark.schema import build_schema, validate_artifact

#: Artifact → the schema definition it is validated against. Only the artifacts
#: the generated schema actually describes; validating a free-form document
#: against a model it was never shaped by would fail for reasons that are not
#: defects.
VALIDATED_ARTIFACTS: dict[str, str] = {
    "benchmark-definition.json": "BenchmarkDefinition",
    "manifest.json": "BenchmarkOutputs",
    "measurement-coverage.json": "MeasurementCoverage",
    "measured-operations.json": "MeasuredOperations",
}

#: Artifacts the exporter reads. A directory missing one of these cannot produce
#: a complete dataset, and the export says which rather than rendering a gap.
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "benchmark-definition.json",
    "manifest.json",
    "target-matrix.json",
    "scenario-matrix.json",
    "classification-results.json",
    "applicability.json",
    "category-observations.json",
    "result-signatures.json",
    "capability-results.json",
    "timings.json",
    "measurement-coverage.json",
    "environment.json",
)


class BenchmarkSourceError(Exception):
    """The benchmark output directory cannot be trusted to describe itself."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_hash(outputs: BenchmarkOutputs) -> str:
    """One digest standing for the whole set of artifacts an execution wrote.

    Over the manifest's *artifact map* rather than over the manifest file, so it
    is stable against the manifest gaining a field and moves the instant any
    artifact's content or name changes. Two datasets exported from one execution
    carry the same value; one exported from a different execution cannot claim it.
    """
    material = json.dumps(outputs.artifacts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class BenchmarkSource:
    """A verified benchmark output directory, with its artifacts loaded."""

    root: Path
    outputs: BenchmarkOutputs
    definition: BenchmarkDefinition
    manifest_digest: str
    artifacts: dict[str, Any] = field(default_factory=dict)

    def json(self, name: str) -> Any:
        """One artifact's parsed content, or ``None`` when it was not present.

        ``None`` rather than a raise: several artifacts are legitimately absent
        from an older execution, and the export turns their absence into a
        NOT_MEASURED state rather than into a crash.
        """
        return self.artifacts.get(name)


def _verify_manifest(root: Path, outputs: BenchmarkOutputs) -> list[str]:
    problems: list[str] = []
    for relative, expected in sorted(outputs.artifacts.items()):
        path = root / relative
        if not path.is_file():
            problems.append(f"{relative}: named in the manifest and absent from disk")
            continue
        observed = sha256_file(path)
        if observed != expected:
            problems.append(
                f"{relative}: the manifest records {expected} and the file on disk "
                f"hashes to {observed}. Something changed after the run recorded it."
            )
    on_disk = {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    for extra in sorted(on_disk - set(outputs.artifacts)):
        problems.append(
            f"{extra}: present on disk and absent from the manifest. An artifact "
            "nobody recorded cannot be attributed to this execution, and the viewer "
            "refuses it rather than displaying unverified content."
        )
    return problems


def load_source(root: Path, *, require_clean_manifest: bool = True) -> BenchmarkSource:
    """Verify a benchmark output directory and load its artifacts.

    ``require_clean_manifest`` exists for one caller only — the schema-validation
    test that deliberately feeds a manifest with problems — and defaults to the
    strict behaviour everywhere else.
    """
    problems: list[str] = []

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise BenchmarkSourceError(
            [
                f"{root}: no manifest.json. Without it nothing in this directory can "
                "be attributed to a benchmark execution."
            ]
        )
    try:
        outputs = BenchmarkOutputs.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkSourceError([f"manifest.json does not load: {exc}"]) from exc

    problems.extend(_verify_manifest(root, outputs))

    if require_clean_manifest and outputs.problems:
        problems.append(
            f"the manifest records {len(outputs.problems)} execution problem(s). A "
            "run that recorded problems is a run telling you its own results are "
            "incomplete, and the first is: "
            f"{outputs.problems[0]}"
        )

    for name in REQUIRED_ARTIFACTS:
        if not (root / name).is_file():
            problems.append(f"{name}: required by the viewer export and absent")

    if problems:
        raise BenchmarkSourceError(problems)

    artifacts: dict[str, Any] = {}
    for relative in sorted(outputs.artifacts):
        if not relative.endswith(".json"):
            continue
        try:
            artifacts[relative] = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{relative}: does not parse as JSON: {exc}")
    artifacts["manifest.json"] = outputs.model_dump(mode="json")

    schema = build_schema()
    for relative, model_name in VALIDATED_ARTIFACTS.items():
        payload = artifacts.get(relative)
        if payload is None:
            continue
        problems.extend(
            f"{relative}: {violation}"
            for violation in validate_artifact(payload, model_name, schema)
        )
    for observation in artifacts.get("category-observations.json") or []:
        problems.extend(
            f"category-observations.json: {violation}"
            for violation in validate_artifact(observation, "CategoryObservation", schema)
        )

    if problems:
        raise BenchmarkSourceError(problems)

    try:
        definition = BenchmarkDefinition.model_validate(artifacts["benchmark-definition.json"])
    except ValueError as exc:
        raise BenchmarkSourceError([f"benchmark-definition.json: {exc}"]) from exc

    return BenchmarkSource(
        root=root,
        outputs=outputs,
        definition=definition,
        manifest_digest=manifest_hash(outputs),
        artifacts=artifacts,
    )
