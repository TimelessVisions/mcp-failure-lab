"""The viewer dataset must be a translation of the evidence, never a second opinion.

Two things are being defended here. The first is that a directory which does not
describe itself completely cannot become a dataset: a mutated artifact, an
unrecorded file, a manifest recording execution problems, a scenario library that
moved since the run. The second is that the four states stay four: PASS,
NOT_APPLICABLE, NOT_MEASURED and INCONCLUSIVE answer different questions, and a
dataset that collapsed any two of them would be wrong in a way no screenshot
review would catch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interop_lab.viewer.export import ViewerExportError, build_dataset
from interop_lab.viewer.fixture import write_fixture
from interop_lab.viewer.model import CellState, ViewerDataset, VisibilityMode
from interop_lab.viewer.source import BenchmarkSourceError, load_source

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def benchmark_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_fixture(tmp_path_factory.mktemp("benchmark"), repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def dataset(benchmark_output: Path) -> ViewerDataset:
    return build_dataset(load_source(benchmark_output), repo_root=REPO_ROOT)


def test_a_mutated_artifact_refuses_the_export(tmp_path: Path) -> None:
    output = write_fixture(tmp_path / "benchmark", repo_root=REPO_ROOT)
    timings = output / "timings.json"
    timings.write_text(timings.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(BenchmarkSourceError) as caught:
        load_source(output)
    assert any("hashes to" in problem for problem in caught.value.problems)


def test_a_file_the_manifest_does_not_name_refuses_the_export(tmp_path: Path) -> None:
    """An artifact nobody recorded cannot be attributed, so it is refused.

    The failure this prevents is a directory assembled by hand or half-copied,
    which looks exactly like a good one to a loader that reads only what it needs.
    """
    output = write_fixture(tmp_path / "benchmark", repo_root=REPO_ROOT)
    (output / "stray.json").write_text("{}", encoding="utf-8")

    with pytest.raises(BenchmarkSourceError) as caught:
        load_source(output)
    assert any("absent from the manifest" in problem for problem in caught.value.problems)


def test_a_manifest_recording_problems_refuses_the_export(tmp_path: Path) -> None:
    output = write_fixture(tmp_path / "benchmark", repo_root=REPO_ROOT)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["problems"] = ["a repetition produced no usable report"]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(BenchmarkSourceError) as caught:
        load_source(output)
    assert any("execution problem" in problem for problem in caught.value.problems)


def test_a_missing_required_artifact_refuses_the_export(tmp_path: Path) -> None:
    output = write_fixture(tmp_path / "benchmark", repo_root=REPO_ROOT)
    (output / "timings.json").unlink()
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].pop("timings.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(BenchmarkSourceError) as caught:
        load_source(output)
    assert any("timings.json" in problem for problem in caught.value.problems)


def test_a_scenario_that_moved_since_the_run_refuses_the_export(tmp_path: Path) -> None:
    """Describing one scenario's results with another's text is the failure here.

    The benchmark pins fingerprints, not prose, so the description shown beside a
    result comes from the live library. That is only sound while the library
    still describes what ran, and the export must refuse rather than mislabel.
    """
    output = write_fixture(tmp_path / "benchmark", repo_root=REPO_ROOT)
    source = load_source(output)
    source.definition.scenarios[0].scenario_fingerprint = "0000000000000000"

    with pytest.raises(ViewerExportError) as caught:
        build_dataset(source, repo_root=REPO_ROOT)
    assert any("changed after the run" in problem for problem in caught.value.problems)


def test_the_dataset_carries_the_manifest_digest_it_was_built_from(
    dataset: ViewerDataset, benchmark_output: Path
) -> None:
    assert dataset.generated_from_benchmark_manifest_hash
    assert (
        dataset.generated_from_benchmark_manifest_hash
        == load_source(benchmark_output).manifest_digest
    )


def test_the_four_states_stay_four(dataset: ViewerDataset) -> None:
    states = {cell.state for cell in dataset.matrix}
    assert CellState.PASS in states
    assert CellState.NOT_APPLICABLE in states
    assert CellState.INCONCLUSIVE in states


def test_not_applicable_never_enters_a_denominator(dataset: ViewerDataset) -> None:
    for cell in dataset.matrix:
        if cell.state is CellState.NOT_APPLICABLE:
            assert not cell.applicable
        if cell.state is CellState.NOT_MEASURED:
            assert not cell.applicable


def test_every_cell_carries_its_own_denominator_and_limitations(dataset: ViewerDataset) -> None:
    for cell in dataset.matrix:
        assert cell.limitations, f"{cell.target_key}/{cell.scenario_id} states no limitation"
        assert cell.applicable_denominator >= 0
        if cell.applicable:
            assert cell.applicable_denominator > 0


def test_the_denominator_matches_the_category_observation(dataset: ViewerDataset) -> None:
    """The cell's denominator is the category's, carried rather than recomputed."""
    by_key = {
        (summary.target_key, summary.category): summary.applicable_scenarios
        for summary in dataset.categories
    }
    for cell in dataset.matrix:
        expected = by_key.get((cell.target_key, cell.category))
        if expected is not None:
            assert cell.applicable_denominator == expected


def test_overview_totals_agree_with_the_category_observations(dataset: ViewerDataset) -> None:
    assert dataset.overview.applicable_results == sum(
        summary.applicable_scenarios for summary in dataset.categories
    )
    assert dataset.overview.stable_pass == sum(
        summary.stable_pass for summary in dataset.categories
    )


def test_the_overview_holds_no_field_that_combines_the_others(dataset: ViewerDataset) -> None:
    """There is no score, and there must be no field that could become one."""
    forbidden = {"score", "rank", "rating", "grade", "overall", "total_score", "best"}
    assert not forbidden & set(dataset.overview.model_dump().keys())


def test_a_scenario_whose_repetitions_disagreed_is_inconclusive_not_a_majority(
    dataset: ViewerDataset,
) -> None:
    unstable = [
        cell
        for cell in dataset.matrix
        if cell.state is CellState.INCONCLUSIVE and len(cell.signature_digests) > 1
    ]
    assert unstable, "the fixture must exercise a genuinely unstable scenario"
    for cell in unstable:
        assert cell.signature_stable is False


def test_signature_stability_is_signature_equality_not_a_repeated_label(
    dataset: ViewerDataset,
) -> None:
    for cell in dataset.matrix:
        if cell.signature_stable is True:
            assert len(cell.signature_digests) == 1
        if cell.signature_stable is False:
            assert len(cell.signature_digests) > 1


def test_security_relevance_travels_beside_the_classification_not_instead_of_it(
    dataset: ViewerDataset,
) -> None:
    relevant = [cell for cell in dataset.matrix if cell.security_relevant]
    assert relevant, "the fixture must exercise a security-relevant result"
    for cell in relevant:
        # The primary outcome is preserved. A security-relevant result is not
        # relabelled as SECURITY_RELEVANT, which is precisely the collapse the
        # dimension split exists to prevent.
        assert cell.state is not CellState.SECURITY_RELEVANT
        assert cell.vulnerability_status in {"unconfirmed", "confirmed_private", "not_assessed"}


def test_no_confirmed_vulnerability_is_produced_by_an_execution_path(
    dataset: ViewerDataset,
) -> None:
    assert dataset.overview.confirmed_private_vulnerabilities == 0


def test_every_promised_metric_reaches_the_dataset(dataset: ViewerDataset) -> None:
    from interop_lab.benchmark.coverage import REQUIRED_METRIC_IDS

    covered = {entry.metric_id for entry in dataset.measurement_coverage.entries}
    assert covered == set(REQUIRED_METRIC_IDS)


def test_every_unmeasured_metric_still_gets_a_timing_card(dataset: ViewerDataset) -> None:
    """A timings screen that showed only what exists invites the wrong conclusion."""
    unmeasured = {
        entry.metric_id
        for entry in dataset.measurement_coverage.entries
        if entry.state.value == "not_measured"
    }
    carded = {card.metric_id for card in dataset.timings if card.state == "not_measured"}
    assert unmeasured <= carded
    for card in dataset.timings:
        if card.state == "not_measured":
            assert card.insufficient_reason


def test_no_timing_card_quotes_a_median_without_its_samples(dataset: ViewerDataset) -> None:
    for card in dataset.timings:
        if card.median_ms is not None:
            assert card.samples_ms
            assert card.min_ms is not None and card.max_ms is not None


def test_containment_keeps_requested_and_observed_apart(dataset: ViewerDataset) -> None:
    assert dataset.containment
    for evidence in dataset.containment:
        for fact in evidence.facts:
            # A merged value would be the one that hides a restriction that was
            # asked for and not applied.
            assert not (fact.requested is None and fact.observed is None and fact.agrees)


def test_the_capability_surface_keeps_declared_and_observed_apart(dataset: ViewerDataset) -> None:
    for surface in dataset.capabilities:
        assert surface.tools or surface.status != "resolved"
        assert isinstance(surface.declared_tools, list)


def test_methodology_and_limitations_are_first_class(dataset: ViewerDataset) -> None:
    sections = {section.section_id for section in dataset.methodology}
    assert {
        "denominator",
        "no-ranking",
        "security-dimension",
        "result-signature",
        "timing",
        "containment",
        "official-conformance",
        "cross-environment",
        "privacy",
    } <= sections
    assert len(dataset.limitations) >= 5


def test_a_private_dataset_is_never_marked_publishable(dataset: ViewerDataset) -> None:
    assert dataset.visibility_mode is VisibilityMode.PRIVATE
    assert dataset.disclosure_controls.publishable is False
