"""What the PD lease read rests on in the engine, held by the pinned `pulumi` itself.

`DelegatedPrefixLease` observes in `check` and writes nothing, so a prefix the
ISP moved has to be a diff a plain preview plans, with no refresh, and a
resource built from the read prefix has to move with it in the same plan. No
mock plans a step, so each case runs the CLI `mise.toml` pins over a `file://`
backend and a `PULUMI_HOME` of the case's own, against a program holding the
real resource and one stand-in that consumes its `prefix` and logs what its
`check` was handed. The resource dials a device stand-in: an SSH server on
`127.0.0.1` presenting a key of the case's own and admitting the case's client
key, which runs each command it is sent under `/bin/sh` with the lease's path
pointed at a file the case writes, and records every command. The project
around the program is `scratch_projects`', and every command leads a POSIX
session of its own that ends with it (`process_sessions`).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess as sp
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, final

import asyncssh
import process_sessions
import pytest
from pulumi.runtime import rpc
from scratch_projects import scratch_project

import kluster
from kluster.providers import pd_lease

#: How long one case may take: its project's set-up and every command it runs
#: against the engine, at the bounds of `tests/test_unifi_bgp_engine.py`, whose
#: handful of commands these are. A stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, below the case bound.
COMMAND_TIMEOUT = 120

#: How long the device stand-in may take to start listening or to stop: a
#: stop-loss, far above the milliseconds either takes.
SERVER_TIMEOUT = 30

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

STACK = 'probe'

#: The lease as the gateway held it, its prefix rewritten under the
#: documentation prefix, and the same lease after the ISP moved the prefix.
RECORDED = (Path(__file__).parent / 'data' / 'pd.leases').read_text()
MOVED = RECORDED.replace('2001:db8:83:171', '2001:db8:84:2')

#: The program: the read, and a stand-in that consumes the read's prefix and
#: logs, from its `check`, the value it was handed in each run.
PROGRAM = """\
import os
import pathlib

from pulumi.dynamic import CheckResult, CreateResult, DiffResult, Resource, ResourceProvider, UpdateResult

from kluster.providers.device_files.provider import Connection
from kluster.providers.pd_lease import DelegatedPrefixLease

LOG = pathlib.Path(os.environ['PROBE_LOG'])


class Consumer(ResourceProvider):
    def check(self, olds, news):
        with LOG.open('a') as handle:
            handle.write(f'check {news.get("prefix")}\\n')
        return CheckResult(news, [])

    def create(self, props):
        return CreateResult('consumer', dict(props))

    def diff(self, _id, olds, news):
        return DiffResult(changes=olds.get('prefix') != news.get('prefix'), replaces=[])

    def update(self, _id, olds, news):
        return UpdateResult(dict(news))


class Consuming(Resource, module='probe', name='Consuming'):
    pass


lease = DelegatedPrefixLease(
    'lease',
    connection=Connection(
        host='127.0.0.1', port=int(os.environ['PROBE_PORT']), host_key=os.environ['PROBE_HOST_KEY']
    ),
    wan='eth8',
)
Consuming(Consumer(), 'consumer', {'prefix': lease.prefix})
"""


def _ed25519() -> asyncssh.SSHKey:
    # asyncssh leaves the algorithm-specific `**kwargs` of its key generator
    # unannotated; an ed25519 key takes none of them.
    return asyncssh.generate_private_key('ssh-ed25519')  # pyright: ignore[reportUnknownMemberType]


@final
@dataclass
class Device:
    """The device stand-in: where it answers, the keys of its session, its lease, and every command it was sent."""

    port: int
    host_key: str
    client_key: str
    lease: Path
    commands: list[str] = field(default_factory=list[str])


@pytest.fixture
def device(tmp_path: Path) -> Iterator[Device]:
    """The stand-in, served on a loopback port from a loop of its own, while the case's commands block."""
    host = _ed25519()
    client = _ed25519()
    lease = tmp_path / 'pd.leases'
    _ = lease.write_text(RECORDED)
    commands: list[str] = []

    async def answer(process: asyncssh.SSHServerProcess[bytes]) -> None:
        command = process.command or ''
        commands.append(command)
        # outside process_sessions -- the device stand-in answers on its own
        # event loop, and the helper blocks; the command is the transport's
        # one-line read, which ends with its output, and the case bound stops
        # a server that does not answer.
        ran = await asyncio.create_subprocess_exec(
            '/bin/sh',
            '-c',
            command.replace(pd_lease.LEASES, str(lease)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await ran.communicate()
        process.stdout.write(out)
        process.stderr.write(err)
        process.exit(ran.returncode or 0)

    async def listen() -> asyncssh.SSHAcceptor:
        return await asyncssh.create_server(
            asyncssh.SSHServer,
            '127.0.0.1',
            0,
            server_host_keys=[host],
            authorized_client_keys=asyncssh.import_authorized_keys(client.export_public_key().decode()),
            process_factory=answer,
            encoding=None,
        )

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(listen(), loop).result(SERVER_TIMEOUT)
    try:
        yield Device(
            port=int(server.sockets[0].getsockname()[1]),
            host_key=host.export_public_key().decode().strip(),
            client_key=client.export_private_key().decode(),
            lease=lease,
            commands=commands,
        )
    finally:
        server.close()
        asyncio.run_coroutine_threadsafe(server.wait_closed(), loop).result(SERVER_TIMEOUT)
        _ = loop.call_soon_threadsafe(loop.stop)
        thread.join(SERVER_TIMEOUT)
        loop.close()


@dataclass
class Engine:
    """One scratch project, the device it reads, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]
    log: Path
    device: Device

    def pulumi(self, *args: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=self.project, env=self.env, text=True, timeout=COMMAND_TIMEOUT
        )

    def plain_preview(self) -> dict[str, Any]:
        """A preview with no refresh, as JSON: what it plans for each resource, by name."""
        previewed = self.pulumi('preview', '--json', '--diff')
        assert previewed.returncode == 0, previewed.stdout + previewed.stderr
        return cast('dict[str, Any]', json.loads(previewed.stdout))

    def checked(self) -> list[str]:
        """What the consumer's `check` was handed, one line per call, and then forgotten."""
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return lines


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _ops(preview: dict[str, Any]) -> dict[str, str]:
    """Each resource's planned operation, by its logical name."""
    steps = cast('list[dict[str, Any]]', preview.get('steps', []))
    return {str(step['urn']).rpartition('::')[2]: str(step['op']) for step in steps}


@pytest.fixture
def fresh(tmp_path: Path, device: Device) -> Engine:
    """A fresh stack, configured and never applied, under the recorded lease."""
    scratch = scratch_project(tmp_path, PROGRAM)
    # A program importing this repository's code names `src/` itself: an
    # editable install's own `.pth` is not read through the case's `.pth`.
    (site_packages,) = (tmp_path / 'venv').glob('lib/python*/site-packages')
    _ = (site_packages / 'kluster.pth').write_text(f'{Path(kluster.__file__).parent.parent}\n')
    engine = Engine(
        project=scratch.directory,
        env=scratch.env
        | {'PROBE_PORT': str(device.port), 'PROBE_HOST_KEY': device.host_key, 'PROBE_LOG': str(tmp_path / 'log')},
        log=tmp_path / 'log',
        device=device,
    )
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = engine.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    configured = engine.pulumi('config', 'set', '--secret', pd_lease.PRIVATE_KEY_CONFIG, '--', device.client_key)
    assert configured.returncode == 0, configured.stderr
    return engine


@pytest.fixture
def launched(fresh: Engine) -> Engine:
    """A fresh stack after its first `up`, under the recorded lease."""
    first = fresh.pulumi('up', '--yes', '--skip-preview')
    assert first.returncode == 0, _ran(first)
    assert fresh.checked() == ['check 2001:db8:83:1710::/60']
    return fresh


def test_the_first_preview_already_knows_the_prefix(fresh: Engine) -> None:
    """The read is a create in the first preview, and what is built from it is handed the prefix it read, known.

    So the first preview of a stack that gains the read renders the
    addresses derived from the prefix, rather than placeholders.
    """
    preview = fresh.plain_preview()

    ops = _ops(preview)
    assert (ops['lease'], ops['consumer']) == ('create', 'create'), ops
    assert fresh.checked() == ['check 2001:db8:83:1710::/60']


def test_a_moved_prefix_is_planned_by_a_plain_preview_and_moves_what_is_built_from_it(launched: Engine) -> None:
    """The ISP moving the prefix is an update of the read and of its consumer, in a preview that refreshes nothing.

    The consumer's `check` is handed the new prefix as a known value in that
    preview, since the read put it in its checked inputs, so a preview
    renders what a dependent's input becomes, not a placeholder.
    """
    _ = launched.device.lease.write_text(MOVED)

    preview = launched.plain_preview()

    ops = _ops(preview)
    assert (ops['lease'], ops['consumer']) == ('update', 'update'), ops
    assert launched.checked() == ['check 2001:db8:84:20::/60']
    assert rpc.UNKNOWN not in json.dumps(preview)


def test_a_renewal_that_keeps_the_prefix_plans_nothing(launched: Engine) -> None:
    _ = launched.device.lease.write_text(RECORDED.replace('1794796588156', '1794796590000'))

    previewed = launched.pulumi('preview', '--expect-no-changes', '--diff')

    assert previewed.returncode == 0, _ran(previewed)


def test_the_moved_prefix_lands_in_state_with_an_up(launched: Engine) -> None:
    _ = launched.device.lease.write_text(MOVED)

    applied = launched.pulumi('up', '--yes', '--skip-preview')
    assert applied.returncode == 0, _ran(applied)
    exported = launched.pulumi('stack', 'export')
    assert exported.returncode == 0, exported.stderr

    resources = cast('list[dict[str, Any]]', json.loads(exported.stdout)['deployment']['resources'])
    (consumer,) = (it for it in resources if str(it['urn']).endswith('::consumer'))
    assert consumer['outputs']['prefix'] == '2001:db8:84:20::/60'
    assert launched.pulumi('preview', '--expect-no-changes').returncode == 0


def test_the_device_is_only_ever_asked_to_read_the_lease(launched: Engine) -> None:
    """Every command the device was sent, across an `up` and a preview, is the transport's read of the lease."""
    _ = launched.device.lease.write_text(MOVED)
    _ = launched.plain_preview()

    read = f'if [ ! -f {pd_lease.LEASES} ]; then exit'
    assert launched.device.commands
    assert all(command.startswith(read) for command in launched.device.commands), launched.device.commands
