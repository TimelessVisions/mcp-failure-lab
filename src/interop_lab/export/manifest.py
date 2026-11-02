"""What may leave this repository, decided by an allowlist.

A denylist is the wrong shape for this problem. It answers "is this one of the
things I remembered to forbid?", so anything added later is public by default
and the first mistake is a disclosure. An allowlist answers "was this explicitly
approved?", so the failure mode of forgetting is that a file is *missing* from
the export — visible, annoying, and harmless.

Every private category this project has is here for a reason:

* third-party findings, which are private until the owner decides otherwise;
* comparative benchmark results about other people's software;
* commercial documents, which are internal and would embarrass in public;
* live run artifacts, which contain machine-specific paths and target traces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

EXPORT_MANIFEST_VERSION = "1"


@dataclass(frozen=True)
class Rule:
    """One allowlist entry and the reason it is safe to publish."""

    pattern: str
    reason: str


#: Explicitly approved for public export. Anything not matching is excluded.
ALLOWED: tuple[Rule, ...] = (
    Rule("README.md", "Project overview, written for a public reader."),
    Rule("SECURITY.md", "Security contact and reporting policy."),
    Rule("LICENSE*", "Apache-2.0 license text."),
    Rule("NOTICE", "Apache-2.0 attribution notice."),
    Rule("THIRD_PARTY_NOTICES", "What the distribution uses and what it does not redistribute."),
    Rule("CONTRIBUTING.md", "Contribution guide."),
    Rule("pyproject.toml", "Build metadata."),
    Rule("uv.lock", "Pinned resolver output — reproducibility, no secrets."),
    Rule(".gitattributes", "Line-ending rules that digest pins depend on."),
    Rule(".gitignore", "Ignore rules."),
    Rule("src/**", "The harness itself."),
    Rule("tests/**", "The tests. A harness whose tests are hidden is not evidence."),
    Rule("fixtures/**", "Locally written fixtures, including deliberate defects."),
    Rule("targets/fixtures/**", "Local fixture target definitions."),
    Rule("targets/http/**", "Local HTTP fixture target definitions."),
    Rule("targets/python/**", "Pinned official SDK target definitions."),
    Rule("targets/typescript/*.yaml", "Pinned official SDK target definition."),
    Rule("targets/typescript/*.mjs", "Pinned official SDK target harness."),
    Rule("targets/typescript/package*.json", "Pinned dependency manifest."),
    Rule("targets/mvp-matrix.yaml", "Local fixture matrix."),
    Rule("suites/**", "Suite definitions naming scenarios."),
    Rule("baselines/**", "Approved baselines over local fixtures only."),
    Rule("tools/conformance/**", "Pinned official conformance boundary."),
    Rule("tools/export_gate.sh", "Public release gate executed by public CI."),
    Rule(
        "tools/public_secret_scan.sh",
        "Public CI secret scan. Excludes generated junit reports, not other paths.",
    ),
    Rule("docs/*.md", "Public documentation."),
    Rule("profiles/**", "Dated protocol profile facts."),
    Rule("artifacts/examples/**", "Curated synthetic example artifacts."),
    Rule("benchmarks/README.md", "Benchmark scope and the publication boundary."),
    Rule("benchmarks/run_benchmark.py", "Benchmark methodology as code."),
    # CI for the public repository, written for it rather than carried across.
    # Named one file at a time, deliberately: `.github/workflows/**` is forbidden
    # below, so a workflow added later is excluded until someone reads it and
    # adds it here. The failure mode of forgetting is a public repository with
    # one workflow fewer, which is visible; the failure mode of a directory
    # glob is a private branch trigger published by accident.
    Rule(".github/workflows/public-ci.yml", "Public CI, written for the public repository."),
    Rule(".github/workflows/public-viewer.yml", "Public viewer CI over synthetic evidence."),
    Rule(
        ".github/workflows/public-release-gate.yml",
        "Public release gate over the sanitized export.",
    ),
    # The viewer's source, not any dataset it renders. Both datasets are
    # generated into git-ignored directories, so neither is a tracked file and
    # neither can reach this list.
    Rule("viewer/*", "Viewer build configuration and manifests."),
    Rule("viewer/src/**", "The viewer application."),
    Rule("viewer/e2e/**", "The viewer's browser tests."),
    Rule("viewer/tests/**", "The viewer's unit tests."),
    Rule("viewer/scripts/**", "Schema compilation for the viewer."),
    Rule("viewer/public/*", "Static assets. The dataset directory is generated, never tracked."),
    # The public-safe demo only. Everything under `demos/private/` is forbidden
    # below, including the parts that are only prose about the recording.
    Rule("demos/README.md", "How both demos are produced."),
    Rule("demos/capture.mjs", "Deterministic capture harness."),
    Rule("demos/verify.mjs", "Recording verification."),
    Rule("demos/public-safe/**", "The synthetic demo, its script and its recording."),
)

#: Never exported, even when an allowlist pattern would otherwise match. This is
#: belt-and-braces: the allowlist alone should already exclude these, and a test
#: asserts that it does. Listing them again means a careless widening of an
#: allowlist rule cannot quietly publish them.
FORBIDDEN: tuple[Rule, ...] = (
    # The one third-party file this repository copies. It is here for a legal
    # reason rather than a privacy one: the OASIS IPR Policy is not an open
    # source license, and whether it permits redistributing the schema file has
    # not been confirmed. Excluding it is reversible in a way that publishing it
    # is not. SARIF *generation* is unaffected and stays in the distribution;
    # only offline validation against the copied schema is withheld.
    Rule("schemas/**", "OASIS SARIF schema; redistribution terms unconfirmed."),
    Rule("artifacts/private-findings/**", "Findings against third-party code."),
    Rule("benchmarks/private-results/**", "Comparative results about other software."),
    Rule("docs/commercialization/**", "Internal commercial documents."),
    # The private demo, including its prose. The script and storyboard describe
    # which real result appears at which timecode, which is a finding written
    # down in a different order.
    Rule("demos/private/**", "The private demo and everything describing it."),
    # The private workflows, named one at a time.
    #
    # Each triggers on the private development branch, and the benchmark and
    # viewer workflows additionally name the private results directory and the
    # private artifact. A public repository needs CI written for its own
    # branches; carrying these across is a branch identifier leaving with
    # nothing gained.
    #
    # This was a `.github/workflows/**` glob, which was correct about these five
    # files and wrong about the consequence: it left the sanitized repository
    # with no CI at all. The public-safe workflows are allowlisted above by
    # exact path instead. The default still excludes: a workflow that is neither
    # forbidden here nor allowed above falls through to "not on the allowlist",
    # so adding one to this directory does not publish it.
    Rule(".github/workflows/ci.yml", "Triggers on the private development branch."),
    Rule(
        ".github/workflows/benchmark.yml",
        "Private branch trigger, private results directory and private artifact.",
    ),
    Rule(
        ".github/workflows/external-matrix.yml",
        "Private branch trigger and the pinned external target matrix.",
    ),
    Rule(
        ".github/workflows/viewer.yml",
        "Private branch trigger and the private viewer artifact wiring.",
    ),
    Rule(".github/workflows/export-gate.yml", "Triggers on the private development branch."),
    # These assert on the workflows above. Their subject is not exported, and
    # they name the private branch and the private artifact in order to pin it.
    Rule("tests/unit/test_benchmark_workflow.py", "Asserts on a workflow that is not exported."),
    Rule("tests/unit/test_viewer_workflow.py", "Asserts on a workflow that is not exported."),
    Rule(
        "tests/unit/test_external_matrix_validation.py",
        "Asserts on a workflow that is not exported; names the private branch.",
    ),
    # These read the pinned external image manifest or the private benchmark
    # definition, neither of which is exported. A test whose input is withheld
    # cannot run, and shipping it means the export's own test suite does not
    # collect — which is a worse signal than the test being absent.
    Rule("tests/integration/test_external_matrix.py", "Reads the external image manifest."),
    Rule("tests/unit/test_external_build.py", "Reads the external image manifest."),
    Rule("tests/unit/test_external_supply_chain.py", "Reads the external image manifest."),
    Rule("tests/unit/test_byte_hash_portability.py", "Reads the external image manifest."),
    Rule(
        "tests/unit/test_benchmark_runner_and_documents.py",
        "Reads the private benchmark definition.",
    ),
    Rule("tests/unit/test_benchmark_semantics.py", "Asserts on the private benchmark definition."),
    # These assert on *this project's* private configuration — which external
    # targets are pinned, what their build contexts hash to, which findings
    # exist — rather than on the harness. In a public tree they would be
    # assertions about files that are not there, which is not a weaker test but
    # a meaningless one.
    Rule("tests/unit/test_external_matrix_shape.py", "Asserts on the pinned external matrix."),
    Rule("tests/unit/test_build_context_identity.py", "Asserts on external build contexts."),
    Rule(
        "tests/unit/test_replay_and_capability_identity.py",
        "Asserts on pinned external target identities.",
    ),
    Rule("tests/unit/test_safe_invocation_arguments.py", "Asserts on the external matrix."),
    Rule("tests/unit/test_no_adhoc_image_parsing.py", "Asserts on the external image manifest."),
    Rule("tests/unit/test_private_findings.py", "Asserts that private findings exist."),
    Rule("research/external-target-selection.md", "Names an undisclosed finding."),
    Rule("artifacts/runs/**", "Live run traces, machine-specific."),
    Rule("artifacts/reports/**", "Live reports, machine-specific."),
    Rule("artifacts/repros/**", "Live reproduction packages."),
    Rule("artifacts/gate/**", "Live gate output."),
    # Generated under `viewer/`, and gitignored, so the export cannot reach them
    # today. Listed anyway: the allowlist's `viewer/*` matches recursively, so a
    # dataset committed here by accident would otherwise be approved by a rule
    # written for build configuration. A private dataset is the single worst
    # thing that could leave this repository.
    Rule("viewer/public/data/**", "An exported dataset. Generated, never published from here."),
    Rule("viewer/dist/**", "A built bundle, which may have been built from private data."),
    Rule("**/*.env", "Environment files."),
    Rule("**/ca-bundle.crt", "Machine-specific trust material."),
    Rule("**/node_modules/**", "Vendored dependencies."),
    Rule("**/__pycache__/**", "Build noise."),
    Rule("**/.venv/**", "Virtual environments."),
)

#: Content that must not appear in any exported file, whatever its path. These
#: are checked against file *contents*, not names, because the reason a file is
#: dangerous is usually what is inside it.
FORBIDDEN_CONTENT: tuple[tuple[str, str], ...] = (
    (r"ILAB-EXT-\d+", "a private third-party finding identifier"),
    (r"/home/user/", "an absolute developer path"),
    (r"/Users/[a-z]", "an absolute macOS path"),
    (r"C:\\\\Users\\\\", "an absolute Windows path"),
    (r"/tmp/claude-", "a scratch path"),  # noqa: S108 - a pattern to detect, not a path to use
    (r"scratchpad", "a scratch path"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (r"(?i)\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
    (r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}", "a GitHub token"),
    (
        r"(?i)\b[A-Z0-9._%+-]+@agentcommercegateway\.com\b",
        "a private contact email",
    ),
    # A branch name locates the private development history even when no commit
    # travels with it, and a run URL is a direct link to private CI output.
    (r"agent/mcp-interop-foundation", "the private development branch"),
    (r"actions/runs/\d+", "a private workflow-run identifier"),
    (r"viewer-private-do-not-publish", "the private viewer artifact's name"),
    # A real target's name next to an exact version is a private measurement
    # written in two fields. Neither half is sensitive alone — the name is public
    # software, the version is a public release — but together they say which
    # build this project ran, and anything nearby saying how it behaved completes
    # a finding. Re-identification does not need every field: language,
    # transport, version and result narrow the candidates to one far sooner than
    # people expect, so the pair is blocked rather than the triple.
    #
    # Public tests use synthetic targets (`synthetic-python-stdio` and friends)
    # with synthetic versions, which carry the same shapes and identify nobody.
    (
        r"\b(mcp-go-everything|server-everything|mcp-server-time)\b[^\n]{0,60}?"
        r"\b\d+\.\d+\.\d+\b",
        "a real target name adjacent to an exact version",
    ),
)

#: Files that legitimately contain a forbidden *pattern* because detecting or
#: redacting that pattern is their job. A scanner that cannot tell a detector
#: from a leak is useless: it either cries wolf on its own safety code, or gets
#: switched off. Each exemption names the file and why it is not a leak.
#:
#: These are path-scoped, not global. A real secret appearing anywhere else
#: still fails the scan.
CONTENT_EXEMPT: tuple[Rule, ...] = (
    Rule(
        "src/interop_lab/artifacts/portability.py",
        "This module *is* the absolute-path detector; the patterns are its rules.",
    ),
    Rule(
        "src/interop_lab/export/manifest.py",
        "This module is the export scanner; the patterns are its rules.",
    ),
    Rule(
        "tests/unit/test_portability_and_runtime.py",
        "Asserts the path detector rejects these shapes. Synthetic inputs.",
    ),
    Rule(
        "tests/unit/test_security.py",
        "Asserts redaction removes a synthetic PEM block. Not a real key.",
    ),
    Rule(
        "tests/unit/test_export.py",
        "Asserts the export scanner catches these shapes. Synthetic inputs.",
    ),
    Rule(
        "tests/unit/test_cli_adoption.py",
        "Asserts a materialised export names no private finding; the literal it "
        "searches for is the pattern, not an identifier.",
    ),
    Rule(
        "tests/unit/test_public_workflows.py",
        "Asserts the public workflows name no private branch, artifact, finding "
        "or target. The terms it searches for are the rule, not a mention.",
    ),
    Rule(
        "src/interop_lab/security/redaction.py",
        "This module redacts secrets; the patterns are its rules.",
    ),
    Rule(
        "src/interop_lab/viewer/redaction.py",
        "This module builds the viewer's denylist; the private branch name is "
        "one of the terms it scans for, not a mention of it.",
    ),
)


_CONTENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), description) for pattern, description in FORBIDDEN_CONTENT
)

#: Files whose bytes are not scanned for forbidden content.
BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2"})


@dataclass
class ExportDecision:
    """Why one path was included or excluded. Recorded, not just applied."""

    path: str
    included: bool
    reason: str


@dataclass
class ExportPlan:
    """The full decision set, so an export can be reviewed before it is trusted."""

    included: list[ExportDecision] = field(default_factory=list)
    excluded: list[ExportDecision] = field(default_factory=list)
    content_violations: list[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return not self.content_violations

    def summary(self) -> dict[str, object]:
        return {
            "manifest_version": EXPORT_MANIFEST_VERSION,
            "included": len(self.included),
            "excluded": len(self.excluded),
            "content_violations": len(self.content_violations),
            "safe": self.is_safe,
        }


def _matches(path: str, pattern: str) -> bool:
    """Glob match where ``**`` spans directories, as in .gitignore."""
    if fnmatch(path, pattern):
        return True
    # fnmatch treats `*` as crossing `/`, which is too permissive, but it does
    # not expand a trailing `**` prefix the way a reader expects. Handle the
    # directory-prefix form explicitly.
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return False


def classify(path: str) -> ExportDecision:
    """Decide one repository-relative path."""
    posix = PurePosixPath(path).as_posix()

    for rule in FORBIDDEN:
        if _matches(posix, rule.pattern):
            return ExportDecision(posix, included=False, reason=f"forbidden: {rule.reason}")

    for rule in ALLOWED:
        if _matches(posix, rule.pattern):
            return ExportDecision(posix, included=True, reason=rule.reason)

    return ExportDecision(
        posix,
        included=False,
        reason="not on the allowlist; add an explicit rule if it should be public",
    )


def scan_content(path: Path, text: str) -> list[str]:
    """Report anything in a file's contents that must never be published."""
    violations: list[str] = []
    for pattern, description in _CONTENT_PATTERNS:
        match = pattern.search(text)
        if match:
            violations.append(f"{path}: contains {description} ({match.group(0)[:40]!r})")
    return violations


def plan(paths: list[str], *, root: Path) -> ExportPlan:
    """Classify every path, then verify the contents of everything included."""
    result = ExportPlan()
    for path in sorted(paths):
        decision = classify(path)
        if decision.included:
            result.included.append(decision)
        else:
            result.excluded.append(decision)

    exempt = {rule.pattern for rule in CONTENT_EXEMPT}
    for decision in result.included:
        source = root / decision.path
        if decision.path in exempt:
            continue
        if not source.is_file() or source.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        result.content_violations.extend(scan_content(Path(decision.path), text))

    return result
