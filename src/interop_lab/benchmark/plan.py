"""Generating the pinned benchmark definition from the repository's own state.

The definition is a *committed artifact*, not something computed at run time. It
is generated here once, reviewed, and checked in — and then every execution
compares itself against it. That order matters: a definition regenerated at the
start of each run would agree with the run by construction and could never detect
that a target, a scenario or a build input had moved.

So this module is the only place that reads the live repository to produce
fingerprints, and running it is a deliberate act that shows up as a diff.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

from interop_lab.benchmark.model import (
    BenchmarkCategory,
    BenchmarkDefinition,
    ConformanceBoundary,
    PinnedScenario,
    PinnedTarget,
)
from interop_lab.ci.baseline import (
    requires_observed_surface,
    scenario_fingerprint,
    target_fingerprint,
)
from interop_lab.external.identity import derive_portable_identity
from interop_lab.external.images import load_images
from interop_lab.models.core import ScenarioCategory
from interop_lab.scenarios.library import ALL_SCENARIOS
from interop_lab.scenarios.model import Scenario

#: Which benchmark category each scenario category reports under.
#:
#: A mapping rather than a rename: the scenario taxonomy describes what a scenario
#: *does*, and the benchmark taxonomy describes what a reader is trying to learn.
#: Several scenario categories legitimately answer the same benchmark question —
#: schema drift and wire integrity are both about whether a malformed request is
#: handled — and forcing them to be the same enum would distort one of the two.
SCENARIO_CATEGORY_MAP: dict[ScenarioCategory, BenchmarkCategory] = {
    ScenarioCategory.BASELINE: BenchmarkCategory.VALID_BASELINE_BEHAVIOR,
    ScenarioCategory.WIRE_INTEGRITY: BenchmarkCategory.JSONRPC_REQUEST_INTEGRITY,
    ScenarioCategory.SCHEMA_DRIFT: BenchmarkCategory.JSONRPC_REQUEST_INTEGRITY,
    ScenarioCategory.VERSION_HEADER: BenchmarkCategory.STREAMABLE_HTTP_SEMANTICS,
    ScenarioCategory.HTTP_AUTHZ: BenchmarkCategory.SECURITY_RELEVANT_OBSERVATIONS,
    ScenarioCategory.HOSTILE_OUTPUT: BenchmarkCategory.SECURITY_RELEVANT_OBSERVATIONS,
    ScenarioCategory.STATEFUL_LIFECYCLE: BenchmarkCategory.STATEFUL_LIFECYCLE_BEHAVIOR,
    ScenarioCategory.STATELESS_LIFECYCLE: BenchmarkCategory.STATEFUL_LIFECYCLE_BEHAVIOR,
    ScenarioCategory.TIMING_CANCELLATION: BenchmarkCategory.TIMING_AND_RECOVERY,
    ScenarioCategory.PROCESS_RECOVERY: BenchmarkCategory.PROCESS_CONTAINMENT_AND_CLEANUP,
}

#: What Private 0.3 does not establish. Carried on the definition so it travels
#: with every output rather than living only in a document someone may not read.
DEFINITION_LIMITATIONS: tuple[str, ...] = (
    "No overall ranking is produced and none can be derived honestly. There is no "
    "best SDK here, no safest implementation, no most compliant target and no "
    "aggregate score.",
    "Thirty-two scenarios against three implementations. Absence of a finding is "
    "absence of a finding from this battery, never evidence of correctness.",
    "Three external targets, three languages, two transports. Nothing here "
    "generalises to implementations, versions or configurations not measured.",
    "Timing figures are not comparable across machines, and GitHub-hosted timing is "
    "subject to shared-runner variance. Correctness identities are comparable; "
    "durations are not.",
    "Raw speed is not a quality verdict, and a comparison of durations between "
    "implementations written in different languages measures the languages and the "
    "runtimes as much as anything else.",
    "Security-relevant observations are observations. A confirmed vulnerability is "
    "a separate, human-reached status that no execution path in this harness writes.",
)


def harness_commit(repo_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def benchmark_category_for(scenario: Scenario) -> BenchmarkCategory:
    """Where this scenario's results are reported.

    A scenario whose nature is security-relevant reports under the security
    category whatever its scenario category says, because that is the question its
    results answer. The two header/body-mismatch scenarios sit in
    ``VERSION_HEADER`` and are about an authorization split, and filing them under
    Streamable HTTP semantics would bury them.
    """
    from interop_lab.models.core import ScenarioNature

    if scenario.definition.nature is ScenarioNature.SECURITY_RELEVANT:
        return BenchmarkCategory.SECURITY_RELEVANT_OBSERVATIONS
    return SCENARIO_CATEGORY_MAP.get(
        scenario.definition.category, BenchmarkCategory.VALID_BASELINE_BEHAVIOR
    )


def pin_scenarios() -> list[PinnedScenario]:
    return [
        PinnedScenario(
            scenario_id=scenario.scenario_id,
            version=scenario.definition.version,
            category=benchmark_category_for(scenario),
            scenario_fingerprint=scenario_fingerprint(scenario),
            requires_observed_surface=requires_observed_surface(scenario.definition.applicability),
        )
        for scenario in sorted(ALL_SCENARIOS.values(), key=lambda s: s.scenario_id)
    ]


def pin_targets(repo_root: Path) -> list[PinnedTarget]:
    """Pin every external target by everything that decides what it is.

    Deliberately the *external* matrix only. The local fixtures are code this
    repository wrote and their results are tests of the harness rather than
    measurements of anyone's implementation; including them would inflate every
    denominator with scenarios whose outcome this project chose.
    """
    pinned: list[PinnedTarget] = []
    for image in sorted(load_images(repo_root), key=lambda i: i.key):
        target = image.target(repo_root)
        source = image.source(repo_root)
        supply = target.supply_chain
        portable = derive_portable_identity(target, repo_root=repo_root)

        dockerfile = repo_root / source.dockerfile
        lock_sha: str | None = None
        for lock_name in ("package-lock.json", "requirements.lock.txt", "go.sum"):
            candidate = repo_root / source.build_context / lock_name
            if candidate.is_file():
                lock_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
                break

        pinned.append(
            PinnedTarget(
                target_key=image.key,
                target_id=target.target_id,
                target_file=image.target_file,
                language=target.language,
                transport=target.transport.value,
                declared_profile=target.protocol_profile,
                relationship=target.relationship.value,
                source_version=target.version,
                source_commit=target.commit_sha,
                target_fingerprint=target_fingerprint(target),
                portable_identity_fingerprint=portable.fingerprint,
                build_context=str(source.build_context),
                dockerfile_sha256=(
                    hashlib.sha256(dockerfile.read_bytes()).hexdigest()
                    if dockerfile.is_file()
                    else None
                ),
                lock_sha256=lock_sha or (supply.lock_sha256 if supply else None),
                source_archive_sha256=supply.source_archive_sha256 if supply else None,
                # Contained conformance execution is not implemented here, and
                # publishing a host port to run it would weaken the containment
                # every other result in this benchmark rests on. NOT_MEASURED, with
                # the reason stated, rather than a gap a reader has to notice.
                official_conformance=ConformanceBoundary.NOT_MEASURED,
                official_conformance_detail=(
                    "The official conformance framework was not executed against this "
                    "target. Running it would require either publishing a host port or "
                    "building a contained conformance runner, and this checkpoint does "
                    "neither: weakening containment to obtain a conformance number "
                    "would invalidate every other result measured under it."
                ),
            )
        )
    return pinned


def build_definition(repo_root: Path) -> BenchmarkDefinition:
    """Assemble the pinned definition from the repository's current state."""
    targets = pin_targets(repo_root)
    scenarios = pin_scenarios()
    return BenchmarkDefinition(
        harness_commit=harness_commit(repo_root),
        targets=targets,
        scenarios=scenarios,
        profiles=sorted({target.declared_profile for target in targets}),
        transports=sorted({target.transport for target in targets}),
        expected_artifacts=[],
        limitations=list(DEFINITION_LIMITATIONS),
    )


def write_definition(definition: BenchmarkDefinition, path: Path) -> None:
    """Write the definition as YAML, deterministically.

    Sorted keys and block style: the file is reviewed as a diff, and a
    re-serialisation that reordered keys would make an unchanged definition look
    edited.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            definition.model_dump(mode="json"),
            sort_keys=True,
            default_flow_style=False,
            width=88,
        ),
        encoding="utf-8",
    )


def load_definition(path: Path) -> BenchmarkDefinition:
    """Read a pinned definition, rejecting anything this code does not understand."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return BenchmarkDefinition.model_validate(payload)
