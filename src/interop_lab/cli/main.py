"""Command line interface.

CLI-first and local-first: every capability is reachable without a server, and
the artifacts on disk are the source of truth.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from interop_lab.benchmark.cli import benchmark_app
from interop_lab.config.loader import ConfigError, load_target
from interop_lab.containment.resolution import (
    DEFAULT_RESOLUTION_PATH,
    BuildResolutionIndex,
    ImageResolutionError,
)
from interop_lab.differential.compare import compare
from interop_lab.events.model import WireTrace
from interop_lab.export.checksums import CHECKSUMS_FILENAME, MANIFEST_FILENAME
from interop_lab.minimizer.reduce import Minimizer
from interop_lab.models.core import Classification, RunResult
from interop_lab.naming import CLI_COMMAND, HARNESS_VERSION, NAME_STATUS, PRODUCT_NAME
from interop_lab.profiles.registry import PROFILES, Transport
from interop_lab.replay.repro import ReplayOutcome, replay, write_repro
from interop_lab.reporting.render import RunReport, render_terminal
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.runner.images import ExecutionContext
from interop_lab.scenarios.library import ALL_SCENARIOS, SUITES, get_scenario, get_suite
from interop_lab.viewer.cli import viewer_app

app = typer.Typer(
    name=CLI_COMMAND,
    help=f"{PRODUCT_NAME} — {NAME_STATUS}",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

DEFAULT_ARTIFACTS = Path("artifacts")

#: Every command that can launch a container takes this. It is optional because
#: a registry-pinned target needs nothing built; it is required in practice for
#: anything locally built, and the resolver says so by name when it is missing.
ResolutionOption = Annotated[
    Path | None,
    typer.Option(
        "--resolution",
        help=(
            "Build-resolution manifest from `external-build`. Required for any target "
            "whose image is built locally; ignored for registry-pinned targets. "
            f"Defaults to {DEFAULT_RESOLUTION_PATH} when that file exists."
        ),
    ),
]


def _fail(message: str) -> None:
    console.print(f"[bold red]error[/]: {message}")
    raise typer.Exit(code=2)


def _execution_context(resolution: Path | None) -> ExecutionContext:
    """Build the context every launch path is handed.

    An explicitly named manifest that cannot be read is an error, not a fallback
    to "no resolution": the caller said which build this run is measuring, and
    quietly measuring something else is the failure this whole mechanism exists
    to prevent. The default path is used only when it happens to exist, so an
    ordinary registry-only run needs no flag at all.
    """
    path = resolution
    if path is None and DEFAULT_RESOLUTION_PATH.is_file():
        path = DEFAULT_RESOLUTION_PATH
    index = BuildResolutionIndex.load(path) if path is not None else None
    return ExecutionContext.create(repo_root=Path.cwd(), index=index)


def _load(paths: list[Path]):
    try:
        return [load_target(p) for p in paths]
    except ConfigError as exc:
        _fail(str(exc))
        raise  # unreachable, satisfies the type checker


@app.command()
def version() -> None:
    """Print harness and naming status."""
    console.print(f"{PRODUCT_NAME} {HARNESS_VERSION}")
    console.print(f"name status: {NAME_STATUS}")


@app.command("list-scenarios")
def list_scenarios() -> None:
    """List every scenario with its applicability."""
    for scenario_id, scenario in sorted(ALL_SCENARIOS.items()):
        definition = scenario.definition
        profiles = ",".join(definition.applicability.profiles) or "any"
        console.print(
            f"[bold]{scenario_id}[/] ({definition.category.value}/{definition.nature.value})"
        )
        console.print(f"  {definition.title}")
        console.print(f"  profiles: {profiles}  severity: {definition.severity.value}")
    console.print(f"\nsuites: {', '.join(sorted(SUITES))}")


@app.command("list-profiles")
def list_profiles() -> None:
    """List supported dated protocol profiles."""
    for profile in sorted(
        {p.profile_id: p for p in PROFILES.values()}.values(), key=lambda p: p.spec_version
    ):
        console.print(f"[bold]{profile.profile_id}[/] — spec {profile.spec_version}")
        console.print(f"  lifecycle: {profile.lifecycle.value}")
        console.print(f"  requires initialize: {profile.requires_initialize}")
        console.print(f"  requires params._meta: {profile.requires_request_meta}")
        console.print(f"  transports: {', '.join(t.value for t in profile.supported_transports)}")


@app.command()
def run(
    target: Annotated[list[Path], typer.Option("--target", "-t", help="Target YAML.")],
    suite: Annotated[str, typer.Option("--suite", "-s", help="Suite name.")] = "smoke",
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Run a single scenario instead of a suite.")
    ] = None,
    artifacts: Annotated[Path, typer.Option("--artifacts", help="Artifact root.")] = (
        DEFAULT_ARTIFACTS
    ),
    seed: Annotated[int, typer.Option("--seed", help="Deterministic seed.")] = 0,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write the JSON report here.")
    ] = None,
    resolution: ResolutionOption = None,
) -> None:
    """Run a suite or scenario against one or more targets."""
    targets = _load(target)
    try:
        scenarios = [get_scenario(scenario)] if scenario else get_suite(suite)
    except KeyError as exc:
        _fail(str(exc))
        return

    try:
        context = _execution_context(resolution)
    except ImageResolutionError as exc:
        _fail(str(exc))
        return

    executor = ScenarioExecutor(artifacts_dir=artifacts, seed=seed, context=context)
    report = RunReport(suite=scenario or suite)

    async def _run_all() -> None:
        for scn in scenarios:
            for tgt in targets:
                result, _ = await executor.run(scn, tgt)
                report.runs.append(result)

    asyncio.run(_run_all())
    render_terminal(report, console)

    out = json_out or (artifacts / "reports" / f"{report.suite.replace('.', '_')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    console.print(f"\n[dim]JSON report: {out}[/]")

    raise typer.Exit(code=1 if report.failures else 0)


@app.command()
def diff(
    left: Annotated[Path, typer.Option("--left", help="Left target YAML.")],
    right: Annotated[Path, typer.Option("--right", help="Right target YAML.")],
    suite: Annotated[str, typer.Option("--suite", "-s")] = "core",
    scenario: Annotated[str | None, typer.Option("--scenario")] = None,
    artifacts: Annotated[Path, typer.Option("--artifacts")] = DEFAULT_ARTIFACTS,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    resolution: ResolutionOption = None,
) -> None:
    """Run identical scenarios against two targets and compare their behaviour."""
    left_target, right_target = _load([left, right])
    try:
        scenarios = [get_scenario(scenario)] if scenario else get_suite(suite)
    except KeyError as exc:
        _fail(str(exc))
        return

    try:
        context = _execution_context(resolution)
    except ImageResolutionError as exc:
        _fail(str(exc))
        return

    # One context for both sides. A differential whose two halves resolved
    # different builds would be comparing something nobody chose to compare.
    executor = ScenarioExecutor(artifacts_dir=artifacts, seed=seed, context=context)
    report = RunReport(suite=f"diff:{scenario or suite}")

    async def _run_all() -> None:
        for scn in scenarios:
            left_run, left_trace = await executor.run(scn, left_target)
            right_run, right_trace = await executor.run(scn, right_target)
            report.runs.extend([left_run, right_run])
            report.differentials.append(compare(left_run, left_trace, right_run, right_trace))

    asyncio.run(_run_all())
    render_terminal(report, console)

    out = artifacts / "reports" / f"diff_{(scenario or suite).replace('.', '_')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    console.print(f"\n[dim]JSON report: {out}[/]")

    divergent = [
        d
        for d in report.differentials
        if d.verdict.value in ("meaningful_divergence", "normative_divergence")
    ]
    raise typer.Exit(code=1 if divergent else 0)


@app.command()
def minimize(
    target: Annotated[Path, typer.Option("--target", "-t")],
    scenario: Annotated[str, typer.Option("--scenario", help="Scenario to reduce.")],
    artifacts: Annotated[Path, typer.Option("--artifacts")] = DEFAULT_ARTIFACTS,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    finding_id: Annotated[
        str | None, typer.Option("--finding-id", help="Name for the reproduction package.")
    ] = None,
    resolution: ResolutionOption = None,
) -> None:
    """Reduce a failing scenario to the smallest reproduction and package it."""
    (tgt,) = _load([target])
    try:
        scn = get_scenario(scenario)
    except KeyError as exc:
        _fail(str(exc))
        return

    try:
        context = _execution_context(resolution)
    except ImageResolutionError as exc:
        _fail(str(exc))
        return

    # Every candidate run in the reduction goes through this executor, so the
    # whole minimisation measures one resolved image rather than drifting onto
    # another halfway through.
    executor = ScenarioExecutor(artifacts_dir=artifacts, seed=seed, context=context)

    async def _minimize() -> tuple[RunResult, WireTrace, object, object] | None:
        baseline, _ = await executor.run(scn, tgt)
        if baseline.classification in (Classification.PASS, Classification.NOT_APPLICABLE):
            console.print(
                f"[yellow]nothing to minimise[/]: {scenario} on {tgt.target_id} is "
                f"{baseline.classification.value}"
            )
            return None

        console.print(
            f"minimising [bold]{scenario}[/] on [bold]{tgt.target_id}[/] "
            f"(target classification: {baseline.classification.value})"
        )
        minimizer = Minimizer(executor, tgt)
        reduced, report = await minimizer.minimize(scn, baseline.classification)
        final_run, final_trace = await executor.run(reduced, tgt)
        return final_run, final_trace, reduced, report

    outcome = asyncio.run(_minimize())
    if outcome is None:
        raise typer.Exit(code=0)

    final_run, final_trace, reduced, min_report = outcome
    fid = finding_id or f"{scenario.replace('.', '_')}__{tgt.target_id.replace('.', '_')}"
    repro_dir = write_repro(
        artifacts,
        finding_id=fid,
        scenario=reduced,  # type: ignore[arg-type]
        target=tgt,
        run=final_run,  # type: ignore[arg-type]
        trace=final_trace,  # type: ignore[arg-type]
        seed=seed,
        minimization=min_report.as_dict(),  # type: ignore[attr-defined]
    )

    data = min_report.as_dict()  # type: ignore[attr-defined]
    console.print(
        f"\nevents {data['original_event_count']} -> {data['minimized_event_count']}   "
        f"body {data['original_body_bytes']} -> {data['minimized_body_bytes']} B   "
        f"headers {data['original_header_override_bytes']} -> "
        f"{data['minimized_header_override_bytes']} B   "
        f"surface {data['original_mutation_surface_bytes']} -> "
        f"{data['minimized_mutation_surface_bytes']} B"
    )
    console.print(f"{data['candidate_runs']} candidate runs in {data['duration_s']}s")
    if data["already_minimal"]:
        console.print(
            "[yellow]already minimal[/]: nothing could be removed without losing the "
            "failure signature. Reported as-is rather than presented as a reduction."
        )
    elif data["reduced"]:
        console.print(f"[green]reduced[/] via {', '.join(data['stages_applied'])}")
    console.print(
        f"stable across {data['stability_runs']} consecutive runs: "
        f"[{'green' if data['stable'] else 'red'}]{data['stable']}[/]"
    )
    console.print(f"reproduction package: [bold]{repro_dir}[/]")


@app.command("conformance")
def conformance_cmd(
    target: Annotated[Path, typer.Option("--target", "-t", help="HTTP target YAML.")],
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Single official scenario id.")
    ] = None,
    artifacts: Annotated[Path, typer.Option("--artifacts")] = DEFAULT_ARTIFACTS,
) -> None:
    """Run the official MCP conformance suite against a target as a baseline.

    With no ``--scenario``, this invokes the official frozen requirement set
    for the target's dated profile (``--requirements``). A ``--scenario`` run
    is one wire-scoped probe at ``--spec-version`` and is not a requirement-set
    result. Official results stay in their own vocabulary.
    """
    from interop_lab.conformance.adapter import (
        ConformanceAdapter,
        is_available,
        unavailable_reason,
    )
    from interop_lab.transports.http.session import StreamableHttpSession

    (tgt,) = _load([target])
    if tgt.transport is not Transport.STREAMABLE_HTTP:
        _fail("the official server suite runs against an HTTP URL; use an HTTP target")
    if not is_available():
        _fail(f"official conformance framework unavailable: {unavailable_reason()}")

    async def _run() -> object:
        session = StreamableHttpSession(tgt, run_id="conformance")
        async with session:
            assert session.base_url is not None
            adapter = ConformanceAdapter(output_root=artifacts)
            return await adapter.run_server(target=tgt, url=session.base_url, scenario=scenario)

    run = asyncio.run(_run())
    data = run.model_dump(mode="json")  # type: ignore[attr-defined]

    console.print(f"[bold]official conformance[/] {data['package']}@{data['version']}")
    console.print(f"source sha: {data['source_sha']}")
    console.print(
        f"selector: {data['selector']}   revision: {data['revision']}   target: {data['target_id']}"
    )
    if data["selector"] == "scenario":
        console.print(
            "[yellow]scenario probe[/]: Failure Lab is not reporting a frozen "
            "requirement-set result. This sentence is printed by conformance_cmd, "
            "not by the official CLI. The official CLI rejects combining "
            "--requirements with --scenario, so this probe uses --spec-version."
        )
    colour = {
        "OFFICIAL_CONFORMANCE_PASS": "green",
        "OFFICIAL_CONFORMANCE_FAILURE": "red",
    }.get(data["outcome"], "yellow")
    console.print(
        f"outcome: [{colour}]{data['outcome']}[/]   "
        f"verdict source: {data['verdict_source']}   exit: {data['exit_code']}"
    )
    console.print(
        f"checks: {data['scenarios_passed']} passed, {data['scenarios_failed']} failed, "
        f"{data['checks_skipped']} skipped, {data['checks_informational']} informational "
        f"of {data['scenarios_total']}"
    )
    for check in data["checks"]:
        mark = "PASS" if check["passed"] else ("SKIP" if check["passed"] is None else "FAIL")
        console.print(f"  [{mark}] {check['check_id']}")
    if data.get("parse_note"):
        console.print(f"[yellow]note:[/] {data['parse_note']}")

    out = artifacts / "conformance" / f"{tgt.target_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    console.print(f"[dim]official result: {out}[/]")
    raise typer.Exit(code=1 if data["outcome"] != "OFFICIAL_CONFORMANCE_PASS" else 0)


@app.command("replay")
def replay_cmd(
    repro: Annotated[Path, typer.Argument(help="Path to a repro.yaml.")],
    artifacts: Annotated[Path, typer.Option("--artifacts")] = DEFAULT_ARTIFACTS,
    resolution: ResolutionOption = None,
) -> None:
    """Re-execute a reproduction package.

    A package whose target is locally built has to be rebuilt on this machine
    first: it carries the portable inputs, never the original machine's image
    ID, so `external-build` produces a fresh resolution and `--resolution`
    points this command at it.
    """
    try:
        context = _execution_context(resolution)
    except ImageResolutionError as exc:
        _fail(str(exc))
        return
    result = asyncio.run(replay(repro, artifacts_dir=artifacts, context=context))
    colour = {
        ReplayOutcome.REPRODUCED: "green",
        ReplayOutcome.NOT_REPRODUCED: "yellow",
    }.get(result.outcome, "red")
    console.print(f"[{colour}]{result.outcome.value}[/]: {result.detail}")
    console.print(f"expected: {result.expected.value}   observed: {result.observed.value}")
    raise typer.Exit(code=0 if result.outcome is ReplayOutcome.REPRODUCED else 1)


@app.command("init")
def init_cmd(
    directory: Annotated[Path, typer.Option("--dir", help="Where to write the scaffold.")] = Path(
        "interop"
    ),
    target: Annotated[
        str, typer.Option("--target-id", help="Identifier for the server under test.")
    ] = "my.server",
    command: Annotated[
        str,
        typer.Option(
            "--launch",
            help="How to start the server, e.g. '${PYTHON} server.py'.",
        ),
    ] = "${PYTHON} server.py",
    profile: Annotated[str, typer.Option("--profile")] = "mcp-2025-11-25",
) -> None:
    """Scaffold a target, matrix and suite for a repository adopting the gate.

    This exists because installing the harness into an unrelated repository
    showed that the first ten minutes are spent guessing file shapes. A
    validation error names a missing field; it does not tell you what a working
    file looks like, and that is only obvious to someone who already has one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    argv = json.dumps(command.split())

    files = {
        "target.yaml": (
            f"target_id: {target}\n"
            "role: server\n"
            "implementation: describe the implementation\n"
            "language: python\n"
            "# Exact release or commit. A moving version is rejected at load.\n"
            'version: "0.1.0"\n'
            f"launch_command: {argv}\n"
            "transport: stdio\n"
            f"protocol_profile: {profile}\n"
            "timeout_s: 10.0\n"
            "# Tools the server advertises. Scenarios needing others are\n"
            "# reported NOT_APPLICABLE rather than failed.\n"
            "expected_tools: []\n"
            "provenance: local_fixture\n"
            "execution_mode: host\n"
        ),
        "matrix.yaml": (
            "matrix_id: adopter\n"
            "description: Targets the gate measures.\n"
            "targets:\n"
            "  - interop/target.yaml\n"
        ),
        "suite.yaml": (
            "suite_id: adopter_gate\n"
            "description: Scenarios the gate protects.\n"
            "scenarios:\n"
            "  - baseline.tools_list\n"
            "  - wire.unknown_method\n"
            "  - wire.response_id_correlation\n"
            "  - wire.malformed_json\n"
        ),
    }

    written: list[str] = []
    for name, body in files.items():
        path = directory / name
        if path.exists():
            console.print(f"[yellow]skipped[/] {path} (already exists)")
            continue
        path.write_text(body, encoding="utf-8")
        written.append(str(path))

    for path_text in written:
        console.print(f"[green]wrote[/] {path_text}")

    console.print(
        "\nNext:\n"
        f"  1. Edit {directory}/target.yaml so launch_command starts your server.\n"
        f"  2. {CLI_COMMAND} run -t {directory}/target.yaml --suite smoke\n"
        f"  3. {CLI_COMMAND} baseline create --matrix {directory}/matrix.yaml "
        f"--suite {directory}/suite.yaml --out {directory}/baseline.json "
        '--reason "why this is approved" --by "you@example.com"\n'
        f"  4. {CLI_COMMAND} ci --baseline {directory}/baseline.json "
        f"--matrix {directory}/matrix.yaml --suite {directory}/suite.yaml\n"
    )


@app.command("export")
def export_cmd(
    out: Annotated[Path, typer.Option("--out", help="Destination directory.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report the plan without writing anything.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite a non-empty destination.")
    ] = False,
) -> None:
    """Materialise a public-safe tree from an allowlist.

    Exclusion is by allowlist, not denylist: anything not explicitly approved is
    left behind. Forgetting therefore produces a *missing file*, which is
    visible and harmless, rather than a disclosure.

    The result is a fresh tree with no `.git` directory. Publishing it means
    creating a new repository, so no private commit, branch or message can
    travel with it.
    """
    import shutil
    import subprocess

    from interop_lab.ci.baseline import EXIT_CONFIGURATION_ERROR
    from interop_lab.export.manifest import EXPORT_MANIFEST_VERSION
    from interop_lab.export.manifest import plan as build_plan

    listing = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        _fail("not a git repository, or `git ls-files` failed")
    paths = [line for line in listing.stdout.splitlines() if line.strip()]

    result = build_plan(paths, root=Path())

    console.print(f"[bold]export plan[/]: {result.summary()}")
    forbidden = [d for d in result.excluded if d.reason.startswith("forbidden")]
    console.print(f"  explicitly forbidden: {len(forbidden)}")
    for decision in forbidden:
        console.print(f"    [red]excluded[/] {decision.path}")

    if result.content_violations:
        console.print("\n[red]content violations — nothing was written:[/]")
        for violation in result.content_violations:
            console.print(f"    {violation}")
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR)

    if dry_run:
        console.print("\n[yellow]dry run[/]: nothing written")
        raise typer.Exit(code=0)

    if out.exists() and any(out.iterdir()) and not force:
        _fail(f"{out} exists and is not empty; pass --force to overwrite")
    if out.exists() and force:
        shutil.rmtree(out)

    # A tracked path that is not a file on disk is a disagreement between
    # `git ls-files` and the working tree, and the manifest written below would
    # claim a file the export does not contain. Skipping it quietly is how a
    # manifest and its tree drift apart, so it stops the export instead.
    for decision in result.included:
        source = Path(decision.path)
        if not source.is_file():
            _fail(
                f"{decision.path} is tracked and approved for export but is not a file "
                "on disk; the working tree does not match the index"
            )
        destination = out / decision.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    # Excluded paths are counted, never named. The manifest ships inside the
    # export, and a list of what was withheld is itself a disclosure: the
    # excluded set contains private finding identifiers, which would travel into
    # the public tree in the very file meant to prove nothing private did.
    withheld: dict[str, int] = {}
    for decision in result.excluded:
        withheld[decision.reason] = withheld.get(decision.reason, 0) + 1

    manifest = {
        "manifest_version": EXPORT_MANIFEST_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "harness_version": HARNESS_VERSION,
        "summary": result.summary(),
        "included": [{"path": d.path, "reason": d.reason} for d in result.included],
        "withheld_by_reason": withheld,
        "note": (
            "Excluded paths are counted rather than listed. Naming what was "
            "withheld would publish the very identifiers the exclusion exists to "
            "protect."
        ),
    }
    (out / "EXPORT-MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    console.print(f"\n[green]wrote[/] {len(result.included)} files to {out}")
    console.print(f"[green]manifest[/] {out / 'EXPORT-MANIFEST.json'}")
    console.print(
        "\nNo .git directory was created. Publishing means initialising a new "
        "repository, so no private history can travel with it."
    )


@app.command("export-checksums")
def export_checksums_cmd(
    root: Annotated[Path, typer.Option("--root", help="The materialised export directory.")],
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help=f"Defaults to <root>/{MANIFEST_FILENAME}."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help=f"Defaults to <root>/{CHECKSUMS_FILENAME}."),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Verify an existing checksum file instead of writing one."),
    ] = False,
) -> None:
    """Hash exactly what `EXPORT-MANIFEST.json` says the export contains.

    A sibling command rather than `export checksums`, because `export` already
    takes its own options and turning it into a command group would break every
    existing caller for no gain.

    The manifest is the only source of paths. By the time the gate reaches this
    step it has installed a virtualenv and a `node_modules` inside the export, and
    hashing the directory would report a release tens of thousands of files
    larger than the one being released. Every disagreement between the manifest
    and the tree — a missing file, a duplicate, a directory listed as a file, a
    path that escapes the root — fails here rather than vanishing from the output.
    """
    from interop_lab.ci.baseline import EXIT_CONFIGURATION_ERROR
    from interop_lab.export.checksums import (
        ChecksumError,
        generate,
        verify,
    )

    manifest_path = manifest if manifest is not None else root / MANIFEST_FILENAME
    checksums_path = out if out is not None else root / CHECKSUMS_FILENAME

    if not root.is_dir():
        _fail(f"{root} is not a directory")

    try:
        report = (
            verify(root, manifest_path, checksums_path)
            if check
            else generate(root, manifest_path, checksums_path)
        )
    except ChecksumError as exc:
        console.print(f"[bold red]checksum error[/]: {exc}")
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc

    # Machine-readable, on stdout, so the gate and its workflow assert on numbers
    # rather than parsing a sentence.
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))


baseline_app = typer.Typer(help="Approved baselines for the CI gate.", no_args_is_help=True)
app.add_typer(baseline_app, name="baseline")

# Private Benchmark 0.3. Its own module rather than more commands here: the
# benchmark has a pinned definition, a validation layer and a reporting layer of
# its own, and none of them belong in the CLI that drives ordinary runs.
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(viewer_app, name="viewer")


async def _run_matrix(
    matrix_path: Path,
    suite_path: Path,
    artifacts: Path,
    seed: int,
    *,
    context: ExecutionContext | None = None,
):
    from interop_lab.config.matrix import load_matrix, load_suite

    _, targets = load_matrix(matrix_path)
    _, scenarios = load_suite(suite_path)
    executor = ScenarioExecutor(artifacts_dir=artifacts, seed=seed, context=context)

    results = []
    for scenario in scenarios:
        for target in targets:
            run, _ = await executor.run(scenario, target)
            results.append((run, scenario, target))
    return results, targets, scenarios


@baseline_app.command("create")
def baseline_create(
    matrix: Annotated[Path, typer.Option("--matrix", help="Target matrix YAML.")],
    suite: Annotated[Path, typer.Option("--suite", help="Suite YAML.")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the baseline.")],
    reason: Annotated[str, typer.Option("--reason", help="Why this baseline is approved.")],
    created_by: Annotated[str, typer.Option("--by")] = "unspecified",
    approve_failure: Annotated[
        list[str] | None,
        typer.Option(
            "--approve-failure",
            help=(
                "Approve one failing result as expected, formatted "
                "target_id:scenario_id. Repeatable. Each needs a matching "
                "--approval-reason in the same order."
            ),
        ),
    ] = None,
    approval_reason: Annotated[
        list[str] | None,
        typer.Option("--approval-reason", help="Why the paired failure is approved."),
    ] = None,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    artifacts: Annotated[Path, typer.Option("--artifacts")] = DEFAULT_ARTIFACTS,
    resolution: ResolutionOption = None,
) -> None:
    """Record what the matrix currently does, as an explicitly approved snapshot.

    A reason is mandatory. A baseline without a stated justification is
    indistinguishable from one created to make a red build green.
    """
    from interop_lab.ci.baseline import UnapprovedFailureError, build_baseline

    if not reason.strip():
        _fail("--reason is required: an unexplained baseline hides the failure it absorbs")

    keys = approve_failure or []
    reasons = approval_reason or []
    if len(keys) != len(reasons):
        _fail(
            f"{len(keys)} --approve-failure but {len(reasons)} --approval-reason; "
            "each approved failure needs its own justification"
        )
    approvals: dict[str, str] = {}
    for key, why in zip(keys, reasons, strict=True):
        if ":" not in key:
            _fail(f"--approve-failure must be target_id:scenario_id, got {key!r}")
        if not why.strip():
            _fail(f"--approval-reason for {key!r} is empty")
        approvals[key] = why

    try:
        context = _execution_context(resolution)
    except ImageResolutionError as exc:
        _fail(str(exc))
        return

    try:
        results, targets, scenarios = asyncio.run(
            _run_matrix(matrix, suite, artifacts, seed, context=context)
        )
    except ConfigError as exc:
        _fail(str(exc))
        return

    try:
        document = build_baseline(
            baseline_id=out.stem,
            reason=reason,
            created_by=created_by,
            suite=suite.stem,
            scenarios=scenarios,
            targets=targets,
            runs=results,
            approved_failures=approvals,
        )
    except UnapprovedFailureError as exc:
        _fail(str(exc))
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document.model_dump_json(indent=2) + "\n", encoding="utf-8")

    console.print(f"baseline [bold]{document.baseline_id}[/] written to {out}")
    console.print(f"entries: {len(document.entries)}   reason: {reason}")
    for entry in document.entries:
        console.print(
            f"  {entry.target_id:38} {entry.scenario_id:34} {entry.allowed_classification.value}"
        )


@baseline_app.command("inspect")
def baseline_inspect(
    path: Annotated[Path, typer.Argument(help="Baseline JSON.")],
) -> None:
    """Show what a baseline approves, and why."""
    from interop_lab.ci.baseline import BaselineSchemaError, load_baseline

    if not path.is_file():
        _fail(f"baseline not found: {path}")
    try:
        document = load_baseline(path)
    except BaselineSchemaError as exc:
        _fail(str(exc))
        return

    console.print(f"[bold]{document.baseline_id}[/] (schema {document.baseline_schema_version})")
    console.print(f"created {document.created_at} by {document.created_by}")
    console.print(f"reason: {document.created_reason}")
    console.print(f"suite: {document.suite} ({document.suite_fingerprint})")
    console.print(f"profiles: {', '.join(document.protocol_profiles)}")
    console.print(f"policy: {document.policy.model_dump()}")
    console.print("")
    for entry in document.entries:
        marker = "" if entry.applicable else " [dim](not applicable)[/]"
        console.print(
            f"  {entry.target_id:38} {entry.scenario_id:34} "
            f"{entry.allowed_classification.value}{marker}"
        )
        if entry.expected_failed_assertions:
            console.print(f"      approved failing assertions: {entry.expected_failed_assertions}")


@app.command("ci")
def ci_gate(
    baseline: Annotated[Path, typer.Option("--baseline", help="Approved baseline JSON.")],
    matrix: Annotated[Path, typer.Option("--matrix")],
    suite: Annotated[Path, typer.Option("--suite")],
    json_out: Annotated[Path | None, typer.Option("--json")] = None,
    junit: Annotated[Path | None, typer.Option("--junit")] = None,
    sarif: Annotated[Path | None, typer.Option("--sarif")] = None,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    artifacts: Annotated[Path, typer.Option("--artifacts")] = DEFAULT_ARTIFACTS,
    resolution: ResolutionOption = None,
) -> None:
    """Run the matrix and compare it against an approved baseline.

    Exit codes: 0 passed, 1 regression, 2 configuration/baseline error,
    3 harness or environment failure.
    """
    from interop_lab.ci.baseline import (
        EXIT_CONFIGURATION_ERROR,
        BaselineSchemaError,
        GateOutcome,
        load_baseline,
    )
    from interop_lab.ci.gate import evaluate
    from interop_lab.reporting import junit as junit_report
    from interop_lab.reporting import sarif as sarif_report

    if not baseline.is_file():
        console.print(f"[bold red]error[/]: baseline not found: {baseline}")
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR)
    try:
        document = load_baseline(baseline)
    except BaselineSchemaError as exc:
        console.print(f"[bold red]error[/]: {exc}")
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc

    try:
        context = _execution_context(resolution)
    except ImageResolutionError as exc:
        console.print(f"[bold red]error[/]: {exc}")
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc

    try:
        results, _, _ = asyncio.run(_run_matrix(matrix, suite, artifacts, seed, context=context))
    except ConfigError as exc:
        console.print(f"[bold red]error[/]: {exc}")
        raise typer.Exit(code=EXIT_CONFIGURATION_ERROR) from exc

    gate = evaluate(baseline=document, runs=results, artifacts_root=Path("artifacts"))
    runs = [run for run, _, _ in results]

    written: list[str] = []
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(gate.model_dump_json(indent=2) + "\n", encoding="utf-8")
        written.append(str(json_out))
    if junit:
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(junit_report.to_string(runs, suite_name=document.suite), encoding="utf-8")
        written.append(str(junit))
    if sarif:
        sarif.parent.mkdir(parents=True, exist_ok=True)
        sarif.write_text(json.dumps(sarif_report.build(runs), indent=2) + "\n", encoding="utf-8")
        written.append(str(sarif))
    gate.artifact_paths = written

    colour = {
        GateOutcome.PASSED: "green",
        GateOutcome.REGRESSION: "red",
    }.get(gate.outcome, "magenta")
    console.print(f"gate [{colour}]{gate.outcome.value}[/] (exit {gate.exit_code})")
    console.print(f"summary: {gate.summary()}")

    for regression in gate.regressions:
        tag = "REGRESSION" if regression.blocking else "warning"
        console.print(
            f"  [{'red' if regression.blocking else 'yellow'}]{tag}[/] "
            f"{regression.kind.value}: {regression.target_id} / {regression.scenario_id}"
        )
        console.print(f"      {regression.detail}")
        if regression.normative_source:
            console.print(f"      basis: {regression.normative_source}")
    for improvement in gate.improvements:
        console.print(
            f"  [cyan]IMPROVEMENT[/] {improvement.target_id} / {improvement.scenario_id}: "
            f"{improvement.baseline_value} -> {improvement.observed_value}"
        )
        console.print(f"      {improvement.detail}")
    for mismatch in gate.environment_mismatches:
        console.print(f"  [yellow]environment[/] {mismatch}")

    if gate.improvements:
        console.print(
            "\n[dim]Improvements never update the baseline automatically. Re-approve it "
            "with `mcp-failure-lab baseline create --reason ...` if the change is intended.[/]"
        )
    for path in written:
        console.print(f"[dim]wrote {path}[/]")

    raise typer.Exit(code=gate.exit_code)


examples_app = typer.Typer(help="Manage committed example artifacts.", no_args_is_help=True)
app.add_typer(examples_app, name="examples")


@examples_app.command("regenerate")
def examples_regenerate(
    workdir: Annotated[
        Path | None, typer.Option("--workdir", help="Scratch directory for the runs.")
    ] = None,
    check: Annotated[
        bool, typer.Option("--check", help="Fail if regeneration changes anything.")
    ] = False,
) -> None:
    """Regenerate every committed example from local synthetic fixtures.

    Deterministic by construction: volatile values are scrubbed, so a clean
    tree stays clean when nothing in the implementation changed.
    """
    import subprocess
    import tempfile

    from interop_lab.artifacts.examples import EXAMPLES, regenerate_example

    scratch = workdir or Path(tempfile.mkdtemp(prefix="interop-examples-"))

    async def _run() -> list[dict[str, object]]:
        return [await regenerate_example(spec, workdir=scratch) for spec in EXAMPLES]

    results = asyncio.run(_run())

    failed = False
    for result in results:
        minimization = result["minimization"]
        verdict = (
            "already minimal"
            if minimization["already_minimal"]  # type: ignore[index]
            else "reduced"
            if minimization["reduced"]  # type: ignore[index]
            else "no reduction"
        )
        console.print(
            f"[bold]{result['name']}[/]: {result['classification']} — {verdict} "
            f"(surface {minimization['original_mutation_surface_bytes']} -> "  # type: ignore[index]
            f"{minimization['minimized_mutation_surface_bytes']} B)"  # type: ignore[index]
        )
        problems = result["portability_problems"]
        if problems:
            failed = True
            for problem in problems:  # type: ignore[union-attr]
                console.print(f"  [red]portability[/]: {problem}")

    if failed:
        _fail("regenerated examples contain non-portable references")

    if check:
        diff = subprocess.run(
            ["git", "--no-pager", "diff", "--stat", "--", str(Path("artifacts/examples"))],
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.stdout.strip():
            console.print("[red]regeneration is not deterministic:[/]")
            console.print(diff.stdout)
            raise typer.Exit(code=1)
        console.print("[green]examples are up to date and regeneration is deterministic[/]")


@app.command("external-build")
def external_build(
    images: Annotated[Path, typer.Option("--images", help="External image manifest.")] = Path(
        "tools/external/images.yaml"
    ),
    resolution: Annotated[
        Path,
        typer.Option("--resolution", help="Where to write the build resolution."),
    ] = DEFAULT_RESOLUTION_PATH,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Ignore the layer cache.")] = False,
    ca_bundle: Annotated[
        Path | None,
        typer.Option("--ca-bundle", help="Root CA for a TLS-terminating proxy."),
    ] = None,
    harness_commit: Annotated[str | None, typer.Option("--harness-commit")] = None,
) -> None:
    """Build every external image and record what this builder produced.

    Writes a build-resolution manifest and **nothing else**. No tracked file is
    modified, which is what makes `git diff --exit-code` after a build a
    meaningful check: the previous `--write-pins` behaviour rewrote the very
    target files the fingerprint is computed from, so a build changed the
    identity of the thing it had just built.
    """
    from interop_lab.external.build import ExternalBuildError, build_all, write_resolution

    if images != Path("tools/external/images.yaml"):
        # Accepted so the flag reads as documented, but the loader resolves the
        # manifest relative to the repository root by design — a second search
        # path is a second place for the image set to disagree with itself.
        _fail(
            f"--images is fixed at tools/external/images.yaml; got {images}. The manifest "
            "is resolved against the repository root so a shell script, a workflow step "
            "and a test all read the same file."
        )

    try:
        manifest = build_all(
            repo_root=Path.cwd(),
            no_cache=no_cache,
            ca_bundle=ca_bundle,
            harness_commit=harness_commit,
        )
    except ExternalBuildError as exc:
        _fail(str(exc))
        return

    written = write_resolution(manifest, resolution)
    console.print(
        f"\n[green]built {len(manifest.entries)} external image(s)[/] on "
        f"{manifest.builder} {manifest.docker_engine_version} "
        f"(storage driver {manifest.storage_driver}, {manifest.architecture})"
    )
    for entry in manifest.entries:
        console.print(f"  {entry.build_key:22} {entry.observed_local_image_id}")
        console.print(f"  {'':22} inputs {entry.portable_identity_fingerprint}")
    console.print(f"\nbuild resolution: {written}")
    console.print(
        "[dim]No tracked file was modified. The image IDs above are local to this "
        "builder and are recorded here rather than committed.[/]"
    )


@app.command("external-matrix")
def external_matrix(
    repetitions: Annotated[
        int,
        typer.Option(
            "--repetitions",
            help="Consecutive runs per target. A result observed once is not stable.",
        ),
    ] = 3,
    manifest_out: Annotated[
        Path, typer.Option("--manifest", help="Where to write the private evidence manifest.")
    ] = Path("artifacts/external/manifest.json"),
    reports: Annotated[Path, typer.Option("--reports")] = Path("artifacts/external"),
    artifacts: Annotated[Path, typer.Option("--artifacts")] = DEFAULT_ARTIFACTS,
    harness_commit: Annotated[str | None, typer.Option("--harness-commit")] = None,
    resolution: ResolutionOption = None,
) -> None:
    """Run every external target under containment and validate that it really ran.

    Exits 1 on any problem: an exit code outside the allowed set, a missing or
    malformed report, a HARNESS_ERROR, a NOT_APPLICABLE with no reason, unusable
    containment, or a classification that changed between repetitions.

    A non-PASS classification against a third-party target is *not* a problem —
    that is a legitimate measurement, and failing the job on it would mean the
    matrix could only stay green while every external implementation stayed
    perfect.
    """
    from interop_lab.external.matrix import build_manifest

    repo_root = Path.cwd()
    resolution_path = resolution or DEFAULT_RESOLUTION_PATH
    try:
        # Loaded here so a missing or stale resolution fails before three
        # targets' worth of runs, rather than as three identical run failures.
        BuildResolutionIndex.load(resolution_path)
    except ImageResolutionError as exc:
        _fail(str(exc))
        return

    manifest = build_manifest(
        repo_root=repo_root,
        reports_dir=reports,
        artifacts_root=artifacts,
        repetitions=repetitions,
        harness_commit=harness_commit,
        resolution=resolution_path,
    )

    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    for target in manifest.targets:
        console.print(
            f"[bold]{target.target_key}[/] {target.language}/{target.transport} "
            f"profile={target.negotiated_profile} "
            f"capabilities={target.capability_status} "
            f"tools={len(target.observed_tools)}"
        )
        console.print(f"    classifications: {target.classifications}")
        console.print(
            f"    exit codes: {[run.exit_code for run in target.runs]} "
            f"stable={target.stable_across_repetitions}"
        )
        if target.declared_but_not_advertised:
            console.print(
                f"    [yellow]declared but not advertised:[/] {target.declared_but_not_advertised}"
            )
        if target.advertised_but_not_declared:
            console.print(
                f"    [yellow]advertised but not declared:[/] {target.advertised_but_not_declared}"
            )

    console.print(f"\nmanifest: {manifest_out}")

    if not manifest.ok:
        console.print("[red]the external matrix did not produce a usable measurement:[/]")
        for problem in manifest.problems:
            console.print(f"  - {problem}")
        raise typer.Exit(code=1)
    console.print("[green]every external target ran contained, and repeated identically[/]")


@app.command("show")
def show(
    report: Annotated[Path, typer.Argument(help="Path to a JSON report.")],
) -> None:
    """Re-render a stored JSON report in the terminal."""
    if not report.is_file():
        _fail(f"report not found: {report}")
    parsed = RunReport.model_validate(json.loads(report.read_text(encoding="utf-8")))
    render_terminal(parsed, console)


if __name__ == "__main__":
    app()
