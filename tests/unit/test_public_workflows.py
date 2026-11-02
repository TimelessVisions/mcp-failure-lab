"""The public repository's own CI, and the boundary that lets it out.

The sanitized export used to forbid `.github/workflows/**` wholesale. That was
right about the five private workflows and wrong about the consequence: the
public repository would have had no CI at all, so nothing would have checked
the thing that was actually published.

These tests pin both halves of the correction — the public workflows leave, the
private ones do not — and hold the public files to the property that makes them
safe to export: they name nothing private.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from interop_lab.export.manifest import classify

WORKFLOWS = Path(".github/workflows")

#: Written for the public repository and exported with it.
PUBLIC_WORKFLOWS = (
    ".github/workflows/public-ci.yml",
    ".github/workflows/public-viewer.yml",
    ".github/workflows/public-release-gate.yml",
)

#: Written for this repository. None of these may ever be exported.
PRIVATE_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/benchmark.yml",
    ".github/workflows/external-matrix.yml",
    ".github/workflows/viewer.yml",
    ".github/workflows/export-gate.yml",
)


def load(path: str) -> dict[object, object]:
    """Parse a workflow.

    The key is `dict[object, object]`, not `dict[str, object]`, because YAML 1.1
    resolves the bare word `on` to the boolean `True`. Every GitHub workflow
    therefore has a trigger block filed under `True` rather than `"on"`, and a
    reader who assumes otherwise gets a `KeyError` on a file that is perfectly
    valid.
    """
    loaded: object = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} is not a mapping"
    return loaded  # pyright: ignore[reportUnknownVariableType]


def triggers_of(path: str) -> dict[object, object]:
    workflow = load(path)
    triggers = workflow.get(True, workflow.get("on"))
    assert isinstance(triggers, dict), f"{path} has no trigger block"
    return triggers  # pyright: ignore[reportUnknownVariableType]


def text_of(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# ------------------------------------------------------- the export boundary


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_are_exported(path: str) -> None:
    decision = classify(path)
    assert decision.included is True, f"{path} would not reach the public repository"


@pytest.mark.parametrize("path", PRIVATE_WORKFLOWS)
def test_private_workflows_are_never_exported(path: str) -> None:
    decision = classify(path)
    assert decision.included is False, f"{path} would be published: {decision.reason}"


@pytest.mark.parametrize("path", PRIVATE_WORKFLOWS)
def test_private_workflows_are_forbidden_not_merely_unlisted(path: str) -> None:
    """Belt and braces: widening an allowlist rule must not publish these."""
    decision = classify(path)
    assert decision.reason.startswith("forbidden"), (
        f"{path} is excluded only because no rule matches it. A careless "
        "allowlist widening would publish a private branch trigger."
    )


def test_an_unknown_future_workflow_is_excluded_by_default() -> None:
    """The allowlist names files, not the directory.

    A workflow added later is excluded until someone reads it and adds it. The
    failure mode of forgetting is a public repository with one workflow fewer,
    which is visible and harmless.
    """
    decision = classify(".github/workflows/some-new-workflow.yml")
    assert decision.included is False
    assert "not on the allowlist" in decision.reason


def test_the_exported_repository_has_at_least_one_workflow() -> None:
    """The defect this whole file exists to prevent."""
    exported = [path for path in PUBLIC_WORKFLOWS if classify(path).included]
    assert exported, "the sanitized repository would ship with no CI at all"


def test_the_release_gate_support_script_is_exported_and_invoked_portably() -> None:
    """A fresh Windows-created Git root may not preserve executable mode bits."""
    path = "tools/export_gate.sh"
    assert Path(path).is_file(), f"{path} is referenced by CI but absent"
    decision = classify(path)
    assert decision.included is True, f"{path} would not reach the public repository"
    assert f"bash {path}" in text_of(".github/workflows/public-release-gate.yml")


def test_every_workflow_on_disk_is_classified_deliberately() -> None:
    """No workflow is left to chance.

    Every file in the directory is either named as public or named as private.
    A new one fails here until a human decides which it is — which is the point
    of the check, not an inconvenience it should route around.
    """
    if not WORKFLOWS.is_dir():  # pragma: no cover - not present in an export
        pytest.skip("no git repository here; this runs in the source tree, not in an export of it")

    on_disk = {path.as_posix() for path in WORKFLOWS.glob("*.yml")}
    accounted = set(PUBLIC_WORKFLOWS) | set(PRIVATE_WORKFLOWS)
    unclassified = on_disk - accounted
    assert not unclassified, (
        f"{sorted(unclassified)} is neither declared public nor declared private. "
        "Decide which it is and add it to the corresponding list."
    )


# ------------------------------------------------ what the public files say

#: Identities a public workflow may never carry.
#:
#: Naming a private *directory* is not on this list, and deliberately so: the
#: release gate has to assert those directories are absent, and a check that
#: cannot say what it is checking for is not a check. `tools/export_gate.sh`
#: already names them in the export for the same reason. What must not travel is
#: an identity — a branch, an artifact, a finding, a real target, a run.
PRIVATE_TERMS = (
    "agent/mcp-interop-foundation",
    "viewer-private-do-not-publish",
    "mcp-go-everything",
    "server-everything",
    "mcp-server-time",
    "private_artifact",
)

#: Work a public workflow must not attempt. Private execution needs private
#: input, so a public runner invoking any of these either fails or, worse,
#: succeeds against something it should not have had.
PRIVATE_EXECUTION = (
    "mcp-failure-lab external-build",
    "mcp-failure-lab external-matrix",
    "mcp-failure-lab benchmark run",
    "mcp-failure-lab benchmark plan",
    "--require-private",
    "download-artifact",
)


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_name_nothing_private(path: str) -> None:
    body = text_of(path)
    named = [term for term in PRIVATE_TERMS if term in body]
    assert not named, f"{path} names {named}"


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_run_no_private_execution(path: str) -> None:
    body = text_of(path)
    attempted = [term for term in PRIVATE_EXECUTION if term in body]
    assert not attempted, f"{path} would run private work: {attempted}"


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_name_no_private_finding_identifier(path: str) -> None:
    """`ILAB-0001` is a synthetic example. `ILAB-EXT-*` is a real finding."""
    import re

    body = text_of(path)
    assert not re.search(r"ILAB-EXT-\d+", body), f"{path} names a private finding"


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_name_no_private_branch(path: str) -> None:
    import re

    body = text_of(path)
    assert "agent/mcp-interop" not in body, f"{path} names the private development branch"
    assert not re.search(r"claude/[a-z0-9-]{6,}", body), f"{path} names a session branch"


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_name_no_workflow_run_identifier(path: str) -> None:
    import re

    body = text_of(path)
    assert not re.search(r"actions/runs/\d+", body), f"{path} names a private workflow run"


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_trigger_on_main(path: str) -> None:
    """A fresh public repository has one branch, and it is `main`."""
    triggers = triggers_of(path)

    branches: list[str] = []
    for event in ("push", "pull_request"):
        config = triggers.get(event)
        if not isinstance(config, dict):
            continue
        declared = config.get("branches")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(declared, list):
            branches.extend(str(branch) for branch in declared)  # pyright: ignore[reportUnknownVariableType]

    assert branches, f"{path} has no branch-scoped trigger"
    assert set(branches) == {"main"}, f"{path} triggers on {sorted(set(branches))}, not just main"


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_verify_stacked_pull_requests(path: str) -> None:
    """A pull request based on another pull request's branch is still verified.

    Filtering `pull_request` to `main` left stacked pull requests with no
    checks at all, which reads as nothing failing rather than nothing measured.
    """
    config = triggers_of(path).get("pull_request")
    if not isinstance(config, dict):
        return
    assert "branches" not in config, f"{path} skips pull requests not based on main"


@pytest.mark.parametrize("path", PUBLIC_WORKFLOWS)
def test_public_workflows_use_the_public_console_script(path: str) -> None:
    """The distribution and the command carry the public name."""
    body = text_of(path)
    assert "interop-lab " not in body, f"{path} uses an obsolete console script name"
    if "mcp-failure-lab" not in body:  # pragma: no cover - every one of them uses it today
        pytest.fail(f"{path} never invokes the public CLI")


def test_the_release_gate_publishes_nothing() -> None:
    """It decides whether the tree *could* be published, never that it is."""
    body = text_of(".github/workflows/public-release-gate.yml")
    for forbidden in ("softprops/action-gh-release", "gh release create", "twine", "npm publish"):
        assert forbidden not in body, f"the release gate would publish via {forbidden}"


def test_the_public_viewer_deploys_nothing() -> None:
    body = text_of(".github/workflows/public-viewer.yml")
    for forbidden in ("actions/deploy-pages", "peaceiris/actions-gh-pages", "gh-pages"):
        assert forbidden not in body, f"the public viewer workflow would deploy via {forbidden}"


@pytest.mark.parametrize("path", (".github/workflows/public-viewer.yml",))
def test_uploaded_viewer_artifact_is_the_synthetic_one(path: str) -> None:
    body = text_of(path)
    assert "viewer-public-safe-synthetic" in body
    assert "retention-days" in body, "an artifact with no retention limit outlives its usefulness"
