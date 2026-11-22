"""The join's reading: how the run reaches each member it dials, and whether full-size packets cross to it.

`action.yml` runs `reading` once a join has reached its targets or given up,
passing or failing, and prints one `zt-reading:` line, so a run that could not
reach the home and one that could are read side by side. A job that joined
runs `again` as its last step once a later step has failed, which prints the
same reading as a `zt-reading(at-failure):` line: the paths as they stood when
the job failed, not only at the join. The line answers three questions:

* **Path** -- one field per member the run dials, named for the member as the
  roster names it, and `roots=` for the planet roots: `DIRECT` or `RELAY`, the
  latency ZeroTier measured, and the address family of the physical path. A
  `RELAY` member is reached through a root or ZeroTier's TCP relay rather than
  peer to peer. The members are the join's own targets (`dialed`): each one
  the roster places at a target's address, and each one a route of the run's
  names as the next hop to a target no member holds, which is how a `dns` run
  dials the gateway.
* **MTU** -- `mtu=` is the overlay interface's MTU as `ip link` reads it, and
  `netconf=` the MTU the network's configuration handed the daemon. The two
  agree once Central's value has reached the runner.
* **Size** -- one group of four fields per target the run depends on, named
  for the leg it probes (`legs`). `<leg>.up.*` sends from the runner towards
  the home, and `<leg>.down.*` has the home send to the runner, each a small
  exchange and then one carrying a segment that fills the interface's MTU.
  It rides TCP to the target's own service, because the flow rules admit a
  continuous-integration identity to its own TCP legs and nothing else: an
  ICMP echo matches no rule that names a port, so it would be dropped on the
  runner itself. A run's targets that stand in for one another, the two
  resolvers, are one dependency, probed in turn until one acknowledges the
  small segment.

How to read a size probe: `small=ok full=stalled` in one direction is a size
fault in that direction; `small=ok full=ok` in both rules the MTU out for that
leg; a small one that fails too is a path fault that says nothing about size.
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
or port of a physical path. `redact` is the one filter, every line the reading
prints passes through it whichever label it carries, and `action.yml` routes
its own `zerotier-cli peers` dump through it too.

It is the `zt-reading` console script, and the action runs it by path from
the checkout instead, with `python3 -I -S`: the runner has no environment of
this project's at that step. Run that way it puts the checkout's `src/` on
`sys.path` itself and needs nothing past the standard library and
`kluster.conventions`, so it reads the members' node ids and the identities'
addresses from the roster rather than from literals of its own.
"""

from __future__ import annotations

import argparse
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
from collections.abc import Callable, Generator, Iterable
from dataclasses import asdict, dataclass
from ipaddress import IPv4Address, IPv4Network, ip_interface
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
    """A deadline, which every step's own wait stays inside."""

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

    def share(self, parts: int) -> Budget:
        """An even share of what is left, among `parts` that still have to run, so one stalled leg cannot spend the rest."""
        return Budget(max(0.0, self._deadline - time.monotonic()) / parts)


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


#: The key exchange the shell's full-size probe asks for: the post-quantum
#: hybrid (draft-ietf-sshm-mlkem-hybrid-kex), whose server reply carries an
#: ML-KEM-768 ciphertext of 1088 bytes besides the host key and its signature,
#: so it always takes a segment that fills an MTU of 1280. A smaller exchange
#: fits in one short segment and would prove nothing.
SSH_KEX = 'mlkem768x25519-sha256'
#: What the probe's `SSH_MSG_KEXINIT` offers besides (RFC 4253 §7.1): wide
#: enough that a stock server finds one of each in common, though the exchange
#: never gets as far as using any of them.
SSH_HOST_KEYS = 'ssh-ed25519,ecdsa-sha2-nistp256,ecdsa-sha2-nistp384,ecdsa-sha2-nistp521,rsa-sha2-512,rsa-sha2-256'
SSH_CIPHERS = 'chacha20-poly1305@openssh.com,aes128-gcm@openssh.com,aes256-gcm@openssh.com,aes128-ctr,aes256-ctr'
SSH_MACS = 'hmac-sha2-256-etm@openssh.com,hmac-sha2-512-etm@openssh.com,hmac-sha2-256,hmac-sha2-512'
SSH_MSG_KEXINIT = 20
SSH_MSG_KEX_ECDH_INIT = 30
SSH_MSG_KEX_ECDH_REPLY = 31
#: The client's share: an ML-KEM-768 encapsulation key of zeros, which passes
#: the key check a server makes (every coefficient below the modulus), then
#: the X25519 base point, whose product with the server's key is never zero.
#: The probe never derives a key from the exchange, so neither half needs a
#: secret behind it.
SSH_CLIENT_SHARE = bytes(1184) + b'\x09' + bytes(31)
#: The longest packet the probe reads (RFC 4253 §6.1 has a server handle
#: 35000 bytes).
SSH_PACKET_LIMIT = 35000


def _ssh_packet(payload: bytes) -> bytes:
    """`payload` as an SSH binary packet before any key is in force: length, padding to eight bytes, no MAC."""
    padding = 8 - (5 + len(payload)) % 8
    padding += 8 if padding < 4 else 0
    return struct.pack('>IB', 1 + len(payload) + padding, padding) + payload + bytes(padding)


def _ssh_string(value: bytes) -> bytes:
    return struct.pack('>I', len(value)) + value


class _Received:
    """A connection read as SSH reads it: lines, then packets, each wait inside one step."""

    def __init__(self, connection: socket.socket, budget: Budget) -> None:
        self._connection = connection
        self._budget = budget
        self._buffer = b''

    def _fill(self) -> None:
        self._connection.settimeout(self._budget.step())
        chunk = self._connection.recv(65536)
        if not chunk:
            raise EOFError
        self._buffer += chunk

    def line(self) -> bytes:
        while b'\n' not in self._buffer:
            self._fill()
        line, _, self._buffer = self._buffer.partition(b'\n')
        return line

    def packet(self) -> bytes | None:
        """The next packet's payload, or `None` for a length no server sends."""
        while len(self._buffer) < 5:
            self._fill()
        length, padding = struct.unpack_from('>IB', self._buffer)
        if not padding < length <= SSH_PACKET_LIMIT:
            return None
        while len(self._buffer) < 4 + length:
            self._fill()
        payload = self._buffer[5 : 4 + length - padding]
        self._buffer = self._buffer[4 + length :]
        return payload


def key_exchange(host: IPv4Address, port: int, mtu: int, budget: Budget) -> str:
    """Home to runner, full: an SSH server's key-exchange reply, which carries a post-quantum ciphertext.

    The exchange goes as far as the server's reply and no further, so nothing
    is authenticated and no credential rides the connection; the server logs
    a client that disconnected before authenticating. A server that offers no
    `SSH_KEX` is never sent a share for it, and reads `short`, since no reply
    of a size that fills the MTU was asked of it; one that answers with
    anything but the reply reads `declined`. The share itself goes up in
    segments of half a full one, so what the reply reads is the downward
    direction's alone.
    """
    # Each name-list for both directions where it has two, then none for the
    # languages, no guessed packet to follow, and the reserved word.
    lists = (SSH_KEX, SSH_HOST_KEYS, SSH_CIPHERS, SSH_CIPHERS, SSH_MACS, SSH_MACS, 'none', 'none', '', '')
    kexinit = bytes([SSH_MSG_KEXINIT]) + os.urandom(16) + b''.join(_ssh_string(names.encode()) for names in lists)
    kexinit += b'\x00' + bytes(4)
    with connected(host, port, budget) as connection:
        received = _Received(connection, budget)
        try:
            # A server may send other lines before its identification.
            while not received.line().startswith(b'SSH-'):
                pass
            connection.sendall(b'SSH-2.0-zt-reading\r\n' + _ssh_packet(kexinit))
            offer = received.packet()
            if offer is None or offer[:1] != bytes([SSH_MSG_KEXINIT]):
                return 'declined'
            (length,) = struct.unpack_from('>I', offer, 17)
            if SSH_KEX not in offer[21 : 21 + length].decode(errors='replace').split(','):
                return 'short'
            share = _ssh_packet(bytes([SSH_MSG_KEX_ECDH_INIT]) + _ssh_string(SSH_CLIENT_SHARE))
            # In two writes, the second once the first is acknowledged, so
            # each leaves as a segment of its own, half a full one: a path
            # that drops full-size segments upward still delivers the share,
            # and the reply measures the downward direction alone.
            half = len(share) // 2
            start = TcpInfo.of(connection).bytes_acked
            connection.sendall(share[:half])
            if not acknowledged(connection, start + half, budget):
                return 'stalled'
            connection.sendall(share[half:])
            reply = received.packet()
        except EOFError:
            return 'empty'
        if reply is None or reply[:1] != bytes([SSH_MSG_KEX_ECDH_REPLY]):
            return 'declined'
        info = TcpInfo.of(connection)
    return 'ok' if info.received_full(mtu) else 'short'


@dataclass(frozen=True)
class Leg:
    """One target's probes: where they go, and how each direction is sent."""

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
    """A resolver's API, whose login page needs no session."""
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
    """The gateway's own shell and controller API, whose TLS flight fills a segment whatever its shell offers."""
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


def _shell(member: overlay.RosterEntry) -> Leg:
    """A member's shell: its identification line, then its key exchange's reply.

    Each of the leg's three connections ends before authentication, as a
    scanner's would. An `sshd` that penalizes such clients by source
    (`PerSourcePenalties`) adds a second for each by default, which runs down
    as it accrues, and enforces nothing until fifteen have accrued.
    """
    address, port = member.address, gateway.SSH_PORT
    return Leg(
        name=member.name,
        host=address,
        port=port,
        # An identification line that never ends, which the server reads up to
        # a limit far above one segment.
        prefix=b'SSH-2.0-',
        filler=b'a',
        small=lambda _mtu, budget: banner(address, port, budget),
        full=lambda mtu, budget: key_exchange(address, port, mtu, budget),
    )


@dataclass(frozen=True)
class Target:
    """A `host:port` the join waited for."""

    host: IPv4Address
    port: int

    @classmethod
    def parse(cls, text: str) -> list[Target]:
        """Every target in a wait input, as the wait step reads it: separated by spaces or newlines."""
        targets: list[Target] = []
        for word in text.split():
            host, _, port = word.rpartition(':')
            with contextlib.suppress(ValueError):
                targets.append(cls(IPv4Address(host), int(port)))
        return targets


def _routes(text: str) -> list[tuple[IPv4Network, IPv4Address]]:
    """A join's `routes` input, one `<target> via <next hop>` per line, as the route step reads it."""
    routes: list[tuple[IPv4Network, IPv4Address]] = []
    for line in text.splitlines():
        match line.split():
            case [target, 'via', hop]:
                with contextlib.suppress(ValueError):
                    routes.append((IPv4Network(target), IPv4Address(hop)))
            case _:
                pass
    return routes


@dataclass(frozen=True)
class Inputs:
    """What a reading is taken from, which the join's reading keeps for the one a failing job takes again."""

    network_id: str
    seconds: float
    #: The targets every one of which the join waited for, as `wait_for` is written.
    every: str = ''
    #: The targets the first answer among which ended the join's wait, as `wait_for_any` is written.
    any_of: str = ''
    #: The run's own routes, as `routes` is written.
    routes: str = ''

    def dialed(self) -> list[overlay.RosterEntry]:
        """The members the run dials: one at a target's address, or the next hop of the run's route to one.

        The route to a target is the one with the longest prefix holding it, as
        the kernel picks it.

        In the order the targets name them, each once. A target no member holds
        and no route of the run's carries names none.
        """
        routes = _routes(self.routes)
        members: list[overlay.RosterEntry] = []
        for target in Target.parse(f'{self.every} {self.any_of}'):
            # The route the kernel takes for it: the longest prefix holding it.
            carrying = [route for route in routes if target.host in route[0]]
            hops = [target.host, *([max(carrying, key=lambda route: route[0].prefixlen)[1]] if carrying else [])]
            found = next((entry for hop in hops for entry in overlay.ROSTER if entry.address == hop), None)
            if found is not None and found not in members:
                members.append(found)
        return members

    def legs(self, member: IPv4Address | None) -> list[list[Leg]]:
        """The legs to probe, one list per dependency of the join, for a runner holding `member`.

        Each target of `every` is a dependency of its own, and the targets of
        `any_of` one together, tried in order. A target's leg is the service it
        names: a resolver's API, the gateway, or another member's shell. A
        runner that holds no continuous-integration identity has no leg the
        flow rules admit it to, and probes none.
        """
        identities = {overlay.member(name).address for name in overlay.CI_MEMBERS}
        if member not in identities:
            return []
        groups = [[leg] for leg in map(_leg, Target.parse(self.every)) if leg is not None]
        alternatives = [leg for leg in map(_leg, Target.parse(self.any_of)) if leg is not None]
        return [*groups, alternatives] if alternatives else groups


def _leg(target: Target) -> Leg | None:
    if target.host == overlay.UDM:
        return _gateway()
    for resolver in gateway.RESOLVERS:
        if resolver.address == target.host and target.port == gateway.ADGUARD_API_PORT:
            return _resolver(resolver)
    for entry in overlay.ROSTER:
        if entry.address == target.host and target.port == gateway.SSH_PORT:
            return _shell(entry)
    return None


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


def key(name: str) -> str:
    """A roster name as a field name: the line is split at spaces, and several names hold one."""
    return '-'.join(name.split())


def _peers(members: Iterable[overlay.RosterEntry]) -> tuple[dict[str, str], str]:
    """Each member's path, found by its node id in the roster, and the roots'."""
    peers = cast('list[dict[str, object]]', _zerotier('peers'))
    by_node = {peer.get('address'): peer for peer in peers}
    paths = {
        key(entry.name): path(by_node.get(entry.node_id)) if isinstance(entry, overlay.EnrolledMember) else 'unknown'
        for entry in members
    }
    roots = [path(peer) for peer in peers if peer.get('role') == 'PLANET']
    return paths, ','.join(roots) or 'none'


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


#: The fields every line opens with, in this order; each member's path and each
#: leg's probes follow in the order the reading takes them, and `error` closes it.
HEAD = ('member', 'roots', 'mtu', 'netconf')
#: The four fields of one leg's probes, each under the leg's name.
PROBES = ('up.small', 'up.full', 'down.small', 'down.full')


class Reading:
    """The line, field by field as the reading takes it, so a reading cut short still prints what it had."""

    def __init__(self, label: str = 'zt-reading') -> None:
        self.label = label
        self.fields: dict[str, str] = {}

    def line(self, *, cut: bool = False) -> str:
        keys = [k for k in HEAD if k in self.fields]
        keys += [k for k in self.fields if k not in HEAD and k != 'error']
        keys += ['error'] if 'error' in self.fields else []
        fields = [f'{k}={self.fields[k]}' for k in keys]
        return redact(' '.join([f'{self.label}:', *fields, *(['cut=timeout'] if cut else [])]))


def reading(inputs: Inputs, taken: Reading) -> None:
    """Take the whole reading into `taken`."""
    budget = Budget(inputs.seconds)
    fields = taken.fields
    members = inputs.dialed()
    try:
        paths, fields['roots'] = _peers(members)
        fields.update(paths)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        fields['roots'] = outcome(error)
        fields.update(dict.fromkeys((key(entry.name) for entry in members), outcome(error)))
    member: IPv4Address | None = None
    mtu: int | None = None
    try:
        member, mtu, configured = _network(inputs.network_id)
        fields['mtu'] = str(mtu)
        fields['netconf'] = str(configured)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        fields['mtu'] = fields['netconf'] = outcome(error)
    fields['member'] = next((e.name for e in overlay.ROSTER if e.address == member), 'none')
    groups = inputs.legs(member)
    for group in groups:
        fields.update(dict.fromkeys((f'{key(group[0].name)}.{probe}' for probe in PROBES), 'skipped'))
    if mtu is None:
        return
    for index, group in enumerate(groups):
        if budget.spent():
            break
        probe(group, mtu, budget.share(len(groups) - index), fields)


def probe(candidates: list[Leg], mtu: int, budget: Budget, fields: dict[str, str]) -> None:
    """Probe each leg in turn into `fields`, under its name, until one's small segment is acknowledged or the last is tried."""
    for leg in candidates:
        if budget.spent():
            break
        name = key(leg.name)
        fields[f'{name}.up.small'], fields[f'{name}.up.full'] = up(
            leg.host, leg.port, leg.prefix, leg.filler, mtu, budget
        )
        if fields[f'{name}.up.small'] != 'ok' and leg is not candidates[-1]:
            # Whatever kept the small segment from this one, the next may answer.
            continue
        fields[f'{name}.down.small'], fields[f'{name}.down.full'] = down(leg, mtu, budget)
        break


def take(inputs: Callable[[], Inputs], taken: Reading) -> int:
    """Take a reading and print its line, which the step's `timeout` cutting it short does not stop."""

    def cut(_signal: int, _frame: FrameType | None) -> None:
        # The step's `timeout` ends a reading that is wedged in a wait its own
        # budget does not bound -- a TLS handshake reads more than once, and
        # `zerotier-cli` may outlive the `sudo` killed around it -- so the
        # fields taken so far go out before it does.
        print(taken.line(cut=True), flush=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, cut)
    try:
        reading(inputs(), taken)
    except Exception as error:  # noqa: BLE001 -- a reading that breaks says so in one word, with no traceback to leak a value
        taken.fields['error'] = type(error).__name__.lower()
    print(taken.line(), flush=True)
    return 0


def _kept(path: Path) -> Inputs:
    kept = cast('dict[str, object]', json.loads(path.read_text()))
    return Inputs(
        network_id=str(kept['network_id']),
        seconds=float(cast('float', kept['seconds'])),
        every=str(kept.get('every', '')),
        any_of=str(kept.get('any_of', '')),
        routes=str(kept.get('routes', '')),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='zt-reading', description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('redact', help='filter standard input to standard output')
    taking = commands.add_parser('reading', help="take the join's reading")
    taking.add_argument('network_id')
    taking.add_argument('seconds', type=float)
    taking.add_argument('--every', default='', help='the targets every one of which the join waited for')
    taking.add_argument('--any', default='', help='the targets the first answer among which ended the wait')
    taking.add_argument('--routes', default='', help="the run's own routes, one `<target> via <next hop>` per line")
    taking.add_argument('--keep', type=Path, help='where to keep the inputs for `again`')
    again = commands.add_parser('again', help="take the join's reading again, as a failing job does")
    again.add_argument('kept', type=Path, help='the file the join kept its inputs in')
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    match cast('str', arguments.command):
        case 'redact':
            sys.stdout.write(redact(sys.stdin.read()))
            return 0
        case 'reading':
            inputs = Inputs(
                network_id=cast('str', arguments.network_id),
                seconds=cast('float', arguments.seconds),
                every=cast('str', arguments.every),
                any_of=cast('str', arguments.any),
                routes=cast('str', arguments.routes),
            )
            kept = cast('Path | None', arguments.keep)
            if kept is not None:
                with contextlib.suppress(OSError):
                    kept.write_text(json.dumps(asdict(inputs)))
            return take(lambda: inputs, Reading())
        case _:
            kept = cast('Path', arguments.kept)
            return take(lambda: _kept(kept), Reading('zt-reading(at-failure)'))


if __name__ == '__main__':
    sys.exit(main())
