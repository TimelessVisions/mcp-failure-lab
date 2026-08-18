"""The public-export boundary.

Every check here corresponds to a way private material could reach a public
tree. Two of them are defects that actually happened during development and are
now pinned so they cannot happen again.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from interop_lab.export.manifest import (
    ALLOWED,
    CONTENT_EXEMPT,
    FORBIDDEN,
    classify,
    plan,
    scan_content,
)


def in_git_repository() -> bool:
    """Whether `git ls-files` can answer here.

    The export deliberately produces a tree with no `.git`, and these tests are
    exported along with the code they cover. Inside that tree the tests cannot
    run — there is no file list to plan an export from — so they skip rather
    than fail. Skipping is right and dropping them would be wrong: the checks
    matter in the repository that has the private material, and that is exactly
    where this returns true.
    """
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError:  # pragma: no cover - git absent entirely
        return False


needs_git = pytest.mark.skipif(
    not in_git_repository(),
    reason="no git repository here; this runs in the source tree, not in an export of it",
)

#: Paths that must never appear in a public export, whatever else changes.
PRIVATE_PATHS = (
    "artifacts/private-findings/ILAB-EXT-0001/finding.json",
    "artifacts/private-findings/ILAB-EXT-0002/finding.json",
    "artifacts/private-findings/README.md",
    "benchmarks/private-results/benchmark-0.3.json",
    "docs/commercialization/founding-pilot.md",
    "docs/commercialization/ci-integration-sprint.md",
    "docs/commercialization/readiness.md",
    "docs/commercialization/private-http-sample-report.md",
    "research/external-target-selection.md",
    "artifacts/runs/some-run/wire.jsonl",
    "artifacts/reports/smoke.json",
    "artifacts/repros/ILAB-EXT-0002/repro.yaml",
    "targets/external/mcp-server-time/ca-bundle.crt",
    # Generated and gitignored, so the export cannot reach them today. Pinned
    # because the allowlist's `viewer/*` matches recursively: a dataset
    # committed here by accident would otherwise be approved by a rule written
    # for build configuration, and a private dataset is the worst single thing
    # that could leave this repository.
    "viewer/public/data/dataset.json",
    "viewer/dist/assets/index.js",
    # The private demo, including the prose describing it. A script that says
    # which real result appears at which timecode is a finding written down in
    # a different order.
    "demos/private/script.md",
    "demos/private/recording/private-standard-1920x1080.webm",
    # Every workflow names the private development branch in its trigger.
    ".github/workflows/benchmark.yml",
)


def tracked_paths() -> list[str]:
    listing = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [line for line in listing.stdout.splitlines() if line.strip()]


# ------------------------------------------------------ the allowlist shape


def test_exclusion_is_by_allowlist_not_denylist() -> None:
    """An unknown path is excluded, so forgetting produces a gap, not a leak."""
    decision = classify("some/brand/new/file.md")
    assert decision.included is False
    assert "not on the allowlist" in decision.reason


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_private_paths_are_never_exported(path: str) -> None:
    decision = classify(path)
    assert decision.included is False, f"{path} would be published: {decision.reason}"


def test_private_paths_are_forbidden_not_merely_unlisted() -> None:
    """Belt and braces: widening an allowlist rule must not publish these."""
    for path in PRIVATE_PATHS:
        decision = classify(path)
        assert decision.reason.startswith("forbidden"), (
            f"{path} is excluded only because no rule matches it. A careless "
            "allowlist widening would publish it."
        )


def test_every_rule_states_a_reason() -> None:
    for rule in (*ALLOWED, *FORBIDDEN, *CONTENT_EXEMPT):
        assert rule.reason.strip(), f"{rule.pattern} has no stated reason"


# ---------------------------------------------------------- content scanning


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see finding ILAB-EXT-0002 for detail", "finding identifier"),
        ("path = /home/user/mcp-failure-lab/x", "absolute developer path"),
        ("cd /tmp/claude-0/session/scratchpad", "scratch path"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
        ("AKIAIOSFODNN7EXAMPLE", "AWS access key"),
        ("ghp_" + "a" * 36, "GitHub token"),
        ("owner" + "@agentcommercegateway.com", "private contact email"),
        ('runtime_tag="interop-lab/mcp-server-time:2026.7.10"', "real target and version"),
        ("Captured from server-everything@2026.7.4", "real target and version"),
        ("mcp-go-everything, built at 1.4.2", "real target and version"),
    ],
)
def test_forbidden_content_is_detected(text: str, expected: str) -> None:
    violations = scan_content(Path("example.md"), text)
    assert violations, f"{expected} was not detected"


def test_ordinary_content_is_not_flagged() -> None:
    assert scan_content(Path("README.md"), "A harness for MCP interoperability.") == []


def test_a_real_target_name_alone_is_not_a_violation() -> None:
    """The pair is the disclosure, not either half.

    Naming public software is not a leak, and neither is a version number. What
    identifies a private measurement is the two together — which build this
    project actually ran — so the rule blocks the combination and leaves prose
    that merely mentions an implementation alone.
    """
    assert scan_content(Path("docs/architecture.md"), "Targets include server-everything.") == []
    assert scan_content(Path("docs/architecture.md"), "Pinned at 2026.7.4 by digest.") == []


def test_the_synthetic_replacements_are_not_flagged() -> None:
    """The public tests keep the shape and lose the identity."""
    for name in ("synthetic-python-stdio", "synthetic-typescript-http", "synthetic-go-stdio"):
        assert scan_content(Path("tests/conftest.py"), f'tag = "interop-lab/{name}:1.2.3"') == []


def test_content_exemptions_are_path_scoped() -> None:
    """A detector may contain the patterns. A real leak elsewhere still fails."""
    exempt = {rule.pattern for rule in CONTENT_EXEMPT}
    assert "src/interop_lab/artifacts/portability.py" in exempt

    # The same text in a file that is not exempt is still a violation.
    assert scan_content(Path("docs/architecture.md"), "/home/user/secret")


# --------------------------------------------------- the real repository


@needs_git
def test_the_real_repository_exports_cleanly() -> None:
    """The check that matters: this repo, as it stands, is safe to export."""
    result = plan(tracked_paths(), root=Path())
    assert result.content_violations == [], result.content_violations
    assert result.is_safe
    assert result.included, "an export with no files is not a useful proof"


@needs_git
def test_no_tracked_private_file_is_included() -> None:
    result = plan(tracked_paths(), root=Path())
    included = {decision.path for decision in result.included}
    for marker in (
        "artifacts/private-findings/",
        "docs/commercialization/",
        "benchmarks/private-results/",
        "research/external-target-selection.md",
    ):
        leaked = [path for path in included if path.startswith(marker)]
        assert not leaked, f"{marker} leaked into the export: {leaked}"


@needs_git
def test_the_manifest_counts_withheld_paths_rather_than_naming_them(tmp_path: Path) -> None:
    """A defect that happened: the manifest published what it withheld.

    The manifest ships inside the export. Listing excluded paths put the private
    finding identifiers into the very file meant to prove nothing private was
    published.
    """
    result = plan(tracked_paths(), root=Path())
    withheld: dict[str, int] = {}
    for decision in result.excluded:
        withheld[decision.reason] = withheld.get(decision.reason, 0) + 1

    manifest = {
        "summary": result.summary(),
        "included": [{"path": d.path, "reason": d.reason} for d in result.included],
        "withheld_by_reason": withheld,
    }
    serialised = json.dumps(manifest)
    assert "ILAB-EXT" not in serialised, "the manifest names a private finding"
    assert "private-findings" not in serialised
    assert "commercialization" not in serialised
    assert withheld, "the manifest must still say how much was withheld"
