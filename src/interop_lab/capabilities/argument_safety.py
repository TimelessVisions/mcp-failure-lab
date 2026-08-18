"""Recursive inspection of an approved invocation's arguments.

The previous check looked at ``arguments`` one level deep and only at values that
were already strings. Everything below that was invisible: a URL inside a nested
object, a path inside an array, a credential-shaped key two levels down. Tool
input schemas are routinely objects of objects, so "top level only" meant the
check did not cover the shape most tools actually take.

**This is a backstop, not the control.** The control is that a human writes each
invocation and it is reviewed like any other committed input. A blocklist cannot
establish that a call is safe — a determined author can write ``/tmp/x`` as
``"/t" "mp/x"`` and nothing here would notice, and no amount of pattern-matching
decides whether calling someone's ``summarise`` tool is a good idea. What it does
is catch the realistic mistake, which is an author reaching for a plausible
value because a synthetic one felt artificial, and catch it at load time with a
pointer to exactly where it is rather than in a report afterwards.

Every refusal names the full JSON pointer of the offending value, because "an
argument looks like a URL" is not actionable against a nested structure.
"""

from __future__ import annotations

import re
from typing import Any

#: How deep an approved invocation's arguments may nest.
#:
#: Not a security boundary — it is a statement about reviewability. An argument
#: structure nobody can hold in their head is one nobody reviewed, and the
#: invocation is supposed to be a fixture a person read and approved.
MAX_ARGUMENT_DEPTH = 8

#: Key-name tokens that make an argument credential-shaped whatever its value.
#:
#: Matched against the key split on non-alphanumeric boundaries, so ``api_key``
#: and ``apiKey`` both reduce to a ``key`` token while ``author`` does not reduce
#: to ``auth``. Substring matching on these produces exactly that false positive.
CREDENTIAL_KEY_TOKENS: frozenset[str] = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "key",
        "passphrase",
        "passwd",
        "password",
        "private",
        "secret",
        "session",
        "signature",
        "token",
    }
)

#: Substrings that make a *value* credential-shaped.
CREDENTIAL_VALUE_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "-----begin",
    "bearer ",
)

#: Shell metacharacters and substitutions. A synthetic argument has no reason to
#: contain any of them, and a tool that interpolates one into a command line is
#: exactly the case a reviewer needs to see before the call is made.
SHELL_FRAGMENTS: tuple[str, ...] = (
    "$(",
    "${",
    "`",
    "&&",
    "||",
    ";",
    "|",
    ">>",
    "<(",
    ">(",
    "\n",
    "\r",
)

#: Traversal, including the percent-encoded spelling a validator that only looked
#: for literal dots would miss.
TRAVERSAL_FRAGMENTS: tuple[str, ...] = ("../", "..\\", "%2e%2e", "..%2f")

#: ``$NAME`` and ``%NAME%``. ``${...}`` is already covered as a shell fragment.
_ENV_REFERENCE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*|%[A-Z_][A-Z0-9_]*%")

#: A Windows drive-qualified path: ``C:\`` or ``C:/``.
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")

#: A dotted name whose final label is alphabetic — ``example.com``,
#: ``host.internal``. Deliberately *not* matched by ``1.2.3`` or ``v2.0``, whose
#: last label is numeric, so ordinary version-shaped synthetic values survive.
_HOSTNAME = re.compile(
    r"\b[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}\b"
)

#: A dotted-quad, with or without a port.
_IPV4 = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")

#: Loopback by name, which is the host a locally bound MCP server listens on and
#: therefore the one an argument must least of all be able to name.
_LOOPBACK = re.compile(r"\blocalhost\b|\bip6-localhost\b")


class UnsafeArgumentError(ValueError):
    """An approved invocation names something that exists, or could.

    Carries the JSON pointer so the message identifies one value inside a nested
    structure rather than describing the argument set as a whole.
    """

    def __init__(self, pointer: str, reason: str) -> None:
        self.pointer = pointer
        self.reason = reason
        super().__init__(f"{pointer or '<root>'}: {reason}")


def json_pointer(segments: list[str | int]) -> str:
    """RFC 6901 pointer for a position inside the argument structure.

    Escaping matters here rather than being pedantry: an argument key containing
    a slash would otherwise produce a pointer that reads as two levels, and the
    pointer is the whole reason this error is actionable.
    """
    parts = [
        str(segment) if isinstance(segment, int) else segment.replace("~", "~0").replace("/", "~1")
        for segment in segments
    ]
    return "".join(f"/{part}" for part in parts)


def _check_key(key: str, pointer: str) -> None:
    tokens = {token for token in re.split(r"[^A-Za-z0-9]+", _split_camel(key).lower()) if token}
    offending = sorted(tokens & CREDENTIAL_KEY_TOKENS)
    if offending:
        raise UnsafeArgumentError(
            pointer,
            f"the argument name {key!r} is credential-shaped (contains {offending[0]!r}). "
            "Nothing resembling a credential — by name or by value — is sent to a target.",
        )


def _split_camel(text: str) -> str:
    """``apiKey`` -> ``api Key`` so camelCase names tokenise like snake_case ones."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)


def _check_string(value: str, pointer: str) -> None:
    """Refuse a string that could name something outside this process."""
    lowered = value.lower()

    for fragment in TRAVERSAL_FRAGMENTS:
        if fragment in lowered:
            raise UnsafeArgumentError(
                pointer,
                f"the value {value!r} contains the traversal sequence {fragment!r}",
            )

    if lowered.startswith(("/", "~", "\\\\", "./", ".\\")) or _DRIVE_PATH.match(value):
        raise UnsafeArgumentError(
            pointer,
            f"the value {value!r} looks like a filesystem path. Safe invocations use "
            "synthetic arguments that name nothing on any machine.",
        )

    if "://" in lowered or lowered.startswith(("www.", "//")):
        raise UnsafeArgumentError(
            pointer,
            f"the value {value!r} looks like a URL. No invocation this harness "
            "performs may direct a target at a network location.",
        )

    if _LOOPBACK.search(lowered) or _IPV4.search(value) or _HOSTNAME.search(lowered):
        raise UnsafeArgumentError(
            pointer,
            f"the value {value!r} looks like a host a network operation could reach. "
            "A tool given a resolvable name may connect to it, and the harness does "
            "not decide at run time to make a third-party server open a socket.",
        )

    for substring in CREDENTIAL_VALUE_SUBSTRINGS:
        if substring in lowered:
            raise UnsafeArgumentError(
                pointer,
                f"the value {value!r} is credential-shaped (contains {substring!r})",
            )

    for fragment in SHELL_FRAGMENTS:
        if fragment in value:
            raise UnsafeArgumentError(
                pointer,
                f"the value {value!r} contains the shell fragment {fragment!r}. A tool "
                "that interpolates it into a command line would execute it.",
            )

    if _ENV_REFERENCE.search(value):
        raise UnsafeArgumentError(
            pointer,
            f"the value {value!r} references an environment variable. Whatever it "
            "expands to is by definition not synthetic.",
        )


def check_value(value: Any, segments: list[str | int]) -> None:
    """Walk one value, refusing anything that could name something real.

    Recurses through every container. Numbers, booleans and null pass at any
    depth: none of them can name a path, a host or a credential, and refusing
    them would only make honest fixtures harder to write.
    """
    pointer = json_pointer(segments)
    if len(segments) > MAX_ARGUMENT_DEPTH:
        raise UnsafeArgumentError(
            pointer,
            f"the argument structure nests deeper than {MAX_ARGUMENT_DEPTH} levels. "
            "An invocation nobody can read is one nobody reviewed.",
        )

    if value is None or isinstance(value, bool | int | float):
        return

    if isinstance(value, str):
        _check_string(value, pointer)
        return

    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise UnsafeArgumentError(
                    pointer, f"the key {key!r} is not a string, so this is not JSON"
                )
            child_segments = [*segments, key]
            _check_key(key, json_pointer(child_segments))
            check_value(child, child_segments)
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            check_value(child, [*segments, index])
        return

    raise UnsafeArgumentError(
        pointer,
        f"the value is a {type(value).__name__}, which has no JSON representation. "
        "An approved invocation records exactly the bytes that will be sent.",
    )


def check_arguments(arguments: dict[str, Any]) -> None:
    """Validate a whole argument set. Raises on the first refusal, with its pointer."""
    check_value(arguments, [])
