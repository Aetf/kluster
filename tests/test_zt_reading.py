"""The join's reading: the filter that keeps physical paths out of a public log, the members it reads, and the verdicts of its probes.

The filter and the reading run as the action runs them, by path under
`python3 -I -S`, so the cases also hold that the script needs nothing past the
standard library and `kluster.conventions`. The probes run against ports on
this host's loopback, each shaped so that a wrong verdict reads differently
from the right one: a far end advertising a runner's MSS, a byte never sent,
a page in writes smaller than a segment, a TLS flight or a key-exchange reply
in narrow segments.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import process_sessions
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from peer_listings import PEER_PORTS, PEERS, addresses, peer, zerotier_stand_ins

from kluster import conventions
from kluster.scripts import zt_reading

#: The script, run by path as the action runs it.
READING = Path(zt_reading.__file__)


def _redact(text: str) -> str:
    """`text` through the filter, run as the action runs it: the standard library alone."""
    done = process_sessions.run(
        [sys.executable, '-I', '-S', str(READING), 'redact'], input=text, text=True, timeout=30, check=True
    )
    return cast('str', done.stdout)


def test_the_peer_listing_keeps_each_paths_kind_and_latency_and_no_address() -> None:
    """The repository is public, so a listing in its log may say how a peer is reached and never where.

    The finder is shown to see every address in the listing first, so its
    finding none afterwards is a finding. Under `-I -S` the script imports
    the conventions it reads the roster from with no installed package, as
    the runner has none.
    """
    assert len(addresses(PEERS)) == 6, addresses(PEERS)

    redacted = _redact(PEERS)

    assert addresses(redacted) == [], redacted
    assert [port for port in PEER_PORTS if port in redacted] == [], redacted
    # Every column up to the path is the daemon's, and stays.
    kept = [line.split()[:5] for line in redacted.splitlines()[2:]]
    assert kept == [line.split()[:5] for line in PEERS.splitlines()[2:]], redacted
    families = [line.split()[-1] for line in redacted.splitlines()[2:-1]]
    assert families == ['<ipv4>', '<ipv6>', '<ipv6>', '<ipv6>', '<ipv6>', '<ipv4>'], redacted


def _roster(name: str) -> conventions.overlay.EnrolledMember:
    entry = conventions.overlay.member(name)
    assert isinstance(entry, conventions.overlay.EnrolledMember), name
    return entry


def test_the_reading_reads_each_member_the_join_dialed_by_its_node_id_in_the_roster(tmp_path: Path) -> None:
    """Each target the join waited for is read as the member the roster places there, found by its node id.

    The two members are leaves among others, listed after a leaf on a relay,
    and each is reached differently, so a reading that took the first leaf, or
    one member's path for the other's, reads wrong. The interface's MTU and the
    configured one differ, so a reading that printed one for the other shows
    it. The member's address is no identity's, so no leg is probed and the case
    dials nothing.
    """
    udm = _roster(conventions.overlay.MEMBER_UDM)
    homelab = _roster(conventions.overlay.MEMBER_HOMELAB)
    peers = [
        peer('0123456789', 'LEAF', 90, None),
        peer(udm.node_id, 'LEAF', 23, '192.0.2.1/21032'),
        peer('778cde7190', 'PLANET', 84, '192.0.2.66/9993'),
        peer(homelab.node_id, 'LEAF', 16, '[2001:db8::7]/21033', tunneled=True),
        peer('cafe04eba9', 'PLANET', 160, '[2001:db8::1]/9993', tunneled=True),
    ]
    network = {'nwid': conventions.overlay.NETWORK_ID, 'mtu': 2800, 'assignedAddresses': ['192.0.2.200/24']}
    bin_dir = zerotier_stand_ins(tmp_path, peers=peers, network=network, mtu=1280)
    targets = f'{udm.address}:22 {homelab.address}:22'

    done = process_sessions.run(
        [sys.executable, '-I', '-S', str(READING), 'reading', conventions.overlay.NETWORK_ID, '10', '--every', targets],
        env={'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'},
        text=True,
        timeout=30,
    )

    line = cast('str', done.stdout)
    assert done.returncode == 0, line
    assert line.startswith('zt-reading: '), line
    fields = dict(field.split('=', 1) for field in line.split()[1:])
    assert fields[udm.name] == 'DIRECT/23ms/ipv4', line
    assert fields[homelab.name] == 'RELAY/16ms/ipv6', line
    assert fields['roots'] == 'DIRECT/84ms/ipv4,RELAY/160ms/ipv6', line
    assert (fields['mtu'], fields['netconf']) == ('1280', '2800'), line
    assert [field for field in fields if '.' in field] == [], line
    assert addresses(line) == [], line


def test_the_members_a_run_dials_are_the_roster_entries_its_targets_or_its_routes_name() -> None:
    """A target is read as the member the roster places at its address, or as the next hop of the run's route to it.

    Every member is found at its own address, so a selection that knew only
    the members today's runs dial misses one. A target no member holds is read
    as the member its route goes through -- a member other than the gateway
    here, so a reading that assumed the gateway for every routed target reads
    the wrong one -- and as none when no route carries it. A member two
    targets name is read once, in the order the targets name it. Of two
    routes that hold a target, the one with the longer prefix is its route, as
    the kernel picks it.
    """
    roster = conventions.overlay.ROSTER
    for entry in roster:
        assert zt_reading.Inputs('n', 1, every=f'{entry.address}:22').dialed() == [entry], entry.name
    haos = conventions.overlay.member('haos')
    routed = zt_reading.Inputs('n', 1, any_of='192.0.2.7:80 192.0.2.8:80', routes=f'192.0.2.0/29 via {haos.address}')
    unrouted = zt_reading.Inputs('n', 1, every='192.0.2.7:80')
    first, second = roster[0], roster[1]
    twice = zt_reading.Inputs('n', 1, every=f'{second.address}:22 {first.address}:22', any_of=f'{second.address}:443')

    assert routed.dialed() == [haos]
    assert unrouted.dialed() == []
    assert twice.dialed() == [second, first]
    # Routes that overlap: the target goes the way of the longest prefix
    # holding it, whichever is listed first.
    udm = conventions.overlay.member(conventions.overlay.MEMBER_UDM)
    overlapping = f'192.0.2.0/24 via {haos.address}\n192.0.2.7/32 via {udm.address}'
    assert zt_reading.Inputs('n', 1, every='192.0.2.7:80', routes=overlapping).dialed() == [udm]


def test_each_target_is_probed_on_the_leg_of_the_service_it_names_and_the_stand_ins_together() -> None:
    """A member's shell, the gateway and a resolver each get their own leg; targets that stand in for one another are one dependency.

    A runner that holds no identity's address is admitted to no leg, so it
    probes none.
    """
    homelab = conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB)
    resolvers = conventions.gateway.RESOLVERS
    inputs = zt_reading.Inputs(
        'n',
        1,
        every=f'{conventions.overlay.UDM}:22 {homelab.address}:22',
        any_of=' '.join(f'{resolver.address}:{conventions.gateway.ADGUARD_API_PORT}' for resolver in resolvers),
    )

    groups = inputs.legs(conventions.overlay.CI_PHYSICAL)

    assert [[(leg.name, leg.host) for leg in group] for group in groups] == [
        [(conventions.overlay.MEMBER_UDM, conventions.overlay.UDM)],
        [(homelab.name, homelab.address)],
        [(resolver.name, resolver.address) for resolver in resolvers],
    ]
    assert inputs.legs(ipaddress.IPv4Address('192.0.2.200')) == []


#: The MSS a runner on an overlay of MTU 1280 advertises: the MTU less the
#: IPv4 and TCP headers.
RUNNER_MSS = 1240
#: An MSS far below that, so that no segment the far end sends fills the MTU.
NARROW_MSS = 536
#: A write far below a segment, and below the size the kernel first assumes
#: a peer's segments to be.
SMALL_WRITE = 500
#: The address the probes reach on this host.
LOOPBACK = ipaddress.IPv4Address('127.0.0.1')


@contextmanager
def _serving(serve: Callable[[socket.socket], None], *, mss: int | None) -> Generator[int]:
    """A port on this host whose connections `serve` answers, one after another, with segments no larger than `mss`.

    `TCP_MAXSEG` set on the listener is the MSS every connection it accepts
    advertises and sends with, so it is what a peer on a narrower link would
    present.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        if mss is not None:
            server.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, mss)
        server.bind((str(LOOPBACK), 0))
        server.listen()
        server.settimeout(10)

        def accept() -> None:
            with contextlib.suppress(OSError):
                while True:
                    client, _ = server.accept()
                    with client, contextlib.suppress(OSError):
                        serve(client)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        yield cast('int', server.getsockname()[1])


def _holding(client: socket.socket) -> None:
    """Read nothing until the probe closes: what arrives is acknowledged by the kernel alone."""
    client.settimeout(10)
    while client.recv(65536):
        pass


@pytest.mark.parametrize(('mtu', 'full'), [(1280, 'ok'), (1281, 'short')])
def test_the_full_segment_sent_is_an_ip_packet_of_exactly_the_mtu(mtu: int, full: str) -> None:
    """Against a far end advertising a runner's MSS, the segment sent fills an MTU of 1280 and falls one byte short of 1281.

    The packet the probe counts is the payload `TCP_INFO` reports plus the
    headers and the timestamp option, so a header size, an option or an offset
    that is off reads the wrong side of one of the two.
    """
    with _serving(_holding, mss=RUNNER_MSS) as port:
        result = zt_reading.up(LOOPBACK, port, b'GET / HTTP/1.1\r\n', b'a', mtu, zt_reading.Budget(10))

    assert result == ('ok', full)


def test_bytes_never_sent_are_never_acknowledged() -> None:
    """The acknowledgement is read as a count of bytes, so asking for one more than was sent is never met.

    The count is shown met for exactly what was sent first, so the second
    reading's `False` is the count's and not a connection that acknowledged
    nothing.
    """
    sent = b'GET / HTTP/1.1\r\n'
    with (
        _serving(_holding, mss=None) as port,
        zt_reading.connected(LOOPBACK, port, zt_reading.Budget(10)) as connection,
    ):
        start = zt_reading.TcpInfo.of(connection).bytes_acked
        connection.sendall(sent)

        assert zt_reading.acknowledged(connection, start + len(sent), zt_reading.Budget(5))
        assert not zt_reading.acknowledged(connection, start + len(sent) + 1, zt_reading.Budget(1))


def _page(client: socket.socket) -> None:
    """Answer one GET with a page of several segments."""
    client.settimeout(10)
    while b'\r\n\r\n' not in client.recv(4096):
        pass
    client.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 8000\r\nConnection: close\r\n\r\n' + b'x' * 8000)


def _trickle(client: socket.socket) -> None:
    """Answer one GET with the same page in writes far smaller than a segment, each sent once the last is acknowledged.

    With nothing outstanding when a write is made, the kernel has nothing to
    coalesce it with, so every segment is the size of its write, while the MSS
    the connection advertises stays the full one.
    """
    client.settimeout(10)
    while b'\r\n\r\n' not in client.recv(4096):
        pass
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    body = b'HTTP/1.1 200 OK\r\nContent-Length: 8000\r\nConnection: close\r\n\r\n' + b'x' * 8000
    start = zt_reading.TcpInfo.of(client).bytes_acked
    for offset in range(0, len(body), SMALL_WRITE):
        client.sendall(body[offset : offset + SMALL_WRITE])
        assert zt_reading.acknowledged(client, start + min(offset + SMALL_WRITE, len(body)), zt_reading.Budget(5))


@pytest.mark.parametrize(('serve', 'full'), [(_page, 'ok'), (_trickle, 'short')], ids=['full-segments', 'small-writes'])
def test_a_page_reads_as_received_in_full_only_when_a_segment_filling_the_mtu_came_in_it(
    serve: Callable[[socket.socket], None], full: str
) -> None:
    """The page arrives whole either way; only the segments it came in decide `ok` against `short`.

    The small writes go out on a connection whose MSS is the full one, so a
    verdict read off the size this end may send rather than the size it was
    sent reads `ok` there. The small exchange asks for nothing about size, so
    it reads `ok` either way.
    """
    with _serving(serve, mss=None) as port:
        received = zt_reading.http_get(LOOPBACK, port, '/login.html', 1280, zt_reading.Budget(10))
        small = zt_reading.http_get(LOOPBACK, port, '/control/status', None, zt_reading.Budget(10))

    assert (small, received) == ('ok', full)


@pytest.fixture(scope='module')
def tls_identity(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A self-signed certificate long enough that its server flight spans more than one segment at MTU 1280."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'probe')])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(f'name-{n}.probe.invalid') for n in range(40)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp('tls')
    (directory / 'key.pem').write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    (directory / 'cert.pem').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return directory / 'cert.pem', directory / 'key.pem'


@pytest.mark.parametrize(('mss', 'full'), [(None, 'ok'), (NARROW_MSS, 'short')], ids=['full-segments', 'narrow'])
def test_a_handshake_reads_as_received_in_full_only_when_a_segment_filling_the_mtu_came_in_it(
    tls_identity: tuple[Path, Path], mss: int | None, full: str
) -> None:
    """The gateway's full-size leg: the server flight completes the handshake either way, in segments that decide the verdict."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*tls_identity)

    def handshake(client: socket.socket) -> None:
        client.settimeout(10)
        with context.wrap_socket(client, server_side=True) as tls:
            _holding(tls)

    with _serving(handshake, mss=mss) as port:
        result = zt_reading.handshake(LOOPBACK, port, 1280, zt_reading.Budget(10))

    assert result == full


def _ssh_string(value: bytes) -> bytes:
    return struct.pack('>I', len(value)) + value


def _ssh_packet(payload: bytes) -> bytes:
    """A packet as RFC 4253 §6 frames one before keys: length, padding length, payload, at least four bytes of padding."""
    padding = 4 + (-(5 + 4 + len(payload)) % 8)
    return struct.pack('>IB', 1 + len(payload) + padding, padding) + payload + bytes(padding)


def _read_packet(client: socket.socket, buffered: bytearray) -> bytes | None:
    """The next packet's payload off `client`, or `None` once it has closed."""
    while len(buffered) < 4 or len(buffered) < 4 + struct.unpack_from('>I', buffered)[0]:
        chunk = client.recv(65536)
        if not chunk:
            return None
        buffered += chunk
    length, padding = struct.unpack_from('>IB', buffered)
    payload = bytes(buffered[5 : 4 + length - padding])
    del buffered[: 4 + length]
    return payload


#: The payload of the key-exchange reply OpenSSH 10 sends for the hybrid with
#: an Ed25519 host key: the host key, the ML-KEM ciphertext and X25519 share,
#: and the signature.
SSH_REPLY = 1267


def _ssh_server(offered: str, seen: list[str]) -> Callable[[socket.socket], None]:
    """An SSH server offering the key exchanges `offered`, which answers a client's share with a reply the size OpenSSH's is.

    What it is sent goes into `seen`, so the case reads the client's side of
    the exchange as the server took it: the identification line, the key
    exchanges the client offers, and the length of the share it sends, or
    that it sent none.
    """

    def serve(client: socket.socket) -> None:
        client.settimeout(10)
        lists = (offered, 'ssh-ed25519', 'aes128-ctr', 'aes128-ctr', 'hmac-sha2-256', 'hmac-sha2-256', 'none', 'none')
        offer = bytes([20]) + bytes(16) + b''.join(_ssh_string(names.encode()) for names in (*lists, '', ''))
        client.sendall(b'SSH-2.0-OpenSSH_10.0\r\n' + _ssh_packet(offer + bytes(5)))
        buffered = bytearray()
        while b'\n' not in buffered:
            buffered += client.recv(4096)
        line, _, rest = bytes(buffered).partition(b'\n')
        buffered[:] = rest
        seen.append(line.decode().strip())
        kexinit = _read_packet(client, buffered)
        assert kexinit is not None
        (length,) = struct.unpack_from('>I', kexinit, 17)
        seen.append(kexinit[21 : 21 + length].decode())
        share = _read_packet(client, buffered)
        if share is None:
            seen.append('no share')
            return
        (length,) = struct.unpack_from('>I', share, 1)
        # The share arrived in segments short of a full one at MTU 1280.
        full = zt_reading.TcpInfo.of(client).received_full(1280)
        seen.append(f'{share[0]} {length}' + (' in a full segment' if full else ''))
        client.sendall(_ssh_packet(bytes([31]) + bytes(SSH_REPLY - 1)))
        _holding(client)

    return serve


@pytest.mark.parametrize(
    ('offered', 'mss', 'full', 'share'),
    [
        ('mlkem768x25519-sha256,curve25519-sha256', None, 'ok', '30 1216'),
        ('mlkem768x25519-sha256,curve25519-sha256', NARROW_MSS, 'short', '30 1216'),
        ('curve25519-sha256', None, 'short', 'no share'),
    ],
    ids=['full-segments', 'narrow', 'no-hybrid'],
)
def test_a_shells_key_exchange_reads_as_received_in_full_only_when_its_reply_came_in_a_segment_filling_the_mtu(
    mss: int | None, offered: str, full: str, share: str
) -> None:
    """A member's shell's full-size leg: the hybrid key exchange, whose reply is larger than one segment at MTU 1280.

    The client's share is an ML-KEM-768 key and an X25519 point, 1216 bytes in
    an `SSH_MSG_KEX_ECDH_INIT`, and it reaches the server in segments short of
    a full one, so a path that drops full-size segments upward cannot keep the
    reply from being sent. A server that offers no hybrid is sent none,
    since the exchange it would answer has no reply that size, and reads
    `short` rather than a failure of the path.
    """
    seen: list[str] = []
    with _serving(_ssh_server(offered, seen), mss=mss) as port:
        result = zt_reading.key_exchange(LOOPBACK, port, 1280, zt_reading.Budget(10))
        give_up = time.monotonic() + 10
        while len(seen) < 3 and time.monotonic() < give_up:
            time.sleep(0.02)

    assert result == full
    assert seen[0].startswith('SSH-2.0-'), seen
    assert 'mlkem768x25519-sha256' in seen[1].split(','), seen
    assert seen[2:] == [share], seen


def _closing(client: socket.socket) -> None:
    """Read the ClientHello, then close without a word of TLS."""
    client.settimeout(10)
    _ = client.recv(4096)


def _plain(client: socket.socket) -> None:
    """Answer the ClientHello as a plain-HTTP server answers anything it cannot parse."""
    client.settimeout(10)
    _ = client.recv(4096)
    client.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')


@pytest.mark.parametrize(('serve', 'word'), [(_closing, 'ssleoferror'), (_plain, 'sslerror')], ids=['closed', 'plain'])
def test_a_handshake_that_fails_reads_as_its_tls_error(serve: Callable[[socket.socket], None], word: str) -> None:
    """A TLS failure carries OpenSSL's error code where an `OSError` carries an errno, and must not read as one.

    Read as an errno, the end of file is `enoexec` and the protocol error
    `eperm`, which send whoever reads the line after a fault that is not there.
    """
    with _serving(serve, mss=None) as port, pytest.raises(ssl.SSLError) as failed:
        _ = zt_reading.handshake(LOOPBACK, port, 1280, zt_reading.Budget(10))

    assert zt_reading.outcome(failed.value) == word


@pytest.mark.parametrize(
    ('error', 'word'),
    [
        (OSError(errno.EHOSTUNREACH, 'No route to host'), 'unreachable'),
        (OSError(errno.ENETUNREACH, 'Network is unreachable'), 'unreachable'),
        (OSError(errno.ECONNABORTED, 'Software caused connection abort'), 'econnaborted'),
        (ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused'), 'refused'),
    ],
    ids=['host', 'network', 'other-errno', 'refused'],
)
def test_a_failure_reads_as_the_one_word_for_it(error: OSError, word: str) -> None:
    """No route to the host or its network is `unreachable`, an unanswered ARP among them (kluster-ops#866)."""
    assert zt_reading.outcome(error) == word


#: An address the kernel refuses a TCP connect to before any packet leaves
#: the host: a multicast group, which it answers with `ENETUNREACH`.
UNROUTABLE = ipaddress.IPv4Address('224.0.0.1')


def _answered(_mtu: int, _budget: zt_reading.Budget) -> str:
    return 'ok'


def _never(_mtu: int, _budget: zt_reading.Budget) -> str:
    raise AssertionError('the reading probed the far end of a leg it should have passed')


@pytest.mark.parametrize('first', ['refused', 'unreachable'])
def test_the_reading_moves_past_a_leg_whose_small_segment_fails_however_it_fails(first: str) -> None:
    """The first of two legs fails its small segment; the reading probes the second, each under its own name.

    A leg left for any failure but a refusal or a stall would stop at the
    unreachable one, as it did when only those two moved the reading on.
    """
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound,
        _serving(_holding, mss=None) as port,
    ):
        bound.bind((str(LOOPBACK), 0))
        host, closed = (LOOPBACK, cast('int', bound.getsockname()[1])) if first == 'refused' else (UNROUTABLE, 80)
        legs = [
            zt_reading.Leg('first', host, closed, b'GET / HTTP/1.1\r\n', b'a', _never, _never),
            zt_reading.Leg('second', LOOPBACK, port, b'GET / HTTP/1.1\r\n', b'a', _answered, _answered),
        ]
        fields: dict[str, str] = {}
        zt_reading.probe(legs, 1280, zt_reading.Budget(10), fields)

    assert fields == {
        'first.up.small': first,
        'first.up.full': 'skipped',
        'second.up.small': 'ok',
        'second.up.full': 'ok',
        'second.down.small': 'ok',
        'second.down.full': 'ok',
    }


class _Exited(BaseException):
    """The process exit the cut handler makes, raised instead so the case outlives it."""


@pytest.mark.parametrize('cut', [False, True], ids=['whole', 'cut'])
def test_each_line_the_reading_prints_carries_no_address(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cut: bool
) -> None:
    """Both ways a reading prints its line pass the filter: once it has finished, and when the step's `timeout` cuts it short.

    No field the reading takes today holds an address; the case has a reading
    take fields that would, and then end, or be signalled as `timeout` signals
    it, which runs the handler the reading installed for that.
    """
    handlers: dict[int, Callable[[int, object], None]] = {}

    def installed(number: int, handler: Callable[[int, object], None]) -> None:
        handlers[number] = handler

    monkeypatch.setattr(zt_reading.signal, 'signal', installed)

    def exit_(code: int) -> None:
        raise _Exited(code)

    monkeypatch.setattr(zt_reading.os, '_exit', exit_)

    def leaking(_inputs: zt_reading.Inputs, taken: zt_reading.Reading) -> None:
        taken.fields.update({'member': 'ci-dns', 'probe': '192.0.2.1:9993', 'path': '[2001:db8::1]:9993'})
        if cut:
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(zt_reading, 'reading', leaking)
    with pytest.raises(_Exited) if cut else contextlib.nullcontext():
        _ = zt_reading.take(lambda: zt_reading.Inputs('n', 1), zt_reading.Reading())

    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith('zt-reading: member=ci-dns '), line
    assert line.endswith(' cut=timeout') == cut, line
    assert addresses(line) == [], line


@pytest.mark.parametrize('command', ['reading', 'again'])
def test_a_reading_ended_by_the_steps_timeout_prints_what_it_had(tmp_path: Path, command: str) -> None:
    """The step's `timeout` ends a reading wedged in a wait, and the line still prints, marked as cut, under its own label.

    The stand-in for `sudo` answers the peer listing and then never answers
    the network listing; the case signals the reading once the stand-in has
    been asked for it, as `timeout` would. The join's reading keeps its inputs
    in a file, and the one a failing job takes again reads them back from it.
    """
    asked = tmp_path / 'asked'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    peers = [peer('778cde7190', 'PLANET', 84, '192.0.2.66/9993')]
    (bin_dir / 'sudo').write_text(
        f'#!/bin/sh\n[ "$3" = peers ] && exec printf %s \'{json.dumps(peers)}\'\ntouch {asked}\nexec sleep 30\n'
    )
    (bin_dir / 'sudo').chmod(0o755)
    kept = tmp_path / 'kept.json'
    target = f'{conventions.overlay.UDM}:22'
    network = conventions.overlay.NETWORK_ID
    reading = [*('reading', network, '60', '--every', target), *('--keep', str(kept))]
    if command == 'again':
        # The join's own reading, which keeps its inputs before it reads,
        # against a daemon that refuses every command.
        refusing = tmp_path / 'refusing'
        refusing.mkdir()
        (refusing / 'sudo').write_text('#!/bin/sh\nexit 64\n')
        (refusing / 'sudo').chmod(0o755)
        _ = process_sessions.run(
            [sys.executable, '-I', '-S', str(READING), *reading],
            env={'PATH': f'{refusing}{os.pathsep}{os.environ["PATH"]}'},
            text=True,
            timeout=60,
        )
    argv = [sys.executable, '-I', '-S', str(READING), *(reading if command == 'reading' else ['again', str(kept)])]
    environment = {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'}
    with process_sessions.started(argv, env=environment, stdout=subprocess.PIPE, text=True) as process:
        give_up = time.monotonic() + 20
        while not asked.exists():
            assert time.monotonic() < give_up, 'the reading never asked for the network listing'
            time.sleep(0.05)
        os.kill(process.pid, signal.SIGTERM)
        stdout, _ = process.communicate(timeout=20)

    label = 'zt-reading' if command == 'reading' else r'zt-reading\(at-failure\)'
    assert re.fullmatch(rf'{label}: roots=DIRECT/84ms/ipv4 udm=absent cut=timeout\n', stdout), stdout
