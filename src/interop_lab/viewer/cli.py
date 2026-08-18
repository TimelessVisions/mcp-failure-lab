"""The ``viewer`` command group: export, scan, fixture, schema.

``export`` is the only way a dataset is produced, and it refuses more often than
it succeeds. A benchmark directory that does not verify, a scenario library that
has moved since the run, a public-safe export from real third-party evidence
without recorded owner approval, a redacted document that still contains a
denylisted term — every one of those stops the export rather than producing a
document with a caveat attached.

That strictness is the feature. A viewer whose export succeeds under doubt is a
viewer nobody can point at and say "this is what was measured".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console

from interop_lab.viewer.export import ViewerExportError, build_dataset
from interop_lab.viewer.fixture import is_synthetic, write_fixture
from interop_lab.viewer.model import EvidenceKind, ViewerDataset, VisibilityMode
from interop_lab.viewer.redaction import (
    LeakFinding,
    build_denylist,
    scan_payload,
    scan_tree,
    to_public_safe,
)
from interop_lab.viewer.schema import DEFAULT_SCHEMA_PATH, build_schema, render
from interop_lab.viewer.source import BenchmarkSourceError, load_source

viewer_app = typer.Typer(
    help="Export and verify the static evidence viewer's dataset.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_FIXTURE = Path("artifacts/viewer-fixture")


def _fail(lines: list[str], *, code: int = 1) -> None:
    for line in lines:
        console.print(f"  [red]·[/] {line}")
    raise typer.Exit(code)


def _write_dataset(dataset: ViewerDataset, out: Path) -> Path:
    """Write the dataset the frontend loads, deterministically.

    Sorted keys and a trailing newline, so two exports of the same execution
    produce identical bytes and a diff between two builds means a difference in
    the evidence rather than in the serialiser.
    """
    out.mkdir(parents=True, exist_ok=True)
    path = out / "dataset.json"
    path.write_text(
        json.dumps(dataset.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _report_leaks(findings: list[LeakFinding]) -> None:
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.term, finding.location)
        if key in seen:
            continue
        seen.add(key)
        console.print(f"  [red]{finding.term}[/] at {finding.location} (from {finding.origin})")


@viewer_app.command("export")
def viewer_export(
    benchmark: Annotated[
        Path,
        typer.Option("--benchmark", help="A benchmark output directory."),
    ],
    mode: Annotated[
        str,
        typer.Option("--mode", help="private or public-safe."),
    ] = "private",
    out: Annotated[
        Path,
        typer.Option("--out", help="Where the dataset is written."),
    ] = Path("viewer/data/private"),
    approved_anonymization: Annotated[
        str | None,
        typer.Option(
            "--approved-anonymization",
            help=(
                "Owner approval reference for anonymised labels standing in for real "
                "third-party identities. Required for a public-safe export from "
                "non-synthetic evidence."
            ),
        ),
    ] = None,
) -> None:
    """Export one benchmark execution as a viewer dataset."""
    repo_root = Path.cwd()
    try:
        visibility = VisibilityMode(mode)
    except ValueError:
        _fail([f"unknown mode {mode!r}; expected 'private' or 'public-safe'"], code=2)
        return

    try:
        source = load_source(benchmark)
    except BenchmarkSourceError as exc:
        console.print(f"[red]{benchmark} does not describe itself completely[/]")
        _fail(exc.problems, code=2)
        return

    try:
        dataset = build_dataset(source, repo_root=repo_root)
    except ViewerExportError as exc:
        console.print("[red]the benchmark and this harness disagree about what was run[/]")
        _fail(exc.problems, code=2)
        return

    denylist = build_denylist(repo_root, benchmark_output=benchmark)

    if visibility is VisibilityMode.PUBLIC_SAFE:
        synthetic = is_synthetic(benchmark)
        if not synthetic and not approved_anonymization:
            _fail(
                [
                    "this benchmark output is not synthetic fixture evidence, and no "
                    "owner approval for anonymised labels was supplied.",
                    "Replacing a name with 'Implementation A' does not make a row "
                    "anonymous: language, transport and result narrow the field in "
                    "combination.",
                    "Either export from a synthetic fixture "
                    "(`mcp-failure-lab viewer fixture`) or pass "
                    "--approved-anonymization with the approval reference.",
                ],
                code=3,
            )
            return
        dataset = to_public_safe(
            dataset,
            anonymization_approved=bool(approved_anonymization),
            approval_reference=approved_anonymization
            or ("synthetic fixture evidence: no third party to anonymise" if synthetic else None),
        )
        findings = scan_payload(dataset.model_dump(mode="json"), denylist)
        if findings:
            console.print(
                f"[red]the public-safe dataset still contains {len(findings)} "
                "denylisted term(s)[/]. The redaction has a gap; the export is refused."
            )
            _report_leaks(findings)
            raise typer.Exit(4)
        dataset = dataset.model_copy(
            update={
                "disclosure_controls": dataset.disclosure_controls.model_copy(
                    update={"denylist_term_count": len(denylist.terms)}
                )
            }
        )

    path = _write_dataset(dataset, out)
    console.print(f"[green]wrote[/] {path}")
    console.print(
        f"  mode {dataset.visibility_mode.value} · "
        f"{len(dataset.targets)} targets · {len(dataset.scenarios)} scenarios · "
        f"{len(dataset.matrix)} cells · {len(dataset.timings)} timing cards"
    )
    counts = dataset.measurement_coverage.counts
    console.print(
        "  measurement coverage: "
        + " · ".join(f"{state} {count}" for state, count in counts.items())
    )
    if dataset.visibility_mode is VisibilityMode.PRIVATE:
        console.print(
            "  [yellow]private[/] — owner-authorized local review only. Never bundle "
            "this into a public build and never deploy it."
        )


@viewer_app.command("scan")
def viewer_scan(
    path: Annotated[
        Path,
        typer.Argument(help="A dataset file, or a built bundle directory."),
    ],
    benchmark: Annotated[
        Path | None,
        typer.Option("--benchmark", help="A benchmark output whose targets extend the denylist."),
    ] = None,
) -> None:
    """Scan a dataset or a built bundle against the private denylist.

    Run against the *built* output, not only the dataset. The dataset is the file
    the redaction produced and the bundle is what would actually be served, and
    the gap between them — an inlined constant, a source map, a stray fixture —
    is exactly where a leak survives a clean dataset scan.
    """
    repo_root = Path.cwd()
    denylist = build_denylist(repo_root, benchmark_output=benchmark)

    if path.is_dir():
        findings = scan_tree(path, denylist)
        scanned = f"{sum(1 for p in path.rglob('*') if p.is_file())} file(s) under {path}"
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _fail([f"{path}: {exc}"], code=2)
            return
        findings = scan_payload(payload, denylist)
        scanned = str(path)

    if findings:
        console.print(f"[red]{len(findings)} denylisted term(s) in {scanned}[/]")
        _report_leaks(findings)
        raise typer.Exit(1)
    console.print(
        f"[green]clean[/] — {scanned} contains none of the {len(denylist.terms)} denylisted terms"
    )


#: What a private dataset built from real measurements has to contain before it
#: is worth presenting. Each entry is a name and a question answered against the
#: loaded document — never against the benchmark directory, because the point is
#: to check what the viewer would actually show.
_PRIVATE_EXPECTATIONS: tuple[str, ...] = (
    "target identities",
    "languages",
    "transports",
    "source versions",
    "source identities",
    "capability fingerprints",
    "classification results",
    "result signatures",
    "containment evidence",
    "timing evidence",
    "measurement coverage",
)


def _audit_private(dataset: ViewerDataset) -> tuple[dict[str, str], list[str]]:
    """Describe a private dataset's real content, and say what is missing.

    Returns what was found and what was expected and absent. An absence is
    reported rather than raised over, because "this execution measured no
    containment" and "this document is broken" are different statements and only
    the caller knows which one matters.
    """
    targets = dataset.targets
    found: dict[str, str] = {}
    missing: list[str] = []

    identified = [target for target in targets if target.target_id]
    found["target identities"] = (
        ", ".join(f"{t.display_name} ({t.target_id})" for t in identified) or "none"
    )
    if not identified:
        missing.append("target identities")

    languages = sorted({target.language for target in targets if target.language})
    found["languages"] = ", ".join(languages) or "none"
    if not languages:
        missing.append("languages")

    transports = sorted({target.transport for target in targets if target.transport})
    found["transports"] = ", ".join(transports) or "none"
    if not transports:
        missing.append("transports")

    versions = [t.source_version for t in targets if t.source_version]
    found["source versions"] = ", ".join(versions) or "none"
    if not versions:
        missing.append("source versions")

    # A version alone does not locate a build. The commit or the build context is
    # what a second machine would rebuild from.
    sources: list[str] = []
    for target in targets:
        identity = target.source_commit or target.build_context
        if identity:
            sources.append(identity)
    found["source identities"] = ", ".join(sources) or "none"
    if not sources:
        missing.append("source identities")

    fingerprints = [t.capability_fingerprint for t in targets if t.capability_fingerprint]
    found["capability fingerprints"] = f"{len(fingerprints)} of {len(targets)}"
    if not fingerprints:
        missing.append("capability fingerprints")

    classified = [cell for cell in dataset.matrix if cell.state]
    states = sorted({cell.state.value for cell in classified})
    found["classification results"] = f"{len(classified)} cells · {', '.join(states)}"
    if not classified:
        missing.append("classification results")

    found["result signatures"] = str(len(dataset.result_signatures))
    if not dataset.result_signatures:
        missing.append("result signatures")

    found["containment evidence"] = str(len(dataset.containment))
    if not dataset.containment:
        missing.append("containment evidence")

    measured = [card for card in dataset.timings if card.samples_ms]
    found["timing evidence"] = f"{len(measured)} measured of {len(dataset.timings)} cards"
    if not measured:
        missing.append("timing evidence")

    coverage = dataset.measurement_coverage
    found["measurement coverage"] = " · ".join(
        f"{state} {count}" for state, count in coverage.counts.items()
    )
    if not coverage.entries:
        missing.append("measurement coverage")

    # Reported, never required: private reproduction summaries exist only where
    # a finding was worked up and approved, and an execution with none is a fact
    # about the execution.
    found["reproduction summaries"] = str(len(dataset.reproduction))

    return found, missing


@viewer_app.command("audit")
def viewer_audit(
    dataset_path: Annotated[
        Path,
        typer.Argument(help="A dataset.json produced by `viewer export`."),
    ],
    require_private: Annotated[
        bool,
        typer.Option(
            "--require-private",
            help=(
                "Fail unless this is a private dataset carrying real third-party "
                "identities. Used by the private-viewer job to prove it built from "
                "a real benchmark artifact rather than from the synthetic fixture."
            ),
        ),
    ] = False,
) -> None:
    """Describe what a dataset actually contains, and hold it to what it claims.

    Separate from `scan`, which asks whether a public document leaked something.
    This asks the opposite question of a private one: is the real evidence
    actually here, or has something upstream quietly produced an empty shape
    that renders without error?
    """
    try:
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail([f"{dataset_path}: {exc}"], code=2)
        return

    try:
        dataset = ViewerDataset.model_validate(payload)
    except ValidationError as exc:
        console.print(f"[red]{dataset_path} is not a valid viewer dataset[/]")
        _fail([str(exc)], code=2)
        return

    console.print(f"[bold]{dataset_path}[/]")
    console.print(f"  visibility        {dataset.visibility_mode.value}")
    console.print(f"  evidence          {dataset.provenance.evidence_kind.value}")
    console.print(f"  benchmark         {dataset.benchmark.benchmark_name or '(withheld)'}")
    console.print(f"  manifest digest   {dataset.generated_from_benchmark_manifest_hash}")

    found, missing = _audit_private(dataset)
    for name, value in found.items():
        console.print(f"  {name:<24}{value}")

    if not require_private:
        console.print("[green]audited[/]")
        return

    problems: list[str] = []
    if dataset.visibility_mode is not VisibilityMode.PRIVATE:
        problems.append(
            f"this dataset is {dataset.visibility_mode.value}, and --require-private "
            "was asked for. A public-safe document has had exactly these fields removed."
        )
    if dataset.provenance.evidence_kind is not EvidenceKind.REAL_MEASUREMENT:
        problems.append(
            "this dataset was built from synthetic fixture evidence. The private "
            "viewer path is only proven when it has consumed a real benchmark artifact."
        )
    problems += [
        f"{name}: expected in a private dataset built from real measurements, and absent"
        for name in missing
        if name in _PRIVATE_EXPECTATIONS
    ]

    if problems:
        console.print(f"[red]{len(problems)} problem(s)[/]")
        _fail(problems)
        return
    console.print(
        "[green]real private evidence[/] — every expected dimension is present and "
        "carries third-party identities"
    )


@viewer_app.command("fixture")
def viewer_fixture(
    out: Annotated[
        Path,
        typer.Option("--out", help="Where the synthetic benchmark output is written."),
    ] = DEFAULT_FIXTURE,
) -> None:
    """Write a synthetic benchmark execution with no real subject.

    Used by CI and by the frontend tests. Everything in it is invented, which is
    what makes it safe to build and upload — and it is a real benchmark output
    rather than a mock, so the exporter's verification runs against it in full.
    """
    path = write_fixture(out, repo_root=Path.cwd())
    console.print(f"[green]wrote[/] a synthetic benchmark execution to {path}")
    console.print(
        "  three invented implementations, this repository's real scenario library, "
        "outcomes assigned deterministically"
    )


@viewer_app.command("schema")
def viewer_schema(
    out: Annotated[
        Path,
        typer.Option("--out", help="Where the generated JSON Schema lives."),
    ] = DEFAULT_SCHEMA_PATH,
    check: Annotated[
        bool,
        typer.Option("--check", help="Fail if the committed schema is out of date."),
    ] = False,
) -> None:
    """Emit the viewer dataset's JSON Schema from the models that define it."""
    rendered = render(build_schema())
    if check:
        if not out.is_file():
            _fail([f"no committed schema at {out}"], code=2)
            return
        if out.read_text(encoding="utf-8") != rendered:
            _fail(
                [
                    f"{out} disagrees with the models. Regenerate it with "
                    "`mcp-failure-lab viewer schema` and review the diff."
                ]
            )
            return
        console.print(f"[green]schema matches the models[/] ({out})")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    console.print(f"[green]wrote[/] {out}")
