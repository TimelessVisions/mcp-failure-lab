"""Deterministic regeneration of committed example artifacts.

Committed evidence must never be maintained by hand-editing JSON. It is
regenerated from local synthetic fixtures by one command, and the result must be
byte-identical when nothing in the implementation changed — otherwise a reviewer
cannot tell a real behaviour change from timestamp noise.

Determinism is achieved by scrubbing genuinely volatile fields (wall-clock
times, durations, run ids, ephemeral ports) from the committed copies. The
underlying run still records them; they are simply not part of the artifact a
reviewer diffs.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from interop_lab.artifacts.portability import validate_payload
from interop_lab.config.loader import load_target
from interop_lab.differential.compare import compare
from interop_lab.minimizer.reduce import Minimizer
from interop_lab.naming import ARTIFACT_SCHEMA_VERSION
from interop_lab.normalization.normalize import normalize_headers, normalize_text
from interop_lab.replay.repro import write_repro
from interop_lab.reporting.render import RunReport
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.scenarios.library import get_scenario

EXAMPLES_ROOT = Path("artifacts/examples")

#: Values replaced in committed copies so regeneration is byte-stable.
STABLE_TIMESTAMP = "<normalized>"

#: Replaced in committed copies. `raw_sha256` and `raw_len` are deliberately
#: absent: they are derived from payload content, are stable across runs, and
#: are what makes a committed trace verifiable.
_VOLATILE_KEYS = frozenset(
    {
        "generated_at",
        "started_at",
        "finished_at",
        "duration_ms",
        "duration_s",
        "wall_clock",
        "monotonic_ns",
        "run_id",
        # When capability discovery ran. The rest of a capability record is
        # derived from what the target advertised — tool names, canonical schema
        # hashes, a target-derived discovery id, a repository-relative trace URI
        # — and is therefore identical on every regeneration. This one field is
        # wall-clock, so leaving it intact made `examples regenerate --check`
        # fail on every run for a reason that carries no behavioural meaning.
        "discovered_at",
        "trace_path",
        "exit_code",
        "signal",
        "stderr_bytes",
        # Machine-specific by nature. A live run records the full declared-vs-
        # observed runtime and any mismatch; a *committed* copy cannot, because
        # the reviewer's Python patch version is not the generator's and the
        # example would show a diff on every machine.
        "runtime",
        "environment_warnings",
    }
)


def scrub_volatile(payload: Any) -> Any:
    """Recursively replace volatile values so a clean regeneration diffs clean."""
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if key in _VOLATILE_KEYS:
                # Type-appropriate placeholders: a committed artifact must still
                # validate against the same models a live one does.
                if isinstance(value, str):
                    result[key] = STABLE_TIMESTAMP
                elif isinstance(value, list):
                    result[key] = []
                elif isinstance(value, dict):
                    result[key] = None
                else:
                    result[key] = 0
            elif key == "headers" and isinstance(value, dict):
                # Date and the timing pseudo-headers change on every run; the
                # same rules that make a normalised trace comparable apply here.
                result[key] = normalize_headers({str(k): str(v) for k, v in value.items()})
            else:
                result[key] = scrub_volatile(value)
        return result
    if isinstance(payload, list):
        return [scrub_volatile(item) for item in payload]
    if isinstance(payload, str):
        return normalize_text(payload)
    return payload


@dataclass
class ExampleSpec:
    """One committed example: what to run and how to label it."""

    name: str
    scenario_id: str
    faulty_target: str
    reference_target: str
    finding_id: str
    title: str
    fact: str
    interpretation: str
    #: Stated plainly in the artifact so a synthetic defect is never mistaken
    #: for a third-party one.
    limitation: str


EXAMPLES: tuple[ExampleSpec, ...] = (
    ExampleSpec(
        name="http-header-mismatch",
        scenario_id="version.http_header_body_mismatch",
        faulty_target="targets/http/faulty-header-mismatch.yaml",
        reference_target="targets/http/fixture-2026.yaml",
        finding_id="EX-HTTP-HEADER-MISMATCH",
        title="Local fixture accepts an Mcp-Method header that disagrees with the body",
        fact=(
            "A POST whose body method was `tools/list` was sent with header "
            "`Mcp-Method: tools/call`. The fixture answered HTTP 200 with a normal "
            "`tools/list` result instead of rejecting the request."
        ),
        interpretation=(
            "Under 2026-07-28 an intermediary may route and authorize on `Mcp-Method` "
            "without parsing the body. A server that executes the body method while a "
            "gateway authorized the header method lets authorization and execution "
            "disagree."
        ),
        limitation=(
            "THIS IS A DELIBERATELY DEFECTIVE LOCAL FIXTURE, not a third-party "
            "implementation. The defect is switched on by "
            "INTEROP_FIXTURE_DEFECTS=ignore_header_body_mismatch and exists solely to "
            "prove the harness detects this condition. This scenario is ALREADY "
            "MINIMAL: one step, one constitutive header, a two-byte body. No reduction "
            "is claimed for it."
        ),
    ),
    ExampleSpec(
        name="http-header-mismatch-minimized",
        scenario_id="version.http_header_body_mismatch_noisy",
        faulty_target="targets/http/faulty-header-mismatch.yaml",
        reference_target="targets/http/fixture-2026.yaml",
        finding_id="EX-HTTP-HEADER-MISMATCH-MINIMIZED",
        title="Same defect, reached through reducible noise, then automatically minimised",
        fact=(
            "The same header/body disagreement was submitted inside a three-step "
            "scenario carrying unused body fields and a decorative header. Automatic "
            "reduction removed the unrelated steps, the unused fields and the "
            "decorative header, while preserving the Mcp-Method mismatch."
        ),
        interpretation=(
            "Reduction works over HTTP when reducible material exists. The constitutive "
            "header survives because header overrides are only reducible when a scenario "
            "explicitly nominates them; preserving the failure signature alone would not "
            "have been sufficient to protect it."
        ),
        limitation=(
            "THIS IS A DELIBERATELY DEFECTIVE LOCAL FIXTURE. The surrounding noise is "
            "synthetic material added to demonstrate reduction; it is not something a "
            "real client would send. It proves the reducer works, not that any real "
            "implementation is defective."
        ),
    ),
)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(scrub_volatile(payload), indent=2) + "\n", encoding="utf-8")


async def regenerate_example(spec: ExampleSpec, *, workdir: Path) -> dict[str, Any]:
    """Run one example end to end and write its committed artifacts."""
    scenario = get_scenario(spec.scenario_id)
    faulty = load_target(Path(spec.faulty_target))
    reference = load_target(Path(spec.reference_target))
    executor = ScenarioExecutor(artifacts_dir=workdir, seed=0)

    good_run, good_trace = await executor.run(scenario, reference)
    bad_run, bad_trace = await executor.run(scenario, faulty)
    differential = compare(good_run, good_trace, bad_run, bad_trace)

    reduced, minimization = await Minimizer(executor, faulty).minimize(
        scenario, bad_run.classification
    )
    final_run, final_trace = await executor.run(reduced, faulty)

    staging = workdir / "staging" / spec.name
    if staging.exists():
        shutil.rmtree(staging)
    repro_dir = write_repro(
        workdir,
        finding_id=spec.finding_id,
        scenario=reduced,
        target=faulty,
        run=final_run,
        trace=final_trace,
        seed=0,
        minimization=minimization.as_dict(),
    )

    destination = EXAMPLES_ROOT / spec.name
    destination.mkdir(parents=True, exist_ok=True)

    # Portable package files, copied from the generated reproduction.
    for name in ("repro.yaml", "run.sh", "run.ps1"):
        source = repro_dir / name
        target_path = destination / name
        target_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        if name == "run.sh":
            target_path.chmod(0o755)

    # Traces are written with volatile runtime values replaced. A live run keeps
    # wall-clock times, monotonic offsets and ephemeral ports; a *committed*
    # trace cannot, or every regeneration would show a diff and a reviewer could
    # not tell noise from a real behaviour change. `raw_sha256` is content-
    # derived and is left intact, so payload integrity remains checkable.
    for name in ("wire.jsonl", "normalized-wire.jsonl"):
        lines = (repro_dir / name).read_text(encoding="utf-8").splitlines()
        scrubbed = [json.dumps(scrub_volatile(json.loads(line))) for line in lines if line.strip()]
        (destination / name).write_text("\n".join(scrubbed) + "\n", encoding="utf-8")

    report = RunReport(suite=f"example:{spec.name}")
    report.runs.extend([good_run, bad_run])
    report.differentials.append(differential)

    _write_json(destination / "report.json", json.loads(report.model_dump_json(exclude_none=True)))
    _write_json(
        destination / "differential.json",
        json.loads(differential.model_dump_json(exclude_none=True)),
    )
    _write_json(destination / "minimization.json", minimization.as_dict())

    finding = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "finding_id": spec.finding_id,
        "classification": bad_run.classification.value,
        "title": spec.title,
        "target_id": bad_run.target_id,
        "target_version": faulty.version,
        "scenario_id": spec.scenario_id,
        "protocol_profile": bad_run.protocol_profile,
        "severity": "high",
        "fact": spec.fact,
        "normative_basis": scenario.definition.normative_source,
        "interpretation": spec.interpretation,
        "limitation": spec.limitation,
        "reproduction": f"uv run mcp-failure-lab replay artifacts/examples/{spec.name}/repro.yaml",
        "first_divergent_seq": differential.first_normalized_difference_seq,
        "evidence_paths": [
            f"artifact://examples/{spec.name}/wire.jsonl",
            f"artifact://examples/{spec.name}/normalized-wire.jsonl",
        ],
        "reproduction_status": "stable" if minimization.stable else "unstable",
        "minimization_status": "minimized" if minimization.reduced else "already_minimal",
        "public_safety": "private",
    }
    _write_json(destination / "finding.json", finding)
    (destination / "README.md").write_text(_readme(spec, minimization.as_dict()), encoding="utf-8")

    problems: list[str] = []
    for path in sorted(destination.iterdir()):
        if path.suffix == ".json":
            problems.extend(
                validate_payload(json.loads(path.read_text(encoding="utf-8")), context=path.name)
            )
    return {
        "name": spec.name,
        "classification": bad_run.classification.value,
        "minimization": minimization.as_dict(),
        "portability_problems": problems,
        "path": str(destination),
    }


def _readme(spec: ExampleSpec, minimization: dict[str, Any]) -> str:
    if minimization["already_minimal"]:
        verdict = (
            "**Already minimal.** Nothing could be removed without losing the failure "
            "signature. No reduction is claimed."
        )
    elif minimization["reduced"]:
        verdict = f"**Reduced** via {', '.join(minimization['stages_applied'])}."
    else:
        verdict = "No reduction achieved."

    return f"""# Example artifact — {spec.name}

**This demonstrates a deliberately faulty local fixture. It is not a defect in
any third-party implementation, and must never be reported as one.**

## What happened

{spec.fact}

## Minimisation

{verdict}

| Metric | Original | Minimised |
|---|---:|---:|
| events | {minimization["original_event_count"]} | {minimization["minimized_event_count"]} |
| body bytes | {minimization["original_body_bytes"]} | {minimization["minimized_body_bytes"]} |
| header-override bytes | {minimization["original_header_override_bytes"]} | \
{minimization["minimized_header_override_bytes"]} |
| mutation surface bytes | {minimization["original_mutation_surface_bytes"]} | \
{minimization["minimized_mutation_surface_bytes"]} |

Preserved failing assertions: `{minimization["preserved_failing_assertions"]}`
Stable across {minimization["stability_runs"]} consecutive runs.

## Reproduce

```bash
./run.sh              # or: pwsh run.ps1
```

Both wrappers resolve their own directory, so this package works from anywhere.

## Limitation

{spec.limitation}

## Regenerate

```bash
uv run mcp-failure-lab examples regenerate
```

Do not hand-edit these files.
"""
