"""What may be compared across two machines, and what may not.

The comparison exists to answer one question — did two environments measure the
same thing — and the way to get it wrong is to compare something that legitimately
differs. A duration differs because a runner is shared. A builder-local image ID
differs because a storage driver assigns it. Neither is drift, and reporting
either as a disagreement would bury the ones that are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from interop_lab.benchmark.model import MeasurementState
from interop_lab.benchmark.reproducibility import ReproducibilityComparison, compare


def write(directory: Path, name: str, payload: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def environment(
    *, identity: str = "env", driver: str = "overlay2", docker: str = "29.3.1"
) -> dict[str, Any]:
    return {
        "os": "Linux",
        "machine": "x86_64",
        "python": "3.12.13",
        "runner_identity": identity,
        "docker_engine_version": docker,
        "storage_driver": driver,
        "architecture": "x86_64",
    }


def populate(
    directory: Path,
    *,
    env: dict[str, Any] | None = None,
    target_fingerprint: str = "tf-1",
    portable: str = "pf-1",
    scenario_fingerprint: str = "sf-1",
    capability_fingerprint: str = "cf-1",
    classification: str = "PASS",
    digest: str = "d-1",
    duration_ms: float = 10.0,
) -> Path:
    write(directory, "environment.json", env or environment())
    write(
        directory,
        "target-matrix.json",
        [
            {
                "target_key": "t",
                "target_fingerprint": target_fingerprint,
                "portable_identity_fingerprint": portable,
            }
        ],
    )
    write(
        directory,
        "scenario-matrix.json",
        [{"scenario_id": "s", "scenario_fingerprint": scenario_fingerprint}],
    )
    write(
        directory,
        "capability-results.json",
        [{"target_key": "t", "capability_fingerprint": capability_fingerprint}],
    )
    write(
        directory,
        "classification-results.json",
        [
            {
                "target_key": "t",
                "scenario_id": "s",
                "repetition": 1,
                "classification": classification,
                # Present in the artifact and deliberately never compared.
                "duration_ms": duration_ms,
            }
        ],
    )
    write(
        directory,
        "result-signatures.json",
        [{"target_key": "t", "scenario_id": "s", "repetition": 1, "digest": digest}],
    )
    return directory


# ------------------------------------------------------------------ agreement


def test_two_identical_executions_agree(tmp_path: Path) -> None:
    comparison = compare(
        populate(tmp_path / "a", env=environment(identity="a")),
        populate(tmp_path / "b", env=environment(identity="b")),
    )

    assert comparison.agrees
    assert comparison.disagreements == []
    assert comparison.state is MeasurementState.MEASURED
    assert comparison.environment_a.identity == "a"
    assert comparison.environment_b.identity == "b"


def test_a_different_duration_is_not_a_disagreement(tmp_path: Path) -> None:
    """A shared runner's timings vary for reasons that are not the code."""
    comparison = compare(
        populate(tmp_path / "a", duration_ms=10.0),
        populate(tmp_path / "b", duration_ms=9_000.0),
    )

    assert comparison.agrees


def test_a_different_storage_driver_is_an_expected_difference(tmp_path: Path) -> None:
    """Identical bits get different image IDs under different drivers."""
    comparison = compare(
        populate(tmp_path / "a", env=environment(driver="overlay2")),
        populate(tmp_path / "b", env=environment(driver="overlayfs")),
    )

    assert comparison.agrees
    assert any("storage driver differs" in entry for entry in comparison.expected_differences)
    assert any("not compared" in entry for entry in comparison.expected_differences)


def test_a_different_docker_version_alone_is_not_a_disagreement(tmp_path: Path) -> None:
    comparison = compare(
        populate(tmp_path / "a", env=environment(docker="29.3.1")),
        populate(tmp_path / "b", env=environment(docker="28.0.0")),
    )

    assert comparison.agrees


# --------------------------------------------------------------- disagreement


def test_a_moved_target_fingerprint_is_a_disagreement(tmp_path: Path) -> None:
    comparison = compare(
        populate(tmp_path / "a", target_fingerprint="tf-1"),
        populate(tmp_path / "b", target_fingerprint="tf-2"),
    )

    assert not comparison.agrees
    assert comparison.target_fingerprints_agree is False
    assert any("fingerprint tf-1 vs tf-2" in entry for entry in comparison.disagreements)


def test_a_moved_portable_build_identity_is_named_as_real_drift(tmp_path: Path) -> None:
    """These are hashes of committed files, so a difference is a genuine change."""
    comparison = compare(
        populate(tmp_path / "a", portable="pf-1"),
        populate(tmp_path / "b", portable="pf-2"),
    )

    assert comparison.portable_build_identities_agree is False
    assert any("This one *is* drift" in entry for entry in comparison.disagreements)


def test_a_moved_capability_surface_says_the_server_advertised_differently(
    tmp_path: Path,
) -> None:
    comparison = compare(
        populate(tmp_path / "a", capability_fingerprint="cf-1"),
        populate(tmp_path / "b", capability_fingerprint="cf-2"),
    )

    assert comparison.capability_fingerprints_agree is False
    assert any("advertised a different surface" in entry for entry in comparison.disagreements)


def test_a_different_classification_is_a_disagreement(tmp_path: Path) -> None:
    comparison = compare(
        populate(tmp_path / "a", classification="PASS"),
        populate(tmp_path / "b", classification="SPEC_VIOLATION"),
    )

    assert comparison.classifications_agree is False
    assert any("PASS vs SPEC_VIOLATION" in entry for entry in comparison.disagreements)


def test_an_agreeing_classification_with_a_different_signature_is_a_disagreement(
    tmp_path: Path,
) -> None:
    """The whole point of signatures, carried across environments."""
    comparison = compare(
        populate(tmp_path / "a", digest="d-1"),
        populate(tmp_path / "b", digest="d-2"),
    )

    assert comparison.classifications_agree is True
    assert comparison.result_signatures_agree is False
    assert any("result signature digest differs" in entry for entry in comparison.disagreements)


def test_measuring_different_sets_is_a_disagreement(tmp_path: Path) -> None:
    left = populate(tmp_path / "a")
    right = populate(tmp_path / "b")
    write(
        right,
        "result-signatures.json",
        [{"target_key": "t", "scenario_id": "other", "repetition": 1, "digest": "d-1"}],
    )

    comparison = compare(left, right)

    assert comparison.result_signatures_agree is False
    assert any("did not measure the same set" in entry for entry in comparison.disagreements)


def test_a_target_present_in_only_one_environment_is_a_disagreement(tmp_path: Path) -> None:
    left = populate(tmp_path / "a")
    right = populate(tmp_path / "b")
    write(right, "target-matrix.json", [])

    comparison = compare(left, right)

    assert any("present in only one environment" in entry for entry in comparison.disagreements)


# ------------------------------------------------------------------ mechanics


def test_a_missing_artifact_is_refused_rather_than_treated_as_empty(tmp_path: Path) -> None:
    left = populate(tmp_path / "a")
    right = populate(tmp_path / "b")
    (right / "capability-results.json").unlink()

    with pytest.raises(FileNotFoundError, match=r"capability-results\.json"):
        compare(left, right)


def test_the_comparison_states_that_timings_were_not_compared(tmp_path: Path) -> None:
    comparison = compare(populate(tmp_path / "a"), populate(tmp_path / "b"))

    joined = " ".join(comparison.limitations)
    assert "not compared across machines" in joined
    assert "not target drift" in joined
    assert "establishes nothing about a third" in joined


def test_the_comparison_round_trips_through_its_own_model(tmp_path: Path) -> None:
    comparison = compare(populate(tmp_path / "a"), populate(tmp_path / "b"))

    restored = ReproducibilityComparison.model_validate(comparison.model_dump(mode="json"))
    assert restored == comparison
