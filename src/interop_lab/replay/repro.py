"""Reproduction packages and replay.

A reproduction is a self-contained directory: the exact scenario, the exact
resolved target, the seed, the captured evidence, and a one-command rerun. It
must not depend on the YAML that produced the original run.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from interop_lab.events.model import WireTrace
from interop_lab.models.core import Classification, RunResult, TargetDefinition
from interop_lab.naming import ARTIFACT_SCHEMA_VERSION, CLI_COMMAND, HARNESS_VERSION
from interop_lab.normalization.normalize import normalize_trace
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.runner.images import ExecutionContext
from interop_lab.scenarios.model import Scenario

#: Portable wrappers. Both resolve their own location, quote for paths with
#: spaces, and contain no absolute path from the generating machine.
BASH_WRAPPER = """#!/usr/bin/env bash
# Replay this reproduction. Portable: resolves its own directory, so the package
# can be moved anywhere and still runs.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
uv run mcp-failure-lab replay "$SCRIPT_DIR/repro.yaml"
"""

POWERSHELL_WRAPPER = """\
# Replay this reproduction. Portable: resolves its own directory, so the
# package can be moved anywhere and still runs. Requires PowerShell 7+.
$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
uv run mcp-failure-lab replay (Join-Path $ScriptDir "repro.yaml")
"""


class ReplayOutcome(StrEnum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    TARGET_UNAVAILABLE = "target_unavailable"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    HARNESS_ERROR = "harness_error"
    #: The package describes a locally built target that has not been built
    #: here, or whose build no longer matches what the package records. Kept
    #: distinct from HARNESS_ERROR because the remedy is specific and known:
    #: rebuild, then replay against the resulting resolution.
    BUILD_REQUIRED = "build_required"


@dataclass
class ReplayResult:
    outcome: ReplayOutcome
    expected: Classification
    observed: Classification
    detail: str
    run_id: str


def environment_fingerprint() -> dict[str, Any]:
    import platform
    import sys

    return {
        "harness_version": HARNESS_VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def write_repro(
    root: Path,
    *,
    finding_id: str,
    scenario: Scenario,
    target: TargetDefinition,
    run: RunResult,
    trace: WireTrace,
    seed: int,
    minimization: dict[str, Any] | None = None,
) -> Path:
    """Write a self-contained reproduction package and return its directory."""
    repro_dir = root / "repros" / finding_id
    repro_dir.mkdir(parents=True, exist_ok=True)

    repro: dict[str, Any] = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "finding_id": finding_id,
        "harness_version": HARNESS_VERSION,
        "expected_classification": run.classification.value,
        "seed": seed,
        "scenario": scenario.model_dump(mode="json", exclude_none=True),
        "target": target.model_dump(mode="json", exclude_none=True),
    }

    # A locally built target carries no image ID in its definition, by design.
    # What the package needs to say is that the replay machine must build it —
    # and, separately, what the original machine's build happened to produce.
    # The second is historical evidence and is deliberately not a precondition:
    # requiring it would make the package unusable everywhere except the one
    # machine that produced it, which is the opposite of a reproduction.
    build = _build_requirement(target, run)
    if build is not None:
        repro["build_requirement"] = build
    (repro_dir / "repro.yaml").write_text(
        yaml.safe_dump(repro, sort_keys=False, width=100), encoding="utf-8"
    )

    (repro_dir / "wire.jsonl").write_text(trace.to_jsonl(), encoding="utf-8")
    (repro_dir / "normalized-wire.jsonl").write_text(
        normalize_trace(trace).to_jsonl(), encoding="utf-8"
    )
    (repro_dir / "target.json").write_text(
        target.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    (repro_dir / "scenario.json").write_text(
        json.dumps(scenario.model_dump(mode="json", exclude_none=True), indent=2),
        encoding="utf-8",
    )
    (repro_dir / "environment.json").write_text(
        json.dumps(environment_fingerprint(), indent=2), encoding="utf-8"
    )
    (repro_dir / "seed.txt").write_text(f"{seed}\n", encoding="utf-8")

    expected: dict[str, Any] = {
        "classification": run.classification.value,
        "assertions": [
            {
                "assertion_id": a.assertion_id,
                "status": a.status.value,
                "expected": a.expected,
                "normative_source": a.normative_source,
            }
            for a in run.assertions
        ],
    }
    (repro_dir / "expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")

    actual: dict[str, Any] = {
        "classification": run.classification.value,
        "timeout_cause": run.timeout_cause.value if run.timeout_cause else None,
        "assertions": [
            {"assertion_id": a.assertion_id, "status": a.status.value, "actual": a.actual}
            for a in run.assertions
        ],
        "process": run.process.model_dump(mode="json"),
    }
    (repro_dir / "actual.json").write_text(json.dumps(actual, indent=2), encoding="utf-8")

    if minimization is not None:
        (repro_dir / "minimization.json").write_text(
            json.dumps(minimization, indent=2), encoding="utf-8"
        )

    # The wrappers resolve their own directory. Baking in the generating
    # machine's absolute path makes the package unusable anywhere else, which is
    # the whole point of shipping it.
    sh = repro_dir / "run.sh"
    sh.write_text(BASH_WRAPPER, encoding="utf-8")
    sh.chmod(sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (repro_dir / "run.ps1").write_text(POWERSHELL_WRAPPER, encoding="utf-8")

    command = f"{CLI_COMMAND} replay <package>/repro.yaml"

    (repro_dir / "README.md").write_text(
        _repro_readme(finding_id, scenario, target, run, command, minimization), encoding="utf-8"
    )
    return repro_dir


def _repro_readme(
    finding_id: str,
    scenario: Scenario,
    target: TargetDefinition,
    run: RunResult,
    command: str,
    minimization: dict[str, Any] | None,
) -> str:
    normative = scenario.definition.normative_source or "no normative citation"
    minimized = ""
    if minimization is not None:
        verdict = (
            "**Already minimal.** Nothing could be removed without losing the failure "
            "signature, so no reduction is claimed."
            if minimization["already_minimal"]
            else f"**Reduced** via {', '.join(minimization['stages_applied'])}."
            if minimization["reduced"]
            else "No reduction achieved."
        )
        minimized = (
            f"\n## Minimisation\n\n"
            f"{verdict}\n\n"
            f"| Metric | Original | Minimised |\n|---|---:|---:|\n"
            f"| events | {minimization['original_event_count']} | "
            f"{minimization['minimized_event_count']} |\n"
            f"| body bytes | {minimization['original_body_bytes']} | "
            f"{minimization['minimized_body_bytes']} |\n"
            f"| header-override bytes | {minimization['original_header_override_bytes']} | "
            f"{minimization['minimized_header_override_bytes']} |\n"
            f"| mutation surface bytes | {minimization['original_mutation_surface_bytes']} | "
            f"{minimization['minimized_mutation_surface_bytes']} |\n\n"
            f"- candidate runs: {minimization['candidate_runs']}\n"
            f"- preserved failing assertions: "
            f"{minimization['preserved_failing_assertions']}\n"
            f"- stable across {minimization['stability_runs']} consecutive runs: "
            f"{minimization['stable']}\n"
        )

    return f"""# Reproduction {finding_id}

**Classification:** {run.classification.value}
**Scenario:** `{scenario.scenario_id}` v{scenario.definition.version}
**Target:** `{target.target_id}` ({target.implementation} {target.version})
**Protocol profile:** {run.protocol_profile} ({run.spec_version})
**Transport:** {run.transport.value}
**Seed:** {run.seed}

## Reproduce

```bash
{command}
```

## What was expected

{scenario.definition.expected_outcome or "see scenario.json"}

Normative basis: {normative}

## What was observed

See `actual.json`. Raw wire traffic is in `wire.jsonl`; the normalised view used
for comparison is in `normalized-wire.jsonl`.
{minimized}
## Limitations

This reproduction is valid for the pinned target version, transport and protocol
profile recorded in `target.json`. It is not a general claim about the
implementation, and a target marked as a local fixture is a synthetic defect
created for testing, not a third-party defect.
"""


def _build_requirement(target: TargetDefinition, run: RunResult) -> dict[str, Any] | None:
    """What a replay machine has to do before this package can run at all."""
    spec = target.container
    source = spec.local_build_source if spec is not None else None
    if source is None:
        return None

    containment = run.containment
    resolved = containment.resolved_image if containment is not None else None
    return {
        "kind": "local_build",
        "build_key": source.build_key,
        "build_context": source.build_context,
        "dockerfile": source.dockerfile,
        "instruction": (
            "This target's image is built, not pulled. Build it on the replay "
            "machine with `mcp-failure-lab external-build`, then replay with "
            "`--resolution artifacts/external/build-resolution.json`. The image "
            "identity is resolved from that build and verified before the run."
        ),
        "portable_identity": (
            resolved.portable_identity.model_dump(mode="json")
            if resolved is not None and resolved.portable_identity is not None
            else None
        ),
        # Historical only. Recorded so a reader can compare two builders'
        # answers, never so a replay can require this machine's answer.
        "original_machine": {
            "note": (
                "The image ID below is what the recording machine's builder "
                "produced. It is not expected to exist anywhere else, and "
                "nothing in a replay requires it."
            ),
            "local_image_id": resolved.expected_local_image_id if resolved else None,
            "builder": resolved.builder if resolved else None,
            "storage_driver": resolved.storage_driver if resolved else None,
        },
    }


def load_repro(path: Path) -> tuple[Scenario, TargetDefinition, Classification, int]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")

    recorded = raw.get("artifact_schema_version")
    if recorded != ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            f"{path} is artifact schema {recorded!r}; this harness reads "
            f"{ARTIFACT_SCHEMA_VERSION!r}. A v1 package pins its container with a "
            "single image digest field that could hold either a registry digest or "
            "a builder-local image ID, and which one was never recorded — so it "
            "cannot be reinterpreted. Regenerate the package."
        )
    scenario = Scenario.model_validate(raw["scenario"])
    target = TargetDefinition.model_validate(raw["target"])
    expected = Classification(raw["expected_classification"])
    seed = int(raw.get("seed", 0))
    return scenario, target, expected, seed


async def replay(
    path: Path, *, artifacts_dir: Path, context: ExecutionContext | None = None
) -> ReplayResult:
    """Re-execute a reproduction package and report whether it reproduced.

    A registry-pinned target replays exactly as before: the digest in the
    package resolves the same bits on any machine with a network. A locally
    built target does not and must not — the package carries what it was built
    from, and this machine has to build it and resolve the result. Every way
    that can fail is reported as ``BUILD_REQUIRED`` with the specific reason:
    not built, inputs drifted, resolution stale, or the image that started not
    being the one resolved.
    """
    try:
        scenario, target, expected, seed = load_repro(path)
    except Exception as exc:
        return ReplayResult(
            ReplayOutcome.HARNESS_ERROR,
            Classification.INCONCLUSIVE,
            Classification.INCONCLUSIVE,
            f"could not load {path}: {exc}",
            "",
        )

    executor = ScenarioExecutor(artifacts_dir=artifacts_dir, seed=seed, context=context)
    result, _ = await executor.run(scenario, target, persist=True)

    if result.classification is Classification.HARNESS_ERROR:
        spec = target.container
        needs_build = spec is not None and spec.local_build_source is not None
        detail = result.harness_error or "harness error"
        if needs_build and _is_resolution_failure(detail):
            return ReplayResult(
                ReplayOutcome.BUILD_REQUIRED,
                expected,
                result.classification,
                detail,
                result.run_id,
            )
        return ReplayResult(
            ReplayOutcome.HARNESS_ERROR,
            expected,
            result.classification,
            detail,
            result.run_id,
        )
    if result.classification is Classification.NOT_APPLICABLE:
        return ReplayResult(
            ReplayOutcome.ENVIRONMENT_MISMATCH,
            expected,
            result.classification,
            result.not_applicable_reason or "scenario not applicable here",
            result.run_id,
        )
    if result.classification is expected:
        return ReplayResult(
            ReplayOutcome.REPRODUCED,
            expected,
            result.classification,
            f"classification {expected.value} reproduced",
            result.run_id,
        )
    return ReplayResult(
        ReplayOutcome.NOT_REPRODUCED,
        expected,
        result.classification,
        f"expected {expected.value}, observed {result.classification.value}",
        result.run_id,
    )


#: Substrings that identify a failure to establish the image, as opposed to any
#: other harness fault. Matched on the message because the resolution error is
#: converted to a containment record before it reaches here, and re-raising the
#: original exception type through the executor would mean every strategy had to
#: preserve it.
_RESOLUTION_MARKERS = (
    "build resolution",
    "build-resolution",
    "ImageResolutionError",
    "external-build",
)


def _is_resolution_failure(detail: str) -> bool:
    return any(marker in detail for marker in _RESOLUTION_MARKERS)
