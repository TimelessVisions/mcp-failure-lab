"""Probes that attempt exactly what containment is supposed to prevent.

This is not an MCP server and never speaks the protocol. It exists so the
harness's containment claims are tested by trying to break them rather than by
reading back the flags that were passed — those two things fail independently.

Each probe prints a single line, ``<name>: allowed`` or ``<name>: denied
<reason>``, and exits 0 either way. Deciding whether "allowed" is a failure
belongs to the test, not to the probe: the same script is also run *without* a
container, where "allowed" is the expected result and is what proves the probe
can detect the thing it claims to detect.

Run as: python hostile_containment.py <probe>
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys

#: Well-known addresses used only to test whether egress is possible at all.
#: No data is sent and nothing about the responder is recorded.
EGRESS_PROBES = (("1.1.1.1", 53), ("8.8.8.8", 443))

CONNECT_TIMEOUT_S = 3.0


def probe_network_egress() -> str:
    """Can the target reach the Internet?"""
    for host, port in EGRESS_PROBES:
        try:
            with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
                return f"allowed reached {host}:{port}"
        except OSError:
            continue
    return "denied no route to any probe address"


def probe_write_root() -> str:
    """Can the target write outside its declared scratch space?"""
    try:
        with open("/interop-lab-escape", "w", encoding="utf-8") as handle:
            handle.write("x")
    except OSError as exc:
        return f"denied {type(exc).__name__}"
    os.unlink("/interop-lab-escape")
    return "allowed wrote to /"


def probe_fork_bomb() -> str:
    """Can the target exhaust the host's process table?

    Bounded deliberately: the point is to hit the pids limit, not to actually
    exhaust anything. A probe that could damage the machine it runs on would be
    a worse bug than the one it is testing for.
    """
    limit = 512
    children: list[int] = []
    try:
        for _ in range(limit):
            pid = os.fork()
            if pid == 0:  # pragma: no cover - child never returns to the test
                os._exit(0)
            children.append(pid)
    except OSError as exc:
        return f"denied after {len(children)} forks: {type(exc).__name__}"
    finally:
        for pid in children:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
    return f"allowed forked {len(children)} processes"


def probe_memory() -> str:
    """Can the target allocate past its declared memory limit?"""
    chunks: list[bytearray] = []
    megabytes = 0
    try:
        # Above the 256 MB used by the contained fixtures, bounded so an
        # uncontained run cannot swamp the host.
        while megabytes < 768:
            chunks.append(bytearray(1024 * 1024))
            megabytes += 1
    except MemoryError:
        return f"denied MemoryError at {megabytes} MB"
    finally:
        chunks.clear()
    return f"allowed allocated {megabytes} MB"


def probe_identity() -> str:
    """Is the target running as root?"""
    uid = os.getuid()
    return f"allowed running as uid {uid}" if uid == 0 else f"denied running as uid {uid}"


PROBES = {
    "network_egress": probe_network_egress,
    "write_root": probe_write_root,
    "fork_bomb": probe_fork_bomb,
    "memory": probe_memory,
    "identity": probe_identity,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in PROBES:
        print(f"usage: {argv[0]} <{'|'.join(sorted(PROBES))}>", file=sys.stderr)
        return 2
    name = argv[1]
    print(f"{name}: {PROBES[name]()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
