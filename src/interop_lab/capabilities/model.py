"""What a target actually advertises, as opposed to what its file claims.

Applicability used to be gated on ``TargetDefinition.expected_tools`` — a list a
human typed. That made a handwritten declaration the source of truth for whether
a requirement applies, which fails in both directions and did:

* One target's definition declared ``expected_tools: []`` while the server
  advertised a large tool surface, so every tool-requiring scenario was reported
  NOT_APPLICABLE against a server that could have run them.
* Another declared a tool that the pinned build does not actually expose. A
  scenario requiring it would have been reported applicable, run, and failed —
  attributing a target defect to a tool that does not exist.

Neither is a defect in anyone's server. Both are the same architectural error:
a declaration standing in for an observation. The targets and versions are
deliberately not named here: the point is the error, and naming a third party's
build beside a described behaviour is a finding whatever the surrounding prose
says.

The models here separate the two permanently. The declaration is kept — it is
still useful as an *expectation* that can be checked — but it never decides
applicability. Only ``ResolvedTargetCapabilities``, obtained from real traffic
against the real target under the real containment, does that.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _canonical_sha256(value: Any) -> str:
    """Hash a JSON value in a form two runs agree on.

    ``sort_keys`` and a fixed separator matter: without them a server that emits
    the same schema with different key ordering would look like a schema change,
    and capability drift would be reported every run.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CapabilityResolutionStatus(StrEnum):
    """Whether the capability record may be relied on.

    There is deliberately no value meaning "assumed". A record that was not
    obtained from the target is not a capability record, and a scenario whose
    prerequisite cannot be checked is INCONCLUSIVE rather than quietly
    NOT_APPLICABLE — the second would be a fabricated reason.
    """

    #: Discovery ran against the target and its answers were parsed.
    RESOLVED = "resolved"
    #: Discovery ran and the target did not answer usably.
    DISCOVERY_FAILED = "discovery_failed"
    #: Discovery could not run under the containment the target requires. Never
    #: retried without containment: that would measure a different program.
    CONTAINMENT_UNUSABLE = "containment_unusable"
    #: No strategy implements this target's combination, so nothing was tried.
    NO_STRATEGY = "no_strategy"
    #: Not attempted. Only legitimate for a caller that supplied capabilities
    #: directly, such as a unit test with a stub.
    NOT_ATTEMPTED = "not_attempted"
    #: Discovery could not succeed because the target is a *deliberately broken
    #: local fixture* whose defect prevents a handshake — one that resets every
    #: connection, for instance. The declared tool list was used instead, and
    #: this status records that the applicability decision rests on a
    #: declaration rather than an observation.
    #:
    #: Permitted only for local fixtures, and refused outright for external
    #: targets: falling back to a declaration is exactly the defect observed
    #: capability resolution exists to remove, and the fixtures that need it are
    #: code this repository wrote and can therefore vouch for.
    DECLARED_FALLBACK = "declared_fallback"


#: Statuses in which advertised tools are unknown, so a tool prerequisite cannot
#: be decided either way. `DECLARED_FALLBACK` is deliberately absent: the
#: decision can be made there, it is simply made from a declaration, and the
#: status says so.
UNUSABLE_RESOLUTION = frozenset(
    {
        CapabilityResolutionStatus.DISCOVERY_FAILED,
        CapabilityResolutionStatus.CONTAINMENT_UNUSABLE,
        CapabilityResolutionStatus.NO_STRATEGY,
        CapabilityResolutionStatus.NOT_ATTEMPTED,
    }
)

#: Statuses in which the tool list came from a human, not from the target.
#: Anything reading a capability record to justify an applicability decision
#: should treat these as weaker evidence, and say so.
NOT_OBSERVED_RESOLUTION = frozenset({CapabilityResolutionStatus.DECLARED_FALLBACK})


class ToolDescriptor(BaseModel):
    """One advertised tool, with its schema pinned by hash.

    The schema hash rather than the schema: a tool keeping its name while
    changing its input contract is exactly the drift that would silently
    invalidate a recorded applicability decision, and the name alone cannot show
    it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    input_schema_sha256: str | None = None
    output_schema_sha256: str | None = None
    title: str | None = None

    @classmethod
    def from_advertised(cls, raw: dict[str, Any]) -> ToolDescriptor:
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"advertised tool has no usable name: {raw!r}")
        # Both spellings appear across revisions and SDKs. Neither is preferred
        # here, because this records what was seen rather than what was expected.
        input_schema = raw.get("inputSchema", raw.get("input_schema"))
        output_schema = raw.get("outputSchema", raw.get("output_schema"))
        return cls(
            name=name,
            input_schema_sha256=(
                _canonical_sha256(input_schema) if input_schema is not None else None
            ),
            output_schema_sha256=(
                _canonical_sha256(output_schema) if output_schema is not None else None
            ),
            title=raw.get("title") if isinstance(raw.get("title"), str) else None,
        )


class ObservedTransportFeatures(BaseModel):
    """Transport behaviour established by watching, not by being told.

    A separate model from the capability record on purpose. A *capability* is
    something the server advertised — it appears in `initialize` or in
    `tools/list`, and recording it is quoting the server. Nothing here was
    advertised: a session identifier is observed on a response header, and
    whether one was issued at all is something only a handshake reveals.

    Calling both of those "capabilities" would put a probe result and a server's
    own claim in the same bucket, and the distinction is exactly what a reader
    needs when deciding how much a NOT_APPLICABLE reason is worth.

    Deliberately narrow. Every field here is one an applicability decision
    actually consults; a field that changes no decision would be an observation
    in search of a use, and it would churn the fingerprint for nothing.
    """

    model_config = ConfigDict(extra="forbid")

    #: Which transport the observation was made over, and by which strategy.
    transport: str | None = None
    execution_strategy: str | None = None

    #: Whether the profile's required handshake completed. `None` for a profile
    #: that requires none, which is a different fact from "it did not complete".
    initialization_completed: bool | None = None
    #: Whether the server assigned a session identifier on `initialize`.
    #:
    #: 2025-11-25 makes this optional, so `False` is a conforming answer and not
    #: a defect. It gates the session lifecycle scenarios: a server that issues
    #: none has nothing for them to be about, and running them anyway would
    #: manufacture failures out of a choice the specification permits.
    session_identifier_issued: bool | None = None
    #: The header it arrived in, recorded rather than assumed, since a scenario
    #: about a stale identifier has to send it back the same way.
    session_identifier_header_name: str | None = None
    #: The protocol version the handshake settled on, as the server stated it.
    accepted_protocol_version: str | None = None

    def as_fingerprint_material(self) -> dict[str, Any]:
        """The subset that changes an applicability decision.

        ``accepted_protocol_version`` is deliberately absent: the negotiated
        profile is already a first-class member of the fingerprint, and
        including it twice would mean one change moved the value for two
        reasons.
        """
        return {
            key: value
            for key, value in {
                "transport": self.transport,
                "execution_strategy": self.execution_strategy,
                "initialization_completed": self.initialization_completed,
                "session_identifier_issued": self.session_identifier_issued,
                "session_identifier_header_name": self.session_identifier_header_name,
            }.items()
            if value is not None
        }


#: What observed transport features do *not* establish. Attached wherever they
#: are recorded, because "observed" reads stronger than one handshake supports.
TRANSPORT_FEATURE_LIMITATIONS: tuple[str, ...] = (
    "Established by a single handshake. A server whose behaviour varies between "
    "connections is described here only as it was on that one.",
    "Absence of a feature means it was not observed, never that the server cannot do it.",
    "Long-lived SSE consumption, Last-Event-ID resumption and server-initiated "
    "requests over a retained stream are not implemented by this harness and are "
    "therefore neither observed nor claimed.",
)


class CapabilityFingerprint(BaseModel):
    """The part of a target's surface that can change an applicability decision.

    Recorded in run evidence so a baseline cannot be reused across a tool-surface
    change without that being visible. Deliberately narrower than the full
    capability record: a changed ``serverInfo.version`` is interesting but does
    not by itself change which scenarios apply, and putting it here would make
    the fingerprint churn for reasons that carry no consequence.
    """

    model_config = ConfigDict(extra="forbid")

    negotiated_profile: str
    tool_names: list[str] = Field(default_factory=list)
    #: Input-schema hash per tool. What a caller must send.
    tool_schema_hashes: dict[str, str] = Field(default_factory=dict)
    #: Output-schema hash per tool, where one is advertised. Separate from the
    #: input hashes because they answer different questions and a scenario can
    #: depend on either: an assertion about a result's shape rests on the output
    #: schema, and a tool that changed only that would look unchanged if the two
    #: were merged or if only the input side were covered.
    tool_output_schema_hashes: dict[str, str] = Field(default_factory=dict)
    server_capabilities: dict[str, Any] = Field(default_factory=dict)
    transport_features: dict[str, Any] = Field(default_factory=dict)

    @property
    def digest(self) -> str:
        """One short value that changes when any of the above changes."""
        return _canonical_sha256(
            {
                "negotiated_profile": self.negotiated_profile,
                "tool_names": sorted(self.tool_names),
                "tool_schema_hashes": self.tool_schema_hashes,
                "tool_output_schema_hashes": self.tool_output_schema_hashes,
                "server_capabilities": self.server_capabilities,
                "transport_features": self.transport_features,
            }
        )


def describe_capability_drift(
    approved: CapabilityFingerprint, observed: CapabilityFingerprint
) -> list[str]:
    """Say what moved between two capability surfaces, in reviewable terms.

    Two hex strings that differ is a true statement and a useless one: it tells a
    reviewer that something changed and gives them nothing to decide with. A
    capability change is not a defect — an upstream release that adds a tool is
    perfectly ordinary — so the whole question a drift report has to answer is
    *what* changed, and a digest comparison cannot answer it.

    Deliberately descriptive and never evaluative. Nothing here decides whether a
    change is acceptable; that is the reviewer's judgement, and the gate's job is
    to stop until it has been made.
    """
    changes: list[str] = []

    if approved.negotiated_profile != observed.negotiated_profile:
        changes.append(
            f"negotiated profile changed: {approved.negotiated_profile or 'none'} -> "
            f"{observed.negotiated_profile or 'none'}"
        )

    before, after = set(approved.tool_names), set(observed.tool_names)
    changes += [f"tool added: {name}" for name in sorted(after - before)]
    changes += [f"tool removed: {name}" for name in sorted(before - after)]

    for label, old_hashes, new_hashes in (
        ("input schema", approved.tool_schema_hashes, observed.tool_schema_hashes),
        (
            "output schema",
            approved.tool_output_schema_hashes,
            observed.tool_output_schema_hashes,
        ),
    ):
        # Only tools present on both sides: a schema that arrived with a new tool
        # is already reported as the tool being added, and saying it twice makes
        # a two-line drift look like four changes.
        for name in sorted(before & after):
            old, new = old_hashes.get(name), new_hashes.get(name)
            if old == new:
                continue
            if old is None:
                changes.append(f"tool {name} now advertises an {label}")
            elif new is None:
                changes.append(f"tool {name} no longer advertises an {label}")
            else:
                changes.append(f"{label} changed for tool {name}")

    for label, old_map, new_map in (
        ("server capability", approved.server_capabilities, observed.server_capabilities),
        ("transport feature", approved.transport_features, observed.transport_features),
    ):
        old_keys, new_keys = set(old_map), set(new_map)
        changes += [f"{label} added: {key}" for key in sorted(new_keys - old_keys)]
        changes += [f"{label} removed: {key}" for key in sorted(old_keys - new_keys)]
        changes += [
            f"{label} changed: {key}: {old_map[key]!r} -> {new_map[key]!r}"
            for key in sorted(old_keys & new_keys)
            if old_map[key] != new_map[key]
        ]

    return changes


class ResolvedTargetCapabilities(BaseModel):
    """What one target advertised, when, and under what isolation."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    #: The target definition's own fingerprint, so a capability record cannot be
    #: read as belonging to a target file it was not taken from.
    target_fingerprint: str | None = None
    #: Which built image this record describes: the resolution manifest in force
    #: and the local image ID it resolved to. The target fingerprint alone cannot
    #: say — it is portable by design, so two different builds of identical
    #: inputs share it, and a capability record that named only the fingerprint
    #: could be read as evidence about an image it was never taken from.
    resolution_identity: str | None = None

    protocol_profile_offered: str | None = None
    protocol_profile_negotiated: str | None = None

    server_name: str | None = None
    server_version: str | None = None
    server_title: str | None = None

    server_capabilities: dict[str, Any] = Field(default_factory=dict)
    tools: list[ToolDescriptor] = Field(default_factory=list)

    #: Advertised in `initialize`, which is a different claim from "listing them
    #: succeeded". A server may advertise `resources` and still fail to list any.
    advertises_resources: bool = False
    advertises_prompts: bool = False
    advertises_logging: bool = False
    advertises_completions: bool = False

    #: Fingerprint material for the observed transport behaviour. Derived from
    #: ``observed_transport_features`` rather than written separately, so the two
    #: cannot come to disagree about what was seen.
    transport_features: dict[str, Any] = Field(default_factory=dict)
    #: The typed record the above is derived from. Kept because a dictionary is
    #: the wrong shape for something a scenario's applicability consults: a typo
    #: in a key name would read as "the feature was not observed".
    observed_transport_features: ObservedTransportFeatures | None = None

    discovered_at: str | None = None
    discovery_run_id: str | None = None
    #: Repository-relative path to the trace this was parsed from. Relative by
    #: construction: an absolute path would travel into artifacts and stop
    #: resolving on any other machine.
    source_trace_uri: str | None = None

    status: CapabilityResolutionStatus = CapabilityResolutionStatus.NOT_ATTEMPTED
    status_detail: str | None = None

    #: The digest of :meth:`fingerprint`, stored rather than left to be recomputed
    #: by every reader.
    #:
    #: A method is not evidence. Until this field existed the fingerprint was
    #: derivable but never serialized, so an artifact recorded the tool *names* a
    #: decision rested on and not the schemas behind them — and a server that kept
    #: a tool's name while changing its input contract left an approved
    #: applicability decision looking untouched. Persisting the digest is what
    #: makes that change visible in a stored record rather than only in a live
    #: process.
    #:
    #: Filled in below for any record whose surface was actually established, and
    #: deliberately absent otherwise: a fingerprint over an empty record would
    #: hash the harness's ignorance into a value that looks like an observation.
    capability_fingerprint: str | None = None

    #: What this record does not establish. Always populated, because "resolved"
    #: invites a stronger reading than one discovery exchange supports.
    limitations: list[str] = Field(default_factory=list)

    #: Kept beside the observation, never merged into it.
    declared_tools: list[str] = Field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.status not in UNUSABLE_RESOLUTION

    @property
    def tool_names(self) -> list[str]:
        return sorted(tool.name for tool in self.tools)

    @property
    def declared_but_not_advertised(self) -> list[str]:
        """Tools the target file promises that the target does not offer.

        A scenario requiring one of these must not be reported applicable: it
        would run, fail, and attribute the failure to the server rather than to
        the target file.
        """
        advertised = set(self.tool_names)
        return sorted(name for name in self.declared_tools if name not in advertised)

    @property
    def advertised_but_not_declared(self) -> list[str]:
        """Tools the target offers that its file does not mention.

        Harmless on its own — `expected_tools` is an expectation, not an
        inventory — but a scenario requiring one of these is now correctly
        applicable, where before it was wrongly skipped.
        """
        declared = set(self.declared_tools)
        return sorted(name for name in self.tool_names if name not in declared)

    @property
    def has_declaration_mismatch(self) -> bool:
        return bool(self.declared_but_not_advertised or self.advertised_but_not_declared)

    def fingerprint(self) -> CapabilityFingerprint:
        return CapabilityFingerprint(
            negotiated_profile=self.protocol_profile_negotiated or "",
            tool_names=self.tool_names,
            tool_schema_hashes={
                tool.name: tool.input_schema_sha256
                for tool in sorted(self.tools, key=lambda t: t.name)
                if tool.input_schema_sha256 is not None
            },
            tool_output_schema_hashes={
                tool.name: tool.output_schema_sha256
                for tool in sorted(self.tools, key=lambda t: t.name)
                if tool.output_schema_sha256 is not None
            },
            server_capabilities=self.server_capabilities,
            transport_features=self.transport_features,
        )

    @model_validator(mode="after")
    def _fingerprint_is_computed_never_asserted(self) -> ResolvedTargetCapabilities:
        """Fill the digest in, and refuse one that disagrees with the record.

        Both halves matter. Filling it in means no code path can produce a usable
        capability record without the fingerprint travelling with it — the field
        cannot be forgotten at one of the several places these records are built.
        Refusing a disagreeing value means a stored record whose digest was edited
        does not load at all, rather than loading and quietly certifying a tool
        surface it does not describe. There is no third option where the supplied
        value wins: the observation is the evidence, and the digest is a statement
        about the observation.
        """
        if not self.is_usable:
            # Nothing was established, so there is nothing to fingerprint. A
            # supplied value here describes a measurement that did not happen.
            if self.capability_fingerprint is not None:
                raise ValueError(
                    f"capability record for {self.target_id!r} is {self.status.value} but "
                    "carries a capability fingerprint. A digest over a record whose "
                    "surface was never observed would read as evidence about the target."
                )
            return self

        computed = self.fingerprint().digest
        if self.capability_fingerprint is None:
            object.__setattr__(self, "capability_fingerprint", computed)
        elif self.capability_fingerprint != computed:
            raise ValueError(
                f"capability record for {self.target_id!r} records fingerprint "
                f"{self.capability_fingerprint} but its contents hash to {computed}. "
                "The record and its digest disagree, so one of them has been edited."
            )
        return self


#: What one discovery exchange does not establish, attached to every resolved
#: record so a reader cannot mistake it for a survey of the target's behaviour.
CAPABILITY_LIMITATIONS: tuple[str, ...] = (
    "Advertised is not implemented: a tool appearing in tools/list may still "
    "fail or be unimplemented when called.",
    "One point in time. A server whose tool surface changes at runtime is "
    "described here only as it was at discovery.",
    "Capabilities advertised in initialize are the server's own claim, recorded "
    "rather than verified.",
)
