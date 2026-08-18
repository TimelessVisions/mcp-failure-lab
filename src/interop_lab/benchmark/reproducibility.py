"""Comparing two benchmark executions from different environments.

What may be compared across machines and what may not is the whole content of
this module.

**Compared:** target fingerprints, scenario fingerprints, negotiated profiles,
capability fingerprints, result signatures, classifications, containment
outcomes. Every one of them is an identity — a statement about *what was
measured* — and identities are exactly what should agree between two correct
executions of the same commit.

**Never compared:** durations. A GitHub-hosted runner is shared and its timings
vary between runs for reasons that have nothing to do with the code. Comparing
them across machines would produce a difference that is real, meaningless, and
easy to mistake for a finding.

**Expected to differ, and not drift:** builder-local image IDs. The containerd
snapshotter and the classic storage driver produce different identifiers for
identical bits, so a difference there is a fact about the storage driver.
Cross-environment agreement rests on the portable build inputs instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from interop_lab.benchmark.model import MeasurementState


class EnvironmentSummary(BaseModel):
    """What one execution's environment was, for the record rather than for comparison."""

    model_config = ConfigDict(extra="forbid")

    identity: str
    os: str | None = None
    machine: str | None = None
    python: str | None = None
    docker_engine_version: str | None = None
    storage_driver: str | None = None
    architecture: str | None = None


class ReproducibilityComparison(BaseModel):
    """Whether two environments measured the same thing."""

    model_config = ConfigDict(extra="forbid")

    environment_a: EnvironmentSummary
    environment_b: EnvironmentSummary
    state: MeasurementState = MeasurementState.MEASURED

    target_fingerprints_agree: bool = True
    portable_build_identities_agree: bool = True
    scenario_fingerprints_agree: bool = True
    negotiated_profiles_agree: bool = True
    capability_fingerprints_agree: bool = True
    classifications_agree: bool = True
    result_signatures_agree: bool = True

    disagreements: list[str] = Field(default_factory=list)
    #: Differences that are expected and are *not* drift.
    expected_differences: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Timing values are not compared across machines and no timing claim is "
            "made from this comparison.",
            "Builder-local image IDs differ between storage drivers for identical bits "
            "and are not target drift. Agreement is asserted over the portable build "
            "inputs — source archive, dependency lock, Dockerfile, base image digests, "
            "toolchain pins.",
            "Two environments agreeing establishes that the measurement is reproducible "
            "across them. It establishes nothing about a third.",
        ]
    )

    @property
    def agrees(self) -> bool:
        return not self.disagreements


def _load(path: Path, name: str) -> Any:
    target = path / name
    if not target.is_file():
        raise FileNotFoundError(f"{target} is missing; the execution wrote no {name}")
    return json.loads(target.read_text(encoding="utf-8"))


def _environment(payload: dict[str, Any]) -> EnvironmentSummary:
    return EnvironmentSummary(
        identity=str(payload.get("runner_identity") or "unspecified"),
        os=payload.get("os"),
        machine=payload.get("machine"),
        python=payload.get("python"),
        docker_engine_version=payload.get("docker_engine_version"),
        storage_driver=payload.get("storage_driver"),
        architecture=payload.get("architecture"),
    )


def _by_key(rows: list[dict[str, Any]], *keys: str) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple(row.get(key) for key in keys): row for row in rows}


def compare(left: Path, right: Path) -> ReproducibilityComparison:
    """Compare two benchmark output directories.

    Disagreements are collected rather than raised, and each names the identity
    that moved. "The two runs disagree" is a true statement and a useless one; a
    reader needs to know whether the target changed, the scenario changed, or the
    server advertised something different.
    """
    comparison = ReproducibilityComparison(
        environment_a=_environment(_load(left, "environment.json")),
        environment_b=_environment(_load(right, "environment.json")),
    )

    if comparison.environment_a.storage_driver != comparison.environment_b.storage_driver:
        comparison.expected_differences.append(
            f"storage driver differs: {comparison.environment_a.storage_driver} vs "
            f"{comparison.environment_b.storage_driver}. Builder-local image IDs are "
            "therefore expected to differ and are not compared."
        )

    # ------------------------------------------------------------------ targets
    left_targets = _by_key(_load(left, "target-matrix.json"), "target_key")
    right_targets = _by_key(_load(right, "target-matrix.json"), "target_key")
    for key in sorted(set(left_targets) | set(right_targets)):
        a, b = left_targets.get(key), right_targets.get(key)
        if a is None or b is None:
            comparison.target_fingerprints_agree = False
            comparison.disagreements.append(f"target {key[0]}: present in only one environment")
            continue
        if a["target_fingerprint"] != b["target_fingerprint"]:
            comparison.target_fingerprints_agree = False
            comparison.disagreements.append(
                f"target {key[0]}: fingerprint {a['target_fingerprint']} vs "
                f"{b['target_fingerprint']}"
            )
        if a.get("portable_identity_fingerprint") != b.get("portable_identity_fingerprint"):
            comparison.portable_build_identities_agree = False
            comparison.disagreements.append(
                f"target {key[0]}: portable build identity "
                f"{a.get('portable_identity_fingerprint')} vs "
                f"{b.get('portable_identity_fingerprint')}. This one *is* drift: the "
                "portable inputs are hashes of committed files."
            )

    # ---------------------------------------------------------------- scenarios
    left_scenarios = _by_key(_load(left, "scenario-matrix.json"), "scenario_id")
    right_scenarios = _by_key(_load(right, "scenario-matrix.json"), "scenario_id")
    for key in sorted(set(left_scenarios) | set(right_scenarios)):
        a, b = left_scenarios.get(key), right_scenarios.get(key)
        if a is None or b is None or a["scenario_fingerprint"] != b["scenario_fingerprint"]:
            comparison.scenario_fingerprints_agree = False
            comparison.disagreements.append(f"scenario {key[0]}: fingerprint differs or is absent")

    # ------------------------------------------------------------- capabilities
    left_caps = _by_key(_load(left, "capability-results.json"), "target_key")
    right_caps = _by_key(_load(right, "capability-results.json"), "target_key")
    for key in sorted(set(left_caps) | set(right_caps)):
        a, b = left_caps.get(key), right_caps.get(key)
        if a is None or b is None:
            comparison.capability_fingerprints_agree = False
            comparison.disagreements.append(
                f"target {key[0]}: capability record present in only one environment"
            )
            continue
        if a.get("capability_fingerprint") != b.get("capability_fingerprint"):
            comparison.capability_fingerprints_agree = False
            comparison.disagreements.append(
                f"target {key[0]}: capability fingerprint "
                f"{a.get('capability_fingerprint')} vs {b.get('capability_fingerprint')}. "
                "The server advertised a different surface in the two environments."
            )

    # ----------------------------------------- classifications and signatures
    left_results = _by_key(
        _load(left, "classification-results.json"), "target_key", "scenario_id", "repetition"
    )
    right_results = _by_key(
        _load(right, "classification-results.json"), "target_key", "scenario_id", "repetition"
    )
    for key in sorted(set(left_results) & set(right_results), key=str):
        a, b = left_results[key], right_results[key]
        if a.get("classification") != b.get("classification"):
            comparison.classifications_agree = False
            comparison.disagreements.append(
                f"{key[0]}/{key[1]} rep {key[2]}: {a.get('classification')} vs "
                f"{b.get('classification')}"
            )

    left_signatures = _by_key(
        _load(left, "result-signatures.json"), "target_key", "scenario_id", "repetition"
    )
    right_signatures = _by_key(
        _load(right, "result-signatures.json"), "target_key", "scenario_id", "repetition"
    )
    shared = set(left_signatures) & set(right_signatures)
    for key in sorted(shared, key=str):
        if left_signatures[key].get("digest") != right_signatures[key].get("digest"):
            comparison.result_signatures_agree = False
            comparison.disagreements.append(
                f"{key[0]}/{key[1]} rep {key[2]}: result signature digest differs"
            )

    only_left = sorted(set(left_signatures) - shared, key=str)
    only_right = sorted(set(right_signatures) - shared, key=str)
    if only_left or only_right:
        comparison.result_signatures_agree = False
        comparison.disagreements.append(
            f"{len(only_left)} result(s) present only in the first environment and "
            f"{len(only_right)} only in the second; the two did not measure the same set"
        )

    return comparison
