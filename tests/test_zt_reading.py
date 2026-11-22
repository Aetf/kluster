"""The join's reading: the filter that keeps physical paths out of a public log, and the verdicts of its probes.

The filter and the reading run as the action runs them, by path under
`python3 -I -S`, so the cases also hold that the script needs nothing past the
standard library and `kluster.conventions`. The probes run against ports on
this host's loopback, each shaped so that a wrong verdict reads differently
from the right one: a far end advertising a runner's MSS, a byte never sent,
a page in writes smaller than a segment, a TLS flight in narrow segments.
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
from peer_listings import PEER_PORTS, PEERS, addresses

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


def _peer(node: str, role: str, latency: int, address: str | None, *, tunneled: bool = False) -> dict[str, object]:
    """One peer as `zerotier-cli -j peers` prints it, with its preferred path at `address`, or none."""
    paths = [] if address is None else [{'address': address, 'active': True, 'expired': False, 'preferred': True}]
    return {'address': node, 'role': role, 'latency': latency, 'paths': paths, 'tunneled': tunneled}


def test_the_reading_names_the_gateway_from_the_roster_and_reports_the_interfaces_mtu(tmp_path: Path) -> None:
    """The reading takes the gateway's node id from the roster, under the standard library alone.

    The gateway is one leaf among others, listed after a leaf on a relay, so a
    reading that took the first leaf reads `RELAY`. The interface's MTU and the
    configured one differ, so a reading that printed one for the other shows
    it. The member's address is no identity's, so the size probes are skipped
    and the case dials nothing.
    """
    udm = conventions.overlay.member(conventions.overlay.MEMBER_UDM)
    assert isinstance(udm, conventions.overlay.EnrolledMember)
    peers = [
        _peer('0123456789', 'LEAF', 90, None),
        _peer(udm.node_id, 'LEAF', 23, '192.0.2.1/21032'),
        _peer('778cde7190', 'PLANET', 84, '192.0.2.66/9993'),
        _peer('cafe04eba9', 'PLANET', 160, '[2001:db8::1]/9993', tunneled=True),
    ]
    network = {
        'nwid': conventions.overlay.NETWORK_ID,
        'mtu': 2800,
        'portDeviceName': 'ztprobe0',
        'assignedAddresses': ['192.0.2.200/24'],
    }
    answers = tmp_path / 'answers'
    answers.mkdir()
    (answers / 'peers').write_text(json.dumps(peers))
    (answers / 'listnetworks').write_text(json.dumps([network]))
    (answers / 'link').write_text(json.dumps([{'ifname': 'ztprobe0', 'mtu': 1280}]))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, script in {
        'sudo': f'#!/bin/sh\n[ "$1 $2" = "zerotier-cli -j" ] && exec cat {answers}/"$3"\nexit 64\n',
        'ip': f'#!/bin/sh\n[ "$*" = "-j link show dev ztprobe0" ] && exec cat {answers}/link\nexit 64\n',
    }.items():
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)

    done = process_sessions.run(
        [sys.executable, '-I', '-S', str(READING), 'reading', conventions.overlay.NETWORK_ID, '10'],
        env={'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'},
        text=True,
        timeout=30,
    )

    line = cast('str', done.stdout)
    assert done.returncode == 0, line
    assert line.startswith('zt-reading: '), line
    fields = dict(field.split('=', 1) for field in line.split()[1:])
    assert fields['gateway'] == 'DIRECT/23ms/ipv4', line
    assert fields['roots'] == 'DIRECT/84ms/ipv4,RELAY/160ms/ipv6', line
    assert (fields['mtu'], fields['netconf']) == ('1280', '2800'), line
    assert {fields[probe] for probe in ('up.small', 'up.full', 'down.small', 'down.full')} == {'skipped'}, line
    assert addresses(line) == [], line


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
    """The first of two legs fails its small segment; the reading probes the second and names it.

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

    assert fields == {'leg': 'second', 'up.small': 'ok', 'up.full': 'ok', 'down.small': 'ok', 'down.full': 'ok'}


def test_a_reading_ended_by_the_steps_timeout_prints_what_it_had(tmp_path: Path) -> None:
    """The step's `timeout` ends a reading wedged in a wait, and the line still prints, marked as cut.

    The stand-in for `sudo` answers the peer listing and then never answers
    the network listing; the case signals the reading once the stand-in has
    been asked for it, as `timeout` would.
    """
    asked = tmp_path / 'asked'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    peers = [_peer('778cde7190', 'PLANET', 84, '192.0.2.66/9993')]
    (bin_dir / 'sudo').write_text(
        f'#!/bin/sh\n[ "$3" = peers ] && exec printf %s \'{json.dumps(peers)}\'\ntouch {asked}\nexec sleep 30\n'
    )
    (bin_dir / 'sudo').chmod(0o755)
    argv = [sys.executable, '-I', '-S', str(READING), 'reading', conventions.overlay.NETWORK_ID, '60']
    environment = {'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'}
    with process_sessions.started(argv, env=environment, stdout=subprocess.PIPE, text=True) as command:
        give_up = time.monotonic() + 20
        while not asked.exists():
            assert time.monotonic() < give_up, 'the reading never asked for the network listing'
            time.sleep(0.05)
        os.kill(command.pid, signal.SIGTERM)
        stdout, _ = command.communicate(timeout=20)

    assert re.fullmatch(r'zt-reading: gateway=absent roots=DIRECT/84ms/ipv4 cut=timeout\n', stdout), stdout
