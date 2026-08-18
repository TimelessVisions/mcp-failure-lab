"""Structured redaction and environment scrubbing.

Targets are untrusted. The harness must never persist inherited secrets, and
must never forward host credentials into a target process.
"""

from __future__ import annotations

import os
import re
from typing import Final

#: Environment names always forwarded, because a process cannot start without them.
BASE_ENV_ALLOWLIST: Final = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"})

#: Names that must never be forwarded even if a target asks for them.
ENV_DENYLIST_PATTERNS: Final = (
    re.compile(r"(?i)token"),
    re.compile(r"(?i)secret"),
    re.compile(r"(?i)password"),
    re.compile(r"(?i)passwd"),
    re.compile(r"(?i)api[_-]?key"),
    re.compile(r"(?i)credential"),
    re.compile(r"(?i)private[_-]?key"),
    re.compile(r"(?i)session"),
    re.compile(r"(?i)^aws_"),
    re.compile(r"(?i)^gh_"),
    re.compile(r"(?i)^github_"),
    re.compile(r"(?i)^anthropic"),
    re.compile(r"(?i)^openai"),
)

_REDACTION_PATTERNS: Final = (
    # Long opaque tokens, bearer headers, and PEM blocks.
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]{16,}"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{16,})\b"), "<redacted-github-token>"),
    (re.compile(r"(?i)\b(sk-[A-Za-z0-9]{16,})\b"), "<redacted-api-key>"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        "<redacted-private-key>",
    ),
    (
        re.compile(r"(?i)(\"(?:password|secret|token|api_key|apikey)\"\s*:\s*\")[^\"]+"),
        r"\1<redacted>",
    ),
)


def is_denied(name: str) -> bool:
    return any(pattern.search(name) for pattern in ENV_DENYLIST_PATTERNS)


def scrub_environment(
    allowlist: list[str] | None = None,
    extra: dict[str, str] | None = None,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a target environment from an allowlist, never the inherited one.

    The denylist wins over the allowlist: a caller cannot opt a credential in.
    """
    env_source = dict(os.environ) if source is None else dict(source)
    permitted = BASE_ENV_ALLOWLIST | set(allowlist or ())
    result = {
        name: value
        for name, value in env_source.items()
        if name in permitted and not is_denied(name)
    }
    for name, value in (extra or {}).items():
        if is_denied(name):
            raise ValueError(f"refusing to forward credential-shaped variable {name!r}")
        result[name] = value
    return result


def redact_text(text: str) -> tuple[str, bool]:
    """Return redacted text and whether anything was replaced."""
    redacted = text
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted, redacted != text


#: Header names whose values must never be persisted.
SENSITIVE_HEADERS: Final = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "mcp-session-id",
    }
)


def redact_headers(headers: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Redact sensitive header values, preserving which headers were present.

    Presence is itself evidence — a scenario may assert that a header was sent —
    so the name is kept and only the value is replaced.
    """
    result: dict[str, str] = {}
    changed = False
    for name, value in headers.items():
        if name.lower() in SENSITIVE_HEADERS:
            result[name] = "<redacted>"
            changed = True
            continue
        redacted, was_changed = redact_text(value)
        result[name] = redacted
        changed = changed or was_changed
    return result, changed
