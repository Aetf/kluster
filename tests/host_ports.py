"""A host port a case publishes a container on, reserved by the case rather than picked by podman.

podman picks the port of a `--publish` with no host port by listening on
port 0, reading the port and closing the listener; pasta binds that port
later, when the container's network comes up (podman#19048). Anything that
binds port 0 in between can be handed the same port -- `pulumi`'s engine,
language hosts and providers among them -- and pasta then fails with
`Address already in use`. Nothing takes a port outside the network
namespace's ephemeral range (`net.ipv4.ip_local_port_range`) by chance:
neither a bind of port 0 nor a `connect()` hands one out, so the only
process that can take such a port is one that names it.

`reserved` hands out such a port:

-   **It comes from `BLOCK`**, below both the ephemeral range and
    Kubernetes' default NodePort range, and a block that overlaps the
    ephemeral range of this process's network namespace is refused, naming
    the setting. That namespace is where a case dials the address the port
    is published on, so it is where the port lives.
-   **A reservation is a bind of the abstract Unix socket
    `kluster-test-port-<n>`.** The bind is atomic -- a second one fails with
    `EADDRINUSE` -- and scoped to the network namespace, as the port is.
    The kernel releases it when the holder exits however it exits, so a
    run killed before its teardown leaves no reservation behind, as a lock
    file would.
-   **A reserved port is bound once over TCP and closed**, with
    `SO_REUSEADDR` as pasta binds it (`sock_l4` in passt's `util.c`), so a
    port a service holds is passed over. The gap that leaves is open only
    to a process that binds that very number.
-   **The walk starts at a random port of the block**, and a block with no
    port left is refused, naming the block.

The caller holds the reservation until the `podman run` that publishes the
port has returned. By then pasta holds the port, and another run's TCP bind
passes over it.
"""

from __future__ import annotations

import errno
import ipaddress
import random
import socket
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

#: The ports a reservation is taken from: below Kubernetes' default NodePort
#: range, 30000-32767, and below the ephemeral range Linux starts with,
#: 32768-60999.
BLOCK = range(20000, 30000)

#: The namespace's ephemeral range: the ports a bind of port 0, and a
#: `connect()`, are handed.
EPHEMERAL = Path('/proc/sys/net/ipv4/ip_local_port_range')

#: What each reservation's abstract socket is named, before the port.
PREFIX = 'kluster-test-port'


class Refused(RuntimeError):
    """No port could be reserved: the block overlaps the ephemeral range, or holds no free port."""


def ephemeral() -> range:
    """The ephemeral range of this process's network namespace."""
    low, high = (int(field) for field in EPHEMERAL.read_text().split())
    return range(low, high + 1)


def _named(block: Sequence[int]) -> str:
    if isinstance(block, range) and block.step == 1 and block:
        return f'ports {block.start}-{block.stop - 1}'
    return f'ports {", ".join(str(port) for port in block)}'


def _bound_by_another(address: str, port: int) -> bool:
    """Whether a TCP bind of `port` on `address`, as pasta makes it, is refused."""
    family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
        except OSError as refused:
            if refused.errno == errno.EADDRINUSE:
                return True
            raise
    return False


def _reservation(port: int, prefix: str) -> socket.socket | None:
    """The bound abstract socket that reserves `port`, or `None` where another holds it."""
    holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        holder.bind(f'\0{prefix}-{port}')
    except OSError as refused:
        holder.close()
        if refused.errno == errno.EADDRINUSE:
            return None
        raise
    return holder


@contextmanager
def reserved(address: str, block: Sequence[int] = BLOCK, *, prefix: str = PREFIX) -> Generator[int]:
    """A TCP port of `address` from `block`, reserved for the length of the block.

    `prefix` names the set of reservations this one is a member of. Every
    publisher passes the default; a case about this module passes one of its
    own, so that what it reserves meets no other run's.
    """
    taken_by_chance = ephemeral()
    if any(port in taken_by_chance for port in block):
        raise Refused(
            f'{_named(block)} overlap the ephemeral range, net.ipv4.ip_local_port_range '
            f'{taken_by_chance.start}-{taken_by_chance.stop - 1}, where a bind of port 0 can be handed any of them'
        )
    start = random.randrange(len(block)) if block else 0
    for port in [*block[start:], *block[:start]]:
        holder = _reservation(port, prefix)
        if holder is None:
            continue
        with holder:
            if _bound_by_another(address, port):
                continue
            yield port
            return
    raise Refused(f'{_named(block)} on {address}: every one is reserved or bound')
