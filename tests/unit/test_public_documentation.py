"""What the exported documentation may and may not say.

Two failures are pinned here, both of which reached a release candidate.

The public README described a system that no longer existed — a scenario count
three checkpoints old, a benchmark and a viewer it claimed not to have — and, in
the course of being honest about coverage, published how many third-party
findings existed and that they concentrated on one target. Finding *existence*
is private evidence. A count and a concentration are most of a disclosure.

The exported benchmark document said "Nothing in this directory may be
published" in a file the export publishes, and linked to four private files that
are not exported, so every one of those links was dead in the public tree.

Neither defect is the kind a reader reports. Both are the kind a test catches.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from interop_lab.export.manifest import classify

#: Exported Markdown whose audience is a stranger reading the public repository.
PUBLIC_DOCUMENTS = (
    "README.md",
    "benchmarks/README.md",
    "viewer/README.md",
    "demos/README.md",
    "SECURITY.md",
)

#: Exact phrases that were true once and are not true now. Each is pinned as a
#: literal rather than a pattern: the point is to fail on the sentence that was
#: actually shipped, not to police every sentence shaped like it.
STALE_PHRASES = (
    "18 scenarios",
    "17 scenarios",
    "11 stdio, 7 HTTP",
    "11 stdio, 7 Streamable",
    "2 applicable scenarios",
    "2 of 18",
    "9 of the 18",
    "No benchmark.",
    "No static HTML report",
    "next checkpoint expands the scenario set",
    "Nothing in this directory may be published",
)

#: Private evidence. A count of findings, or where they landed, is a disclosure
#: written in fewer words — re-identification does not need the target's name.
PRIVATE_FINDING_METADATA = (
    "two stable findings",
    "Two stable findings",
    "both against the same target",
    "both pre-existing",
    "Three external implementations have been tested",
    "three pinned third-party implementations",
    "independent Go stdio",
    "independent Go implementation",
    "Every external implementation examined speaks",
)


def exported_documents() -> list[str]:
    return [path for path in PUBLIC_DOCUMENTS if classify(path).included]


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# ------------------------------------------------------------ stale content


@pytest.mark.parametrize("path", PUBLIC_DOCUMENTS)
@pytest.mark.parametrize("phrase", STALE_PHRASES)
def test_no_exported_document_repeats_a_stale_claim(path: str, phrase: str) -> None:
    if not classify(path).included:  # pragma: no cover - every one is exported today
        pytest.skip(f"{path} is not exported")
    assert phrase not in read(path), f"{path} still says {phrase!r}"


@pytest.mark.parametrize("path", PUBLIC_DOCUMENTS)
@pytest.mark.parametrize("phrase", PRIVATE_FINDING_METADATA)
def test_no_exported_document_discloses_private_finding_metadata(path: str, phrase: str) -> None:
    if not classify(path).included:  # pragma: no cover - every one is exported today
        pytest.skip(f"{path} is not exported")
    assert phrase not in read(path), (
        f"{path} discloses private finding metadata: {phrase!r}. Finding existence, "
        "count, concentration and affected target are all private evidence."
    )


def test_the_readme_states_the_current_scenario_count() -> None:
    """Not merely "not stale" — actually current, and matching the registry."""
    from interop_lab.scenarios.library import ALL_SCENARIOS

    readme = read("README.md")
    assert f"{len(ALL_SCENARIOS)} deterministic" in readme, (
        f"the README does not state the registry's actual size ({len(ALL_SCENARIOS)})"
    )


def test_the_readme_states_the_public_identity() -> None:
    readme = read("README.md")
    for identifier in ("MCP Failure Lab", "mcp-failure-lab", "interop_lab"):
        assert identifier in readme, f"the README never names {identifier}"


def test_the_readme_says_the_public_evidence_is_synthetic() -> None:
    """And does not imply it is anonymised private evidence."""
    readme = read("README.md")
    assert "synthetic" in readme.lower()
    assert "not anonymised" in readme.lower() or "not anonymized" in readme.lower(), (
        "the README must say the synthetic evidence is not redacted private evidence"
    )


def test_the_readme_has_both_proof_boundary_sections() -> None:
    readme = read("README.md")
    assert "## What this public distribution proves" in readme
    assert "## What it does not prove" in readme


def test_the_readme_claims_no_certification_or_endorsement() -> None:
    readme = read("README.md")
    lowered = readme.lower()
    for overclaim in ("certified", "guarantees correctness", "officially endorsed"):
        assert overclaim not in lowered, f"the README claims {overclaim!r}"


def test_the_benchmark_document_does_not_forbid_its_own_publication() -> None:
    """It ships in the export. A file that says it may not be published is a
    contradiction a reader has to resolve, and they will resolve it wrongly."""
    body = read("benchmarks/README.md")
    assert "Nothing in this directory may be published" not in body
    assert classify("benchmarks/README.md").included is True


def test_the_benchmark_document_keeps_the_vocabulary_distinct() -> None:
    body = read("benchmarks/README.md")
    for term in ("NOT_APPLICABLE", "NOT_MEASURED", "INCONCLUSIVE"):
        assert term in body, f"the benchmark document does not distinguish {term}"
    assert "no overall ranking" in body.lower()


def test_the_benchmark_document_keeps_disclosure_required() -> None:
    body = read("benchmarks/README.md")
    assert "responsible-disclosure.md" in body


# -------------------------------------------------------------- dead links

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def relative_links(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in LINK.finditer(line):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            found.append((number, target))
    return found


@pytest.mark.parametrize("document", PUBLIC_DOCUMENTS)
def test_every_relative_link_in_an_exported_document_resolves(document: str) -> None:
    """Inside the source tree first. The export gate re-checks the materialised
    tree, but a link that is dead here is dead there and cheaper to find here."""
    if not classify(document).included:  # pragma: no cover
        pytest.skip(f"{document} is not exported")

    source = Path(document)
    if not source.is_file():  # pragma: no cover - present in both trees today
        pytest.skip(f"{document} is not in this tree")

    dead = [
        f"{document}:{number}: {target}"
        for number, target in relative_links(source)
        if not (source.parent / target.split("#", 1)[0]).exists()
    ]
    assert not dead, f"dead relative link(s): {dead}"


@pytest.mark.parametrize("document", PUBLIC_DOCUMENTS)
def test_no_exported_document_links_to_a_path_the_export_withholds(document: str) -> None:
    """A link that resolves here and not in the export is the worse failure: it
    passes review in the source tree and 404s for every public reader."""
    if not classify(document).included:  # pragma: no cover
        pytest.skip(f"{document} is not exported")

    source = Path(document)
    if not source.is_file():  # pragma: no cover
        pytest.skip(f"{document} is not in this tree")

    withheld: list[str] = []
    for number, target in relative_links(source):
        # `benchmarks/../README.md` and `README.md` are the same file, and only
        # one of them matches an allowlist rule. Normalise before classifying.
        resolved = Path(os.path.normpath(source.parent / target.split("#", 1)[0]))
        if resolved.is_dir():
            continue
        if not classify(resolved.as_posix()).included:
            withheld.append(f"{document}:{number}: {target} -> {resolved.as_posix()}")

    assert not withheld, f"link(s) to paths the export withholds: {withheld}"
