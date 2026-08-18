"""Environment scrubbing and redaction.

Targets are untrusted. These tests guard the two directions that matter: we must
not hand a credential to a target, and we must not persist one into an artifact.
"""

from __future__ import annotations

import pytest

from interop_lab.security.redaction import redact_text, scrub_environment


def test_inherited_environment_is_not_forwarded_by_default() -> None:
    source = {"PATH": "/usr/bin", "MY_APP_SETTING": "value", "HOME": "/root"}
    env = scrub_environment(source=source)
    assert env == {"PATH": "/usr/bin", "HOME": "/root"}
    assert "MY_APP_SETTING" not in env


def test_allowlist_admits_named_variables() -> None:
    source = {"PATH": "/usr/bin", "MY_APP_SETTING": "value"}
    env = scrub_environment(["MY_APP_SETTING"], source=source)
    assert env["MY_APP_SETTING"] == "value"


@pytest.mark.parametrize(
    "name",
    [
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "MY_API_KEY",
        "DB_PASSWORD",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SESSION_ID",
        "SERVICE_PRIVATE_KEY",
    ],
)
def test_denylist_wins_over_allowlist(name: str) -> None:
    """A caller must not be able to opt a credential into the target environment."""
    env = scrub_environment([name], source={name: "sensitive", "PATH": "/usr/bin"})
    assert name not in env


def test_explicit_extra_cannot_smuggle_a_credential() -> None:
    with pytest.raises(ValueError, match="credential-shaped"):
        scrub_environment([], extra={"GITHUB_TOKEN": "ghp_x"}, source={})


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        'here is {"password": "hunter2hunter2"} in a body',
        "key sk-abcdefghijklmnopqrstuvwxyz0123",
    ],
)
def test_secrets_are_redacted(text: str) -> None:
    redacted, changed = redact_text(text)
    assert changed
    assert "hunter2" not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted


def test_private_key_block_is_redacted() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nAAAAsecretmaterialAAAA\n-----END RSA PRIVATE KEY-----"
    redacted, changed = redact_text(pem)
    assert changed
    assert "secretmaterial" not in redacted


def test_ordinary_protocol_traffic_is_untouched() -> None:
    payload = '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"hello"}]}}'
    redacted, changed = redact_text(payload)
    assert not changed
    assert redacted == payload
