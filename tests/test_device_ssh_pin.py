"""The gateway's host-key pin, at the call that opens the session.

`tests/test_device_files.py` checks the value `ssh.pinned_host_keys` builds.
This module checks what `ssh.connect` does with it: every case here dials an
asyncssh server running in this process on a loopback port, through the
provider's own `connect`, so the arguments that call hands `asyncssh.connect`
are what is under test rather than a value they were built from.

The pin is one of the ambient inputs the call refuses (`ssh`'s module
docstring); the others are the host trust a client takes from its home
directory, an OpenSSH client configuration, and an `ssh-agent`. Each case runs
with those planted. `$HOME` is a scratch directory holding the configuration
and the `known_hosts`, and `SSH_AUTH_SOCK` names a socket beside it that
records every connection made to it; the X.509 cases add a certificate
authority to `$HOME` as well. A client that consults any of them leaves a trace
a case reads, and the control cases at the end show that a client left at
asyncssh's defaults does leave one -- which is what makes an absent trace mean
something.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, final

import asyncssh
import pytest
import pytest_asyncio
from asyncssh.public_key import SSHX509Certificate

from kluster.providers.device_files import ssh

#: The one address every server here listens on and every client dials.
HOST = '127.0.0.1'

#: What the in-process server answers every command with, so a case can tell
#: a session that ran a command from one that merely opened.
ANSWER = 'ran on the pinned server'


def _answer(process: asyncssh.SSHServerProcess[Any]) -> None:
    process.stdout.write(ANSWER)
    process.exit(0)


def _ed25519() -> asyncssh.SSHKey:
    # asyncssh leaves the algorithm-specific `**kwargs` of its key generator
    # unannotated; an ed25519 key takes none of them.
    return asyncssh.generate_private_key('ssh-ed25519')  # pyright: ignore[reportUnknownMemberType]


@final
@dataclass
class Keys:
    """The key material one case needs, all of it generated for the case."""

    pinned: asyncssh.SSHKey = field(default_factory=_ed25519)
    interposer: asyncssh.SSHKey = field(default_factory=_ed25519)
    client: asyncssh.SSHKey = field(default_factory=_ed25519)

    def device(self, port: int) -> ssh.Device:
        """The device as the provider is handed it: the pinned key, the client credential."""
        return ssh.Device(
            host=HOST,
            port=port,
            username='root',
            private_key=self.client.export_private_key().decode(),
            host_key=self.pinned.export_public_key().decode(),
        )


@final
@dataclass
class AmbientInputs:
    """What an operator's machine offers a client that did not refuse it.

    `home` is the `$HOME` the case runs under. `proxied` is the file the
    configuration's `ProxyCommand` creates, so it exists once anything has
    read the configuration and tried to connect through it. `agent_contacts`
    counts connections to the agent socket.
    """

    home: Path
    proxied: Path
    agent_contacts: int = 0


@pytest.fixture
def keys() -> Keys:
    return Keys()


@pytest_asyncio.fixture(autouse=True)
async def ambient(tmp_path: Path, keys: Keys, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[AmbientInputs]:
    """A `$HOME` and an agent that would each change the session if consulted.

    The configuration sends every host through a `ProxyCommand` that leaves a
    file behind, which is how a bastion in an operator's `~/.ssh/config`
    redirects a connection. A directive `connect` also passes as an argument
    -- the port, the user, the keys, the trust -- would prove nothing, because
    the argument wins whether or not the file was read; a `ProxyCommand` still
    takes effect, and its trace stays on this machine, where a `Hostname`
    redirect is seen only by what answers at another address. The `known_hosts`
    trusts the interposer's key for every host, so a client that fell back on
    it would accept the one server the pin exists to refuse. The agent socket
    records the connection, takes the request, and hangs up without an answer
    -- an agent asyncssh carries on without, so the record is the only trace
    it leaves.
    """
    home = tmp_path / 'home'
    dot_ssh = home / '.ssh'
    dot_ssh.mkdir(parents=True)
    proxied = tmp_path / 'proxied'
    (dot_ssh / 'config').write_text(f'Host *\n  ProxyCommand touch {shlex.quote(str(proxied))}\n')
    (dot_ssh / 'known_hosts').write_bytes(b'* ' + keys.interposer.export_public_key())
    monkeypatch.setenv('HOME', str(home))

    found = AmbientInputs(home=home, proxied=proxied)

    async def record(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        found.agent_contacts += 1
        # The request is read before the hang-up so the client meets an end
        # of stream, which it takes as an agent with nothing to offer. Closing
        # over an unread request resets the connection instead, and asyncssh
        # fails the whole session on that.
        length = int.from_bytes(await reader.readexactly(4), 'big')
        _ = await reader.readexactly(length)
        writer.close()

    socket = tmp_path / 'agent.sock'
    agent = await asyncio.start_unix_server(record, path=socket)
    monkeypatch.setenv('SSH_AUTH_SOCK', str(socket))
    try:
        yield found
    finally:
        agent.close()
        await agent.wait_closed()


async def _serve(
    host_key: asyncssh.SSHKey, client: asyncssh.SSHKey, certificate: SSHX509Certificate | None = None
) -> asyncssh.SSHAcceptor:
    """A server on a loopback port, presenting `host_key` and admitting `client`.

    With a `certificate`, the server presents that X.509 certificate for its
    key as well as the bare key. Listening has begun when this returns, so the
    port it reports is one a client can dial at once.
    """
    return await asyncssh.create_server(
        asyncssh.SSHServer,
        HOST,
        0,
        server_host_keys=[host_key if certificate is None else (host_key, certificate)],
        authorized_client_keys=asyncssh.import_authorized_keys(client.export_public_key().decode()),
        process_factory=_answer,
    )


def _port(server: asyncssh.SSHAcceptor) -> int:
    return int(server.sockets[0].getsockname()[1])


@pytest_asyncio.fixture
async def pinned(keys: Keys) -> AsyncGenerator[asyncssh.SSHAcceptor]:
    """The server the device is: it presents the pinned key."""
    server = await _serve(keys.pinned, keys.client)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def interposer(keys: Keys) -> AsyncGenerator[asyncssh.SSHAcceptor]:
    """An interposer: everything the device is, but its key is not the pinned one."""
    server = await _serve(keys.interposer, keys.client)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()


#: Where asyncssh's defaults take X.509 host trust from: a bundle of
#: authorities, and a directory of them named by subject hash.
TRUST_LOCATIONS = ('ca-bundle.crt', 'crt')


#: asyncssh builds and checks X.509 certificates through pyOpenSSL calls that
#: pyOpenSSL has deprecated. The warning is about asyncssh's code, which no
#: case here can change, so the cases that touch certificates are muted for it.
X509_DEPRECATIONS = pytest.mark.filterwarnings(r'ignore:X509\.\w+ is deprecated:DeprecationWarning')


@pytest.fixture(params=TRUST_LOCATIONS)
def certificate(request: pytest.FixtureRequest, keys: Keys, ambient: AmbientInputs) -> SSHX509Certificate:
    """An X.509 host certificate for the interposer's key, from an authority `$HOME` trusts.

    The certificate names the address the session dials, which is the check
    asyncssh makes of it beside the chain. The authority goes where the
    parameter says: into the bundle, or into the directory under the name
    asyncssh looks it up by -- the issuer's hash and a sequence number.
    """
    location = str(request.param)
    authority = _ed25519()
    subject = 'CN=interposer authority'
    authority_certificate = authority.generate_x509_ca_certificate(authority, subject)
    certificate = authority.generate_x509_host_certificate(
        keys.interposer, 'CN=gateway', issuer=subject, principals=[HOST]
    )
    pem = authority_certificate.export_certificate('pem')
    dot_ssh = ambient.home / '.ssh'
    if location == 'ca-bundle.crt':
        (dot_ssh / location).write_bytes(pem)
    else:
        (dot_ssh / location).mkdir()
        (dot_ssh / location / f'{certificate.issuer_hash}.0').write_bytes(pem)
    return certificate


@pytest_asyncio.fixture
async def certified_interposer(keys: Keys, certificate: SSHX509Certificate) -> AsyncGenerator[asyncssh.SSHAcceptor]:
    """The interposer, presenting a certificate an authority in `$HOME` issued."""
    server = await _serve(keys.interposer, keys.client, certificate)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_server_presenting_another_key_is_refused(keys: Keys, interposer: asyncssh.SSHAcceptor) -> None:
    """The interposer admits the client credential, so the pin is all that stands
    between the provider and a session on it; and the `known_hosts` in `$HOME`
    vouches for the interposer, so a call that fell back on the user's own trust
    would open that session too."""
    with pytest.raises(ssh.HostKeyRefused):
        async with ssh.connect(keys.device(_port(interposer))):
            pass


@X509_DEPRECATIONS
@pytest.mark.asyncio
async def test_a_certificate_from_an_authority_in_home_does_not_stand_in_for_the_pin(
    keys: Keys, certified_interposer: asyncssh.SSHAcceptor
) -> None:
    """asyncssh validates an X.509 host certificate against its authorities
    alone, never against the pinned keys. With X.509 switched off the
    interposer is left with its bare key, which is refused like any other
    unpinned key -- so the refusal is the provider's own `HostKeyRefused`."""
    with pytest.raises(ssh.HostKeyRefused):
        async with ssh.connect(keys.device(_port(certified_interposer))):
            pass


@pytest.mark.asyncio
async def test_the_pinned_key_opens_a_session(keys: Keys, pinned: asyncssh.SSHAcceptor) -> None:
    async with ssh.connect(keys.device(_port(pinned))) as transport:
        result = await transport.run('true')

    assert (result.exit_status, result.text) == (0, ANSWER)


@pytest.mark.asyncio
async def test_the_session_reads_no_client_configuration(
    keys: Keys, pinned: asyncssh.SSHAcceptor, ambient: AmbientInputs
) -> None:
    # A call that read the configuration fails to connect at all, because the
    # `ProxyCommand` carries nothing; checking the trace on the way out names
    # that cause rather than the connection error it produces.
    try:
        async with ssh.connect(keys.device(_port(pinned))) as transport:
            result = await transport.run('true')
    finally:
        assert not ambient.proxied.exists(), 'the session went through the ProxyCommand in $HOME/.ssh/config'

    assert result.text == ANSWER


@pytest.mark.asyncio
async def test_the_session_asks_no_agent(keys: Keys, pinned: asyncssh.SSHAcceptor, ambient: AmbientInputs) -> None:
    async with ssh.connect(keys.device(_port(pinned))) as transport:
        result = await transport.run('true')

    assert result.text == ANSWER
    assert ambient.agent_contacts == 0, 'the session connected to the agent SSH_AUTH_SOCK names'


##
## Controls: the planted inputs are what a client at asyncssh's defaults
## consults. Each leaves the one input under test at its default and switches
## the others off, so the trace it finds is that input's alone. Without them
## a trace the cases above find absent could be absent because asyncssh stopped
## looking where this module plants it.
##


@pytest.mark.asyncio
async def test_control_a_default_client_trusts_the_planted_known_hosts(
    keys: Keys, interposer: asyncssh.SSHAcceptor
) -> None:
    async with asyncssh.connect(
        HOST,
        port=_port(interposer),
        username='root',
        client_keys=[keys.client],
        x509_trusted_certs=None,
        agent_path=None,
        config=None,
    ) as connection:
        completed = await connection.run('true', check=True)

    assert completed.stdout == ANSWER


@X509_DEPRECATIONS
@pytest.mark.asyncio
async def test_control_a_default_client_trusts_the_planted_authority_over_the_pin(
    keys: Keys, certified_interposer: asyncssh.SSHAcceptor
) -> None:
    """The pin is handed over exactly as `connect` hands it, and the session
    opens on the interposer anyway: the certificate is the whole of it."""
    async with asyncssh.connect(
        HOST,
        port=_port(certified_interposer),
        username='root',
        client_keys=[keys.client],
        known_hosts=ssh.pinned_host_keys(keys.pinned.export_public_key().decode()),
        agent_path=None,
        config=None,
    ) as connection:
        completed = await connection.run('true', check=True)

    assert completed.stdout == ANSWER


@pytest.mark.asyncio
async def test_control_a_default_client_reads_the_planted_configuration(
    keys: Keys, pinned: asyncssh.SSHAcceptor, ambient: AmbientInputs
) -> None:
    # The `ProxyCommand` exits as soon as it has left its file, so the
    # connection it carried fails; the file is the trace.
    with pytest.raises((OSError, asyncssh.Error)):
        async with asyncssh.connect(
            HOST,
            port=_port(pinned),
            username='root',
            client_keys=[keys.client],
            known_hosts=ssh.pinned_host_keys(keys.pinned.export_public_key().decode()),
            x509_trusted_certs=None,
            agent_path=None,
        ):
            pass

    assert ambient.proxied.exists()


@pytest.mark.asyncio
async def test_control_a_default_client_asks_the_planted_agent(
    keys: Keys, pinned: asyncssh.SSHAcceptor, ambient: AmbientInputs
) -> None:
    async with asyncssh.connect(
        HOST,
        port=_port(pinned),
        username='root',
        client_keys=[keys.client],
        known_hosts=ssh.pinned_host_keys(keys.pinned.export_public_key().decode()),
        x509_trusted_certs=None,
        config=None,
    ) as connection:
        completed = await connection.run('true', check=True)

    assert completed.stdout == ANSWER
    assert ambient.agent_contacts > 0
