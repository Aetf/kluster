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
    Kubernetes' default NodePort range, and ports that overlap the
    ephemeral range of this process's network namespace are refused
    (`OverlapsEphemeral`). That namespace is where a case dials the address
    the port is published on, so it is where the port lives.
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
-   **The walk starts at a random one of the ports**, and ports with none
    left are refused (`Exhausted`).

The caller holds the reservation until the `podman run` that publishes the
port has returned. By then pasta holds the port, and another run's TCP bind
passes over it.

**This is a workaround layer for podman#19048** (style/README.md, "A
workaround for an upstream defect is a layer of its own"). Its members:

-   this module and `tests/test_host_ports.py`;
-   the reservation around each `podman run` that publishes a port: in
    `_network` of `tests/test_state_roles.py`, which every network
    container there is started through, and in the `box` fixture of
    `tests/test_release_lock.py`;
-   the `port` those sites pass to `_published` and `_network_container`,
    and to `--publish` in that fixture;
-   `test_the_port_a_box_is_reached_on_lies_outside_the_ephemeral_range`
    and the tripwire,
    `test_remove_the_reserved_ports_once_podman_holds_the_port_it_picks`,
    both in `tests/test_state_roles.py`;
-   the paragraph of framework/testing.md §1.2 on the port a container is
    published on.

**They go once the podman on CI's runner holds a host port from the moment
it picks it**, so that a container created with `--publish ADDRESS::PORT`
carries no host port another process can bind; the tripwire fails there
then. Each publisher then goes back to `--publish ADDRESS::PORT` and reads
its port back with `podman port`.
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

#: The setting that holds the namespace's ephemeral range: the ports a bind
#: of port 0, and a `connect()`, are handed.
PORT_RANGE_SETTING = Path('/proc/sys/net/ipv4/ip_local_port_range')

#: What each reservation's abstract socket is named, before the port.
PREFIX = 'kluster-test-port'


class Refused(RuntimeError):
    """No port could be reserved."""


def _named(ports: Sequence[int]) -> str:
    if isinstance(ports, range) and ports.step == 1 and ports:
        return f'ports {ports.start}-{ports.stop - 1}'
    return f'ports {", ".join(str(port) for port in ports)}'


class OverlapsEphemeral(Refused):
    """`ports` overlap `ephemeral`, the namespace's ephemeral range, where a bind of port 0 can be handed any of them."""

    def __init__(self, ports: Sequence[int], ephemeral: range) -> None:
        self.ports = ports
        self.ephemeral = ephemeral
        super().__init__(
            f'{_named(ports)} overlap the ephemeral range, net.ipv4.ip_local_port_range '
            f'{ephemeral.start}-{ephemeral.stop - 1}, where a bind of port 0 can be handed any of them'
        )


class Exhausted(Refused):
    """Every one of `ports` on `address` is reserved or bound."""

    def __init__(self, ports: Sequence[int], address: str) -> None:
        self.ports = ports
        self.address = address
        super().__init__(f'{_named(ports)} on {address}: every one is reserved or bound')


def ephemeral() -> range:
    """The ephemeral range of this process's network namespace."""
    low, high = (int(field) for field in PORT_RANGE_SETTING.read_text().split())
    return range(low, high + 1)


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
def reserved(address: str, ports: Sequence[int] = BLOCK, *, prefix: str = PREFIX) -> Generator[int]:
    """A TCP port of `address` from `ports`, reserved while the context is open.

    `prefix` names the set of reservations this one is a member of. Every
    publisher passes the default; a case about this module passes one of its
    own, so that what it reserves meets no other run's.
    """
    taken_by_chance = ephemeral()
    if any(port in taken_by_chance for port in ports):
        raise OverlapsEphemeral(ports, taken_by_chance)
    start = random.randrange(len(ports)) if ports else 0
    for port in [*ports[start:], *ports[:start]]:
        holder = _reservation(port, prefix)
        if holder is None:
            continue
        with holder:
            if _bound_by_another(address, port):
                continue
            yield port
            return
    raise Exhausted(ports, address)
