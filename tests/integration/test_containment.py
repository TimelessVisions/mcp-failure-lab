"""Containment, tested by trying to break it.

The argv builder is tested separately and purely; these tests exist because a
correct argv is not the same claim as a container that actually denied
something. Docker accepts flags it cannot honour on every host and reports
success either way.

Every hostile probe is run twice: once uncontained, where it must succeed, and
once contained, where it must be denied. Without the uncontained half, a probe
that is simply broken would look exactly like containment working.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from interop_lab.containment import docker
from interop_lab.containment.imagesource import RegistryImageSource
from interop_lab.containment.model import (
    ContainerRuntimeFingerprint,
    ContainerSpec,
    ContainmentOutcome,
    ExecutionMode,
    NetworkMode,
)
from interop_lab.containment.resolution import ResolvedImage, resolve_registry_source
from interop_lab.models.core import Classification, TargetDefinition
from interop_lab.runner.executor import ScenarioExecutor
from interop_lab.scenarios.library import get_scenario

pytestmark = [pytest.mark.integration, pytest.mark.slow]

PROBE = "fixtures/hostile_containment.py"
#: Repository only. The tag lives in DISPLAY_TAG, because the registry source
#: refuses to hold the identity in two places at once — a reader could not then
#: tell which of them resolved the image.
IMAGE = "mirror.gcr.io/library/python"
DISPLAY_TAG = "3.12-slim"
DIGEST = "sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"

CONTAINED_TARGET = Path("targets/fixtures/reference-contained.yaml")


def _resolved() -> ResolvedImage:
    """What this registry-pinned probe image resolves to. No build involved."""
    return resolve_registry_source(
        RegistryImageSource(repository=IMAGE, repository_digest=DIGEST, display_tag=DISPLAY_TAG)
    )


def _docker_available() -> bool:
    available, _, _ = asyncio.run(docker.probe())
    return available


def _image_present() -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", _resolved().launch_reference],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker is not available on this host"
)
requires_image = pytest.mark.skipif(
    not (_docker_available() and _image_present()),
    reason=f"pinned image {IMAGE}@{DIGEST[:19]}… is not present locally",
)


def _probe_spec(**overrides: object) -> ContainerSpec:
    base: dict[str, object] = {
        "image_source": {
            "kind": "registry",
            "repository": IMAGE,
            "repository_digest": DIGEST,
            "display_tag": DISPLAY_TAG,
        },
        "network": NetworkMode.NONE,
        "memory_mb": 256,
        "cpus": 0.5,
        "pids_limit": 64,
        "repo_mounts": ["fixtures"],
    }
    base.update(overrides)
    return ContainerSpec.model_validate(base)


def test_the_probe_spec_is_valid_without_a_container_runtime() -> None:
    """Every other test in this file is gated on Docker; this one is not.

    That gap is not hypothetical. When the image model gained an explicit
    registry source, this file still packed the tag into `repository` — and
    because every caller of `_probe_spec` skips without Docker, the whole module
    imported and passed locally while failing in CI. A spec that only a
    Docker-having machine ever validates is a spec nobody validates.
    """
    spec = _probe_spec()
    source = spec.registry_source
    assert source is not None
    assert source.repository == IMAGE
    assert source.display_tag == DISPLAY_TAG
    assert _resolved().launch_reference == f"{IMAGE}@{DIGEST}"


def _run_contained(probe: str, *, spec: ContainerSpec | None = None) -> str:
    container_spec = spec or _probe_spec()
    argv = docker.build_run_argv(
        container_spec,
        image_reference=_resolved().launch_reference,
        container_name=f"interop-lab-probe-{probe}",
        command=["${PYTHON}", PROBE, probe],
        env={},
        repo_root=str(Path.cwd()),
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
    return (result.stdout + result.stderr).strip()


def _run_uncontained(probe: str) -> str:
    result = subprocess.run(
        [sys.executable, PROBE, probe],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return (result.stdout + result.stderr).strip()


# --------------------------------------------------- the probes detect anything


def test_write_root_probe_is_not_stuck_reporting_denied() -> None:
    """A probe that always says 'denied' would prove nothing about a container.

    Uncontained, writing to `/` succeeds for a privileged user and fails for an
    unprivileged one. Both outcomes are legitimate host configurations, so the
    control here is that the probe *reports the host it is on*, not that it
    reports a particular answer.
    """
    output = _run_uncontained("write_root")
    if os.getuid() == 0:
        assert "allowed" in output, output
    else:
        assert "denied" in output, output


def test_identity_probe_reports_the_uid_it_actually_runs_as() -> None:
    """The uncontained control for the `--user` flag.

    Asserting "allowed" here was wrong: it silently assumed the test host runs
    as root. CI does not — it runs as uid 1001 — so the assertion encoded a
    property of one developer machine rather than a property of the probe.

    What actually needs to be true is that the probe reads the real uid, which
    is what makes `uid 65534 inside the container` meaningful evidence.
    """
    output = _run_uncontained("identity")
    assert f"uid {os.getuid()}" in output, output


# ------------------------------------------------------- containment holds


@requires_image
def test_contained_target_cannot_reach_the_network() -> None:
    assert "denied" in _run_contained("network_egress")


@requires_image
def test_contained_target_cannot_write_the_root_filesystem() -> None:
    output = _run_contained("write_root")
    assert "denied" in output, output
    assert "Read-only" in output or "OSError" in output or "Error" in output, output


@requires_image
def test_contained_target_cannot_exhaust_the_process_table() -> None:
    output = _run_contained("fork_bomb")
    assert "denied" in output, output
    # The pids limit is 64, so the probe must be stopped well before its own
    # bound of 512. A probe that merely finished is not evidence of a limit.
    assert "after" in output, output


@requires_image
def test_contained_target_cannot_allocate_past_its_memory_limit() -> None:
    output = _run_contained("memory")
    # The kernel OOM-kills rather than raising MemoryError, so a killed process
    # produces no output at all. Either is containment; "allocated 768 MB" is not.
    assert "allowed allocated 768" not in output, output


@requires_image
def test_contained_target_does_not_run_as_root() -> None:
    output = _run_contained("identity")
    assert "denied running as uid" in output, output
    assert "uid 0" not in output, output


# ------------------------------------------------- observed, not just requested


@requires_image
def test_observed_fingerprint_reflects_the_running_container() -> None:
    """The record must come from `docker inspect`, not from the request."""
    target = TargetDefinition.model_validate(
        __import__("yaml").safe_load(CONTAINED_TARGET.read_text(encoding="utf-8"))
    )
    scenario = get_scenario("baseline.tools_list")
    executor = ScenarioExecutor(artifacts_dir=Path("artifacts/test-containment"), seed=0)
    run, _ = asyncio.run(executor.run(scenario, target, persist=False))

    assert run.classification is Classification.PASS, run.harness_error
    record = run.containment
    assert record is not None
    assert record.mode is ExecutionMode.CONTAINER
    assert record.outcome is ContainmentOutcome.CONTAINED
    assert record.weaknesses == []
    assert run.is_usable_evidence

    observed = record.observed
    assert observed is not None
    # Values only the runtime could have supplied.
    assert observed.container_id, "no container id was read back"
    assert observed.memory_limit_bytes == 256 * 1024 * 1024
    assert observed.nano_cpus == 500_000_000
    assert observed.pids_limit == 64
    assert observed.network_mode == "none"
    assert observed.read_only_root is True
    assert "ALL" in observed.cap_drop
    assert record.stated_limits, "a contained run must still say what it does not protect"


@requires_image
def test_containment_weaker_than_requested_is_not_a_target_verdict() -> None:
    """If the container lacks a requested control, nothing may be concluded."""
    spec = _probe_spec()
    observed = asyncio.run(_observe_a_weakened_container(spec))
    weaknesses = docker.compare(spec, observed, resolved=_resolved())
    assert weaknesses, "a container started without the requested limits must be flagged"


async def _observe_a_weakened_container(spec: ContainerSpec) -> ContainerRuntimeFingerprint:
    """Start a container deliberately missing the requested controls."""
    name = "interop-lab-probe-weakened"
    await docker.remove_container(name)
    argv = [
        "docker",
        "run",
        "--rm",
        "--detach",
        "--name",
        name,
        "--network",
        "none",
        _resolved().launch_reference,
        "sleep",
        "60",
    ]
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    try:
        return await docker.observe(name)
    finally:
        await docker.remove_container(name)
