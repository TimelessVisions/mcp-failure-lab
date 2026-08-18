"""The secondary dimensions of a result, kept apart from its primary outcome.

A single enum was carrying two orthogonal questions at once, and it could only
answer one of them per run:

* What did the target *do*? — PASS, SPEC_VIOLATION, TIMEOUT, RECOVERY_FAILURE.
* What does it *mean* for exposure? — is this security-relevant, and if so, has
  anyone established that it is exploitable?

``security.http_invalid_origin`` is where the collapse showed. Its assertion
quotes a MUST from the transport binding, so a target that fails it has violated
a normative requirement — and the scenario reported ``SECURITY_RELEVANT``, which
is not a statement about conformance at all. Choosing that label lost the
normative failure; choosing ``SPEC_VIOLATION`` would have lost the exposure. The
two are not alternatives, so they are no longer modelled as though they were.

Nothing here upgrades an observation into a claim. ``security_relevant`` says a
result touches a security property. ``vulnerability_status`` says what anyone has
actually established about exploiting it, and its default is that nobody has
looked.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interop_lab.models.severity import Severity


class SecurityCategory(StrEnum):
    """Which security property a result touches.

    A closed set rather than free text. The categories exist so results can be
    counted and compared across targets, which a prose field cannot support, and
    a new category is a deliberate addition rather than a typo away.
    """

    #: Origin validation, and by extension DNS-rebinding protection.
    ORIGIN_VALIDATION = "origin_validation"
    #: A routing or authorization header that disagrees with the body it
    #: accompanies, so an intermediary authorizes one method and the server
    #: executes another.
    HEADER_BODY_AUTHORIZATION_SPLIT = "header_body_authorization_split"
    #: Session identifiers: issuance, binding, reuse after termination.
    SESSION_BINDING = "session_binding"
    #: Content a client renders or forwards, where the server controls the bytes.
    HOSTILE_OUTPUT_HANDLING = "hostile_output_handling"
    #: Resource exhaustion and unbounded work.
    RESOURCE_EXHAUSTION = "resource_exhaustion"


class VulnerabilityStatus(StrEnum):
    """What anyone has established about exploiting a security-relevant result.

    Deliberately ordered from "nobody looked" to "a maintainer may be told". The
    gap between ``UNCONFIRMED`` and ``CONFIRMED_PRIVATE`` is the important one:
    it is the difference between observing that a server answered 200 to a
    request carrying a hostile Origin, and establishing that a real deployment
    can be driven by a page the user did not trust.
    """

    #: Not a security-relevant result, or one nobody has assessed.
    NOT_ASSESSED = "not_assessed"
    #: Security-relevant and observed, with practical exploitability unestablished.
    #: The honest default for a fresh observation.
    UNCONFIRMED = "unconfirmed"
    #: Practical exploitation established, under conditions recorded in a private
    #: finding package. Still private.
    CONFIRMED_PRIVATE = "confirmed_private"
    #: A human decided this may be disclosed. Never set by code.
    APPROVED_FOR_DISCLOSURE = "approved_for_disclosure"


#: Statuses that assert something about exploitability. Reaching one of these is
#: a human judgement recorded in a finding package, never a harness inference.
ASSERTS_EXPLOITABILITY = frozenset(
    {VulnerabilityStatus.CONFIRMED_PRIVATE, VulnerabilityStatus.APPROVED_FOR_DISCLOSURE}
)


class ResultDimensions(BaseModel):
    """The secondary, typed dimensions of one result.

    Every field answers a question the primary classification cannot. They are
    independent by construction: a result may be normative and not
    security-relevant, security-relevant and not normative, or both — and the
    last case is the one the previous single-enum design could not express.
    """

    model_config = ConfigDict(extra="forbid")

    #: An explicit normative assertion — one citing a dated requirement — was
    #: evaluated and decided. Says nothing about whether it passed.
    normative: bool = False
    #: This result touches a security property.
    security_relevant: bool = False
    security_category: SecurityCategory | None = None
    #: This result is about behaviour under fault, not about conformance.
    resilience_relevant: bool = False
    severity: Severity = Severity.INFO
    vulnerability_status: VulnerabilityStatus = VulnerabilityStatus.NOT_ASSESSED
    #: What the security reading does *not* establish. Required whenever the
    #: result is security-relevant: "the server did not validate Origin" invites
    #: a conclusion about exploitability that one HTTP exchange cannot support.
    security_limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_security_claim_carries_its_own_qualifications(self) -> ResultDimensions:
        if not self.security_relevant:
            if self.security_category is not None:
                raise ValueError(
                    "a security category was recorded on a result that is not marked "
                    "security-relevant. One of the two is wrong, and guessing which "
                    "would produce a category nobody assigned."
                )
            if self.vulnerability_status is not VulnerabilityStatus.NOT_ASSESSED:
                raise ValueError(
                    f"vulnerability_status is {self.vulnerability_status.value} on a "
                    "result that is not security-relevant. A status about exploiting "
                    "something has no subject here."
                )
            return self

        if self.security_category is None:
            raise ValueError(
                "a security-relevant result records no security category, so it "
                "cannot be counted alongside any other result of its kind"
            )
        if self.vulnerability_status is VulnerabilityStatus.NOT_ASSESSED:
            # A result cannot be both established as security-relevant and
            # unassessed for security relevance — that pairing reads, in a report,
            # as "nobody looked", which is the opposite of what happened.
            # ``UNCONFIRMED`` is the honest floor and the only correct value here:
            # this touches a security property, and nothing about exploiting it
            # has been established.
            #
            # Filled in rather than refused because it can only ever take this one
            # value, and a constructor that forgets it should not be able to
            # produce a record that quietly under-reports itself.
            object.__setattr__(self, "vulnerability_status", VulnerabilityStatus.UNCONFIRMED)
        if not self.security_limitations:
            raise ValueError(
                "a security-relevant result records no limitations. Every one of "
                "these reads stronger than the evidence behind it, and the limitation "
                "is what stops a report from being read as a vulnerability claim."
            )
        return self

    @property
    def is_confirmed_vulnerability(self) -> bool:
        """Whether anyone established practical exploitation.

        The property exists so a report can count confirmed vulnerabilities
        separately from security-relevant observations without every caller
        re-deciding what the boundary is. An observation is never a vulnerability
        merely because it is security-relevant.
        """
        return self.vulnerability_status in ASSERTS_EXPLOITABILITY

    @property
    def digest_material(self) -> dict[str, object]:
        """The part that belongs in a result signature.

        ``security_limitations`` is deliberately absent: it is prose written for
        a reader, and rewording it must not read as the result having changed.
        """
        return {
            "normative": self.normative,
            "security_relevant": self.security_relevant,
            "security_category": (
                self.security_category.value if self.security_category is not None else None
            ),
            "resilience_relevant": self.resilience_relevant,
            "severity": self.severity.value,
            "vulnerability_status": self.vulnerability_status.value,
        }


#: What a security-relevant observation from this harness does not establish.
#: Attached to any dimension record that states no limitations of its own.
DEFAULT_SECURITY_LIMITATIONS: tuple[str, ...] = (
    "Observed against one pinned build, under this harness's containment, on one "
    "code path. Whether the behaviour holds in a deployed configuration was not "
    "measured.",
    "Practical exploitability was not established. A security-relevant observation "
    "records what the target did, not what an attacker could achieve with it.",
    "Whether the protection is absent, opt-in, or delegated to a deployment layer "
    "is a separate question from whether this entry point exhibited it.",
)
