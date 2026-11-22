"""The join's reading: how the run reaches the overlay, and whether full-size packets cross it.

`action.yml` runs `reading` once a join has reached its targets or given up,
passing or failing, and prints one `zt-reading:` line, so a run that could not
reach the home and one that could are read side by side. The line answers
three questions:

* **Path** -- `gateway=` is the gateway's member, `roots=` the planet roots:
  `DIRECT` or `RELAY`, the latency ZeroTier measured, and the address family of
  the physical path. A `RELAY` gateway is reached through a root or ZeroTier's
  TCP relay rather than peer to peer.
* **MTU** -- `mtu=` is the overlay interface's MTU as `ip link` reads it, and
  `netconf=` the MTU the network's configuration handed the daemon. The two
  agree once Central's value has reached the runner.
* **Size** -- `up.*` sends from the runner towards the home, and `down.*` has
  the home send to the runner, each a small exchange and then one carrying a
  segment that fills the interface's MTU. It rides TCP because the flow rules
  admit a continuous-integration identity to its own TCP legs and nothing else:
  an ICMP echo matches no rule that names a port, so it would be dropped on the
  runner itself.

How to read a size probe: `small=ok full=stalled` in one direction is a size
fault in that direction; `small=ok full=ok` in both rules the MTU out for that
run; a small one that fails too is a path fault that says nothing about size.
`full=short` means the exchange completed without a packet the MTU's size
crossing, so it proves nothing. The same rules drop every ICMP message sent to
the identity, "fragmentation needed" among them, so path MTU discovery never
lowers a probe's segment: a path narrower than the MTU stalls the full-size
segment outright, and always reads `stalled`.

A connect that fails reads `refused` or `unreachable` (no route to the host or
its network, which is what an ARP that goes unanswered gives), and any other
failure its `errno` name. A run cut short by the step's `timeout` still prints
what it had read by then, marked `cut=timeout`.

The repository is public, so its logs are: nothing printed carries the address
or port of a physical path. `redact` is the one filter, and `action.yml` routes
its own `zerotier-cli peers` dump through it too.

It is the `zt-reading` console script, and the action runs it by path from
the checkout instead, with `python3 -I -S`: the runner has no environment of
this project's at that step. Run that way it puts the checkout's `src/` on
`sys.path` itself and needs nothing past the standard library and
`kluster.conventions`, so it reads the gateway's node id and the identities'
addresses from the roster rather than from literals of its own.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_interface
from pathlib import Path
from types import FrameType
from typing import cast

if not __package__:
    # Run by path, as the action runs it: the checkout's `src/` is not on the
    # path of an interpreter with no environment of this project's.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kluster.conventions import gateway, overlay

#: One address of a physical path, with the port written after it, in each
#: form a tool writes one: ZeroTier's `<address>/<port>`, `<IPv4>:<port>`, and
#: `[<IPv6>]:<port>`. The IPv6 form is any run of hexadecimal digits, colons and
#: dots holding two colons or more, so an IPv4-mapped address goes with it;
#: anything else that shape takes, such as a MAC address, is redacted too,
#: which costs the reading nothing.
ADDRESS = re.compile(
    r'(?P<v6>\[[0-9A-Fa-f:.]+(?:%[\w.-]+)?\](?::\d+)?'
    r'|(?<![\w.:])(?=[0-9A-Fa-f.]*:[0-9A-Fa-f.]*:)[0-9A-Fa-f:.]+(?:%[\w.-]+)?(?:/\d+)?)'
    r'|(?P<v4>(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?:[/:]\d+)?(?!\w)(?!\.\d))'
)

#: The bytes an IPv4 and a TCP header add to a segment's payload, and what the
#: timestamp option adds when the connection carries it.
HEADERS = 40
TIMESTAMPS = 12
#: The longest any one connect, send or read waits.
STEP_SECONDS = 3.0

#: Offsets into Linux's `struct tcp_info` (`include/uapi/linux/tcp.h`), which
#: only ever grows at its end.
TCPI_OPTIONS = 5
TCPI_SND_MSS = 16
TCPI_RCV_MSS = 20
TCPI_BYTES_ACKED = 120
TCPI_LENGTH = 128
TCPI_OPT_TIMESTAMPS = 1
#: `IP_MTU_DISCOVER` (`include/uapi/linux/in.h`), which Python's `socket` does
#: not name, and its value that sets don't-fragment. A TCP socket has it by
#: default; it is set again so that the probe does not depend on the default.
IP_MTU_DISCOVER = 10
IP_PMTUDISC_DO = 2


def redact(text: str) -> str:
    """`text` with every address and port of a physical path replaced by its family alone."""
    return ADDRESS.sub(lambda found: '<ipv6>' if found['v6'] else '<ipv4>', text)


@dataclass(frozen=True)
class TcpInfo:
    """What the probes read of a connection's `TCP_INFO`."""

    snd_mss: int
    rcv_mss: int
    bytes_acked: int
    timestamps: bool

    @classmethod
    def of(cls, connection: socket.socket) -> TcpInfo:
        raw = connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, TCPI_LENGTH)
        if len(raw) < TCPI_LENGTH:
            raise OSError(f'TCP_INFO is {len(raw)} bytes, short of tcpi_bytes_acked')
        (options,) = struct.unpack_from('=B', raw, TCPI_OPTIONS)
        (snd_mss,) = struct.unpack_from('=I', raw, TCPI_SND_MSS)
        (rcv_mss,) = struct.unpack_from('=I', raw, TCPI_RCV_MSS)
        (acked,) = struct.unpack_from('=Q', raw, TCPI_BYTES_ACKED)
        return cls(snd_mss, rcv_mss, acked, bool(options & TCPI_OPT_TIMESTAMPS))

    def packet(self, payload: int) -> int:
        """The IP packet a segment of `payload` bytes makes on this connection."""
        return payload + HEADERS + (TIMESTAMPS if self.timestamps else 0)

    def received_full(self, mtu: int) -> bool:
        """Whether a segment filling `mtu` has arrived on this connection.

        `rcv_mss` is the kernel's measure of the largest segment the peer has
        sent, so it reaches the MTU less the headers only once such a segment
        has come in.
        """
        return self.packet(self.rcv_mss) >= mtu


class Spent(TimeoutError):
    """The reading's deadline has passed, so a probe that would start now is skipped rather than run."""


class Budget:
    """The reading's one deadline, which every step's own wait stays inside."""

    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    def step(self) -> float:
        """How long the next step may wait: `STEP_SECONDS`, or what is left if less."""
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise Spent('the reading has no time left')
        return min(STEP_SECONDS, left)

    def spent(self) -> bool:
        return time.monotonic() >= self._deadline


#: The errors a connect gives when nothing answers for the address on the way:
#: no route to the host, the ARP for it unanswered, or no route to its network.
UNREACHABLE = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH})


def outcome(error: BaseException) -> str:
    """A probe's failure as one word, never with an address in it."""
    match error:
        case Spent():
            return 'skipped'
        case ConnectionRefusedError():
            return 'refused'
        case ConnectionResetError() | BrokenPipeError():
            return 'reset'
        case TimeoutError():
            return 'stalled'
        case ssl.SSLError():
            # An `OSError` whose `errno` is OpenSSL's error code, not the
            # kernel's, so the code would read as an unrelated errno name.
            return type(error).__name__.lower()
        case OSError() if error.errno in UNREACHABLE:
            return 'unreachable'
        case OSError() if error.errno is not None:
            return errno.errorcode.get(error.errno, type(error).__name__).lower()
        case _:
            return type(error).__name__.lower()


@contextlib.contextmanager
def connected(host: IPv4Address, port: int, budget: Budget) -> Generator[socket.socket]:
    """A TCP connection with don't-fragment set and no send coalescing."""
    connection = socket.create_connection((str(host), port), timeout=budget.step())
    try:
        connection.setsockopt(socket.IPPROTO_IP, IP_MTU_DISCOVER, IP_PMTUDISC_DO)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        yield connection
    finally:
        connection.close()


def acknowledged(connection: socket.socket, target: int, budget: Budget) -> bool:
    """Whether the peer has acknowledged `target` bytes, within one step."""
    until = time.monotonic() + budget.step()
    while TcpInfo.of(connection).bytes_acked < target:
        if time.monotonic() >= until:
            return False
        time.sleep(0.02)
    return True


def up(host: IPv4Address, port: int, prefix: bytes, filler: bytes, mtu: int, budget: Budget) -> tuple[str, str]:
    """Runner to home: whether a small segment, then a full-size one, is acknowledged.

    `prefix` is what the service is sent first, and `filler` a byte that
    continues it, so the service keeps reading and never acts on what it is
    sent. The acknowledgement is the far end's kernel's, which does not wait
    on the service. The full-size segment is the largest payload one segment
    carries on the connection: the interface's MTU less the headers, unless
    the far end takes less, which reads `short`.
    """
    small = 'skipped'
    try:
        with connected(host, port, budget) as connection:
            start = TcpInfo.of(connection).bytes_acked
            connection.sendall(prefix)
            if not acknowledged(connection, start + len(prefix), budget):
                return 'stalled', 'skipped'
            small = 'ok'
            info = TcpInfo.of(connection)
            if info.packet(info.snd_mss) < mtu:
                # The far end takes smaller segments than the MTU allows.
                return small, 'short'
            connection.sendall(filler * info.snd_mss)
            if not acknowledged(connection, start + len(prefix) + info.snd_mss, budget):
                return small, 'stalled'
            return small, 'ok'
    except OSError as error:
        return (outcome(error), 'skipped') if small == 'skipped' else (small, outcome(error))


def http_get(host: IPv4Address, port: int, path: str, mtu: int | None, budget: Budget) -> str:
    """Home to runner: a GET read to its end, and, given `mtu`, whether a packet that size came in it."""
    with connected(host, port, budget) as connection:
        connection.sendall(f'GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
        received = 0
        while True:
            connection.settimeout(budget.step())
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += len(chunk)
        info = TcpInfo.of(connection)
    if received == 0:
        return 'empty'
    return 'ok' if mtu is None or info.received_full(mtu) else 'short'


def banner(host: IPv4Address, port: int, budget: Budget) -> str:
    """Home to runner, small: the identification line an SSH server sends first."""
    with connected(host, port, budget) as connection:
        received = b''
        while b'\n' not in received:
            connection.settimeout(budget.step())
            chunk = connection.recv(256)
            if not chunk:
                return 'empty'
            received += chunk
    return 'ok'


def handshake(host: IPv4Address, port: int, mtu: int, budget: Budget) -> str:
    """Home to runner, full: a TLS handshake, whose server flight carries the certificate chain.

    It verifies nothing and sends nothing past the handshake: what it measures
    is whether the flight arrives, and no credential rides the connection.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with connected(host, port, budget) as connection:
        connection.settimeout(budget.step())
        with context.wrap_socket(connection, do_handshake_on_connect=False) as tls:
            tls.do_handshake()
            info = TcpInfo.of(tls)
    return 'ok' if info.received_full(mtu) else 'short'


@dataclass(frozen=True)
class Leg:
    """One identity's probes: where it goes, and how each direction is sent."""

    name: str
    host: IPv4Address
    port: int
    prefix: bytes
    filler: bytes
    small: Callable[[int, Budget], str]
    full: Callable[[int, Budget], str]


def down(leg: Leg, mtu: int, budget: Budget) -> tuple[str, str]:
    """Home to runner: the leg's small exchange, then its full-size one, each failure reported as its outcome."""
    results: list[str] = []
    for probe in (leg.small, leg.full):
        try:
            results.append(probe(mtu, budget))
        except OSError as error:
            results.append(outcome(error))
    return results[0], results[1]


def _resolver(resolver: gateway.BridgedService) -> Leg:
    """A `dns` run's leg: a resolver's API, whose login page needs no session."""
    address, port = resolver.address, gateway.ADGUARD_API_PORT
    return Leg(
        name=resolver.name,
        host=address,
        port=port,
        # A request whose header never ends, which the server buffers.
        prefix=f'GET / HTTP/1.1\r\nHost: {address}\r\nX-Probe: '.encode(),
        filler=b'a',
        # The status endpoint refuses a request with no session in a few
        # hundred bytes; the login page is served to anyone, and one that fit
        # in a single segment would read `short`.
        small=lambda _mtu, budget: http_get(address, port, '/control/status', None, budget),
        full=lambda mtu, budget: http_get(address, port, '/login.html', mtu, budget),
    )


def _gateway() -> Leg:
    """A `physical` run's leg: the gateway's own shell and controller API."""
    address = overlay.UDM
    return Leg(
        name=overlay.MEMBER_UDM,
        host=address,
        port=gateway.UNIFI_API_PORT,
        # A TLS handshake record announcing the largest length a record may
        # have, which the server waits to fill.
        prefix=b'\x16\x03\x01\x40\x00',
        filler=b'\x00',
        small=lambda _mtu, budget: banner(address, gateway.SSH_PORT, budget),
        full=lambda mtu, budget: handshake(address, gateway.UNIFI_API_PORT, mtu, budget),
    )


def legs(member: IPv4Address | None) -> list[Leg]:
    """The legs the member at `member` may probe, in the order to try them: the ones its flow rules admit."""
    if member == overlay.CI_DNS:
        return [_resolver(resolver) for resolver in gateway.RESOLVERS]
    if member == overlay.CI_PHYSICAL:
        return [_gateway()]
    return []


def _zerotier(command: str) -> object:
    done = subprocess.run(
        ['sudo', 'zerotier-cli', '-j', command], capture_output=True, text=True, timeout=STEP_SECONDS, check=True
    )
    return cast('object', json.loads(done.stdout))


def path(peer: dict[str, object] | None) -> str:
    """How one peer is reached: `DIRECT` or `RELAY`, the latency, and the physical path's family."""
    if peer is None:
        return 'absent'
    latency = peer.get('latency')
    measured = f'{latency}ms' if isinstance(latency, int) and latency >= 0 else '?ms'
    paths = cast('list[dict[str, object]]', peer.get('paths') or [])
    preferred = next((p for p in paths if p.get('preferred')), None)
    if preferred is None:
        return f'RELAY/{measured}'
    redacted = redact(str(preferred.get('address')))
    family = 'ipv6' if '<ipv6>' in redacted else 'ipv4' if '<ipv4>' in redacted else 'unknown'
    link = 'RELAY' if peer.get('tunneled') else 'DIRECT'
    return f'{link}/{measured}/{family}'


def _peers() -> tuple[str, str]:
    peers = cast('list[dict[str, object]]', _zerotier('peers'))
    udm = overlay.member(overlay.MEMBER_UDM)
    node = udm.node_id if isinstance(udm, overlay.EnrolledMember) else None
    gateway_peer = next((peer for peer in peers if peer.get('address') == node), None)
    roots = [path(peer) for peer in peers if peer.get('role') == 'PLANET']
    return path(gateway_peer), ','.join(roots) or 'none'


def _network(network_id: str) -> tuple[IPv4Address | None, int | None, int | None]:
    """The member's overlay address, the interface's MTU as `ip link` reads it, and the configured MTU."""
    networks = cast('list[dict[str, object]]', _zerotier('listnetworks'))
    network = next((n for n in networks if n.get('nwid') == network_id), None)
    if network is None:
        return None, None, None
    member = next(
        (
            interface.ip
            for interface in map(ip_interface, cast('list[str]', network.get('assignedAddresses') or []))
            if isinstance(interface.ip, IPv4Address)
        ),
        None,
    )
    configured = network.get('mtu')
    device = str(network.get('portDeviceName'))
    links = subprocess.run(
        ['ip', '-j', 'link', 'show', 'dev', device], capture_output=True, text=True, timeout=STEP_SECONDS, check=True
    )
    (link,) = cast('list[dict[str, object]]', json.loads(links.stdout))
    mtu = link.get('mtu')
    return member, mtu if isinstance(mtu, int) else None, configured if isinstance(configured, int) else None


#: The fields of the line, in the order it prints them.
ORDER = (
    'member',
    'gateway',
    'roots',
    'mtu',
    'netconf',
    'leg',
    'up.small',
    'up.full',
    'down.small',
    'down.full',
    'error',
)


class Reading:
    """The line, field by field as the reading takes it, so a reading cut short still prints what it had."""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}

    def line(self, *, cut: bool = False) -> str:
        fields = [f'{key}={self.fields[key]}' for key in ORDER if key in self.fields]
        return redact(' '.join(['zt-reading:', *fields, *(['cut=timeout'] if cut else [])]))


def reading(network_id: str, seconds: float, taken: Reading) -> None:
    """Take the whole reading into `taken`."""
    budget = Budget(seconds)
    fields = taken.fields
    try:
        fields['gateway'], fields['roots'] = _peers()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        fields['gateway'] = fields['roots'] = outcome(error)
    member: IPv4Address | None = None
    mtu: int | None = None
    try:
        member, mtu, configured = _network(network_id)
        fields['mtu'] = str(mtu)
        fields['netconf'] = str(configured)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        fields['mtu'] = fields['netconf'] = outcome(error)
    candidates = legs(member)
    fields['member'] = next((e.name for e in overlay.ROSTER if e.address == member), 'none')
    probes = ('up.small', 'up.full', 'down.small', 'down.full')
    fields.update(dict.fromkeys(probes, 'skipped'))
    if mtu is None:
        return
    probe(candidates, mtu, budget, fields)


def probe(candidates: list[Leg], mtu: int, budget: Budget, fields: dict[str, str]) -> None:
    """Probe the first leg whose small segment is acknowledged, or the last one tried, into `fields`."""
    for leg in candidates:
        if budget.spent():
            break
        fields['leg'] = leg.name
        fields['up.small'], fields['up.full'] = up(leg.host, leg.port, leg.prefix, leg.filler, mtu, budget)
        if fields['up.small'] != 'ok' and leg is not candidates[-1]:
            # Whatever kept the small segment from this one, the next may answer.
            continue
        fields['down.small'], fields['down.full'] = down(leg, mtu, budget)
        break


def main(argv: list[str] | None = None) -> int:
    match sys.argv[1:] if argv is None else argv:
        case ['redact']:
            sys.stdout.write(redact(sys.stdin.read()))
            return 0
        case ['reading', network_id, seconds]:
            taken = Reading()

            def cut(_signal: int, _frame: FrameType | None) -> None:
                # The step's `timeout` ends a reading that is wedged in a wait
                # its own budget does not bound -- a TLS handshake reads more
                # than once, and `zerotier-cli` may outlive the `sudo` killed
                # around it -- so the fields taken so far go out before it does.
                print(taken.line(cut=True), flush=True)
                os._exit(0)

            signal.signal(signal.SIGTERM, cut)
            try:
                reading(network_id, float(seconds), taken)
            except Exception as error:  # noqa: BLE001 -- a reading that breaks says so in one word, with no traceback to leak a value
                taken.fields['error'] = type(error).__name__.lower()
            print(taken.line(), flush=True)
            return 0
        case _:
            print('usage: zt-reading redact | reading <network id> <seconds>', file=sys.stderr)
            return 2


if __name__ == '__main__':
    sys.exit(main())
