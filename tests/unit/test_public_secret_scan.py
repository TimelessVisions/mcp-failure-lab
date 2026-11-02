"""The public CI secret scan must ignore generated junit output and still fail
on a real secret-shaped string anywhere else.

``public-junit.xml`` repeats detector fixtures from tests that are already
excluded by path. Scanning that report turned a green suite into a red job.
Excluding the report must not exclude an ordinary file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCAN = Path("tools/public_secret_scan.sh")


def fake_aws_key() -> str:
    # Split so this source file does not itself match the scanner.
    return "AKIA" + "IOSFODNN7EXAMPLE"


def fake_github_token() -> str:
    return "ghp_" + ("b" * 24)


def fake_finding_id() -> str:
    return "ILAB-EXT-" + "4"


def scan(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCAN), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_generated_junit_with_detector_fixtures_does_not_fail(tmp_path: Path) -> None:
    report = tmp_path / "public-junit.xml"
    report.write_text(
        "\n".join((fake_aws_key(), fake_github_token(), fake_finding_id())),
        encoding="utf-8",
    )
    (tmp_path / "pytest.log").write_text(fake_aws_key(), encoding="utf-8")

    result = scan(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAILED" not in result.stdout


def test_a_planted_secret_outside_the_generated_report_still_fails(tmp_path: Path) -> None:
    """Negative control: the same pattern in an ordinary file fails the scan."""
    (tmp_path / "public-junit.xml").write_text(fake_aws_key(), encoding="utf-8")
    planted = tmp_path / "notes.txt"
    planted.write_text("leak " + fake_aws_key() + "\n", encoding="utf-8")

    result = scan(tmp_path)

    assert result.returncode == 1
    assert "notes.txt" in result.stdout
    assert "public-junit.xml" not in result.stdout
    assert "no AWS access key id" in result.stdout
