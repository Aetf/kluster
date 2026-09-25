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

import os
import shutil
import subprocess as sp
import tempfile
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


def _home_reaches_the_client_configuration() -> bool:
    """Whether this `ssh` takes its user configuration from `$HOME`.

    The multiplexing case below needs a client to read a configuration this
    test wrote, and to read it **the way an operator's own is read** -- through
    the default user path, which is the channel `-F /dev/null` closes. Handing
    the file over with `-F` instead would prove nothing: the mutation that has
    to redden that case is a pinned client picking up a configuration nobody
    named on its command line, and a file this test named is not that.

    Measured rather than inferred from a version string, because it is the
    property the case needs rather than the version that decides: OpenSSH
    resolves that default path through the password database on some builds
    (9.6p1 reads `getpwuid()`'s home directory and ignores `$HOME`) and
    through `$HOME` on others (10.5p1). Where it is the former, the operator
    configuration the case turns on could only be supplied by writing the real
    `~/.ssh/config`, which a test may not do.
    """
    if shutil.which('ssh') is None:
        return False
    with tempfile.TemporaryDirectory() as scratch:
        configuration = Path(scratch) / '.ssh' / 'config'
        configuration.parent.mkdir()
        _ = configuration.write_text('Host *\n  ControlMaster auto\n')
        # A user configuration anyone but its owner may write is ignored, so a
        # machine whose umask is 0002 would otherwise answer "no" for a reason
        # that has nothing to do with the question being asked.
        configuration.chmod(0o600)
        probed = sp.run(
            ['ssh', '-G', f'core@{ADDRESS}'],
            capture_output=True,
            text=True,
            timeout=30,
            env=dict(os.environ, HOME=scratch),
        )
    return 'controlmaster auto' in probed.stdout.lower()


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


needs_home_backed_configuration = pytest.mark.skipif(
    not _home_reaches_the_client_configuration(),
    reason='this ssh reads its user configuration from the password database rather than $HOME, so the '
    'operator configuration this case turns on cannot be supplied without writing the real ~/.ssh/config',
)


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


def _serving(directory: Path, host: Host, *, admits: Path | None = None) -> str:
    """A `ProxyCommand` running `sshd` under `host`'s identity.

    `admits` names a public key the server accepts, for the one case that
    needs a session to actually open. Left out, authentication has nothing to
    accept: what these cases are about happens before it, and a server that
    can let nobody in is the clearest way to see that a client got that far.
    """
    configuration = directory / f'sshd_config-{host.private.name}'
    _ = configuration.write_text(
        f'HostKey {host.private}\n'
        'PidFile none\n'
        # The key and its directory live under the test's own tree.
        'StrictModes no\n'
        'UsePAM no\n'
        'PasswordAuthentication no\n'
        'KbdInteractiveAuthentication no\n'
        f'AuthorizedKeysFile {admits or "none"}\n'
        'LogLevel ERROR\n'
    )
    # `-e` puts the server's own errors on stderr beside the client's, so a
    # server that failed to start reads as that rather than as a refusal.
    return f'{SSHD} -i -e -f {configuration}'


def _client(argv: list[str], *, cwd: Path, home: Path | None = None) -> sp.CompletedProcess[str]:
    """One `ssh` run, with the home directory its configuration is read from.

    `home` is what lets a case put a client configuration in front of the
    connection without touching the one this machine's user has.
    """
    environment = dict(os.environ)
    if home is not None:
        environment['HOME'] = str(home)
    return sp.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd, env=environment)


def _pinned_argv(known_hosts: Path, proxy: str, command: str = 'true') -> list[str]:
    return [
        'ssh',
        *provision.pin_options(known_hosts),
        # Scaffolding, not part of the pin: without it a case that is meant to
        # end in a refusal can sit waiting for a prompt instead.
        '-o',
        'BatchMode=yes',
        '-o',
        f'ProxyCommand={proxy}',
        f'core@{ADDRESS}',
        command,
    ]


def _through_one_directory(
    argv: list[str], *, cwd: Path, home: Path
) -> tuple[sp.CompletedProcess[str], sp.CompletedProcess[str]]:
    """An unpinned probe and then `argv`, both run in `cwd`.

    **One working directory for both, structurally.** A relative
    `ControlPath` makes "is a multiplexing master reachable" a property of the
    directory a client runs in rather than of the socket existing, so a pinned
    client's refusal means "the pin held" only where an unpinned one would
    have got through. Run from two directories, the pinned client would refuse
    for want of a master rather than for want of trust and the case would go
    green having proven nothing -- which is why the caller cannot point the
    two anywhere different.

    The probe carries no `ProxyCommand` and no pin, so reusing the master is
    the only way it can succeed: anything else dials the address for real,
    which is what the connect timeout bounds.
    """
    probe = _client(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', f'core@{ADDRESS}', 'true'],
        cwd=cwd,
        home=home,
    )
    return probe, _client(argv, cwd=cwd, home=home)


def _dial(directory: Path, *, serving: Host, pinned: Host) -> sp.CompletedProcess[str]:
    known_hosts = config.write_known_hosts(directory / 'slot', address=ADDRESS, public_key=pinned.public)
    return _client(_pinned_argv(known_hosts, _serving(directory, serving)), cwd=directory)


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


def test_a_new_pin_replaces_the_one_before_it(tmp_path: Path) -> None:
    """The slot holds one identity for the address, never a history of them.

    The address is reserved and the box behind it is replaced, so the
    identity the last run delivered is the only correct one. A file that
    kept the earlier line beside it would accept every box the address has
    ever named -- including one that is gone, whose key is wherever its
    boot volume went.
    """
    before = Host(tmp_path, 'before')
    after = Host(tmp_path, 'after')

    first = config.write_known_hosts(tmp_path / 'slot', address=ADDRESS, public_key=before.public)
    second = config.write_known_hosts(tmp_path / 'slot', address=ADDRESS, public_key=after.public)

    assert second == first
    assert second.read_text() == f'{ADDRESS} {after.public}\n'


@needs_ssh
@needs_sshd
def test_a_server_holding_a_superseded_pin_is_refused(tmp_path: Path) -> None:
    # The case above as OpenSSH reads the file: after a replacement, the box
    # the address used to name is an interposer like any other.
    before = Host(tmp_path, 'before')
    after = Host(tmp_path, 'after')
    _ = config.write_known_hosts(tmp_path / 'slot', address=ADDRESS, public_key=before.public)

    dialed = _dial(tmp_path, serving=before, pinned=after)

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


#: How long the master outlives the bare login that started it. Above the
#: per-case bound (`timeout` in `pyproject.toml`), so nothing the case does
#: can take longer than the master stays up: a stall between the bare login
#: and the pinned exec -- swap, a contended machine -- ends the case by its
#: bound, naming the stall, rather than by a master that went away and a
#: message about reuse. The teardown's `-O exit` is what removes it, on every
#: way out of the case; this is only how long one survives a run killed
#: outright.
CONTROL_PERSIST = 120


@needs_ssh
@needs_sshd
@needs_home_backed_configuration
def test_a_multiplexing_master_cannot_carry_the_pinned_exec(tmp_path: Path, pytestconfig: pytest.Config) -> None:
    """A client configuration is not something the pin may depend on being absent.

    `ControlMaster auto` in a `Host *` block is ordinary on an operator's
    workstation, and one bare `ssh core@<address>` outside the tool -- the
    mistake the runbook already anticipates -- leaves a master socket behind
    for as long as `ControlPersist` says. A pinned exec that read the same
    configuration would attach to that socket and run **with no host-key
    verification performed at all**: the pin would be suppressed by a file
    neither this code nor the operator thought of as part of the connection.
    Suppressing the two known-hosts files does not reach it; `-F /dev/null`
    does, and that is the mutation this case exists for.
    """
    bound = float(pytestconfig.getoption('timeout', None) or pytestconfig.getini('timeout') or 0)
    assert not bound or CONTROL_PERSIST > bound, 'the master could expire inside the case that needs it'
    interposer = Host(tmp_path, 'interposer')
    appliance = Host(tmp_path, 'appliance')
    identity = Host(tmp_path, 'identity')
    # The socket's path goes into a `sockaddr_un`, which stops at 108 bytes --
    # shorter than a pytest temporary directory plus a name. So the path is
    # relative, and every client below runs in the directory holding it: that
    # shared working directory is how the pinned exec would see the master,
    # and it is what `_through_one_directory` refuses to let drift apart.
    sockets = tmp_path / 'm'
    sockets.mkdir()
    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    configuration = home / '.ssh' / 'config'
    _ = configuration.write_text(
        f'Host *\n  ControlMaster auto\n  ControlPath ./mux-%h\n  ControlPersist {CONTROL_PERSIST}\n'
    )
    # `ssh` ignores a user configuration anyone but its owner may write, and a
    # machine whose umask is 0002 writes one of those by default.
    configuration.chmod(0o600)
    admitted = tmp_path / 'authorized_keys'
    _ = admitted.write_text(identity.public + '\n')
    proxy = _serving(tmp_path, interposer, admits=admitted)

    try:
        # The operator's bare login: trust-on-first-use, against the wrong
        # box, leaving the master that the pinned exec must not inherit.
        # Inside the `try` so that the teardown owns it too -- its own timeout
        # is the way this line raises, and it raises with a master standing.
        bare = _client(
            [
                'ssh',
                '-o',
                'StrictHostKeyChecking=accept-new',
                '-o',
                f'UserKnownHostsFile={home / ".ssh" / "known_hosts"}',
                '-o',
                'IdentitiesOnly=yes',
                '-i',
                str(identity.private),
                '-o',
                'BatchMode=yes',
                '-o',
                f'ProxyCommand={proxy}',
                f'{os.environ["USER"]}@{ADDRESS}',
                'true',
            ],
            cwd=sockets,
            home=home,
        )
        assert bare.returncode == 0, f'the bare login never opened a session: {bare.stderr}'
        assert list(sockets.iterdir()), 'no master socket, so this case would pass without proving anything'

        known_hosts = config.write_known_hosts(tmp_path / 'slot', address=ADDRESS, public_key=appliance.public)
        reuse, pinned = _through_one_directory(_pinned_argv(known_hosts, proxy), cwd=sockets, home=home)

        # The probe got through to a server it holds no pin for and could not
        # have dialled, so the master is reusable from where the pinned client
        # ran -- without which its refusal below would be a refusal for want
        # of a master, and the case would prove nothing about the pin.
        assert reuse.returncode == 0, (
            'no master is reusable from the directory both clients ran in, '
            f'so the refusal below would prove nothing: {reuse.stderr}'
        )
        assert pinned.returncode != 0
        assert REFUSED in pinned.stderr
    finally:
        _ = _client(
            ['ssh', '-O', 'exit', '-o', 'ControlPath=./mux-%h', f'{os.environ["USER"]}@{ADDRESS}'],
            cwd=sockets,
            home=home,
        )
