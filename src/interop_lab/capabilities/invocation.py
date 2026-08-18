"""Operator-approved safe tool invocations.

The tool-call baseline was tied to a literal ``echo`` tool with a literal
``{"text": ...}`` argument. That is a fixture's tool surface, and it matches none
of the external matrix: `mcp-server-time` has time tools, the official TypeScript
server's echo takes ``message``, and `mcp-go` advertises its own set. So the
scenario that exercises the primary tool-invocation path was NOT_APPLICABLE
against every third-party target the project measures.

The obvious fix — synthesise a call from whatever schema the target advertises —
is refused here, and the refusal is the point. A tool surface is untrusted input.
Generating a call from it means the harness decides, at run time and from a
document the target wrote, to invoke a named operation with arguments it made up.
That is how a differential test sends `delete_file` to a server that advertised
one, and no amount of care in the generator makes it a decision this project
should be making automatically.

So an invocation is a **fixture**: written by an operator, reviewed like any other
committed input, and naming exactly one tool with exactly the arguments it will
be called with. Observed capabilities do not supply it — they *confirm* it. The
tool must exist and its input schema must still hash to the value the contract
was written against, and when either fails the scenario becomes unresolved or
capability-drifted rather than being called anyway.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SideEffectClass(StrEnum):
    """What invoking the tool is permitted to change.

    Only two values, and there is deliberately no third. A class for "writes to a
    database, but only a test one" or "sends a message, but only to a sandbox"
    would be a place to put a judgement that nobody can verify from here — the
    target is a container this project did not write, and its idea of a sandbox
    is not observable.
    """

    #: Reads or computes. Nothing outside the process changes.
    NONE = "none"
    #: Writes only inside the container, which is destroyed after the run.
    LOCAL_TEMPORARY = "local_temporary"


#: Substrings that name a capability an approved invocation must not have.
#:
#: A blocklist is a weak instrument and is not the primary control — the primary
#: control is that a human writes each invocation. This exists to catch the
#: obvious mistake at load time rather than in a report afterwards, which is
#: worth having precisely because it is cheap.
FORBIDDEN_TOOL_SUBSTRINGS: tuple[str, ...] = (
    "delete",
    "remove",
    "rm_",
    "write",
    "create",
    "update",
    "exec",
    "shell",
    "command",
    "spawn",
    "eval",
    "fetch",
    "http",
    "request",
    "download",
    "upload",
    "send",
    "email",
    "mail",
    "sms",
    "message_",
    "post",
    "publish",
    "credential",
    "secret",
    "token",
    "password",
    "auth",
    "key",
    "sudo",
    "admin",
)


class SafeToolInvocation(BaseModel):
    """One reviewed, safe call this target may be asked to perform.

    Every field exists because a reader of a report has to be able to answer a
    specific question without leaving the artifact: what was called, with what,
    why that call, what it was allowed to change, what the answer should look
    like, and what the result does not establish.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    #: The exact arguments sent. Synthetic values only — see the validator below.
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Why this call, in terms of what it exercises. Required, because an
    #: invocation nobody could justify in a sentence is one nobody reviewed.
    purpose: str
    #: The input-schema hash this contract was written against. Confirmed against
    #: the observed surface before the call is made: a tool that kept its name
    #: while changing its contract would otherwise be sent arguments written for
    #: a schema that no longer exists.
    expected_input_schema_sha256: str
    side_effect_class: SideEffectClass = SideEffectClass.NONE
    #: What a conforming answer looks like, in prose, for the report.
    expected_result_shape: str = ""
    #: What this call does not establish. Always populated for the same reason
    #: capability records carry limitations: "the tool worked" invites a stronger
    #: reading than one call supports.
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _the_tool_is_not_one_that_may_never_be_called(self) -> SafeToolInvocation:
        lowered = self.tool_name.lower()
        for forbidden in FORBIDDEN_TOOL_SUBSTRINGS:
            if forbidden in lowered:
                raise ValueError(
                    f"safe invocation names tool {self.tool_name!r}, whose name contains "
                    f"{forbidden!r}. Network, filesystem-destructive, credential-bearing, "
                    "messaging and execution tools are not invoked by this harness against "
                    "anyone's server. If the name is a false positive, the tool still has "
                    "to be approved under a name that does not read as one of those."
                )
        return self

    @model_validator(mode="after")
    def _a_purpose_is_stated(self) -> SafeToolInvocation:
        if not self.purpose.strip():
            raise ValueError(
                f"safe invocation of {self.tool_name!r} states no purpose. An invocation "
                "nobody could justify in a sentence is one nobody reviewed."
            )
        return self

    @model_validator(mode="after")
    def _arguments_are_synthetic(self) -> SafeToolInvocation:
        """Refuse anything that reads like a real path, address or credential.

        Recursive: through dictionaries, through arrays, and through strings at
        any depth. The previous version looked only at top-level values that were
        already strings, which left a URL inside a nested object, a path inside an
        array and a credential-shaped key two levels down entirely uninspected —
        and tool input schemas are routinely objects of objects, so the shape most
        tools actually take was the shape it did not cover.

        Not a security boundary — a determined author can write ``/tmp/x`` as
        ``/t`` + ``mp/x`` — and emphatically not a substitute for the human
        approval that is the real control. What it stops is the realistic mistake:
        someone reaching for a plausible-looking value because a synthetic one
        felt artificial. The whole point of a synthetic argument is that it cannot
        name anything that exists.
        """
        from interop_lab.capabilities.argument_safety import UnsafeArgumentError, check_arguments

        try:
            check_arguments(self.arguments)
        except UnsafeArgumentError as exc:
            # Re-raised with the tool name and the JSON pointer, because against a
            # nested structure "an argument looks like a URL" identifies nothing.
            raise ValueError(
                f"safe invocation of {self.tool_name!r} has an unsafe argument at "
                f"{exc.pointer or '<root>'}: {exc.reason}"
            ) from exc
        return self

    def schema_agrees(self, observed_input_schema_sha256: str | None) -> bool:
        """Whether the observed tool still has the contract this was written for."""
        return (
            observed_input_schema_sha256 is not None
            and observed_input_schema_sha256 == self.expected_input_schema_sha256
        )


#: What a safe invocation does not establish, attached to every one that omits
#: its own list.
DEFAULT_INVOCATION_LIMITATIONS: tuple[str, ...] = (
    "One tool, one argument set, one call. A conforming answer here says nothing "
    "about the target's other tools or about this one under different arguments.",
    "The invocation is an operator-approved fixture, not an observation. It "
    "records what this project chose to send, and the capability record records "
    "what the target offered.",
)
