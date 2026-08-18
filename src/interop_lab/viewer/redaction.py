"""Producing the public-safe dataset, and proving it is one.

Two ideas carry this module, and the second is the one that matters.

**Redaction is mechanical.** Every field that can name a third party is removed
or replaced, by name, in one place. Nothing relies on a component happening not
to render a value.

**Anonymisation is not assumed to be sufficient.** Replacing a repository name
with "Implementation A" does not make a row anonymous. Language, exact version,
transport and result re-identify a target in combination far more often than
people expect — there are not many independent Go MCP servers — so the public-safe
transform drops the version, the commit, every fingerprint, every build hash and
every observed tool name, and the export still refuses unless the owner has
explicitly approved anonymised labels standing in for real identities.

After the transform, the produced document is scanned against a denylist
generated from the private inventories. A surviving term refuses the export. That
check exists precisely because the transform above is a list of fields someone
maintains by hand, and a list maintained by hand acquires a gap.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from interop_lab.viewer.export import (
    ANONYMISED_DISCLAIMER,
    PUBLIC_SAFE_ANONYMISED_EYEBROW,
    PUBLIC_SAFE_EYEBROW,
)
from interop_lab.viewer.model import (
    CapabilitySurface,
    ContainmentEvidence,
    DatasetProvenance,
    DisclosureControls,
    EvidenceKind,
    ReproductionSummary,
    SignatureRecord,
    TimingCard,
    ViewerDataset,
    ViewerTarget,
    VisibilityMode,
)

#: Terms shorter than this are not scanned for. A three-character fragment
#: matches inside unrelated words and turns the leak scan into noise that people
#: learn to ignore, which is worse than not running it.
MINIMUM_TERM_LENGTH = 5

#: Tokens that appear in private inventories and are not identifying on their
#: own. Every one is a path fragment or a file name this repository publishes
#: about itself; the rest of the exclusions are computed, not listed.
NON_IDENTIFYING: frozenset[str] = frozenset(
    {
        "targets",
        "external",
        "docker",
        "dockerfile",
        "package-lock.json",
        "requirements.lock.txt",
        "go.sum",
        # Implementation languages. Retained deliberately rather than scrubbed:
        # the applicability rules cannot be read without knowing which language
        # and transport a row is, and a matrix that hid them would be
        # uninterpretable. They do narrow the field, which is why every
        # public-safe target carries that as a stated limitation instead of the
        # document pretending the row is unidentifiable.
        "python",
        "typescript",
        "javascript",
        "golang",
        "rust",
    }
)


def _harness_vocabulary() -> frozenset[str]:
    """Every string this harness uses to describe itself.

    Computed rather than listed, and the distinction matters. A target file is
    mostly this project's own vocabulary — `stdio`, `container`, `server`,
    `not_measured`, `mcp-2025-11-25` — and a denylist that collected those would
    match its own methodology prose on every scan. People respond to a scanner
    that cries wolf by not reading it, which is a worse outcome than not having
    one.

    Derived from the enums and the scenario library themselves, so a value added
    to either is excluded automatically instead of joining a hand-maintained list
    that nobody updates.
    """
    from enum import StrEnum

    from interop_lab import (
        benchmark,
        capabilities,
        containment,
        models,
        profiles,
        results,
    )

    vocabulary: set[str] = set()
    modules = [
        "interop_lab.models.core",
        "interop_lab.models.dimensions",
        "interop_lab.models.severity",
        "interop_lab.containment.model",
        "interop_lab.benchmark.model",
        "interop_lab.benchmark.coverage",
        "interop_lab.capabilities.model",
        "interop_lab.profiles.registry",
        "interop_lab.results.signature",
        "interop_lab.replay.repro",
        "interop_lab.scenarios.model",
    ]
    del benchmark, capabilities, containment, models, profiles, results

    import importlib

    for name in modules:
        try:
            module = importlib.import_module(name)
        except ImportError:  # pragma: no cover - every module here is in-tree
            continue
        for attribute in vars(module).values():
            if isinstance(attribute, type) and issubclass(attribute, StrEnum):
                vocabulary.update(member.value for member in attribute)

    # This harness's own containment defaults. `65534:65534` is the nobody user
    # every contained run requests; it appears in target files because this
    # project put it there, and it identifies nobody.
    from interop_lab.containment.model import ContainerSpec
    from interop_lab.profiles.registry import PROFILES
    from interop_lab.scenarios.library import ALL_SCENARIOS, SUITES

    for value in ContainerSpec.model_fields.values():
        if isinstance(value.default, str):
            vocabulary.add(value.default)

    vocabulary.update(PROFILES)
    vocabulary.update(ALL_SCENARIOS)
    vocabulary.update(SUITES)
    for scenario in ALL_SCENARIOS.values():
        vocabulary.add(scenario.definition.title)
        for step in scenario.steps:
            vocabulary.update(assertion.assertion_id for assertion in step.assertions)
            if step.method:
                vocabulary.add(step.method)
    return frozenset(value.lower() for value in vocabulary)


@dataclass
class DenylistTerm:
    value: str
    #: Where the term came from, so a hit can be explained rather than merely
    #: reported. "a string matched" is not an actionable finding.
    origin: str


@dataclass
class Denylist:
    terms: list[DenylistTerm] = field(default_factory=list)

    @property
    def values(self) -> list[str]:
        return [term.value for term in self.terms]

    def scan(self, text: str) -> list[DenylistTerm]:
        lowered = text.lower()
        return [term for term in self.terms if term.value.lower() in lowered]


def _admit(
    value: Any, origin: str, into: dict[str, DenylistTerm], vocabulary: frozenset[str]
) -> None:
    if not isinstance(value, str):
        return
    candidate = value.strip()
    if len(candidate) < MINIMUM_TERM_LENGTH:
        return
    lowered = candidate.lower()
    if lowered in NON_IDENTIFYING or lowered in vocabulary:
        return
    into.setdefault(candidate, DenylistTerm(value=candidate, origin=origin))


def build_denylist(repo_root: Path, *, benchmark_output: Path | None = None) -> Denylist:
    """Everything a public build must not contain, gathered from where it lives.

    Generated rather than hand-written, and generated from the private
    inventories themselves: a denylist someone types is a denylist that goes
    stale the first time a target is added, and it goes stale silently.
    """
    collected: dict[str, DenylistTerm] = {}
    vocabulary = _harness_vocabulary()

    # -- pinned external targets: repositories, packages, versions, identities
    for target_file in sorted((repo_root / "targets" / "external").glob("*.yaml")):
        origin = f"target definition {target_file.name}"
        try:
            import yaml

            payload = yaml.safe_load(target_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        _walk(payload, origin, collected, vocabulary)
        _admit(target_file.stem, origin, collected, vocabulary)

    images = repo_root / "tools" / "external" / "images.yaml"
    if images.is_file():
        try:
            import yaml

            _walk(
                yaml.safe_load(images.read_text(encoding="utf-8")),
                "external image manifest",
                collected,
                vocabulary,
            )
        except (OSError, ValueError):
            pass

    # -- private finding identifiers
    findings = repo_root / "artifacts" / "private-findings"
    for directory in sorted(p for p in findings.glob("*") if p.is_dir()):
        _admit(directory.name, "private finding package", collected, vocabulary)
        finding_json = directory / "finding.json"
        if finding_json.is_file():
            try:
                payload = json.loads(finding_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for key in ("finding_id", "target_id", "affected_version", "package", "repository"):
                _admit(
                    payload.get(key),
                    f"private finding {directory.name}",
                    collected,
                    vocabulary,
                )

    # -- the private benchmark's own target inventory, when one is supplied
    #
    # Skipped for a synthetic execution. Its target matrix is invented, so
    # admitting it would put the fixture's own prose on the denylist and then
    # find that prose in the dataset the fixture produced — a scanner arguing
    # with itself, and every hit a false one.
    from interop_lab.viewer.fixture import is_synthetic

    if benchmark_output is not None and not is_synthetic(benchmark_output):
        matrix = benchmark_output / "target-matrix.json"
        if matrix.is_file():
            try:
                for entry in json.loads(matrix.read_text(encoding="utf-8")):
                    _walk(entry, "benchmark target matrix", collected, vocabulary)
            except (OSError, ValueError):
                pass

    # -- private branches and workflow surfaces
    for branch in ("agent/mcp-interop-foundation",):
        _admit(branch, "private development branch", collected, vocabulary)
    _admit("private-results", "private results directory", collected, vocabulary)
    _admit("benchmarks/private-results", "private results directory", collected, vocabulary)
    _admit("artifacts/private-findings", "private findings directory", collected, vocabulary)

    return Denylist(terms=sorted(collected.values(), key=lambda term: term.value))


def _walk(
    payload: Any, origin: str, into: dict[str, DenylistTerm], vocabulary: frozenset[str]
) -> None:
    """Collect every string in a nested structure.

    Deliberately indiscriminate. A denylist built from a curated subset of keys
    is a denylist that misses the field somebody adds next week, and the cost of
    over-collecting is a few extra terms that never appear in a public dataset
    anyway.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            _walk(value, f"{origin}:{key}", into, vocabulary)
    elif isinstance(payload, list):
        for item in payload:
            _walk(item, origin, into, vocabulary)
    else:
        _admit(payload, origin, into, vocabulary)


# ------------------------------------------------------------------- transform


def _labels(dataset: ViewerDataset) -> dict[str, tuple[str, str]]:
    """target_key → (display label, slug), assigned in a stable order.

    Alphabetical by pinned key, which is arbitrary with respect to results and
    deliberately so: a labelling that followed outcomes would put a ranking back
    into a document whose whole point is that it has none.
    """
    mapping: dict[str, tuple[str, str]] = {}
    for index, target in enumerate(sorted(dataset.targets, key=lambda t: t.target_key)):
        letter = chr(ord("A") + index)
        mapping[target.target_key] = (
            f"Implementation {letter}",
            f"implementation-{letter.lower()}",
        )
    return mapping


def _relabel(text: str, labels: dict[str, tuple[str, str]]) -> str:
    """Replace every occurrence of a real target key with its public label.

    Applied to free text — timing names, limitation prose — where a key is
    embedded rather than held in a field of its own. Longest key first, so a key
    that is a prefix of another cannot half-replace it.
    """
    for key in sorted(labels, key=len, reverse=True):
        text = re.sub(re.escape(key), labels[key][0], text)
    return text


def _public_target(target: ViewerTarget, labels: dict[str, tuple[str, str]]) -> ViewerTarget:
    label, slug = labels[target.target_key]
    return ViewerTarget(
        target_key=slug,
        display_name=label,
        # Everything below this line is dropped rather than obscured. Each one
        # either names a third party outright or, in combination with the others,
        # narrows the field to a single project.
        target_id=None,
        target_file=None,
        source_version=None,
        source_commit=None,
        build_context=None,
        language=target.language,
        transport=target.transport,
        declared_profile=target.declared_profile,
        negotiated_profile=target.negotiated_profile,
        relationship=target.relationship,
        target_fingerprint=None,
        portable_identity_fingerprint=None,
        dockerfile_sha256=None,
        lock_sha256=None,
        source_archive_sha256=None,
        capability_fingerprint=None,
        official_conformance=target.official_conformance,
        official_conformance_detail=target.official_conformance_detail,
        limitations=[
            *target.limitations,
            "Identity withheld. Language and transport are retained because the "
            "applicability rules cannot be read without them, and they narrow the "
            "field: this row is anonymised, not unidentifiable.",
        ],
    )


def _public_capability(
    surface: CapabilitySurface, labels: dict[str, tuple[str, str]]
) -> CapabilitySurface:
    """A capability surface with its names removed and its shape intact.

    Tool names are the single most identifying thing a server advertises — a tool
    list is close to a signature — so they become positional labels. The schema
    fingerprints stay, because they carry the structural fact this view exists to
    show and cannot be inverted into a name.
    """
    renamed = []
    for index, tool in enumerate(sorted(surface.tools, key=lambda t: t.name), start=1):
        renamed.append(tool.model_copy(update={"name": f"tool-{index:02d}"}))
    return CapabilitySurface(
        target_key=labels[surface.target_key][1],
        status=surface.status,
        status_detail=None,
        negotiated_profile=surface.negotiated_profile,
        offered_profile=surface.offered_profile,
        capability_fingerprint=None,
        server_capabilities=surface.server_capabilities,
        tools=renamed,
        declared_tools=[
            f"declared-{index:02d}" for index in range(1, len(surface.declared_tools) + 1)
        ],
        advertises_resources=surface.advertises_resources,
        advertises_prompts=surface.advertises_prompts,
        advertises_logging=surface.advertises_logging,
        advertises_completions=surface.advertises_completions,
        observed_transport_features=surface.observed_transport_features,
        declared_versus_observed=[
            f"{len(surface.declared_versus_observed)} difference(s) between the "
            "declared tool list and the observed one"
        ]
        if surface.declared_versus_observed
        else [],
        fingerprint_stable_across_repetitions=surface.fingerprint_stable_across_repetitions,
        limitations=[
            *surface.limitations,
            "Tool names are positional labels here. The surface's shape is shown; "
            "what it is a surface of is not.",
        ],
    )


def _public_containment(
    evidence: ContainmentEvidence, labels: dict[str, tuple[str, str]]
) -> ContainmentEvidence:
    return evidence.model_copy(
        update={
            "target_key": labels[evidence.target_key][1],
            "portable_build_identity": None,
            "builder_local_image_identity": None,
        }
    )


def _public_signature(
    record: SignatureRecord, labels: dict[str, tuple[str, str]]
) -> SignatureRecord:
    """A signature with its fingerprints removed and its contract intact.

    The fingerprints go because each is a value someone holding a candidate
    target could recompute and compare, which turns a guess into a confirmation.
    Everything that makes the signature a stability contract — the classification,
    the failing assertions, the process outcome, the cleanup and containment
    results — is first-party vocabulary and stays.
    """
    return record.model_copy(
        update={
            "target_key": labels[record.target_key][1],
            "target_fingerprint": None,
            "capability_fingerprint": None,
        }
    )


def _public_timing(card: TimingCard, labels: dict[str, tuple[str, str]]) -> TimingCard:
    return card.model_copy(
        update={
            "name": _relabel(card.name, labels),
            # A runner identity names a machine, and in a private matrix that is
            # frequently a person's hostname.
            "environment": "environment withheld",
            "insufficient_reason": (
                _relabel(card.insufficient_reason, labels) if card.insufficient_reason else None
            ),
            "limitations": [_relabel(text, labels) for text in card.limitations],
        }
    )


def _public_provenance(dataset: ViewerDataset) -> DatasetProvenance:
    """Narrow the private provenance into the public-safe one.

    Two genuinely different documents come out of this transform and they must
    not borrow each other's wording. A synthetic build shares only the schema
    with the private benchmark and withholds nothing, because there is nothing
    behind it to withhold. A build redacted from real measurements *is* the
    private matrix with its identities removed, and says so. Labelling the first
    as though it were the second is the specific claim this project cannot make.
    """
    provenance = dataset.provenance
    if provenance.evidence_kind is EvidenceKind.SYNTHETIC:
        # The note and the disclaimer were already built for exactly this
        # document by the private assembly path; only the eyebrow gains the
        # public-safe prefix, because only now is the document public-safe.
        return provenance.model_copy(update={"eyebrow": PUBLIC_SAFE_EYEBROW})
    return provenance.model_copy(
        update={
            "eyebrow": PUBLIC_SAFE_ANONYMISED_EYEBROW,
            "disclaimer": ANONYMISED_DISCLAIMER,
            "benchmark_identity_withheld": True,
            "benchmark_identity_note": (
                "The private benchmark's identity, and the identities of every "
                "implementation it measured, are withheld. The measurements "
                "themselves are real."
            ),
        }
    )


def to_public_safe(
    dataset: ViewerDataset,
    *,
    anonymization_approved: bool,
    approval_reference: str | None = None,
) -> ViewerDataset:
    """Transform the private dataset into the public-safe one.

    Does not decide whether the transform is *permitted* — that is the export
    command's job, because permission depends on where the evidence came from and
    this function only sees the document.
    """
    labels = _labels(dataset)

    matrix = [
        cell.model_copy(
            update={
                "target_key": labels[cell.target_key][1],
                "limitations": [_relabel(text, labels) for text in cell.limitations],
            }
        )
        for cell in dataset.matrix
    ]
    categories = [
        summary.model_copy(
            update={
                "target_key": (
                    labels[summary.target_key][1] if summary.target_key in labels else None
                )
            }
        )
        for summary in dataset.categories
    ]

    reproduction: list[ReproductionSummary] = [
        summary
        for summary in dataset.reproduction
        # Only first-party fixtures whose defect this repository authored. A
        # private finding's *existence* is disclosure, so the summaries are not
        # anonymised here — they are absent.
        if summary.synthetic
    ]

    return dataset.model_copy(
        update={
            "visibility_mode": VisibilityMode.PUBLIC_SAFE,
            "generated_from_harness_commit": None,
            # The harness commit travels in three places, and dropping one of
            # them would have been a redaction that reads as complete and is
            # not. A commit identifies the private repository state the run
            # happened at, which is enough to date and locate the whole
            # execution.
            #
            # `benchmark_name` goes with them. The private execution's name is
            # an identity, and a public-safe build that kept it as its title was
            # presenting invented rows under the private benchmark's masthead.
            "benchmark": dataset.benchmark.model_copy(
                update={
                    "benchmark_name": None,
                    "definition_harness_commit": None,
                    "execution_harness_commit": None,
                    "visibility": VisibilityMode.PUBLIC_SAFE.value,
                }
            ),
            "provenance": _public_provenance(dataset),
            "targets": [_public_target(target, labels) for target in dataset.targets],
            "matrix": matrix,
            "categories": categories,
            "capabilities": [_public_capability(s, labels) for s in dataset.capabilities],
            "result_signatures": [_public_signature(r, labels) for r in dataset.result_signatures],
            "containment": [_public_containment(c, labels) for c in dataset.containment],
            "timings": [_public_timing(card, labels) for card in dataset.timings],
            "containment_overhead": [
                pair.model_copy(
                    update={
                        "host": _public_timing(pair.host, labels) if pair.host else None,
                        "contained": (
                            _public_timing(pair.contained, labels) if pair.contained else None
                        ),
                    }
                )
                for pair in dataset.containment_overhead
            ],
            "reproduction": reproduction,
            "disclosure_controls": DisclosureControls(
                visibility_mode=VisibilityMode.PUBLIC_SAFE,
                publishable=False,
                anonymization_approved=anonymization_approved,
                approval_reference=approval_reference,
                redactions_applied=[
                    "third-party target identities, files, versions and commits",
                    "target, capability and portable build fingerprints",
                    "Dockerfile, dependency-lock and source-archive hashes",
                    "observed and declared tool names",
                    "builder-local image identities",
                    "runner and environment identities on every timing",
                    "private finding packages, including their existence",
                    "every harness commit — the pin, the execution and the export",
                ],
                notes=[
                    "Anonymised, not unidentifiable. Language, transport and result "
                    "narrow the field in combination, and this document does not "
                    "claim otherwise.",
                    "Not published by this checkpoint. `publishable` stays false "
                    "until a separate release gate says otherwise.",
                ],
            ),
        }
    )


# ----------------------------------------------------------------- leak scanning


@dataclass
class LeakFinding:
    term: str
    origin: str
    location: str


def scan_payload(
    payload: Any, denylist: Denylist, *, location: str = "<root>"
) -> list[LeakFinding]:
    """Walk a decoded document and report every denylisted term it contains.

    Structural rather than a regex over the serialised bytes, so a hit can name
    the path it was found at. "the bundle contains a private version string" is a
    report; "the bundle contains a private version string at
    targets[1].source_version" is a fix.
    """
    findings: list[LeakFinding] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            findings.extend(scan_payload(value, denylist, location=f"{location}.{key}"))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            findings.extend(scan_payload(item, denylist, location=f"{location}[{index}]"))
    elif isinstance(payload, str):
        findings.extend(
            LeakFinding(term=term.value, origin=term.origin, location=location)
            for term in denylist.scan(payload)
        )
    return findings


def scan_text(text: str, denylist: Denylist, *, location: str) -> list[LeakFinding]:
    """Scan an opaque artifact — a built bundle, an HTML file — as one string."""
    return [
        LeakFinding(term=term.value, origin=term.origin, location=location)
        for term in denylist.scan(text)
    ]


def scan_tree(root: Path, denylist: Denylist) -> list[LeakFinding]:
    """Scan every readable file under a directory. Used on the built bundle."""
    findings: list[LeakFinding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A binary asset cannot carry a term the scan would recognise as
            # text, and decoding it as one produces false negatives dressed as
            # coverage. Skipped explicitly rather than silently.
            continue
        findings.extend(scan_text(text, denylist, location=str(path.relative_to(root))))
    return findings
