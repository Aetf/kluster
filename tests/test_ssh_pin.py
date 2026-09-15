"""The host-key pin, against a real OpenSSH client and a real `sshd`.

Every other case about the pin reads an argument vector. This one is the
only place the claim is checked end to end -- that the options
`provision.pin_options` produces make a client *refuse* a server holding
the wrong key -- and it needs both programs to do it: OpenSSH decides what
those options mean, and a test asserting on a string decides nothing.

The server runs under `ssh`'s own `ProxyCommand`, which hands `sshd -i` the
connection on its standard streams. That is what keeps the case
deterministic: there is no port to pick, nothing to listen, and no moment
at which the server may not be up yet.
"""

from __future__ import annotations

import shutil
import subprocess as sp
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kluster.scripts.state_backend import config, provision

#: sshd is not on the default `PATH` of every distribution, and a runner
#: image may carry the client without the server (`openssh-server`).
SSHD = shutil.which('sshd') or shutil.which('sshd', path='/usr/sbin:/usr/libexec/openssh:/usr/lib/ssh')
needs_sshd = pytest.mark.skipif(SSHD is None, reason='no sshd on this machine (openssh-server)')
needs_ssh = pytest.mark.skipif(shutil.which('ssh') is None, reason='no ssh client on this machine')

#: What the appliance's reserved address stands in as. Never resolved: the
#: `ProxyCommand` below is what the client talks to, and this is only ever the
#: name the `known_hosts` entry is keyed by -- which is the half of the pin
#: under test.
ADDRESS = '192.0.2.10'

#: OpenSSH's own words for the two outcomes this case tells apart. Both are
#: the client's, not this repository's, which is what makes them worth
#: asserting on: they are how an operator will read the same failure.
REFUSED = 'Host key verification failed'
PAST_THE_HOST_KEY = 'Permission denied'


class Host:
    """One ed25519 identity, as the two files a server and a client need."""

    def __init__(self, directory: Path, name: str) -> None:
        key = Ed25519PrivateKey.generate()
        self.private: Path = directory / name
        _ = self.private.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        # sshd refuses a host key anyone else can read.
        self.private.chmod(0o600)
        self.public: str = (
            key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )


def _serving(directory: Path, host: Host) -> str:
    """A `ProxyCommand` running `sshd` under `host`'s identity.

    Authentication is left with nothing to accept: what this case is about
    happens before it, and a server that cannot let anybody in is the clearest
    way to see that the client got that far.
    """
    configuration = directory / 'sshd_config'
    _ = configuration.write_text(
        f'HostKey {host.private}\n'
        'PidFile none\n'
        # The key and its directory live under the test's own tree.
        'StrictModes no\n'
        'UsePAM no\n'
        'PasswordAuthentication no\n'
        'KbdInteractiveAuthentication no\n'
        'AuthorizedKeysFile none\n'
        'LogLevel ERROR\n'
    )
    # `-e` puts the server's own errors on stderr beside the client's, so a
    # server that failed to start reads as that rather than as a refusal.
    return f'{SSHD} -i -e -f {configuration}'


def _dial(directory: Path, *, serving: Host, pinned: Host) -> sp.CompletedProcess[str]:
    known_hosts = config.write_known_hosts(directory / 'slot', address=ADDRESS, public_key=pinned.public)
    return sp.run(
        [
            'ssh',
            *provision.pin_options(known_hosts),
            # Scaffolding, not part of the pin: without them a case that is
            # meant to end in a refusal can sit waiting for a prompt instead.
            '-o',
            'BatchMode=yes',
            '-o',
            f'ProxyCommand={_serving(directory, serving)}',
            f'core@{ADDRESS}',
            'true',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


@needs_ssh
@needs_sshd
def test_a_server_holding_a_different_key_is_refused(tmp_path: Path) -> None:
    """The whole point of the pin: an interposer answers with a key of its own.

    Nothing about the wrong key is visible to the client except that it is not
    the pinned one -- which is the only thing that has to be enough.
    """
    interposer = Host(tmp_path, 'interposer')
    appliance = Host(tmp_path, 'appliance')

    dialed = _dial(tmp_path, serving=interposer, pinned=appliance)

    assert dialed.returncode != 0
    assert REFUSED in dialed.stderr


@needs_ssh
@needs_sshd
def test_a_server_holding_the_pinned_key_gets_past_the_host_key_phase(tmp_path: Path) -> None:
    """The control, without which the refusal above proves nothing.

    A fixture whose server never starts refuses every connection too, and the
    case above would pass on it. What separates the two is where the failure
    lands: here the client is through host-key verification and stopped by
    authentication, which this server accepts from nobody.
    """
    appliance = Host(tmp_path, 'appliance')

    dialed = _dial(tmp_path, serving=appliance, pinned=appliance)

    assert REFUSED not in dialed.stderr
    assert PAST_THE_HOST_KEY in dialed.stderr
