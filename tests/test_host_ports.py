"""`host_ports.reserved`: a reserved port is handed to one holder, skips a bound port, and stays out of the ephemeral range.

Each case works on ports it holds a reservation of through the default
`prefix`, so no other run of the suite publishes on them, and reserves among
them under a `prefix` of its own, so its reservations meet no other run's.
A reservation another process holds is held by a child that reserves, prints
the port and then reads its stdin until the case ends it. Every wait is on an
event -- a line the child prints, the child's exit -- and the bounds are
stop-losses.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import sys
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import host_ports
import process_sessions
import pytest

ADDRESS = '127.0.0.1'

#: The bound on a child's exit once it is killed: a stop-loss, which nothing
#: asserts on.
EXIT_TIMEOUT = 30

#: A process that reserves a port of `argv[4:]` under the prefix `argv[3]`,
#: prints it, and holds it until its stdin closes.
HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
import host_ports
with host_ports.reserved(sys.argv[2], [int(port) for port in sys.argv[4:]], prefix=sys.argv[3]) as port:
    print(port, flush=True)
    sys.stdin.read()
"""


@pytest.fixture
def ports() -> Iterator[tuple[int, int]]:
    """Two ports reserved through the default prefix, for the case's length."""
    with host_ports.reserved(ADDRESS) as first, host_ports.reserved(ADDRESS) as second:
        yield first, second


@pytest.fixture
def prefix() -> str:
    return f'kluster-test-port-case-{uuid.uuid4().hex}'


def _held(stack: ExitStack, prefix: str, block: tuple[int, ...]) -> tuple[process_sessions.Command, int]:
    """A child holding a reservation from `block`, and the port it holds."""
    child = stack.enter_context(
        process_sessions.started(
            [sys.executable, '-c', HOLDER, str(Path(__file__).parent), ADDRESS, prefix, *map(str, block)],
            stdin=process_sessions.PIPE,
            stdout=process_sessions.PIPE,
            text=True,
        )
    )
    assert child.stdout is not None
    printed = child.stdout.readline()
    assert printed, f'the holder ended without reserving from {block}'
    return child, int(printed)


def test_a_port_another_process_holds_is_not_handed_out(ports: tuple[int, int], prefix: str) -> None:
    with ExitStack() as stack:
        _, theirs = _held(stack, prefix, ports)
        with host_ports.reserved(ADDRESS, ports, prefix=prefix) as ours:
            pass

    assert {theirs, ours} == set(ports)


def test_a_block_whose_every_port_is_held_is_refused_naming_the_block(ports: tuple[int, int], prefix: str) -> None:
    held, _ = ports
    with ExitStack() as stack:
        _ = _held(stack, prefix, (held,))
        with pytest.raises(host_ports.Refused, match=re.escape(f'ports {held} on {ADDRESS}')):
            stack.enter_context(host_ports.reserved(ADDRESS, (held,), prefix=prefix))


def test_a_port_a_listener_holds_is_skipped(ports: tuple[int, int], prefix: str) -> None:
    bound, free = ports
    with socket.socket() as listener:
        listener.bind((ADDRESS, bound))
        listener.listen()
        with host_ports.reserved(ADDRESS, ports, prefix=prefix) as handed:
            pass
        with (
            pytest.raises(host_ports.Refused, match=re.escape(f'ports {bound} on {ADDRESS}')),
            host_ports.reserved(ADDRESS, (bound,), prefix=prefix),
        ):
            pass

    assert handed == free


def test_a_reservation_goes_with_the_process_that_held_it(ports: tuple[int, int], prefix: str) -> None:
    """A holder killed with SIGKILL, which runs none of its own clean-up, leaves its port to be reserved again."""
    port, _ = ports
    with ExitStack() as stack:
        child, held = _held(stack, prefix, (port,))
        os.kill(child.pid, signal.SIGKILL)
        _ = child.wait(timeout=EXIT_TIMEOUT)
        with host_ports.reserved(ADDRESS, (port,), prefix=prefix) as again:
            pass

    assert again == held


def test_a_block_overlapping_the_ephemeral_range_is_refused(prefix: str) -> None:
    lowest = host_ports.ephemeral().start
    with (
        pytest.raises(host_ports.Refused, match=re.escape('net.ipv4.ip_local_port_range')),
        host_ports.reserved(ADDRESS, range(lowest - 1, lowest + 1), prefix=prefix),
    ):
        pass
