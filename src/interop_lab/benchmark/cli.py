"""The ``benchmark`` command group.

``plan`` and ``schema`` write committed artifacts; ``run`` and ``compare`` read
them. The split is the point. ``plan`` reads the live repository and writes the
pinned definition, which is then reviewed and checked in — a deliberate act that
shows up as a diff — and ``run`` holds an execution to that committed pin. A
benchmark that regenerated its own definition at the start of each run would
agree with itself by construction and could never detect that a target, a
scenario or a build input had moved.

``compare`` takes two executions from different environments and compares only
what may honestly be compared across machines: identities, never durations.
"""

from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from interop_lab.benchmark.containment_overhead import (
    OVERHEAD_SCENARIOS,
    measure_overhead,
    overhead_timings,
    summarise,
)
from interop_lab.benchmark.coverage import build_coverage
from interop_lab.benchmark.documents import (
    benchmark_junit,
    benchmark_sarif,
    executive_summary,
    limitations_document,
    methodology_document,
)
from interop_lab.benchmark.model import (
    MIN_TIMING_SAMPLES,
    BenchmarkCategory,
    BenchmarkSample,
    MeasurementState,
)
from interop_lab.benchmark.operations import measure_operations
from interop_lab.benchmark.plan import (
    build_definition,
    harness_commit,
    load_definition,
    write_definition,
)
from interop_lab.benchmark.reporting import (
    build_manifest,
    observe_category,
    verify_manifest,
    write_json,
)
from interop_lab.benchmark.reproducibility import compare
from interop_lab.benchmark.runner import (
    TargetExecution,
    as_json,
    execute_target,
    first_actionable_result_ms,
    suite_duration_samples,
    tracked_tree_is_clean,
)
from interop_lab.benchmark.schema import (
    DEFAULT_SCHEMA_PATH,
    build_schema,
    render,
    validate_artifact,
)
from interop_lab.benchmark.validation import check_signature_stability, summarise_timing
from interop_lab.containment.resolution import DEFAULT_RESOLUTION_PATH

benchmark_app = typer.Typer(
    help="MCP Interoperability and Failure-Recovery Benchmark — Private 0.3.",
    no_args_is_help=True,
)
console = Console()


def _docker_facts() -> dict[str, Any]:
    """Engine facts a timing number is meaningless without.

    Recorded rather than assumed, and recorded as ``None`` when the daemon cannot
    be reached — an absent fact is better than a defaulted one that reads as an
    observation.
    """

    def ask(template: str) -> str | None:
        completed = subprocess.run(
            ["docker", "info", "--format", template],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() or None if completed.returncode == 0 else None

    return {
        "docker_engine_version": ask("{{.ServerVersion}}"),
        "storage_driver": ask("{{.Driver}}"),
        "architecture": ask("{{.Architecture}}"),
        "cpus": ask("{{.NCPU}}"),
        "memory_bytes": ask("{{.MemTotal}}"),
        "cgroup_version": ask("{{.CgroupVersion}}"),
    }


def environment_record(runner_identity: str | None) -> dict[str, Any]:
    return {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "runner_identity": runner_identity or "unspecified",
        **_docker_facts(),
        "timing_caveat": (
            "Timing figures are comparable only within this environment. A "
            "GitHub-hosted runner is shared and its timings vary between runs for "
            "reasons that have nothing to do with the code under measurement. "
            "Correctness identities — fingerprints, signatures, classifications — "
            "are comparable across environments; durations are not."
        ),
    }


@benchmark_app.command("plan")
def benchmark_plan(
    out: Annotated[
        Path,
        typer.Option("--out", help="Where to write the pinned definition."),
    ] = Path("benchmarks/definitions/private-0.3.yaml"),
    check: Annotated[
        bool,
        typer.Option("--check", help="Fail if the committed definition is out of date."),
    ] = False,
) -> None:
    """Pin the current repository state as a benchmark definition.

    With ``--check`` nothing is written and the command fails when the committed
    definition disagrees with what the repository would produce now. That is the
    form CI uses: a definition that drifted from the targets and scenarios it
    claims to pin is a definition measuring something it does not describe.
    """
    repo_root = Path.cwd()
    definition = build_definition(repo_root)

    if check:
        if not out.is_file():
            console.print(f"[red]no committed definition at {out}[/]")
            raise typer.Exit(2)
        committed = load_definition(out)
        # The harness commit legitimately differs — the committed definition names
        # the commit it was generated at, and CI runs at a later one. Everything
        # that describes *what is measured* must agree.
        mine = definition.model_dump(mode="json")
        theirs = committed.model_dump(mode="json")
        mine.pop("harness_commit", None)
        theirs.pop("harness_commit", None)
        if mine != theirs:
            differing = sorted(key for key in mine if mine.get(key) != theirs.get(key))
            console.print(
                f"[red]the committed definition is out of date[/]: {differing} differ "
                "from what this repository would pin now. Regenerate it deliberately "
                "with `mcp-failure-lab benchmark plan` and review the diff."
            )
            raise typer.Exit(1)
        console.print(f"[green]definition matches the repository[/] ({out})")
        return

    write_definition(definition, out)
    console.print(f"[green]wrote[/] {out}")
    console.print(
        f"  {len(definition.targets)} targets, {len(definition.scenarios)} scenarios, "
        f"profiles {definition.profiles}, transports {definition.transports}"
    )


@benchmark_app.command("schema")
def benchmark_schema(
    out: Annotated[
        Path,
        typer.Option("--out", help="Where the generated JSON Schema lives."),
    ] = DEFAULT_SCHEMA_PATH,
    check: Annotated[
        bool,
        typer.Option("--check", help="Fail if the committed schema is out of date."),
    ] = False,
) -> None:
    """Emit the benchmark JSON Schema from the models that define it.

    Generated rather than hand-written, because a schema someone typed alongside a
    model drifts from it silently — and the first thing anyone learns is that
    validation passed against a shape the code no longer produces. ``--check`` is
    what makes that drift a build failure instead of a surprise.
    """
    rendered = render(build_schema())

    if check:
        if not out.is_file():
            console.print(f"[red]no committed schema at {out}[/]")
            raise typer.Exit(2)
        if out.read_text(encoding="utf-8") != rendered:
            console.print(
                f"[red]{out} disagrees with the models[/]. Regenerate it with "
                "`mcp-failure-lab benchmark schema` and review the diff."
            )
            raise typer.Exit(1)
        console.print(f"[green]schema matches the models[/] ({out})")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    console.print(f"[green]wrote[/] {out}")


@benchmark_app.command("compare")
def benchmark_compare(
    first: Annotated[Path, typer.Option("--a", help="First environment's output directory.")],
    second: Annotated[Path, typer.Option("--b", help="Second environment's output directory.")],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Where to write the comparison."),
    ] = None,
) -> None:
    """Compare two executions from different environments.

    Identities only. Timing values are never compared across machines, and a
    builder-local image ID differing between storage drivers is expected rather
    than drift — cross-environment agreement rests on the portable build inputs.
    """
    comparison = compare(first, second)
    if out is not None:
        write_json(out, comparison.model_dump(mode="json"))
        console.print(f"[green]wrote[/] {out}")

    a, b = comparison.environment_a, comparison.environment_b
    console.print(f"A: {a.identity} ({a.storage_driver}, docker {a.docker_engine_version})")
    console.print(f"B: {b.identity} ({b.storage_driver}, docker {b.docker_engine_version})")
    for expected in comparison.expected_differences:
        console.print(f"[yellow]expected difference[/] {expected}")

    if comparison.agrees:
        console.print(
            "[green]the two environments measured the same thing[/]: target and "
            "scenario fingerprints, negotiated profiles, capability fingerprints, "
            "classifications and result signatures all agree"
        )
        return

    console.print(f"[red]{len(comparison.disagreements)} disagreement(s)[/]")
    for entry in comparison.disagreements[:20]:
        console.print(f"  {entry}")
    raise typer.Exit(1)


def _build_resolution_entries(resolution: Path) -> list[dict[str, Any]]:
    """The portable half of each build entry, plus what the build took.

    Deliberately not the whole manifest. ``observed_local_image_id`` is this
    builder's identifier for these bits and means nothing on another machine, so
    carrying it into the results beside a duration would invite a comparison
    across environments that the value cannot support. What is carried is what
    travels: the portable identity fingerprint, the status, and the duration.

    Returns an empty list rather than raising when the manifest cannot be read —
    the coverage ledger then reports the build metric NOT_MEASURED, which is the
    honest reading of a build nobody can see.
    """
    from interop_lab.containment.resolution import BuildResolutionManifest

    if not resolution.is_file():
        return []
    try:
        manifest = BuildResolutionManifest.model_validate_json(
            resolution.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    return [
        {
            "target_id": entry.target_id,
            "build_key": entry.build_key,
            "portable_identity_fingerprint": entry.portable_identity_fingerprint,
            "build_status": entry.build_status.value,
            "build_duration_ms": entry.build_duration_ms,
        }
        for entry in manifest.entries
    ]


def _category_samples(
    executions: list[TargetExecution], category: BenchmarkCategory, target_key: str | None = None
) -> list[BenchmarkSample]:
    return [
        sample
        for execution in executions
        for sample in execution.correctness_samples
        if sample.category is category and (target_key is None or sample.target_key == target_key)
    ]


@benchmark_app.command("run")
def benchmark_run(
    definition_path: Annotated[
        Path,
        typer.Option("--definition", help="The committed benchmark definition."),
    ] = Path("benchmarks/definitions/private-0.3.yaml"),
    out: Annotated[
        Path,
        typer.Option("--out", help="Private output directory."),
    ] = Path("benchmarks/private-results/0.3"),
    resolution: Annotated[
        Path,
        typer.Option("--resolution", help="Build resolution from `external-build`."),
    ] = DEFAULT_RESOLUTION_PATH,
    work_dir: Annotated[
        Path,
        typer.Option("--work", help="Where intermediate reports are written."),
    ] = Path("artifacts/benchmark"),
    runner_identity: Annotated[
        str | None,
        typer.Option("--runner", help="What machine this is, for the environment record."),
    ] = None,
) -> None:
    """Execute the pinned definition and write the private benchmark outputs."""
    repo_root = Path.cwd()
    definition = load_definition(definition_path)
    problems: list[str] = []

    if definition.environment.tracked_tree_must_be_clean and not tracked_tree_is_clean(repo_root):
        console.print(
            "[red]the tracked tree is dirty[/]. A benchmark measures a commit; a dirty "
            "tree means the results would describe something the commit does not."
        )
        raise typer.Exit(2)

    if definition.environment.build_resolution_required and not resolution.is_file():
        console.print(
            f"[red]no build resolution at {resolution}[/]. Every external target is built "
            "here, so the identity of the image to run exists nowhere else. Run "
            "`mcp-failure-lab external-build` first."
        )
        raise typer.Exit(2)

    out.mkdir(parents=True, exist_ok=True)
    scenarios = {scenario.scenario_id: scenario for scenario in definition.scenarios}

    executions: list[TargetExecution] = []
    for target in definition.targets:
        console.print(f"[cyan]{target.target_key}[/] — {target.language} / {target.transport}")
        execution = execute_target(
            target,
            definition=definition,
            scenarios=scenarios,
            repo_root=repo_root,
            work_dir=work_dir,
            resolution=resolution,
        )
        executions.append(execution)
        problems.extend(execution.problems)

    # ---------------------------------------------------------------- signatures
    signature_problems: list[str] = []
    for execution in executions:
        by_scenario: dict[str, list[BenchmarkSample]] = {}
        for sample in execution.correctness_samples:
            by_scenario.setdefault(sample.scenario_id or "", []).append(sample)
        for repetitions in by_scenario.values():
            signature_problems.extend(
                f"{execution.target.target_key}: {problem}"
                for problem in check_signature_stability(repetitions)
            )
    problems.extend(signature_problems)

    # ---------------------------------------------------------------- artifacts
    write_json(out / "benchmark-definition.json", definition.model_dump(mode="json"))
    write_json(out / "environment.json", environment_record(runner_identity))
    write_json(
        out / "build-resolution.json",
        as_json(
            {
                "source": str(resolution),
                "present": resolution.is_file(),
                "note": (
                    "The builder-local image IDs inside are identity on this builder "
                    "only. Cross-environment agreement is asserted over the portable "
                    "build inputs, never over these."
                ),
                # The build durations the builder already recorded, carried through
                # so an image build is a visible measurement rather than a number
                # that exists only in a file nobody reads beside the results. One
                # per target, no warm-up, and the builder does not record whether
                # the layer cache was cold — which is exactly why the coverage
                # ledger reports this partially measured and quotes no median.
                "entries": _build_resolution_entries(resolution),
                "duration_note": (
                    "One build per target, timed by the builder on a monotonic "
                    "clock. No warm-up, no repetition, and no record of cache "
                    "state, so these are individual durations and never a median."
                ),
            }
        ),
    )
    write_json(
        out / "target-matrix.json",
        as_json([target.model_dump(mode="json") for target in definition.targets]),
    )
    write_json(
        out / "scenario-matrix.json",
        as_json([scenario.model_dump(mode="json") for scenario in definition.scenarios]),
    )

    applicability: list[dict[str, Any]] = []
    classification_results: list[dict[str, Any]] = []
    capability_results: list[dict[str, Any]] = []
    containment_results: list[dict[str, Any]] = []
    signatures: list[dict[str, Any]] = []
    for execution in executions:
        key = execution.target.target_key
        capability_results.append(
            {
                "target_key": key,
                "capability_fingerprint": execution.capability_fingerprint,
                # The surface itself, not only its fingerprint. A fingerprint
                # records that something was observed; without the record beside
                # it a reader cannot see what the target actually advertised, and
                # cannot tell a declared tool list from an observed one.
                "capabilities": as_json(execution.capability_record),
                "note": (
                    "One surface, observed from the first repetition and held to by "
                    "every later sample. A surface that moved mid-execution rejects "
                    "the samples on both sides of the move."
                ),
            }
        )
        containment_results.append(
            {
                "target_key": key,
                "containment": as_json(execution.containment_record),
                "note": (
                    "Requested and observed are recorded separately and are never "
                    "merged. A requested restriction that the runtime did not apply "
                    "is the case this pair exists to make visible."
                ),
            }
        )
        for sample in execution.correctness_samples:
            applicability.append(
                {
                    "target_key": key,
                    "scenario_id": sample.scenario_id,
                    "repetition": sample.repetition,
                    "classification": (
                        sample.classification.value if sample.classification else None
                    ),
                    "applicable": sample.classification is not None
                    and sample.classification.value != "NOT_APPLICABLE",
                    "valid_sample": sample.valid,
                    "rejections": [r.value for r in sample.rejections],
                }
            )
            classification_results.append(
                {
                    "target_key": key,
                    "scenario_id": sample.scenario_id,
                    "repetition": sample.repetition,
                    "classification": (
                        sample.classification.value if sample.classification else None
                    ),
                    "dimensions": sample.dimensions.model_dump(mode="json"),
                    "valid_sample": sample.valid,
                    "rejection_detail": sample.rejection_detail,
                }
            )
            if sample.result_signature is not None:
                signatures.append(
                    {
                        "target_key": key,
                        "scenario_id": sample.scenario_id,
                        "repetition": sample.repetition,
                        "digest": sample.result_signature_digest,
                        "signature": sample.result_signature.model_dump(mode="json"),
                    }
                )

    write_json(out / "applicability.json", applicability)
    write_json(out / "classification-results.json", classification_results)
    write_json(out / "capability-results.json", capability_results)
    write_json(out / "containment-results.json", containment_results)
    write_json(out / "result-signatures.json", signatures)

    # ------------------------------------------------------------------ timings
    timings: list[dict[str, Any]] = []
    for execution in executions:
        key = execution.target.target_key
        suite = summarise_timing(
            f"{key}: applicable suite runtime",
            suite_duration_samples(execution),
            minimum_samples=MIN_TIMING_SAMPLES,
            limitations=[
                "Summed per-scenario durations, excluding harness startup and "
                "dependency resolution. Image build time is never included here.",
            ],
        )
        timings.append(suite.model_dump(mode="json"))

        first = first_actionable_result_ms(
            [s for s in execution.correctness_samples if s.repetition == 1]
        )
        timings.append(
            {
                "name": f"{key}: time to first actionable result",
                "unit": "ms",
                "state": (
                    MeasurementState.MEASURED.value
                    if first is not None
                    else MeasurementState.NOT_MEASURED.value
                ),
                "value_ms": first,
                "limitations": [
                    "Cumulative scenario time to the first decided non-pass, in suite "
                    "order. A property of this suite's ordering, not of the target.",
                    "Absent when the battery reached no actionable result at all, which "
                    "is a legitimate outcome and not a zero.",
                ],
            }
        )
    # ------------------------------------------------------ containment overhead
    #
    # Measured with the implementation and the scenario held constant, using
    # first-party fixtures. Never inferred by comparing two external targets: a Go
    # binary in a scratch image and a Node server are different programs, and the
    # gap between them is not the cost of a container.
    overhead_pairs = measure_overhead(
        repo_root=repo_root,
        work_dir=work_dir / "overhead",
        repetitions=definition.timing_measured_runs,
        warmups=definition.timing_warmup_runs,
        timeout_s=definition.timing_bounds.per_sample_timeout_s,
    )
    write_json(
        out / "containment-overhead.json",
        {
            "environment": environment_record(runner_identity),
            "scenarios_held_constant": list(OVERHEAD_SCENARIOS),
            "scenario_note": (
                "Held constant within each pair, which is the comparison. Across pairs "
                "it varies because baseline.tools_list is stdio-only and "
                "baseline.http_tools_list is HTTP-only; two pairs are never compared."
            ),
            "pairs": [
                summarise(pair, minimum_samples=MIN_TIMING_SAMPLES) for pair in overhead_pairs
            ],
        },
    )
    timings.extend(
        summary.model_dump(mode="json")
        for summary in overhead_timings(overhead_pairs, minimum_samples=MIN_TIMING_SAMPLES)
    )
    write_json(out / "timings.json", timings)

    # ------------------------------------------------- directly measured operations
    #
    # The boundaries that exist and had simply never been timed: a resolver call,
    # a session teardown, a serialisation, a gate evaluation, a minimisation, a
    # replay. All against first-party fixtures, so none of these figures is a
    # statement about anyone else's implementation. The dimensions with no
    # boundary — target startup, first valid request — are absent here on purpose
    # and appear in the coverage ledger as NOT_MEASURED with their reason.
    write_json(
        out / "measured-operations.json",
        measure_operations(
            repo_root=repo_root,
            work_dir=work_dir / "operations",
            repetitions=definition.timing_measured_runs,
            warmups=definition.timing_warmup_runs,
        ).model_dump(mode="json"),
    )

    # ------------------------------------------------------ reproduction results
    #
    # Cross-environment agreement is asserted over identities and never over
    # durations. The second environment is recorded as NOT_MEASURED unless one was
    # actually run — fabricating agreement from a single machine would be the most
    # damaging thing this benchmark could do.
    write_json(
        out / "reproduction-results.json",
        {
            "environment_a": {
                "identity": runner_identity or "unspecified",
                "state": MeasurementState.MEASURED.value,
                "compared": [
                    "target fingerprints",
                    "scenario fingerprints",
                    "negotiated profiles",
                    "capability fingerprints",
                    "result signatures",
                    "classifications",
                    "containment outcomes",
                ],
            },
            "environment_b": {
                "state": MeasurementState.NOT_MEASURED.value,
                "reason": (
                    "This execution ran in one environment. Cross-environment "
                    "reproducibility is reported as NOT_MEASURED rather than inferred "
                    "from a single machine."
                ),
            },
            "expected_local_image_id_differences": (
                "Builder-local image IDs differ between storage drivers for identical "
                "bits and are NOT target drift. Agreement across environments is "
                "asserted over the portable build inputs — source archive, dependency "
                "lock, Dockerfile, base image digests, toolchain pins."
            ),
            "timing_note": "Timing values are never compared across machines.",
            "per_target": [
                {
                    "target_key": target.target_key,
                    "target_fingerprint": target.target_fingerprint,
                    "portable_identity_fingerprint": target.portable_identity_fingerprint,
                    "dockerfile_sha256": target.dockerfile_sha256,
                    "lock_sha256": target.lock_sha256,
                    "source_archive_sha256": target.source_archive_sha256,
                }
                for target in definition.targets
            ],
        },
    )

    # --------------------------------------------------- conformance boundary
    write_json(
        out / "conformance-boundary.json",
        {
            "note": (
                "Recorded separately and never merged into a Failure Lab "
                "classification. An outcome from the official framework is a statement "
                "by that framework; folding it into this vocabulary would attribute its "
                "verdict to this project."
            ),
            "per_target": [
                {
                    "target_key": target.target_key,
                    "official_conformance": target.official_conformance.value,
                    "detail": target.official_conformance_detail,
                    "conformance_package_version": None,
                    "source_sha": None,
                    "requirements_revision": None,
                    "scenarios_executed": [],
                    "outcome": None,
                }
                for target in definition.targets
            ],
        },
    )

    # -------------------------------------------------------- category observations
    observations: list[dict[str, Any]] = []
    for category in BenchmarkCategory:
        for execution in executions:
            key = execution.target.target_key
            samples = _category_samples(executions, category, target_key=key)
            if not samples:
                continue
            observations.append(
                observe_category(category, samples, target_key=key).model_dump(mode="json")
            )
    write_json(out / "category-observations.json", observations)

    # ------------------------------------------------- measurement coverage ledger
    #
    # Written last of the data artifacts, because every state in it is derived
    # from a file this execution already wrote. A metric is measured here because
    # samples exist on disk for it, never because the methodology promised it —
    # which is the whole point: the ledger exists to make the gap between what
    # was promised and what was measured visible rather than absent.
    #
    # Recorded as a problem rather than raised. A ledger that cannot be built is
    # worth knowing about, and it is not worth losing an hour of correctness
    # results to — the samples behind it are already on disk either way.
    try:
        coverage = build_coverage(out)
        write_json(out / "measurement-coverage.json", coverage.model_dump(mode="json"))
    except Exception as exc:  # the correctness results outlive this ledger
        problems.append(
            f"measurement-coverage.json: the ledger could not be built from the "
            f"artifacts this run wrote ({type(exc).__name__}: {exc}). Every metric's "
            "state is therefore unknown, which is itself the gap the ledger exists "
            "to report."
        )

    # ------------------------------------------------------- integrity reporting
    #
    # JUnit reports the *execution integrity* of the benchmark — whether each
    # sample was valid — rather than the targets' conformance. A dashboard that
    # showed a rejected sample as green would defeat the whole rejection design.
    all_samples = [sample for execution in executions for sample in execution.correctness_samples]
    (out / "junit.xml").write_text(
        benchmark_junit(all_samples, suite_name=definition.benchmark_name), encoding="utf-8"
    )
    write_json(out / "sarif.json", benchmark_sarif(all_samples, definition=definition))

    # --------------------------------------------------------------- documents
    for name, body in (
        ("methodology.md", methodology_document()),
        ("limitations.md", limitations_document(definition)),
        ("executive-summary.md", executive_summary(definition, observations, problems)),
    ):
        (out / name).write_text(body, encoding="utf-8")

    # ----------------------------------------------------------- schema validation
    schema = build_schema()
    for relative, model_name in (
        ("benchmark-definition.json", "BenchmarkDefinition"),
        ("manifest.json", "BenchmarkOutputs"),
        ("measurement-coverage.json", "MeasurementCoverage"),
        ("measured-operations.json", "MeasuredOperations"),
    ):
        path = out / relative
        if not path.is_file():
            continue
        violations = validate_artifact(
            json.loads(path.read_text(encoding="utf-8")), model_name, schema
        )
        problems.extend(f"{relative}: {violation}" for violation in violations)
    for observation in observations:
        problems.extend(
            f"category-observations.json: {violation}"
            for violation in validate_artifact(observation, "CategoryObservation", schema)
        )

    outputs = build_manifest(
        out,
        definition=definition,
        # The commit this execution ran at, read from the working tree that
        # produced these outputs — never the one the definition was pinned at.
        harness_commit=harness_commit(repo_root),
        problems=problems,
    )
    write_json(out / "manifest.json", outputs.model_dump(mode="json"))

    mismatches = verify_manifest(out, outputs)
    if mismatches:
        console.print("[red]manifest does not describe what is on disk[/]")
        for entry in mismatches:
            console.print(f"  {entry}")
        raise typer.Exit(3)

    console.print(f"[green]wrote[/] {len(outputs.artifacts)} artifacts to {out}")
    if problems:
        console.print(f"[yellow]{len(problems)} problem(s) recorded in the manifest[/]")
        for entry in problems[:10]:
            console.print(f"  {entry}")
        raise typer.Exit(1)
    console.print(
        f"[green]clean[/] — {len(definition.targets)} targets, "
        f"{len(definition.scenarios)} scenarios, "
        f"{definition.correctness_repetitions} correctness repetitions, "
        f"{definition.timing_measured_runs} measured timing samples"
    )
