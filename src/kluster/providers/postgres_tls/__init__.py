"""A Postgres server's TLS handshake, waited for as a resource.

`Readiness` is created once the server at `address` completes a TLS
handshake on `port` with a certificate that chains to `ca_certificate`,
under OpenSSL's strict checks on every interpreter (`handshake`), and names
`address`. That is the whole of what the resource asserts: a box that
sends such a certificate is the box the address is meant to reach, and it
sends it before anything authenticates, so the wait needs no credential.
Postgres speaks TLS only after its own request for it, so the handshake opens
with that request (the protocol's `SSLRequest`) and goes on only when the
server answers that it will.

**Not answering is waited out; the wrong certificate is refused at once.** A
box that is still booting refuses or drops the connection, which is the
expected state for minutes after a launch, so each attempt that fails to
connect is followed by another until `timeout` runs out. A box that answers
with a certificate that does not chain to the authority, or that names
another address, is not going to become the right box by waiting, and the
create fails naming what the certificate is not.

**The inputs are what the wait dials and what it holds the answer to**,
plus `instance_id`, which the wait does not read: it is there so that a
caller declaring `replace_on_changes` on it gets this resource replaced, and
its creation run again, whenever the box behind the address is a new one.
There is no `diff`, so any other change updates the resource, and an update
waits again.
"""

from __future__ import annotations

import logging
import socket
import ssl
import struct
import time
from collections.abc import Callable
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic

__all__ = (
    'ATTEMPT_TIMEOUT',
    'INTERVAL',
    'SSL_REQUEST',
    'TIMEOUT',
    'NotAnswering',
    'Readiness',
    'ReadinessProvider',
    'WrongCertificate',
    'handshake',
    'wait',
)

log = logging.getLogger(__name__)

#: How long a create waits for a box that does not answer yet. A first boot
#: pulls the Postgres image before it listens, which is minutes.
TIMEOUT = 900

#: How long one attempt may take, and how long passes between two.
ATTEMPT_TIMEOUT = 30
INTERVAL = 15

#: The `SSLRequest` message: its length, then the request code the protocol
#: reserves for it. The server answers one byte, `S` for TLS to follow.
SSL_REQUEST = struct.pack('!ii', 8, 80877103)


class NotAnswering(Exception):
    """The server did not get as far as presenting a certificate."""


@final
class WrongCertificate(Exception):
    """The server presented a certificate that does not chain to the authority, or names another address."""

    def __init__(self, address: str, port: int, reason: str) -> None:
        super().__init__(
            f'{address}:{port} answered with a certificate the declared authority does not vouch for as '
            f'{address}: {reason}'
        )


def handshake(address: str, port: int, ca_certificate: str, *, timeout: float = ATTEMPT_TIMEOUT) -> None:
    """One attempt: `SSLRequest`, then TLS, the certificate held to `ca_certificate` and to `address`.

    `address` is what the certificate must name, as an IP address or a host
    name, which is how the server's clients hold it.

    The chain is held to OpenSSL's strict checks (`VERIFY_X509_STRICT`) by
    this function rather than by the interpreter's default: Python's default
    context sets the flag from 3.13 and not before, so leaving it to the
    default would accept on one interpreter a certificate another refuses.
    """
    context = ssl.create_default_context(cadata=ca_certificate)
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    try:
        with socket.create_connection((address, port), timeout=timeout) as raw:
            raw.sendall(SSL_REQUEST)
            answer = raw.recv(1)
            if answer != b'S':
                raise NotAnswering(f'{address}:{port} did not offer TLS (answered {answer!r})')
            with context.wrap_socket(raw, server_hostname=address):
                pass
    except ssl.SSLCertVerificationError as exc:
        raise WrongCertificate(address, port, exc.verify_message or str(exc)) from None
    except OSError as exc:
        raise NotAnswering(f'{address}:{port}: {exc}') from exc


def wait(
    address: str,
    port: int,
    ca_certificate: str,
    *,
    timeout: float = TIMEOUT,
    attempt: Callable[[str, int, str], None] = handshake,
) -> None:
    """Attempt the handshake until it succeeds, refusing on the wrong certificate and giving up after `timeout`."""
    log.info(
        'waiting for %s:%d to complete a TLS handshake as %s, trying every %ds for up to %ds — a first boot '
        'pulls the Postgres image before it listens',
        address,
        port,
        address,
        INTERVAL,
        int(timeout),
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            attempt(address, port, ca_certificate)
        except NotAnswering as exc:
            if time.monotonic() + INTERVAL > deadline:
                raise NotAnswering(f'{address}:{port} did not answer within {int(timeout)}s; last: {exc}') from exc
            log.info('not yet: %s', exc)
            time.sleep(INTERVAL)
            continue
        log.info('%s:%d answered as %s', address, port, address)
        return


def _waited(props: dict[str, Any]) -> None:
    wait(str(props['address']), int(props['port']), str(props['ca_certificate']))


@final
class ReadinessProvider(dynamic.ResourceProvider):
    """Create waits; update waits again; delete forgets."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        _waited(props)
        return dynamic.CreateResult(id_=f'{props["address"]}:{props["port"]}', outs=props)

    def update(self, _id: str, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        _waited(news)
        return dynamic.UpdateResult(outs=news)

    def delete(self, _id: str, _props: dict[str, Any]) -> None:
        """Nothing is held, so nothing is released."""


@final
class Readiness(dynamic.Resource, module='postgres_tls', name='Readiness'):
    """The Postgres server at `address` answering TLS as `address`, under `ca_certificate`."""

    address: pulumi.Output[str]
    port: pulumi.Output[int]
    ca_certificate: pulumi.Output[str]
    instance_id: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        address: pulumi.Input[str],
        port: pulumi.Input[int],
        ca_certificate: pulumi.Input[str],
        instance_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            ReadinessProvider(),
            name,
            {'address': address, 'port': port, 'ca_certificate': ca_certificate, 'instance_id': instance_id},
            opts,
        )
