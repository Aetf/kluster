"""The appliance's Postgres roles, run in the images the box runs, behind its pooler.

The rendered Ignition's own files -- `pg_hba.conf`, the init script, the
pooler's configuration, the TLS material -- go into the pinned images under
each unit's own `podman run` arguments: Postgres's, and PgBouncer's in front
of it. What changes is only what has to: the address the server certificate
names, the port the pooler is published on and the network it is reached
through, which is a third container's (`_network`), the delivered
directories, which are copied into each container rather than mounted from a
host that does not have them, the directory the two units share for
Postgres's socket, which is an anonymous volume of the Postgres container's
that the pooler mounts from it (`_unit_container`), and a data directory on
a tmpfs, which starts empty and goes away with the container. So what is exercised is the image's own initialization mechanism
(`/docker-entrypoint-initdb.d`), the pooler's own authentication and cap, the
server's own authorization, and real clients: `pulumi` from this
workstation, and the Postgres client tools from the same image, which are the
server's own release. The workstation's client, `mise.toml`'s `postgres` pin,
is held only to the server's major (`tests/test_postgres_client.py`).

Nothing here mounts a host path. A remote `podman` -- one whose service runs
in another mount namespace than this process -- sees none of this process's
temporary files, and `podman cp` is the channel that reaches it either way.

Every container here ends by itself once it has started (`_bounded`); one
left `Created` goes only when something removes it, so each is named for a
teardown armed before the command that creates it (`_removing`,
`Launcher.arm`), which `process_sessions` holds every command to. Every
command a case starts -- `podman`, the client tools, the log follower --
runs through `process_sessions` under a bound below the case's. Both are
framework/testing.md §1.2.

The properties held here are the ones the box's roles and its pooler exist
for: the bootstrap superuser answers on the container's local socket and to
no certificate, the client roles are not superusers, `pulumi` writes as one
client role and reads as the other, a dump from a box whose client roles are
superusers restores into one whose roles are not, a dump and a restore go
through the pooler, the appliance's dump script and the operator's
`state.verify_dump` count the stack checkpoints the pinned `pulumi` writes --
none on a box the backend has only opened, one after `stack init` -- a
client beyond the pooler's cap waits rather than being refused, and the
pooler starts only once the serving Postgres answers.

**Skipped where the images cannot be run without fetching them.** A test that
pulls an image needs the network. This one runs where both pinned images are
already local -- a workstation has them once it has pulled
`settings.POSTGRES_IMAGE` and `settings.PGBOUNCER_IMAGE`, the tags and
digests the box runs, and again after each move of a pin -- and is skipped
elsewhere with the reason named.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import errno
import gzip
import io
import itertools
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import struct
import subprocess as sp
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from urllib import parse as urllib_parse

import host_ports
import process_sessions
import psutil
import pytest
from root_credentials import fake
from scratch_projects import cli_directories

from kluster.conventions import identity
from kluster.lib import bundle, pulumi_cli, stack_environment, workstation
from kluster.lib.bundle import CA_ENV, CERT_ENV, KEY_ENV, ssl_env
from kluster.lib.state_backend import render, settings, state, table
from kluster.providers import postgres_tls
from kluster.scripts.credentials import pki, pulumi_config
from kluster.scripts.operator_stack import driver
from kluster.scripts.state_backend import census, cli, config, probe

ADDRESS = '127.0.0.1'
IMAGE = settings.POSTGRES_IMAGE
POOLER_IMAGE = settings.PGBOUNCER_IMAGE
CLIENT_ROLES = (settings.CI_ROLE, settings.OPERATOR_ROLE)

#: The bound on each command a case starts, and on each one the code under
#: test starts for it -- `pulumi`, and the client tools `state` runs, whose
#: own bounds `clients` lowers to this one. Every call is local. With the
#: module's cases spread over four workers held to four cores beside twelve
#: busy processes, the slowest -- a box's wait from its start to serving, and
#: `pulumi stack init` -- took under a second, and the slowest case 5 s. That
#: load reaches the clients and not the container service, which is why the
#: bound sits so far above it. A stop-loss; nothing asserts on elapsed time.
COMMAND_TIMEOUT = 60

#: The bound on a case, its share of the module's set-up included: above
#: every command bound, so a stalled command fails naming it rather than as
#: the case's bound (framework/testing.md §8) -- one the case starts as a
#: `TimeoutExpired`, a `pulumi` the code under test starts as the refusal
#: `pulumi_cli.run_pulumi` raises at its bound, and a client tool `state`
#: runs as a `StateError`. The
#: largest command bound is not this module's: `render` gives `butane` 120 s
#: of its own, in `ignition`'s set-up. A stop-loss; nothing asserts on
#: elapsed time.
CASE_TIMEOUT = 240

#: How long a container these cases start may run: the run's outer bound
#: (framework/testing.md §1). One worker's cases share the module's
#: containers for as long as that worker runs them, and none is wanted past
#: the longest run the gate admits.
CONTAINER_TIMEOUT = 1200

#: The label every container these cases start carries, and so the census of
#: what a run has left: `podman ps --all --filter label=kluster-test=<module>`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Each image's own `VOLUME`s. A container is given an anonymous volume for
#: each one nothing is mounted on, so each is mounted as a tmpfs instead;
#: `test_every_container_ends_by_itself_and_mounts_no_named_volume` holds this
#: to the images.
IMAGE_VOLUMES = {
    IMAGE: (PurePosixPath('/var/lib/postgresql/data'),),
    POOLER_IMAGE: (),
}

#: The file `_own` leaves in each directory a box shares with its pooler, so
#: that podman meets the volume with something in it and leaves its owners
#: alone.
OWNED = '.kluster-test-owned'

#: The variables a client tool is handed from the case's environment, where it
#: has them: the bundle's three files, which every connection reads, and the
#: two `state` adds to a connection of its own -- the bound on opening it, and
#: the settings a census session is opened with.
TOOL_ENV = (CA_ENV, CERT_ENV, KEY_ENV, table.CONNECT_TIMEOUT_ENV, table.SESSION_OPTIONS_ENV)

#: The units whose `podman run` this repeats: Postgres, and the pooler in
#: front of it.
UNIT = 'pgstate.service'
POOLER_UNIT = 'pgbouncer.service'

#: Where the pinned `pulumi` keeps a stack's checkpoint: the backend's default
#: table, and the directory of keys under the database's own prefix. Written
#: out rather than read from `state`, because what these cases measure is the
#: backend, and `state` is what is held to it.
BACKEND_TABLE = 'pulumi_state'
BACKEND_STACKS = '.pulumi/stacks/'

#: The environment the dump unit runs its script under, and the script.
DUMP_ENV = PurePosixPath('/etc/kluster/state-dump.env')
DUMP_SCRIPT = Path(render.__file__).with_name(render.MACHINE) / render.DUMP_SCRIPT

#: What the image's entrypoint prints once initialization is over, and what
#: the serving Postgres prints once it is up. The second alone is not enough:
#: the entrypoint runs the init scripts against a temporary server, which
#: announces readiness too.
INIT_DONE = 'PostgreSQL init process complete; ready for start up.'
READY = 'database system is ready to accept connections'
#: What the pooler prints once it listens.
POOLER_UP = 'process up: PgBouncer'

#: Where the dumps of a box these cases start would go; none is taken.
RECIPIENT = 'age1exampleexampleexampleexampleexampleexampleexampleexamplezzzz'

#: The stack a first client role writes and the other must then read.
STACK = 'roles'

#: The status coreutils `timeout` exits with when its command ran out of time.
TIMED_OUT = 124

#: How often `_until` reads the state it waits on. Nothing is decided by it:
#: the condition decides, and `COMMAND_TIMEOUT` is the stop-loss.
POLL_INTERVAL = 0.1

#: How many zero bytes the tripwire on `_archive`'s workaround puts after its
#: probe's end-of-archive blocks: sixteen times what the pipe between podman
#: and its copier holds, 64 KiB, the kernel's default, which Go's `os.Pipe`
#: keeps. So a copier that stops at the blocks fails that copy whatever the
#: timing. The padding `tarfile` writes, under one 10 KiB record, fits a pipe
#: of that size, and fails the copy only when the copier wins a race or the
#: pipe is smaller than it, as the 8 KiB pipes of containers/buildah#6573 were.
PAST_THE_END = 1 << 20


pytestmark = [
    pytest.mark.skipif(shutil.which('butane') is None, reason='butane is not on PATH (mise x -- ...)'),
    pytest.mark.skipif(shutil.which('pulumi') is None, reason='pulumi is not on PATH (mise x -- ...)'),
    pytest.mark.timeout(CASE_TIMEOUT),
]


@pytest.fixture(scope='module', autouse=True)
def image_is_local() -> None:
    """Skip the module where the image cannot be run without fetching it.

    Asked here rather than at import, so that collecting the suite never
    waits on `podman`.
    """
    if shutil.which('podman') is None:
        pytest.skip('podman is not on PATH')
    for image in (IMAGE, POOLER_IMAGE):
        found = process_sessions.run(['podman', 'image', 'exists', image], timeout=COMMAND_TIMEOUT)
        if found.returncode != 0:
            pytest.skip(f'there is no local copy of {image}')


def _podman(*args: str, stdin: bytes | None = None) -> bytes:
    done = process_sessions.run(['podman', *args], input=stdin, timeout=COMMAND_TIMEOUT)
    if done.returncode != 0:
        raise AssertionError(f'podman {" ".join(args[:2])} failed: {done.stderr.decode().strip()}')
    return done.stdout


@dataclass(frozen=True)
class File:
    """One file a container is given, owned and permitted as its source says."""

    path: PurePosixPath
    content: bytes
    mode: int = 0o644
    uid: int = 0
    gid: int = 0


def _archive(files: list[File]) -> bytes:
    """A tar stream of `files`, rooted at `/`, with their parent directories, ending at its end-of-archive blocks.

    Ownership travels inside the stream, and `podman cp --archive=false`
    keeps it: that is how a server key reaches the uid the image runs
    Postgres as, which refuses a key it does not own.

    **The stream stops at the end-of-archive blocks, without the padding
    `tarfile` adds up to a whole record: a workaround for
    containers/buildah#6573.** podman's copier stops reading at those blocks
    and exits, and podman's write of the padding into its pipe then fails
    the copy with `passing bulk input to subprocess: write |1: broken pipe`
    whenever the copier has exited first. On podman 4.9 a stream of one
    small file, which is mostly padding, fails that way reliably. Its
    boundary is the slice on the return line, which
    `test_an_archive_ends_at_its_end_of_archive_blocks` holds, and the
    tripwire `test_remove_the_archive_workaround_once_the_runners_copier_drains_its_stream`
    with its `PAST_THE_END`. All of it goes once the podman on CI's runner
    carries a copier that drains the stream, as buildah's does from 1.44
    (containers/buildah#6678): the tripwire fails there then.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        made: set[PurePosixPath] = set()
        for file in files:
            for parent in reversed(file.path.parents[:-1]):
                if parent not in made:
                    made.add(parent)
                    entry = tarfile.TarInfo(str(parent.relative_to('/')))
                    entry.type = tarfile.DIRTYPE
                    entry.mode = 0o755
                    tar.addfile(entry)
            entry = tarfile.TarInfo(str(file.path.relative_to('/')))
            entry.size = len(file.content)
            entry.mode = file.mode
            entry.uid = file.uid
            entry.gid = file.gid
            tar.addfile(entry, io.BytesIO(file.content))
    # Closed, `tar.offset` is where the end-of-archive blocks end.
    return buffer.getvalue()[: tar.offset]


def _delivered(ignition: dict[str, Any]) -> list[File]:
    """Every file the Ignition writes, at the path it writes it to."""
    files: list[File] = []
    for entry in ignition['storage']['files']:
        head, _, body = str(entry['contents']['source']).partition(',')
        raw = base64.b64decode(body) if head.endswith(';base64') else urllib_parse.unquote_to_bytes(body)
        if entry['contents'].get('compression') == 'gzip':
            raw = gzip.decompress(raw)
        user = int(entry['user']['id']) if 'user' in entry else 0
        group = int(entry['group']['id']) if 'group' in entry else user
        files.append(File(PurePosixPath(entry['path']), raw, int(entry.get('mode', 0o644)), user, group))
    return files


def _unit_lines(ignition: dict[str, Any], name: str) -> list[str]:
    """One unit's lines, its continuations joined."""
    unit = next(entry for entry in ignition['systemd']['units'] if entry['name'] == name)
    return str(unit['contents']).replace('\\\n', ' ').splitlines()


def _unit_command(ignition: dict[str, Any], name: str = UNIT) -> list[str]:
    """A unit's `ExecStart`, as an argument vector."""
    lines = _unit_lines(ignition, name)
    return shlex.split(next(line for line in lines if line.startswith('ExecStart=')).removeprefix('ExecStart='))


def _unit_env(ignition: dict[str, Any]) -> dict[str, str]:
    argv = _unit_command(ignition)
    pairs = (argv[at + 1].split('=', 1) for at, arg in enumerate(argv) if arg == '--env')
    return dict(pairs)


def _bounded(verb: str, *args: str, image: str = IMAGE) -> list[str]:
    """`podman <verb>` of a container that ends and goes by itself, and is given no volume.

    `--timeout` has conmon end the container at `CONTAINER_TIMEOUT` and
    `--rm` removes it then. conmon runs under the container service, not
    under the test process, so this holds however the test process ended,
    an outright kill that runs no teardown included; until then the label
    lists it. A container created and never started is not reached: conmon
    counts from the start.

    Each of `image`'s `VOLUME`s is a tmpfs. Otherwise each would be an
    anonymous volume, which holds one of the host's podman locks and
    outlives a container `podman rm` removes without `--volumes`.

    The log is a file of the container's own (`k8s-file`), because the
    readiness waits read it (`_await_log`) and a host whose podman logs to
    journald drops lines from a burst of containers starting at once: its
    rate limit suppresses them, and a wait for a line that was dropped
    ends only at its bound.
    """
    tmpfs = [f'--tmpfs={volume}' for volume in IMAGE_VOLUMES[image]]
    return [
        verb,
        '--rm',
        '--timeout',
        str(CONTAINER_TIMEOUT),
        '--label',
        LABEL,
        '--log-driver',
        'k8s-file',
        *tmpfs,
        *args,
    ]


def _unit_image(ignition: dict[str, Any], name: str) -> str:
    """The image a unit runs: its `ExecStart`'s first argument that is no option or option value."""
    argv = _unit_command(ignition, name)
    (image,) = [arg for arg in argv if arg in IMAGE_VOLUMES]
    return image


def _volumes(ignition: dict[str, Any], name: str) -> list[tuple[str, str, str]]:
    """A unit's `--volume` arguments, as host path, path inside, and options."""
    argv = _unit_command(ignition, name)
    found: list[tuple[str, str, str]] = []
    for at, arg in enumerate(argv):
        if arg == '--volume':
            host, inside, *options = argv[at + 1].split(':')
            found.append((host, inside, ':'.join(options)))
    return found


def _shared(ignition: dict[str, Any]) -> list[str]:
    """The host directories both units mount: what the two containers share."""
    postgres = {host for host, _, _ in _volumes(ignition, UNIT)}
    return sorted(host for host, _, _ in _volumes(ignition, POOLER_UNIT) if host in postgres)


def _anonymous(inside: str, options: list[str]) -> list[str]:
    """The `--mount` of an anonymous volume at `inside`, with a unit's `--volume` options in `--mount`'s words.

    `--volume` names an anonymous volume only by its path alone, which
    leaves no place for options, so the options are translated: an option
    with no translation here fails, naming itself, rather than being dropped.
    """
    words = {'z': 'relabel=shared', 'Z': 'relabel=private', 'ro': 'ro', 'rw': 'rw'}
    given = [option for joined in options for option in joined.split(',') if option]
    unknown = [option for option in given if option not in words]
    assert not unknown, unknown
    return ['--mount', ','.join(['type=volume', f'destination={inside}', *(words[option] for option in given)])]


def _unit_container(
    ignition: dict[str, Any], name: str = UNIT, source: str | None = None
) -> tuple[list[str], list[File]]:
    """A unit's `podman run` as a `podman create`, and what to copy in.

    Every argument is the unit's except these: `--replace` and the name,
    which the caller gives; the published port, which the box does without,
    since it is reached through the network the caller joins it to
    (`_network`); each volume a delivered file sits under, which becomes a
    copy of those files at the path the volume mounts them on; each directory
    the two units share (`_shared`); and the one volume nothing is delivered
    into, which is the box's own state and starts here empty, as the tmpfs
    `_bounded` mounts on the path the image's own `VOLUME` names.
    `_bounded`'s arguments are added.

    **A shared directory is an anonymous volume of the Postgres container's,
    and the pooler mounts it from that container.** With no `source`, each
    one becomes an anonymous volume at the path the unit mounts it on; given
    `source`, the container the unit's shared directories are mounted from,
    they become `--volumes-from` that container. podman removes an anonymous
    volume with the last container that mounts it, `--rm` at a container's
    `--timeout` included, so a run killed before its teardown leaves no
    volume once its containers have ended, where a named volume would stay
    for good. `--volumes-from` rather than the volume's name, because a
    mount by name of a volume that is gone creates a named volume of that
    name, and a pooler created after its Postgres container ended would make
    one; `--volumes-from` a container that is gone fails the `create`
    instead, and records no dependency that would hold either container's
    `--rm` (`test_a_box_left_without_its_teardown_goes_with_its_volume`).
    `--volumes-from` mounts each volume at the path the source mounts it on,
    so the two units name the same path.
    """
    argv = _unit_command(ignition, name)
    assert argv[:2] == ['/usr/bin/podman', 'run'], argv
    delivered = _delivered(ignition)
    shared = _shared(ignition)
    owned = {host: inside for host, inside, _ in _volumes(ignition, UNIT)}
    created = _bounded('create', image=_unit_image(ignition, name))
    copies: list[File] = []
    joined = False
    rest = iter(argv[2:])
    for arg in rest:
        match arg:
            case '--replace':
                pass
            case '--name':
                _ = next(rest)
            case '--publish':
                _ = next(rest)
            case '--volume':
                host, inside, *options = next(rest).split(':')
                if host in shared:
                    if source is None:
                        created += _anonymous(inside, options)
                        continue
                    assert inside == owned[host], (host, inside, owned[host])
                    if not joined:
                        created += ['--volumes-from', ':'.join([source, *options])]
                        joined = True
                    continue
                under = [file for file in delivered if file.path.is_relative_to(host)]
                if not under:
                    volumes = IMAGE_VOLUMES[_unit_image(ignition, name)]
                    assert PurePosixPath(inside) in volumes, (inside, volumes)
                copies += [
                    File(
                        PurePosixPath(inside) / file.path.relative_to(host), file.content, file.mode, file.uid, file.gid
                    )
                    for file in under
                ]
            case _:
                created.append(arg)
    return created, copies


@dataclass(frozen=True)
class Box:
    """One running Postgres container, the name its superuser goes by, and the pooler in front of it.

    `port` is the host port clients reach the box on: the pooler's, on a box
    the appliance's Ignition builds, and Postgres's own on one without a
    pooler.
    """

    name: str
    port: int
    superuser: str
    pooler: str | None = None

    def local(self, sql: str) -> str:
        """SQL as the superuser on the container's local socket: the dump timer's way in."""
        done = process_sessions.run(
            ['podman', 'exec', self.name, 'psql', '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-At']  # noqa: RUF005 -- keeps -U, -d and -c beside their values
            + ['-U', self.superuser, '-d', settings.DATABASE, '-c', sql],
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        if done.returncode != 0:
            raise state.StateError(done.stderr.strip())
        return done.stdout.strip()

    def pools(self) -> list[dict[str, str]]:
        """The pooler's `SHOW POOLS`, a row per pool: its statistics console, on its own socket."""
        assert self.pooler is not None, f'{self.name} has no pooler'
        done = process_sessions.run(
            ['podman', 'exec', self.pooler, 'psql', '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '--csv']  # noqa: RUF005 -- keeps -h, -U and -c beside their values
            + ['-h', '/tmp', '-p', str(settings.PORT), '-U', 'pgbouncer', '-c', 'SHOW POOLS', 'pgbouncer'],
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        if done.returncode != 0:
            raise AssertionError(done.stderr.strip())
        header, *rows = done.stdout.strip().splitlines()
        return [dict(zip(header.split(','), row.split(','), strict=True)) for row in rows]


def _await_log(name: str, marks: tuple[str, ...], *, timeout: float = COMMAND_TIMEOUT) -> None:
    """Follow the container's log until each of `marks` has appeared, in order.

    The log is the event: the entrypoint prints each stage as it reaches it,
    and a container that dies ends the stream, which ends the wait with the
    log in hand. A container that stays up and goes silent is ended by the
    follower's own bound, coreutils `timeout`, whose exit status 124 is
    raised as a `TimeoutExpired` naming the follower. Leaving the block
    kills the follower whichever way it is left.
    """
    seen: list[str] = []
    follower = ['timeout', str(timeout), 'podman', 'logs', '--follow', name]
    with process_sessions.started(
        follower, stdout=process_sessions.PIPE, stderr=process_sessions.STDOUT, text=True
    ) as follow:
        assert follow.stdout is not None
        waiting = list(marks)
        for text in follow.stdout:
            seen.append(text)
            if waiting[0] in text:
                del waiting[0]
                if not waiting:
                    return
        if follow.wait(timeout=COMMAND_TIMEOUT) == TIMED_OUT:
            raise sp.TimeoutExpired(follower, timeout, output=''.join(seen))
        raise AssertionError(f'{name} stopped before it printed {waiting[0]!r}:\n{"".join(seen)}')


def _await_ready(name: str, *, timeout: float = COMMAND_TIMEOUT) -> None:
    """Follow a Postgres container's log until the serving Postgres says it is up.

    Readiness alone is not enough: the entrypoint runs the init scripts
    against a temporary server, which announces readiness too.
    """
    _await_log(name, (INIT_DONE, READY), timeout=timeout)


def _remove(name: str) -> None:
    """Remove a container these cases started, and any anonymous volume it mounted that no other container does.

    The volume a box's two containers share (`_unit_container`) goes with
    whichever of them is removed last, and the `--volumes` every removal
    carries is the guard for one the image's `VOLUME`s would add if
    `IMAGE_VOLUMES` fell behind them (`process_sessions.remove`).
    """
    process_sessions.remove(name, timeout=COMMAND_TIMEOUT)


def _removing(*names: str) -> AbstractContextManager[None]:
    """Arm the removal of the containers `names` for a block that creates them, under this module's command bound."""
    return process_sessions.removing(*names, timeout=COMMAND_TIMEOUT)


def _named(created: list[str], name: str) -> list[str]:
    """A `podman create` built without a name, given `name`."""
    return [*created[:1], '--name', name, *created[1:]]


def _joined(created: list[str], netns: str) -> list[str]:
    """A `podman create` built without a network, given the network namespace at `netns`."""
    return [*created[:1], '--network', f'ns:{netns}', *created[1:]]


def _published(port: int) -> list[str]:
    """The arguments that publish the box port on `port` of `ADDRESS`, through pasta.

    **The module needs pasta, so it names it** rather than take the network
    a container gets without `--network`, which is the host's
    `containers.conf` to decide. `process_sessions` ends the session of
    each `podman` command with the command, and a port forwarder left in
    that session goes with it, after which the published port refuses
    connections: slirp4netns leaves its forwarder there, and pasta leaves
    the session. Where the host's default is pasta the argument changes
    nothing, and a podman without pasta fails the command naming it rather
    than a case failing later on a refused connection.
    """
    return ['--network', 'pasta', '--publish', f'{ADDRESS}:{port}:{settings.PORT}']


def _network_container(name: str, port: int) -> list[str]:
    """The `podman run` of the container whose network a box joins, with the box port published on `port`."""
    return _bounded('run', '--detach', *_published(port), '--name', name, '--entrypoint', 'sleep', IMAGE, 'infinity')


def _network(name: str) -> tuple[str, int]:
    """Start a box's network as the container `name`: the namespace's path, and the host port it is published on.

    **The port is one this case reserved** (`host_ports`), held until the
    `podman run` that publishes it has returned, and never one podman picks:
    podman picks a free port and pasta binds it later, and anything that
    binds port 0 in between can be handed the same port (podman#19048,
    framework/testing.md §1.2).

    **pasta binds a published port only when podman sets up the network,
    at `init` or `start`**, not at `create` (`libpod/networking_pasta_linux.go`).
    A box is created, then filled with `podman cp`, then started, and a port
    its `create` named would be free for anything else on the machine to
    take until the start, and then pasta could not bind it and the start
    would fail. So the port is published by a container of its own, which
    `podman run` creates and starts in one command, and a box joins that
    container's network namespace by its path. The port is bound before this
    returns it, so no box is created on a port nothing holds.

    The path rather than `--network container:<name>`, because the second
    makes the box a dependent of this container, and podman removes no
    container another depends on, whether `--rm` or `podman rm` asks: this
    one, which starts first and so ends first, would outlive its own `--rm`,
    and a teardown that removes it first would be refused.
    `test_a_network_is_removed_while_a_box_joined_to_it_exists` holds it.
    """
    with host_ports.reserved(ADDRESS) as port:
        _ = _podman(*_network_container(name, port))
    netns = _podman('container', 'inspect', '--format', '{{.NetworkSettings.SandboxKey}}', name).decode().strip()
    return netns, port


def _network_name(box: str) -> str:
    """The name of the container whose network the box `box` joins (`_network`)."""
    return f'{box}-network'


def _client(name: str) -> list[str]:
    """The `podman run` of the container that serves the image's client tools."""
    return _bounded('run', '--detach', '--network', 'host', '--name', name, '--entrypoint', 'sleep', IMAGE, 'infinity')


def _superusers() -> list[str]:
    """The `podman create` of a box whose image superuser is `ci`, which is a client role's name."""
    return _bounded(
        'create',
        *('--env', f'POSTGRES_DB={settings.DATABASE}'),
        *('--env', f'POSTGRES_USER={settings.CI_ROLE}'),
        *('--env', 'POSTGRES_HOST_AUTH_METHOD=trust'),
        IMAGE,
    )


def _directory(ignition: dict[str, Any], path: str) -> tuple[int, int, int]:
    """The mode, owner and group the Ignition creates a directory with."""
    (entry,) = [entry for entry in ignition['storage']['directories'] if entry['path'] == path]
    user = int(entry['user']['id']) if 'user' in entry else 0
    group = int(entry['group']['id']) if 'group' in entry else user
    return int(entry.get('mode', 0o755)), user, group


def _fill(name: str, created: list[str], copies: list[File]) -> None:
    """Create the container `name` and copy its delivered files in."""
    _ = _podman(*_named(created, name))
    if copies:
        _ = _podman('cp', '--archive=false', '-', f'{name}:/', stdin=_archive(copies))


def _own(ignition: dict[str, Any], box: str) -> None:
    """Give each directory `box` shares with its pooler the mode and owners the Ignition gives it.

    The directory is an anonymous volume (`_unit_container`), which takes no
    owners as options, so a container that mounts it from `box` sets them
    from inside, before `box` starts. **That container also leaves a file
    there, `OWNED`, because podman hands an empty volume to the user of every
    container that mounts it** (`fixVolumePermissionsUnlocked` in
    `libpod/container_internal_common.go`): at each mount it changes the
    owner of a volume that is empty to the container's user, and stops only
    once it meets the volume with something in it. Postgres's container runs
    as root, and its server puts its socket there only after the start, so
    without the file the start would give the directory back to root, and
    the server, which is not root, could not write its socket into it.
    """
    owned = {host: inside for host, inside, _ in _volumes(ignition, UNIT)}
    for index, host in enumerate(_shared(ignition)):
        mode, user, group = _directory(ignition, host)
        inside = owned[host]
        name = f'{box}-own-{index}'
        with _removing(name):
            _ = _podman(
                *_named(_bounded('run', '--volumes-from', box, '--entrypoint', 'sh', IMAGE), name),
                '-c',
                'install -d -o "$1" -g "$2" -m "$3" "$4" && touch "$4/$5"',
                'sh',
                str(user),
                str(group),
                f'{mode:o}',
                inside,
                OWNED,
            )


class Launcher:
    """Create, fill and start boxes, each in a network of its own, and remove every one when the module is done.

    Called, it starts one container as a box of its own. `appliance` starts
    the two the appliance's Ignition defines: Postgres, which has no
    network, and the pooler, which joins the box's network and is published
    there; the directory the two units share is an anonymous volume of the
    Postgres container's, which the pooler mounts from it, with the mode and
    owners the Ignition gives the directory (`_own`), and which goes with the
    last of the two containers.
    """

    def __init__(self) -> None:
        self.containers: list[str] = []
        self._teardowns = ExitStack()

    def arm(self, *names: str) -> None:
        """Arm the removal of the containers `names` at `close`, before anything creates them (`_removing`)."""
        self.containers.extend(names)
        self._teardowns.enter_context(_removing(*names))

    def __call__(self, created: list[str], copies: list[File], superuser: str) -> Box:
        name = f'kluster-test-pgstate-{uuid.uuid4().hex[:12]}'
        network = _network_name(name)
        self.arm(network, name)
        netns, port = _network(network)
        _fill(name, _joined(created, netns), copies)
        _ = _podman('start', name)
        _await_ready(name)
        return Box(name=name, port=port, superuser=superuser)

    def appliance(self, ignition: dict[str, Any], *, held: File | None = None) -> Box:
        """The appliance's Postgres and its pooler, as the Ignition builds them.

        The pooler starts once Postgres serves, which is what its unit
        waits for on the box. `held` is one more file for the Postgres
        container, an init script the caller holds the initialization with;
        a box started with it is returned once Postgres has started, before
        it serves, and with no pooler started.
        """
        name = f'kluster-test-pgstate-{uuid.uuid4().hex[:12]}'
        network, pooler = _network_name(name), f'{name}-pooler'
        self.arm(network, name, pooler)
        netns, port = _network(network)
        created, copies = _unit_container(ignition, UNIT)
        _fill(name, created, [*copies, *([held] if held else [])])
        _own(ignition, name)
        _ = _podman('start', name)
        superuser = _unit_env(ignition)['POSTGRES_USER']
        if held is not None:
            return Box(name=name, port=port, superuser=superuser, pooler=pooler)
        _await_ready(name)
        box = Box(name=name, port=port, superuser=superuser, pooler=pooler)
        self.pooler(ignition, box, netns)
        return box

    def pooler(self, ignition: dict[str, Any], box: Box, netns: str) -> None:
        """Start `box`'s pooler in the network at `netns`, with the directories it shares mounted from `box`."""
        assert box.pooler is not None
        created, copies = _unit_container(ignition, POOLER_UNIT, box.name)
        _fill(box.pooler, _joined(created, netns), copies)
        _ = _podman('start', box.pooler)
        _await_log(box.pooler, (POOLER_UP,))

    def close(self) -> None:
        self._teardowns.close()


Start = Launcher


@pytest.fixture(scope='module')
def start() -> Iterator[Start]:
    launcher = Launcher()
    yield launcher
    launcher.close()


@pytest.fixture(scope='module')
def roots() -> config.Roots:
    return config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT,))


@pytest.fixture(scope='module')
def ignition(roots: config.Roots) -> dict[str, Any]:
    built = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')
    return json.loads(render.render_ignition(built))


@pytest.fixture(scope='module')
def capped(roots: config.Roots) -> dict[str, Any]:
    """The appliance's Ignition with the transaction entry's cap at one server process.

    Every client of that entry is then served by the same server process,
    one transaction at a time, so what one client leaves on it is what the
    next one meets, every time rather than when the pooler happens to lend
    it the same process.
    """
    built = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')
    return json.loads(render.render_ignition(dataclasses.replace(built, pool_cap=1)))


def _appliance(start: Start, ignition: dict[str, Any]) -> Box:
    """The appliance's Postgres and its pooler, as its Ignition builds them."""
    return start.appliance(ignition)


@pytest.fixture(scope='module')
def box(start: Start, ignition: dict[str, Any]) -> Box:
    """One appliance for every case that does not restore into it.

    Shared because a start is most of what a case here costs. The one case
    that writes to it checks first that nothing has, so a second writer
    added later fails that case by name rather than weakening it.
    """
    return _appliance(start, ignition)


@pytest.fixture(scope='module')
def tools(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The Postgres client tools, as commands, and the directory they see.

    The tools are the image's, run in a container of their own on the host's
    network. Before each call the tool is handed a fresh copy of this
    directory at the same path, so every path a caller names means the same
    inside as out; and the file `pg_dump` writes, the one its `--file=`
    names, is copied back out once it has written it.
    """
    directory = tmp_path_factory.mktemp('clients')
    name = f'kluster-test-pgclient-{uuid.uuid4().hex[:12]}'
    with _removing(name):
        _ = _podman(*_client(name))
        _ = _podman('exec', name, 'mkdir', '-p', str(directory))
        for tool in (table.PSQL, state.PG_RESTORE, state.PG_DUMP):
            script = directory / tool
            run = (
                f'podman exec --interactive {" ".join(f"--env {variable}" for variable in TOOL_ENV)} {name} {tool} "$@"'
            )
            _ = script.write_text(
                '#!/bin/sh\n'
                f'podman cp {shlex.quote(f"{directory}/.")} {shlex.quote(f"{name}:{directory}")} || exit 1\n'
                + (
                    f'{run} || exit $?\n'
                    'for arg; do case $arg in --file=*) '
                    f'podman cp "{name}:${{arg#--file=}}" "${{arg#--file=}}" || exit 1;; esac; done\n'
                    if tool == state.PG_DUMP
                    else f'exec {run}\n'
                )
            )
            script.chmod(0o755)
        yield directory


@pytest.fixture
def clients(tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where a case keeps its bundles and archives, with `state` and `pulumi` pointed there.

    `state` runs the image's `pg_dump`, `pg_restore` and `psql`. `pulumi` gets a home of its own,
    and none of the operator's `PGSSL*` variables: a case names its bundle's
    files or connects without TLS, never through what the operator's shell
    happened to hold. Each command either one starts is bounded by
    `COMMAND_TIMEOUT` rather than by its runner's own bound, which is set for
    a transfer over the network and, for `state`'s, sits above the run's.
    """
    monkeypatch.setattr(state, 'PG_RESTORE', str(tools / state.PG_RESTORE))
    monkeypatch.setattr(state, 'PG_DUMP', str(tools / state.PG_DUMP))
    monkeypatch.setattr(table, 'PSQL', str(tools / table.PSQL))
    monkeypatch.setattr(pulumi_cli, 'TIMEOUT', COMMAND_TIMEOUT)
    monkeypatch.setattr(table, 'LISTING_TIMEOUT', COMMAND_TIMEOUT)
    monkeypatch.setattr(state, 'TRANSFER_TIMEOUT', COMMAND_TIMEOUT)
    for variable, directory in cli_directories(tmp_path).items():
        monkeypatch.setenv(variable, directory)
    monkeypatch.setenv('PULUMI_SKIP_UPDATE_CHECK', 'true')
    for variable in (CA_ENV, CERT_ENV, KEY_ENV):
        monkeypatch.delenv(variable, raising=False)
    return tools


def _bundle(roots: config.Roots, box: Box, clients: Path, role: str) -> state.Connection:
    """A client bundle for `role`, written the way `state-backend bundle` writes one."""
    bundle = config.client_bundle(roots.ca, name=role, address=ADDRESS)
    directory = clients / role
    config.write_client_bundle(bundle, directory)
    url = bundle.url().replace(f'@{ADDRESS}:{settings.PORT}/', f'@{ADDRESS}:{box.port}/')
    return state.Connection(url=url, env=ssl_env(directory))


def _psql(clients: Path, target: state.Connection, sql: str) -> str:
    done = process_sessions.run(
        [str(clients / 'psql'), '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-At', f'--dbname={target.url}', '-c', sql],
        env={**os.environ, **target.env},
        text=True,
        timeout=COMMAND_TIMEOUT,
    )
    if done.returncode != 0:
        raise state.StateError(done.stderr.strip())
    return done.stdout.strip()


def _stack_init(target: state.Connection, project: Path, stack: str, env: dict[str, str] | None = None) -> None:
    """Write a stack into the backend: what `pulumi` does with it, not a row. `env` is overlaid on the run's."""
    project.mkdir(parents=True, exist_ok=True)
    _ = (project / 'Pulumi.yaml').write_text(f'name: {project.name}\nruntime: yaml\n')
    _pulumi(target, project, ['stack', 'init', stack], env)


def _up(target: state.Connection, project: Path, stack: str, env: dict[str, str] | None = None) -> None:
    """An update of a stack `_stack_init` wrote, whose program declares nothing: a writing operation, whole."""
    _pulumi(target, project, ['up', '--yes', '--skip-preview', '--stack', stack], env)


def _pulumi(target: state.Connection, project: Path, args: list[str], env: dict[str, str] | None) -> str:
    return pulumi_config.run_pulumi(
        args,
        cwd=project,
        env={
            pulumi_config.BACKEND_URL_ENV: target.form(bundle.Purpose.PULUMI),
            pulumi_config.PASSPHRASE_ENV: fake(pulumi_config.PASSPHRASE_ENV),
            **target.env,
            **(env or {}),
        },
        stdin=None,
    )


def _volume_mounts(name: str) -> dict[str, str]:
    """The volumes the container `name` mounts, by name, and the path each is mounted on."""
    made = json.loads(_podman('container', 'inspect', name))[0]
    return {mount['Name']: mount['Destination'] for mount in made['Mounts'] if mount['Type'] == 'volume'}


def _exists(kind: str, name: str) -> bool:
    """Whether podman knows the container or volume `name`, `kind` saying which."""
    return process_sessions.run(['podman', kind, 'exists', name], timeout=COMMAND_TIMEOUT).returncode == 0


@pytest.mark.parametrize('container', ['appliance', 'pooler', 'superusers', 'network', 'client'])
def test_every_container_ends_by_itself_and_mounts_no_named_volume(ignition: dict[str, Any], container: str) -> None:
    """Each container these cases start is bounded, removed when it ends, labelled, and mounts no named volume.

    Read from the container podman made of the arguments rather than from
    the arguments: what podman does with them is what leaves a lock or a
    volume behind. The volumes a box's two containers mount are the
    directories the two units share, each an anonymous volume of the
    Postgres container's, which the pooler's mounts from it; no other
    container mounts a volume at all.
    """
    name = f'kluster-test-volumes-{uuid.uuid4().hex[:12]}'
    source = f'{name}-source'
    run: Callable[[], object] = {
        'appliance': lambda: _podman(*_named(_unit_container(ignition, UNIT)[0], name)),
        'pooler': lambda: _podman(*_named(_unit_container(ignition, POOLER_UNIT, source)[0], name)),
        'superusers': lambda: _podman(*_named(_superusers(), name)),
        'network': lambda: _network(name),
        'client': lambda: _podman(*_client(name)),
    }[container]
    image = {'pooler': POOLER_IMAGE}.get(container, IMAGE)
    with _removing(name, source):
        if container == 'pooler':
            _ = _podman(*_named(_unit_container(ignition, UNIT)[0], source))
        _ = run()
        made = json.loads(_podman('container', 'inspect', name))[0]
        mounted = _volume_mounts(name)
        sourced = _volume_mounts(source) if container == 'pooler' else mounted
        anonymous = {
            volume: json.loads(_podman('volume', 'inspect', volume))[0].get('Anonymous', False) for volume in mounted
        }
    declared: dict[str, Any] = json.loads(_podman('image', 'inspect', image))[0]['Config'].get('Volumes') or {}
    shares = sorted(inside for host, inside, _ in _volumes(ignition, UNIT) if host in _shared(ignition))

    # The premise: the Postgres image declares volumes, so a container given
    # none is one whose tmpfs covers each, and `IMAGE_VOLUMES` names them all;
    # and the two units share a directory.
    assert IMAGE_VOLUMES[IMAGE]
    assert sorted(declared) == sorted(str(volume) for volume in IMAGE_VOLUMES[image])
    assert shares

    assert sorted(mounted.values()) == (shares if container in {'appliance', 'pooler'} else [])
    assert mounted == sourced
    assert all(anonymous.values()), anonymous
    assert made['Config']['Timeout'] == CONTAINER_TIMEOUT
    assert made['HostConfig']['AutoRemove'] is True
    assert LABEL in {f'{key}={value}' for key, value in made['Config']['Labels'].items()}


def test_a_box_left_without_its_teardown_goes_with_its_volume(ignition: dict[str, Any]) -> None:
    """A box whose run was killed before its teardown leaves no container and no volume once its containers end.

    A kill -- SIGKILL, or the outer `timeout`'s SIGTERM -- runs no teardown,
    so here the launcher that started the box is never closed. Each of the
    box's containers is then ended the way its `--timeout` ends it, a kill
    of its process, which conmon answers by removing the container (`--rm`)
    whoever sent the kill; the timer itself is held by
    `test_every_container_ends_by_itself_and_mounts_no_named_volume`.

    The Postgres container ends first, while its pooler runs: `--volumes-from`
    records no dependency that would refuse its removal, and the volume the
    two share stays for the pooler. The volume goes with the pooler, the
    last container that mounts it.
    """
    left = Launcher()
    try:
        box = left.appliance(ignition)
        assert box.pooler is not None
        volumes = _volume_mounts(box.name)
        pooled = _volume_mounts(box.pooler)

        _ = _podman('kill', '--signal', 'KILL', box.name)
        _ = _until(
            lambda: _exists('container', box.name),
            lambda there: not there,
            'the Postgres container removed while its pooler runs',
        )
        kept = [volume for volume in volumes if _exists('volume', volume)]
        _ = _podman('kill', '--signal', 'KILL', *(name for name in left.containers if name != box.name))
        remaining = _until(
            lambda: [
                *(container for container in left.containers if _exists('container', container)),
                *(volume for volume in volumes if _exists('volume', volume)),
            ],
            lambda remaining: not remaining,
            'every container and volume of the box gone',
        )
    finally:
        # A stop-loss for a case that fails before its containers end; on
        # one that passes, every name is already gone.
        left.close()

    # The premise: the two containers share a volume, which the first of them
    # to go leaves to the other.
    assert volumes
    assert pooled == volumes
    assert kept == list(volumes)
    assert remaining == []


def test_the_port_a_box_is_reached_on_is_bound_before_the_box_is_created() -> None:
    """The host port `_network` returns is already taken when it returns.

    A box is created, filled and started after this, and nothing outside
    can take its port in between: a bind of it from here is refused. A port
    a `podman create` publishes is not taken yet, so the same bind succeeds,
    and the `podman start` after it fails with pasta's `Address already in
    use`.
    """
    name = f'kluster-test-pgnetwork-{uuid.uuid4().hex[:12]}'
    with _removing(name):
        _, port = _network(name)
        with socket.socket() as taker, pytest.raises(OSError) as refused:
            taker.bind((ADDRESS, port))
    assert refused.value.errno == errno.EADDRINUSE


def test_the_port_a_box_is_reached_on_lies_outside_the_ephemeral_range() -> None:
    """No process is handed the box's host port by binding port 0 or by connecting.

    The range is this process's network namespace's, which is where a case
    dials `ADDRESS`. A port podman picks lies inside it.
    """
    name = f'kluster-test-pgnetwork-{uuid.uuid4().hex[:12]}'
    with _removing(name):
        _, port = _network(name)
    low, high = (int(field) for field in Path('/proc/sys/net/ipv4/ip_local_port_range').read_text().split())

    assert not low <= port <= high, (port, low, high)


def test_remove_the_reserved_ports_once_podman_holds_the_port_it_picks() -> None:
    """The tripwire on `host_ports`: red on CI's runner once its podman holds the port it picks from the moment it picks it.

    A container is created with the box port published on a port podman
    picks, and never started. podman#19048's window is that podman records
    the port it picked at `create` while nothing holds it until pasta binds
    it at start. So the probe reads the port the container records and
    binds it from this process with `SO_REUSEADDR`, as pasta binds it.

    -   **A port is recorded, and the bind succeeds: the case passes.** The
        window is open, and the reservation is needed.
    -   **No port is recorded before the start, or the bind is refused**:
        the case fails on CI's runner, naming the layer to remove
        (`host_ports`' docstring lists it), and skips anywhere else, naming
        what it found. A refused bind can also be another process taking
        that one port between the two steps, the race itself, which the
        message names too.
    """
    name = f'kluster-test-pickedport-{uuid.uuid4().hex[:12]}'
    with _removing(name):
        _ = _podman(
            *_bounded('create', '--network', 'pasta', '--publish', f'{ADDRESS}::{settings.PORT}', '--name', name),
            '--entrypoint',
            'sleep',
            IMAGE,
            'infinity',
        )
        bindings: dict[str, list[dict[str, str]]] | None = json.loads(
            _podman('container', 'inspect', '--format', '{{json .HostConfig.PortBindings}}', name)
        )
        recorded = [binding.get('HostPort', '') for binding in (bindings or {}).get(f'{settings.PORT}/tcp') or []]
        picked = int(recorded[0]) if recorded and recorded[0] not in {'', '0'} else None
        # The probe runs while the container exists: a podman that holds the
        # port it picks holds it for as long as the container does.
        if picked is not None:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind((ADDRESS, picked))
                except OSError as refused:
                    if refused.errno != errno.EADDRINUSE:
                        raise
                else:
                    return

    if picked is None:
        found = f'a container only created records no host port for {settings.PORT}/tcp: {bindings!r}'
    else:
        found = (
            f'a bind of port {picked}, which a container only created records, is refused: '
            'podman holds the port it picks, or another process took that one port in between'
        )
    _runners_verdict(found, 'the reserved ports, the workaround layer `host_ports` lists, and this case')


def test_a_network_is_removed_while_a_box_joined_to_it_exists() -> None:
    """A box records no dependency on the container whose network it joins, so that container's `--rm` stands.

    podman removes no container another depends on, whether `--rm` or
    `podman rm` asks. The box is only created, which is where podman records
    a dependency.
    """
    network = f'kluster-test-pgnetwork-{uuid.uuid4().hex[:12]}'
    box = f'{network}-box'
    with _removing(box, network):
        netns, _ = _network(network)
        _ = _podman(*_named(_joined(_superusers(), netns), box))
        _remove(network)


def test_a_container_whose_start_never_comes_goes_with_its_teardown() -> None:
    """A container left `Created`, as a `podman run` killed between its create and its start leaves it, goes with the teardown armed for its name.

    The kill is stood in for by what it leaves: the container is created and
    not started, and the block is left with the `TimeoutExpired` that the
    bound on a command raises.
    """
    name = f'kluster-test-created-{uuid.uuid4().hex[:12]}'
    status: list[str] = []
    with pytest.raises(sp.TimeoutExpired), _removing(name):
        _ = _podman(*_named(_superusers(), name))
        status.append(_podman('container', 'inspect', '--format', '{{.State.Status}}', name).decode().strip())
        raise sp.TimeoutExpired(['podman', 'run', '--name', name], COMMAND_TIMEOUT)

    assert status == ['created']
    assert not _exists('container', name)


def test_an_archive_ends_at_its_end_of_archive_blocks() -> None:
    """`_archive`'s stream is a whole tar, and nothing follows its end-of-archive blocks.

    The workaround's own contract (`_archive`): what podman's copier reads up
    to is the whole stream, so podman has nothing left to write into a pipe
    the copier has stopped reading. The race the workaround avoids is not
    held still here: only a stream with more past its blocks than a pipe
    holds makes the defect show every time, and that is the tripwire's
    probe. This is what the workaround promises, read by `tarfile`'s
    reader, which finds the blocks on its own.
    """
    files = [
        File(PurePosixPath('/etc/kluster/a.conf'), b'one\n', 0o600, 999, 999),
        File(PurePosixPath('/srv/b'), b'two' * 1000),
    ]
    stream = _archive(files)

    with tarfile.open(fileobj=io.BytesIO(stream), mode='r:') as tar:
        read = {
            member.name: (member.mode, member.uid, member.gid, extracted.read())
            for member in tar.getmembers()
            if member.isfile() and (extracted := tar.extractfile(member)) is not None
        }
        end = tar.offset
    assert read == {str(file.path.relative_to('/')): (file.mode, file.uid, file.gid, file.content) for file in files}
    assert stream[end:] == bytes(2 * tarfile.BLOCKSIZE)


def test_remove_the_archive_workaround_once_the_runners_copier_drains_its_stream() -> None:
    """The tripwire on `_archive`'s workaround: red on CI's runner once its podman reads past the end-of-archive blocks.

    The probe is `_archive`'s stream with `PAST_THE_END` zero bytes after
    its blocks, copied into a box that is only created. A copier that stops
    at the blocks (containers/buildah#6573) takes the archive up to them
    whole and fails the copy every time; one that drains the stream takes
    all of it and the copy succeeds. The probe file is read back either
    way. What the probe finds says whether this host's podman needs the
    workaround, and the outcome says which it found:

    -   **It stops at the blocks: the case passes.** The workaround is
        needed here.
    -   **It drains the stream, on CI's runner: the case fails**, naming the
        layer to remove. The runner's run is the verdict, being the podman
        every change runs this module on.
    -   **It drains the stream anywhere else: the case skips**, naming what
        it found. A workstation's run sees only its own podman, so its
        finding cannot clear the workaround for the runner.

    A copy that fails before the archive is in -- the probe file is not
    read back whole -- fails the case, since that failure says nothing
    about the copier. podman's message does not tell the two apart: any
    copy that ends before podman has passed on its input prints
    `io: read/write on closed pipe`, and a remote client sees only that and
    the service's `500`.
    """
    name = f'kluster-test-drain-{uuid.uuid4().hex[:12]}'
    path, content = PurePosixPath('/kluster-drain-probe'), b'probe\n'
    probe = _archive([File(path, content)]) + bytes(PAST_THE_END)
    with _removing(name):
        _ = _podman(*_named(_superusers(), name))
        copied = process_sessions.run(
            ['podman', 'cp', '--archive=false', '-', f'{name}:/'], input=probe, timeout=COMMAND_TIMEOUT
        )
        read = process_sessions.run(['podman', 'cp', f'{name}:{path}', '-'], timeout=COMMAND_TIMEOUT)

    # Whichever way the copy ended, the archive before the blocks went in
    # whole; a copy that failed short of it failed on something else.
    assert read.returncode == 0, (copied.stderr.decode(), read.stderr.decode())
    with tarfile.open(fileobj=io.BytesIO(read.stdout), mode='r:') as tar:
        extracted = tar.extractfile(path.name)
        assert extracted is not None and extracted.read() == content, copied.stderr.decode()
    if copied.returncode != 0:
        return
    buildah = _podman('info', '--format', '{{.Host.BuildahVersion}}').decode().strip()
    _runners_verdict(
        f'podman cp read past the end-of-archive blocks on buildah {buildah}',
        'the `_archive` workaround (containers/buildah#6573) and this case',
    )


def _on_ci_runner() -> bool:
    """Whether this run is a job on CI's runner, whose podman and pasta every change runs this module on."""
    # GitHub sets `GITHUB_ACTIONS` on every job's runner.
    return os.environ.get('GITHUB_ACTIONS') == 'true'


def _runners_verdict(found: str, remove: str) -> NoReturn:
    """A workaround's tripwire found the defect gone: fail on CI's runner, naming what to remove, and skip anywhere else.

    The runner's podman and pasta are the ones every change runs this
    module on, and a workstation's run sees only its own, so its finding
    cannot clear a workaround for the runner.
    """
    if not _on_ci_runner():
        pytest.skip(f"{found}: the workaround is unneeded here, and CI's runner decides the removal of {remove}")
    pytest.fail(f"{found} on CI's runner: remove {remove}")


def test_a_box_silent_before_it_serves_fails_the_wait_naming_the_follower() -> None:
    """A container that stays up and prints nothing ends the readiness wait at the follower's bound.

    The client container is that container: it sleeps and never writes its
    log. The bound is the event here, since nothing else ends the follow.
    """
    name = f'kluster-test-silent-{uuid.uuid4().hex[:12]}'
    with _removing(name):
        _ = _podman(*_client(name))
        with pytest.raises(sp.TimeoutExpired) as stalled:
            _await_ready(name, timeout=1)

    assert stalled.value.cmd == ['timeout', '1', 'podman', 'logs', '--follow', name]


def _names(stacks: list[str]) -> list[str]:
    return sorted(name.rsplit('/', 1)[-1] for name in stacks)


def test_the_client_roles_the_image_initializes_are_not_superusers(box: Box) -> None:
    # The init script ran through the image's own mechanism, and what it made
    # is what the design says: two login roles, neither of them a superuser.
    listed = box.local("SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname IN ('ci', 'operator')")

    assert sorted(listed.splitlines()) == [f'{role}|f|t' for role in sorted(CLIENT_ROLES)]
    assert box.superuser not in CLIENT_ROLES


def test_a_certificate_naming_the_superuser_is_refused(
    box: Box, roots: config.Roots, clients: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CA's key can sign any name, so the box is what refuses this one.

    `pki` issues to the two client roles alone, but that is the issuing
    code's rule, and anyone holding the key is not bound by it; the case
    lifts the rule to mint what such a holder could. The certificate is
    genuine -- same CA, verified the same way as a client role's -- and the
    superuser is a role that exists. What refuses it is that no line of the
    pooler's `hba.conf` admits that role from the network.
    """
    monkeypatch.setattr(pki, 'CLIENT_NAMES', (*pki.CLIENT_NAMES, box.superuser))
    target = _bundle(roots, box, clients, box.superuser)

    with pytest.raises(state.StateError, match=re.escape('no authentication method is found')):
        _ = _psql(clients, target, 'SELECT 1')


def test_a_client_role_cannot_run_a_program_on_the_box(box: Box, roots: config.Roots, clients: Path) -> None:
    # What a superuser's certificate would carry: a shell in the container
    # that holds the server's key.
    target = _bundle(roots, box, clients, settings.CI_ROLE)

    with pytest.raises(state.StateError, match='permission denied to COPY to or from an external program'):
        _ = _psql(clients, target, "COPY (SELECT 1) TO PROGRAM 'true'")


def test_pulumi_writes_as_one_client_role_and_reads_as_the_other(
    box: Box, roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """Both directions, because the backend's first use is what creates its table.

    Whichever role opens the backend first creates the table there, and the
    other then runs the same `CREATE INDEX IF NOT EXISTS` against a table it
    did not create, which Postgres answers only for the table's owner.
    """
    ci = _bundle(roots, box, clients, settings.CI_ROLE)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    # The premise: the table does not exist yet, so `ci` is the role whose
    # first use creates it.
    assert box.local("SELECT to_regclass('pulumi_state') IS NULL") == 't'

    _stack_init(ci, tmp_path / 'written-by-ci', STACK)
    assert _names(state.stacks(operator)) == [STACK]

    _stack_init(operator, tmp_path / 'written-by-operator', f'{STACK}-2')
    assert _names(state.stacks(ci)) == [STACK, f'{STACK}-2']


def test_a_dump_from_a_box_whose_client_roles_are_superusers_restores(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """A box with these roles takes a dump from a box whose client roles are superusers.

    The dump comes from a box whose image superuser is `ci` and whose
    `operator` is a second superuser, which is the shape whose archive names
    those two as owners. It is taken the way the nightly timer takes one,
    `pg_dump -Fc` over the local socket, and it lands through the operator's
    bundle the way `state-backend restore` lands one: into a box just
    replaced, after `pulumi` has asked it what it serves, which is the first
    use that creates the table the restore then drops and recreates.
    """
    box = _appliance(start, ignition)
    before = start(
        _superusers(),
        [
            File(
                PurePosixPath('/docker-entrypoint-initdb.d/00-roles.sql'),
                f'CREATE ROLE {settings.OPERATOR_ROLE} LOGIN SUPERUSER;\n'.encode(),
            )
        ],
        settings.CI_ROLE,
    )
    written = state.Connection(
        url=f'postgres://{settings.CI_ROLE}@{ADDRESS}:{before.port}/{settings.DATABASE}?sslmode=disable', env={}
    )
    _stack_init(written, tmp_path / 'before', STACK)
    archive = clients / 'before.dump'
    _ = archive.write_bytes(_podman('exec', before.name, 'pg_dump', '-Fc', '-U', settings.CI_ROLE, settings.DATABASE))
    listing = process_sessions.run(
        [state.PG_RESTORE, '--list', str(archive)], text=True, timeout=COMMAND_TIMEOUT, check=True
    )
    # The premise: the archive hands the table to a role the restoring one
    # cannot become.
    assert f'TABLE public pulumi_state {settings.CI_ROLE}' in listing.stdout
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    assert state.stacks(operator) == []

    state.pg_restore(operator, archive)

    for role in CLIENT_ROLES:
        assert _names(state.stacks(_bundle(roots, box, clients, role))) == [STACK], role


def _dump_env(ignition: dict[str, Any]) -> dict[str, str]:
    """What the dump unit's `EnvironmentFile=` sets, as the Ignition delivers it."""
    (delivered,) = [file for file in _delivered(ignition) if file.path == DUMP_ENV]
    pairs = (line.split('=', 1) for line in delivered.content.decode().splitlines() if '=' in line)
    return {name.strip(): value.strip() for name, value in pairs}


def _nightly(box: Box, clients: Path, env: dict[str, str], name: str) -> Path:
    """An archive of the box taken the way the dump unit takes one, where the client tools can read it.

    `pg_dump -Fc` over the container's local socket, as the unit's role, with
    its output a pipe rather than a file: the archive then carries no data
    offsets, which is what the script's later reads of it have to cope with.
    """
    archive = clients / f'{name}.dump'
    _ = archive.write_bytes(_podman('exec', box.name, 'pg_dump', '-Fc', '-U', env['PG_ROLE'], env['PG_DATABASE']))
    return archive


def _box_count(box: Box, archive: Path, env: dict[str, str]) -> int:
    """The script's count of an archive's stack checkpoints, over rows read the way the script reads them."""
    printed = _podman(
        'exec',
        '-i',
        box.name,
        'pg_restore',
        '--data-only',
        f'--table={BACKEND_TABLE}',
        '--file=-',
        stdin=archive.read_bytes(),
    )
    counted = process_sessions.run(
        [str(DUMP_SCRIPT), 'count-stacks'],
        input=printed,
        env={'PATH': os.environ['PATH'], 'PG_DATABASE': env['PG_DATABASE']},
        timeout=COMMAND_TIMEOUT,
    )
    assert counted.returncode == 0, counted.stderr.decode()
    return int(counted.stdout)


#: The backend's compression settings, the variable that turns each on, and
#: the suffix its checkpoint then carries. Each has an alternative name
#: (`PULUMI_SELF_MANAGED_STATE_*`), cleared too so the operator's shell
#: decides nothing here.
COMPRESSION: dict[str, tuple[dict[str, str], str]] = {
    'none': ({}, '.json'),
    'gzip': ({'PULUMI_DIY_BACKEND_GZIP': 'true'}, '.json.gz'),
    'zstd': ({'PULUMI_DIY_BACKEND_ZSTD': 'true'}, '.json.zst'),
}
COMPRESSION_ENV = (
    'PULUMI_DIY_BACKEND_GZIP',
    'PULUMI_DIY_BACKEND_ZSTD',
    'PULUMI_SELF_MANAGED_STATE_GZIP',
    'PULUMI_SELF_MANAGED_STATE_ZSTD',
)


@pytest.mark.parametrize(('compression', 'suffix'), list(COMPRESSION.values()), ids=list(COMPRESSION))
def test_both_copies_of_the_checkpoint_grammar_count_what_pulumi_writes(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compression: dict[str, str],
    suffix: str,
) -> None:
    """The rule of state-backend.md §5, against the backend's own layout rather than a fixture of it.

    The backend lays out its keys itself, and a `pulumi` release could move
    them; the grammar in the script and in `state` would then count nothing
    on a box serving stacks, and every nightly would refuse. Here the pinned
    `pulumi` writes them into the pinned image: a box `pulumi stack ls` has
    opened holds the table and its meta row and counts 0 on both sides, and
    after `stack init` -- which writes the checkpoint and the `.bak` beside
    it, compressed as the backend is told to -- 1 on both.
    """
    for variable in COMPRESSION_ENV:
        monkeypatch.delenv(variable, raising=False)
    box = _appliance(start, ignition)
    env = _dump_env(ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    # The premise: nothing has opened this box yet.
    assert box.local(f"SELECT to_regclass('{BACKEND_TABLE}') IS NULL") == 't'

    assert state.stacks(operator) == []
    opened = _nightly(box, clients, env, 'opened')
    assert box.local(f'SELECT count(*) FROM {BACKEND_TABLE}') == '1'
    assert _box_count(box, opened, env) == 0
    with pytest.raises(state.StateError, match='holds no stack checkpoint'):
        _ = state.verify_dump(opened)

    _stack_init(operator, tmp_path / 'project', STACK, compression)
    serving = _nightly(box, clients, env, 'serving')
    assert _box_count(box, serving, env) == 1
    assert state.verify_dump(serving) == [f'{env["PG_DATABASE"]}/{BACKEND_STACKS}project/{STACK}{suffix}']


#: Every variable, under each of its names, that changes which keys a save
#: writes: compression, the `backups/` copy, a retained copy. Cleared before a
#: case that names its own, so the operator's shell -- or the suite's own
#: `mise x` -- decides nothing there.
STATE_SWITCHES_ENV = (
    *COMPRESSION_ENV,
    'PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS',
    'PULUMI_DISABLE_CHECKPOINT_BACKUPS',
    'PULUMI_DIY_BACKEND_RETAIN_CHECKPOINTS',
    'PULUMI_RETAIN_CHECKPOINTS',
)


def _keys(box: Box) -> set[str]:
    """Every key of the box's state table, read on the local socket."""
    return set(box.local(f'SELECT key FROM {BACKEND_TABLE}').splitlines())


def _copies(keys: set[str], stack: str, *, layout: str) -> set[str]:
    """The keys among `keys` that are `stack`'s `history/` and `backups/` copies, records excluded."""
    history = f'{layout}history/project/{stack}/{stack}-'
    backups = f'{layout}backups/project/{stack}/{stack}.'
    return {key for key in keys if (key.startswith(history) and '.checkpoint.' in key) or key.startswith(backups)}


def _history(target: state.Connection, project: Path, stack: str) -> object:
    """`pulumi stack history` of `stack`, as the pinned `pulumi` prints it."""
    return json.loads(_pulumi(target, project, ['stack', 'history', '--json', '--stack', stack], None))


def test_a_switch_to_compression_leaves_plain_copies_and_the_prune_takes_those_alone(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rows `--prune-superseded` exists for, as the pinned `pulumi` leaves them, and the prune of them.

    A stack updated uncompressed and then twice under `mise.toml`'s
    variables keeps, beside its compressed checkpoint and that checkpoint's
    own `.bak`, `<stack>.json.bak` and the
    uncompressed `history/` and `backups/` copies its first update wrote,
    and nothing the backend does replaces or removes any of them. A
    neighbour written uncompressed throughout keeps the same kinds of copy,
    in the format its checkpoint is still stored in. The census without
    `--yes` deletes nothing; with it, exactly the first stack's
    uncompressed copies go, and `pulumi stack history` prints what it
    printed before. Asked by name, the statement deletes no checkpoint, no
    copy in its stack's format, and no update record.
    """
    for variable in STATE_SWITCHES_ENV:
        monkeypatch.delenv(variable, raising=False)
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    neighbour = f'{STACK}-plain'
    for stack in (STACK, neighbour):
        _stack_init(operator, project, stack)
        _up(operator, project, stack)
    plain = _keys(box)
    for _ in range(2):
        _up(operator, project, STACK, stack_environment.state_switches())
    layout = f'{settings.DATABASE}/.pulumi/'
    stacks = f'{layout}stacks/{project.name}/'
    before = _keys(box)
    stale = {f'{stacks}{STACK}.json.bak', *_copies(plain, STACK, layout=layout)}
    kept_copies = _copies(before, neighbour, layout=layout) | (_copies(before, STACK, layout=layout) - stale)
    records = {key for key in before if '.history.json' in key}
    assert {key for key in stale if '/history/' in key}
    assert {key for key in stale if '/backups/' in key}
    assert {key for key in kept_copies if key.endswith('.checkpoint.json.zst')}
    assert {key for key in kept_copies if '/history/' in key and neighbour in key}
    assert {key for key in kept_copies if '/backups/' in key and neighbour in key}
    current = {f'{stacks}{STACK}.json.zst', f'{stacks}{STACK}.json.zst.bak', f'{stacks}{neighbour}.json'}
    current |= {f'{stacks}{neighbour}.json.bak'}
    assert stale | current <= before
    assert f'{stacks}{STACK}.json' not in before
    history = _history(operator, project, STACK)
    assert isinstance(history, list)
    assert len(cast('list[object]', history)) == 3

    listed = io.StringIO()
    assert census.run(operator, prune=True, yes=False, out=listed) == 0
    assert _keys(box) == before
    listing = listed.getvalue().split('superseded copies:', 1)[1]
    assert {line.split()[0] for line in listing.splitlines()[1:] if line.strip()} == stale

    assert table.prune(operator, sorted(current | kept_copies | records), database=settings.DATABASE) == []
    assert _keys(box) == before

    pruned = io.StringIO()
    assert census.run(operator, prune=True, yes=True, out=pruned) == 0
    assert _keys(box) == before - stale
    assert all(f'deleted  {key}' in pruned.getvalue() for key in stale)
    assert _history(operator, project, STACK) == history


def test_a_copy_its_stack_is_written_in_again_is_kept_by_the_delete_listed_before(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delete reads the table again rather than trusting the listing it is handed.

    The listing names the uncompressed copies of a compressed stack; a
    writer without compression then saves the stack, which moves its
    checkpoint back to `<stack>.json`. The delete of that listing, run
    after, takes none of them, because each is in the format its stack is
    stored in again.
    """
    for variable in STATE_SWITCHES_ENV:
        monkeypatch.delenv(variable, raising=False)
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    _stack_init(operator, project, STACK)
    _up(operator, project, STACK)
    _up(operator, project, STACK, stack_environment.state_switches())
    listing = [row.key for row in table.superseded(table.census(operator), settings.DATABASE)]
    assert listing

    _up(operator, project, STACK)
    after = _keys(box)
    assert f'{settings.DATABASE}/{BACKEND_STACKS}{project.name}/{STACK}.json' in after

    assert table.prune(operator, listing, database=settings.DATABASE) == []
    assert _keys(box) == after


#: The lengths the retention cases hand the rule in place of the settings', and
#: how far back a case moves a row it ages: past the first, so an aged row is
#: old enough and an untouched one, written seconds ago, is not.
EXPIRY = dt.timedelta(hours=1)
AGED = 2 * EXPIRY


def _age(box: Box, keys: set[str]) -> None:
    """Move each of `keys` back by `AGED`, as if it had been written that long ago."""
    listed = ', '.join(f"'{key}'" for key in sorted(keys))
    _ = box.local(
        f'UPDATE {BACKEND_TABLE} SET updated_at = now() - make_interval(secs => {AGED.total_seconds()}) '
        f'WHERE key IN ({listed})'
    )


def _expired_listed(printed: str) -> set[str]:
    """The keys a `--prune-expired` census lists, read off what it printed."""
    listing = printed.split('expired rows:', 1)[1]
    return {line.split()[0] for line in listing.splitlines()[1:] if line.startswith('  ')}


def test_the_expired_copies_go_and_the_history_an_operator_reads_does_not_change(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rows `--prune-expired` exists for, as the pinned `pulumi` leaves them, and the prune of them.

    A stack updated once with a `backups/` copy and once without, both
    aged, then once more: the listing is exactly the aged `history/` and
    `backups/` copies -- no checkpoint, no `.bak` beside one, no record
    inside the bound, no `meta.yaml`, nothing young -- and deletes nothing
    without `--yes`. With it they go, `pulumi stack history` prints what it
    printed before, and the stack updates again.
    """
    for variable in STATE_SWITCHES_ENV:
        monkeypatch.delenv(variable, raising=False)
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    _stack_init(operator, project, STACK)
    _up(operator, project, STACK)
    _up(operator, project, STACK, stack_environment.state_switches())
    aged = _keys(box)
    _age(box, aged)
    _up(operator, project, STACK, stack_environment.state_switches())
    before = _keys(box)
    copies = _copies(aged, STACK, layout=f'{settings.DATABASE}/.pulumi/')
    assert {key for key in copies if '/history/' in key}
    assert {key for key in copies if '/backups/' in key}
    assert {key for key in aged if '.history.json' in key}
    history = _history(operator, project, STACK)
    assert isinstance(history, list)
    assert len(cast('list[object]', history)) == 3

    listed = io.StringIO()
    assert (
        census.run(
            operator, prune=False, expire=True, yes=False, out=listed, age=EXPIRY, records=settings.HISTORY_RECORDS
        )
        == 0
    )
    assert _keys(box) == before
    assert _expired_listed(listed.getvalue()) == copies

    pruned = io.StringIO()
    assert (
        census.run(
            operator, prune=False, expire=True, yes=True, out=pruned, age=EXPIRY, records=settings.HISTORY_RECORDS
        )
        == 0
    )
    assert _keys(box) == before - copies
    assert all(f'deleted  {key}' in pruned.getvalue() for key in copies)
    assert _history(operator, project, STACK) == history
    _up(operator, project, STACK, stack_environment.state_switches())
    assert len(cast('list[object]', _history(operator, project, STACK))) == 4


def test_the_update_records_beyond_the_bound_go_and_each_stack_keeps_its_newest(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a bound of two, an aged stack of three updates keeps its newest two, and a stack of one loses none."""
    for variable in STATE_SWITCHES_ENV:
        monkeypatch.delenv(variable, raising=False)
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    other = f'{STACK}-other'
    _stack_init(operator, project, STACK)
    for _ in range(3):
        _up(operator, project, STACK, stack_environment.state_switches())
    _stack_init(operator, project, other)
    _up(operator, project, other, stack_environment.state_switches())
    history = _history(operator, project, STACK)
    assert isinstance(history, list)
    assert len(cast('list[object]', history)) == 3
    others = _history(operator, project, other)
    _age(box, _keys(box))

    assert census.run(operator, prune=False, expire=True, yes=True, out=io.StringIO(), age=EXPIRY, records=2) == 0
    assert _history(operator, project, STACK) == cast('list[object]', history)[:2]
    assert _history(operator, project, other) == others


def test_a_lone_bak_goes_and_a_bak_beside_its_checkpoint_stays(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pulumi stack rm` leaves its stack's checkpoint as a `.bak` with no checkpoint, which the rule expires."""
    for variable in STATE_SWITCHES_ENV:
        monkeypatch.delenv(variable, raising=False)
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    removed = f'{STACK}-removed'
    _stack_init(operator, project, removed)
    _ = _pulumi(operator, project, ['stack', 'rm', '--yes', '--stack', removed], None)
    _stack_init(operator, project, STACK)
    _up(operator, project, STACK)
    stacks = f'{settings.DATABASE}/{BACKEND_STACKS}{project.name}/'
    lone = f'{stacks}{removed}.json.bak'
    beside = f'{stacks}{STACK}.json.bak'
    keys = _keys(box)
    assert {lone, beside, f'{stacks}{STACK}.json'} <= keys
    assert f'{stacks}{removed}.json' not in keys
    _age(box, keys)

    listing = {
        row.key: row.clause
        for row in table.expired(operator, database=settings.DATABASE, age=EXPIRY, records=settings.HISTORY_RECORDS)
    }
    assert listing[lone] is table.Expiry.LONE
    assert beside not in listing

    assert (
        census.run(
            operator,
            prune=False,
            expire=True,
            yes=True,
            out=io.StringIO(),
            age=EXPIRY,
            records=settings.HISTORY_RECORDS,
        )
        == 0
    )
    after = _keys(box)
    assert lone not in after
    assert {beside, f'{stacks}{STACK}.json'} <= after


def test_the_delete_applies_the_rule_as_of_its_own_snapshot(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listed row written again before the delete is kept, named, and fails the run; the rest go.

    And handed every key the table then holds -- a checkpoint, the `.bak`
    beside it, the young copy, records inside the bound, `meta.yaml` -- the
    delete takes nothing.
    """
    for variable in STATE_SWITCHES_ENV:
        monkeypatch.delenv(variable, raising=False)
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    _stack_init(operator, project, STACK)
    _up(operator, project, STACK)
    _up(operator, project, STACK)
    _age(box, _keys(box))
    listing = [
        row.key
        for row in table.expired(operator, database=settings.DATABASE, age=EXPIRY, records=settings.HISTORY_RECORDS)
    ]
    assert len(listing) > 1
    young = listing[0]
    delete = table.prune_expired

    def written_again_first(
        target: table.Target, keys: list[str], *, database: str, age: dt.timedelta, records: int
    ) -> list[str]:
        _ = box.local(f"UPDATE {BACKEND_TABLE} SET updated_at = now() WHERE key = '{young}'")
        return delete(target, keys, database=database, age=age, records=records)

    monkeypatch.setattr(table, 'prune_expired', written_again_first)
    pruned = io.StringIO()
    assert (
        census.run(
            operator, prune=False, expire=True, yes=True, out=pruned, age=EXPIRY, records=settings.HISTORY_RECORDS
        )
        == 1
    )
    assert f'kept  {young}' in pruned.getvalue()
    assert all(f'deleted  {key}' in pruned.getvalue() for key in listing[1:])
    left = _keys(box)
    assert young in left
    assert not set(listing[1:]) & left

    stacks = f'{settings.DATABASE}/{BACKEND_STACKS}{project.name}/'
    assert {f'{stacks}{STACK}.json', f'{stacks}{STACK}.json.bak', f'{settings.DATABASE}/.pulumi/meta.yaml'} <= left
    assert {key for key in left if '.history.json' in key}
    assert (
        delete(operator, sorted(left), database=settings.DATABASE, age=EXPIRY, records=settings.HISTORY_RECORDS) == []
    )
    assert _keys(box) == left


def test_census_takes_one_mode_a_run(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        _ = cli.main(['census', '--prune-superseded', '--prune-expired'])

    assert refused.value.code == 2
    assert 'not allowed with' in capsys.readouterr().err


#: Keys under `<database>/.pulumi/`, whether a case ages each, and the clause
#: that expires it with a bound of two records per directory, or `None`.
EXPIRING: list[tuple[str, bool, table.Expiry | None]] = [
    ('stacks/p/a.json.zst', True, None),
    ('stacks/p/a.json.zst.bak', True, None),
    ('stacks/p/a.json.bak', True, None),
    ('stacks/p/a.pulumi-tags', True, None),
    ('stacks/p/gone.json.zst.bak', True, table.Expiry.LONE),
    ('stacks/p/young.json.bak', False, None),
    ('stacks/solo.json.bak', True, table.Expiry.LONE),
    ('stacks/i.json', True, None),
    ('stacks/i.json.bak', True, None),
    ('history/p/a/a-1790591454000000001.history.json', True, table.Expiry.RECORD),
    ('history/p/a/a-1790591454000000002.history.json.zst', True, None),
    ('history/p/a/a-1790591454000000003.history.json.zst', False, None),
    ('history/p/a/a-1790591454000000001.checkpoint.json', True, table.Expiry.COPY),
    ('history/p/a/a-1790591454000000002.checkpoint.json.zst', True, table.Expiry.COPY),
    ('history/p/a/a-1790591454000000003.checkpoint.json.zst', False, None),
    ('history/p/b/b-1790591454000000005.history.json.gz', True, None),
    ('history/p/c/c-1790591454000000001.history.json', True, table.Expiry.RECORD),
    ('history/p/c/c-1790591454000000002.history.json', True, None),
    ('history/p/c/c-1790591454000000003.history.json', True, None),
    ('history/p/gone/gone-1790591454000000001.checkpoint.json.gz', True, table.Expiry.COPY),
    ('history/i/i-1790591454000000001.history.json', True, None),
    ('history/i/i-1790591454000000001.checkpoint.json', True, table.Expiry.COPY),
    ('backups/p/a/a.1790591454000000001.json', True, table.Expiry.COPY),
    ('backups/k-8s.x/k-8s.x.1790591454000000017.json.zst', True, table.Expiry.COPY),
    ('backups/p/a/a.1790591454000000002.json', False, None),
    ('history/p/j/k-1.checkpoint.json', True, None),
    ('history/p/j/j-1.checkpoint.json.bak', True, None),
    ('history/p/j-1.history.json', True, None),
    ('backups/p/j/j.json', True, None),
    ('locks/organization/p/a/id.json', True, None),
]


def test_the_rule_reads_the_backends_layout_and_the_delete_takes_what_it_lists(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
) -> None:
    """`EXPIRED` against every case above, and the same keys under another database's prefix, which it never lists.

    The listing is the clauses the table names, and the delete, handed
    every key, takes exactly what the listing names.
    """
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    _stack_init(operator, tmp_path / 'project', STACK)
    layout = f'{settings.DATABASE}/.pulumi/'
    keys = [prefix + path for path, _, _ in EXPIRING for prefix in (layout, 'other/.pulumi/')]
    values = ', '.join(f'(\'{key}\', \'{{"data":""}}\')' for key in keys)
    _ = box.local(f'INSERT INTO {BACKEND_TABLE} (key, data) VALUES {values}')
    _age(box, {prefix + path for path, aged, _ in EXPIRING if aged for prefix in (layout, 'other/.pulumi/')})
    expected = {layout + path: clause for path, _, clause in EXPIRING if clause is not None}

    listing = table.expired(operator, database=settings.DATABASE, age=EXPIRY, records=2)
    assert {row.key: row.clause for row in listing} == expected

    assert sorted(table.prune_expired(operator, keys, database=settings.DATABASE, age=EXPIRY, records=2)) == sorted(
        expected
    )


#: The ceiling the ceiling cases hold the table to in place of
#: `settings.LARGEST_ROW_BYTES`, and the size of the value each writes over
#: it: twice the ceiling, and repetitive, so TOAST's compression stores it
#: in a fraction of the ceiling.
CEILING = 100_000
OVER = 2 * CEILING


def _put(box: Box, rows: Mapping[str, int]) -> None:
    """Write each key of `rows` with a value of that many bytes, shaped as the backend stores one, as the superuser.

    The value is `{"data":"<payload>"}` with a payload of one repeated
    four-character group, which compresses to almost nothing: its stored
    size is far under its own.
    """
    for key, size in rows.items():
        payload = size - len('{"data":""}')
        _ = box.local(
            f'INSERT INTO {BACKEND_TABLE} (key, data) '
            f"VALUES ('{key}', ('{{\"data\":\"' || rpad('', {payload}, 'QUJD') || '\"}}')::json) "
            'ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()'
        )


def _over_listed(printed: str) -> dict[str, int]:
    """The rows a `--ceiling` run names over the ceiling, and each one's value size, read off what it printed."""
    listing = printed.split('values over the ceiling:', 1)[1]
    return {
        line.split()[0]: int(line.split()[1].replace(',', ''))
        for line in listing.splitlines()[1:]
        if line.startswith('  ')
    }


def test_a_value_over_the_ceiling_fails_the_run_by_name_whatever_size_it_is_stored_at(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """Every row is held to the ceiling by its value's size, which is what a statement's memory is a multiple of.

    A checkpoint and an update record, each of a value over the ceiling
    stored in a fraction of it, are named with their value sizes and fail
    the run; a row under the ceiling is not named, and the largest value is
    printed. The census alone, the same table, passes.
    """
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    _stack_init(operator, project, STACK)
    layout = f'{settings.DATABASE}/.pulumi/'
    checkpoint = f'{layout}stacks/{project.name}/big.json'
    record = f'{layout}history/{project.name}/big/big-1790591454000000001.history.json'
    under = f'{layout}stacks/{project.name}/small.json'
    sizes = {checkpoint: OVER, record: CEILING + 1}
    _put(box, sizes | {under: CEILING})
    for key, size in sizes.items():
        stored, value = box.local(
            f"SELECT pg_column_size(data) || ' ' || octet_length(data::text) FROM {BACKEND_TABLE} WHERE key = '{key}'"
        ).split()
        assert int(stored) < CEILING < int(value) == size

    assert census.run(operator, prune=False, yes=False, out=io.StringIO(), bound=CEILING) == 0
    printed = io.StringIO()
    assert census.run(operator, prune=False, yes=False, ceiling=True, out=printed, bound=CEILING) == 1
    assert _over_listed(printed.getvalue()) == sizes
    assert f'largest value: {checkpoint}  {OVER:,} bytes' in printed.getvalue()


def test_a_copy_over_the_ceiling_that_the_rule_expires_passes_after_the_prune(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """The ceiling reads the table as the prune left it: an expired copy over it fails a read before, and is gone."""
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    _stack_init(operator, project, STACK)
    copy = f'{settings.DATABASE}/.pulumi/history/{project.name}/{STACK}/{STACK}-1790591454000000001.checkpoint.json'
    _put(box, {copy: OVER})
    _age(box, {copy})

    before = io.StringIO()
    assert census.run(operator, prune=False, yes=False, ceiling=True, out=before, bound=CEILING) == 1
    assert _over_listed(before.getvalue()) == {copy: OVER}

    pruned = io.StringIO()
    assert (
        census.run(operator, prune=False, expire=True, yes=True, ceiling=True, out=pruned, age=EXPIRY, bound=CEILING)
        == 0
    )
    assert f'deleted  {copy}' in pruned.getvalue()
    assert _over_listed(pruned.getvalue()) == {}
    assert copy not in _keys(box)


@pytest.mark.parametrize('over', [True, False], ids=['a row over the ceiling', 'every row within it'])
def test_the_ceiling_runs_after_a_row_the_delete_kept_and_the_run_fails(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    over: bool,
) -> None:
    """A listed row written again before the delete is kept and fails the run, and the ceiling still runs.

    With a row over the ceiling, the ceiling names it; with every row
    within it, the ceiling passes and the kept row alone fails the run.
    """
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    _stack_init(operator, project, STACK)
    history = f'{settings.DATABASE}/.pulumi/history/{project.name}/{STACK}/{STACK}-'
    copies = {f'{history}179059145400000000{n}.checkpoint.json' for n in range(1, 3)}
    big = f'{settings.DATABASE}/{BACKEND_STACKS}{project.name}/big.json'
    _put(box, dict.fromkeys(copies, CEILING // 2) | ({big: OVER} if over else {}))
    _age(box, copies)
    young = min(copies)
    delete = table.prune_expired

    def written_again_first(
        target: table.Target, keys: list[str], *, database: str, age: dt.timedelta, records: int
    ) -> list[str]:
        _ = box.local(f"UPDATE {BACKEND_TABLE} SET updated_at = now() WHERE key = '{young}'")
        return delete(target, keys, database=database, age=age, records=records)

    monkeypatch.setattr(table, 'prune_expired', written_again_first)
    printed = io.StringIO()
    assert (
        census.run(operator, prune=False, expire=True, yes=True, ceiling=True, out=printed, age=EXPIRY, bound=CEILING)
        == 1
    )
    assert f'kept  {young}' in printed.getvalue()
    assert 'largest value: ' in printed.getvalue()
    assert _over_listed(printed.getvalue()) == ({big: OVER} if over else {})


def test_the_program_does_what_the_census_with_its_prune_and_its_ceiling_does(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """`table.py`, run as an interpreter runs it, over a bundle and a settings file, against the same table twice.

    Its exit status, what it prints and the table it leaves are those of
    `census --prune-expired --yes --ceiling` with the same lengths and
    ceiling, run over the table filled the same way again. The client it
    runs is found on `PATH`, where these cases' `psql` is put first. That
    the program needs nothing beyond the standard library is held by its
    imports (`tests/test_state_table.py`), not here: `-I` leaves out the
    user's site directory, and the environment's packages stay reachable.
    """
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    project = tmp_path / 'project'
    _stack_init(operator, project, STACK)
    history = f'{settings.DATABASE}/.pulumi/history/{project.name}/{STACK}/{STACK}-'
    aged = {f'{history}1790591454000000001.checkpoint.json': OVER, f'{history}1790591454000000002.checkpoint.json': 15}
    rows = aged | {f'{settings.DATABASE}/{BACKEND_STACKS}{project.name}/big.json': OVER}

    def fill() -> set[str]:
        _put(box, rows)
        _age(box, set(aged))
        return _keys(box)

    program = clients / f'program-{uuid.uuid4().hex[:12]}'
    shutil.copytree(clients / settings.OPERATOR_ROLE, program)
    _ = (program / bundle.URL_FILE).write_text(f'{operator.url}\n')
    configured = tmp_path / 'settings.json'
    _ = configured.write_text(
        dataclasses.replace(state.table_settings(), expiry_age=EXPIRY, largest_row_bytes=CEILING).dumps()
    )

    filled = fill()
    ran = process_sessions.run(
        [sys.executable, '-I', '-B', table.__file__, str(configured), str(program)],
        env={**os.environ, 'PATH': f'{clients}{os.pathsep}{os.environ["PATH"]}'},
        text=True,
        timeout=COMMAND_TIMEOUT,
    )
    left = _keys(box)

    assert fill() == filled
    printed = io.StringIO()
    status = census.run(
        operator, prune=False, expire=True, yes=True, ceiling=True, out=printed, age=EXPIRY, bound=CEILING
    )
    assert (ran.returncode, ran.stdout, left) == (status, printed.getvalue(), _keys(box))
    assert status == 1
    assert left == filled - set(aged)


def _status(text: str) -> dict[str, int]:
    """The memory fields of a `/proc/<pid>/status`, in kB."""
    fields = (line.split(':', 1) for line in text.splitlines() if ':' in line)
    return {name: int(value.split()[0]) for name, value in fields if value.strip().endswith('kB')}


def test_a_write_costs_its_server_process_no_more_than_the_multiple_the_cap_assumes(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """The private memory of a server process writing a checkpoint, per byte of the value, held to `WRITE_MULTIPLE`.

    The write is the backend's: an upsert with the key and the value as its
    parameters, over a row that already holds a value of the size, as a
    save replaces a checkpoint, and the value is shaped as the backend
    stores a compressed checkpoint, base64 that compression cannot shrink.
    The server process reads its own status before and after. Its peak
    (`VmHWM`) counts the shared and file-backed pages it maps beside its
    private memory, and a process keeps every such page mapped once it
    has touched it, so the peak less what it maps of them after the write
    is a lower bound on the private peak: the case fails only on a write
    that is certainly dearer than the cap assumes.
    """
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    _stack_init(operator, tmp_path / 'project', STACK)
    raw = 22_800_000
    value = '/tmp/kluster-test-value.json'
    _ = _podman(
        'exec',
        box.name,
        'sh',
        '-c',
        f'head -c {raw} /dev/urandom | base64 -w0 | {{ printf \'{{"data":"\'; cat; printf \'"}}\'; }} > {value}',
    )
    size = int(_podman('exec', box.name, 'stat', '-c', '%s', value))
    write = (
        f'INSERT INTO {BACKEND_TABLE} (key, data) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET data = $2 '
        "\\bind 'written' :v \\g\n"
    )
    read = "SELECT pg_read_file('/proc/self/status');\n"

    def session(script: str) -> str:
        done = process_sessions.run(
            ['podman', 'exec', '--interactive', box.name, 'psql', '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-qAt']  # noqa: RUF005 -- keeps -U, -d and -f beside their values
            + ['-U', box.superuser, '-d', settings.DATABASE, '-f', '-'],
            input=f'\\set v `cat {value}`\n{script}',
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    _ = session(write)
    before, after = (_status(part) for part in session(f'{read}\\echo @@@\n{write}{read}').split('@@@'))

    private = after['VmHWM'] - after['RssFile'] - after['RssShmem'] - before['RssAnon']
    assert private * 1024 / size <= settings.WRITE_MULTIPLE, (before, after, size)


def test_a_credentials_run_against_an_operator_stack_in_the_box_reaches_it_with_its_bundles_files(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`github`'s state is in the estate's backend, and a `credentials` run keeps none of its caller's libpq variables.

    So the run is handed the three files of the bundle its URL was read from,
    as the `operator-stack` driver hands them, and reaches the box with them
    while the caller's own name files that do not exist.
    """
    stack = identity.STACK_NAMES.github
    assert stack_environment.home(stack) is identity.StateHome.BACKEND
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    for variable in (CA_ENV, CERT_ENV, KEY_ENV):
        monkeypatch.setenv(variable, str(tmp_path / 'elsewhere' / variable))
    project = tmp_path / 'project'
    project.mkdir()
    _ = (project / 'Pulumi.yaml').write_text(f'name: {project.name}\nruntime: yaml\n')
    environment = pulumi_config.BackendEnvironment(
        operator=lambda: fake(pulumi_config.PASSPHRASE_ENV),
        url=operator.url,
        bundle_dir=clients / settings.OPERATOR_ROLE,
    )
    slot = pulumi_config.Stack(name=stack, directory=project, environment=environment)

    assert not slot.exists()
    slot.ensure()
    assert slot.exists()


def test_an_operator_stack_in_the_box_is_stored_as_the_stacks_ci_deploys_are(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`github`, created by a `credentials` run and updated by the `operator-stack` driver, whatever its caller exports.

    The `credentials` run's `stack init` stores the checkpoint compressed, as
    `<stack>.json.zst`, and the update the driver starts adds an update record
    under `history/` and no copy under `backups/`: what `mise.toml` makes of
    every writer of a stack CI deploys. The caller exports the opposite of
    each switch, and neither run keeps it, so what the stack is given is the
    module's to give.
    """
    stack = identity.STACK_NAMES.github
    assert stack_environment.home(stack) is identity.StateHome.BACKEND
    for variable in STATE_SWITCHES_ENV:
        monkeypatch.delenv(variable, raising=False)
    for variable in ('PULUMI_DIY_BACKEND_ZSTD', 'PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS'):
        monkeypatch.setenv(variable, 'false')
    box = _appliance(start, ignition)
    checkout = tmp_path / 'kluster'
    checkout.mkdir()
    _ = (checkout / 'Pulumi.yaml').write_text(f'name: {checkout.name}\nruntime: yaml\n')
    # The checkout's slots, as `state-backend bundle operator` and the
    # operator passphrase's `recover` fill them, the URL naming the box's port.
    slots = workstation.slots(checkout)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    shutil.copytree(clients / settings.OPERATOR_ROLE, slots / stack_environment.BUNDLE_SLOT)
    _ = (slots / stack_environment.BUNDLE_SLOT / bundle.URL_FILE).write_text(operator.url + '\n')
    _ = (slots / stack_environment.OPERATOR_PASSPHRASE_SLOT).write_text(fake(pulumi_config.PASSPHRASE_ENV) + '\n')
    environment = pulumi_config.BackendEnvironment(
        operator=lambda: fake(pulumi_config.PASSPHRASE_ENV),
        url=operator.url,
        bundle_dir=slots / stack_environment.BUNDLE_SLOT,
    )
    stored = f'{settings.DATABASE}/{BACKEND_STACKS}{checkout.name}/'

    pulumi_config.Stack(name=stack, directory=checkout, environment=environment).ensure()

    checkpoints = {key for key in _keys(box) if table.kind(key, settings.DATABASE) is table.Kind.CHECKPOINT}
    assert checkpoints == {f'{stored}{stack}.json.zst'}

    run = driver.Run.open(stack, checkout)
    _ = pulumi_config.run_pulumi(
        ['up', '--yes', '--skip-preview', '--stack', stack], cwd=checkout, env=run.env, stdin=None
    )

    keys = _keys(box)
    checkpoints = {key for key in keys if table.kind(key, settings.DATABASE) is table.Kind.CHECKPOINT}
    assert checkpoints == {f'{stored}{stack}.json.zst'}
    assert [key for key in keys if table.kind(key, settings.DATABASE) is table.Kind.HISTORY]
    assert [key for key in keys if table.kind(key, settings.DATABASE) is table.Kind.BACKUPS] == []


#: Each read of the whole table, as the statement it runs and the function
#: that runs it: the census's, and the ceiling's.
READS: dict[str, Callable[[table.Target], list[table.Row]]] = {
    'CENSUS_QUERY': table.census,
    'VALUE_SIZES_QUERY': table.value_sizes,
}


@pytest.mark.parametrize('statement', list(READS))
@pytest.mark.parametrize('role', CLIENT_ROLES)
def test_the_census_reads_in_a_session_that_cannot_write(
    box: Box, roots: config.Roots, clients: Path, monkeypatch: pytest.MonkeyPatch, role: str, statement: str
) -> None:
    """Through the pooler's transaction entry, for either role, and for the census's read and the ceiling's.

    The read's statement is replaced by a write: the server refuses it,
    because the session was opened read-only, not because of what the
    read's own statement happens to be. The setting reaches the server
    process that serves the statement because the pooler carries it from the
    client's startup, and it stays with the read: the role's next client,
    served by the same pool, writes.
    """
    target = _bundle(roots, box, clients, role)
    monkeypatch.setattr(table, statement, 'CREATE TABLE census_wrote (x int)')

    with pytest.raises(state.StateError, match='read-only transaction'):
        _ = READS[statement](target)
    assert box.local("SELECT to_regclass('census_wrote') IS NULL") == 't'
    after = f'census_after_{role}_{statement.lower()}'
    _ = _psql(clients, target, f'CREATE TABLE {after} (x int)')
    assert box.local(f'DROP TABLE {after}') == 'DROP TABLE'


def _until(read: Callable[[], object], done: Callable[[Any], bool], what: str) -> Any:
    """Read until `done` holds of what was read, and return it; fail naming `what` at the stop-loss.

    The condition is the judgement. The bound, `COMMAND_TIMEOUT`, only ends a
    wait whose condition never comes.
    """
    deadline = time.monotonic() + COMMAND_TIMEOUT
    while True:
        found = read()
        if done(found):
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f'{what} never happened; last read: {found!r}')
        time.sleep(POLL_INTERVAL)


def _client_backends(box: Box, state_: str | None = None) -> int:
    """How many server processes serve a client role, in `state_` if one is named."""
    where = f" AND state = '{state_}'" if state_ else ''
    return int(
        box.local(
            f"SELECT count(*) FROM pg_stat_activity WHERE usename IN ('{settings.CI_ROLE}', "
            f"'{settings.OPERATOR_ROLE}'){where}"
        )
    )


def _waiting(box: Box) -> int:
    return sum(int(pool['cl_waiting']) for pool in box.pools())


def _session(stack: ExitStack, clients: Path, target: state.Connection) -> process_sessions.Command:
    """A `psql` session over the bundle, reading its statements from a pipe this case writes, ended with `stack`."""
    return stack.enter_context(
        process_sessions.started(
            [str(clients / 'psql'), '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-At', f'--dbname={target.url}'],
            env={**os.environ, **target.env},
            stdin=process_sessions.PIPE,
            stdout=process_sessions.PIPE,
            stderr=process_sessions.PIPE,
            text=True,
        )
    )


def _say(session: process_sessions.Command, sql: str) -> None:
    assert session.stdin is not None
    _ = session.stdin.write(sql + '\n')
    session.stdin.flush()


def test_a_client_beyond_the_cap_waits_and_runs_once_a_transaction_commits(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path
) -> None:
    """With the cap's worth of transactions open across both roles, the next statement waits, and is not refused.

    The cap is one count over both roles' pools of the transaction entry, so
    the transactions held are of both. The next client's statement waits in
    the pooler -- its client is counted waiting, it holds no server process,
    and the server processes stay at the cap -- until a transaction commits
    and frees its server process, and then it runs. The commit is what the
    case waits on; no wait here is decided by time.
    """
    box = _appliance(start, ignition)
    cap = settings.POOL_CAP
    ci = _bundle(roots, box, clients, settings.CI_ROLE)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    roles = [operator] * (cap // 2) + [ci] * (cap - cap // 2)
    with ExitStack() as stack:
        holders = [_session(stack, clients, target) for target in roles]
        for holder in holders:
            _say(holder, 'BEGIN; SELECT 1;')
        _ = _until(lambda: _client_backends(box, 'idle in transaction'), lambda held: held == cap, 'the cap held')

        waiter = stack.enter_context(
            process_sessions.started(
                [str(clients / 'psql'), '--no-psqlrc', '-At', f'--dbname={ci.url}', '-c', 'SELECT 42'],
                env={**os.environ, **ci.env},
                stdout=process_sessions.PIPE,
                stderr=process_sessions.PIPE,
                text=True,
            )
        )
        _ = _until(lambda: _waiting(box), lambda waiting: waiting == 1, 'a client waiting in the pooler')
        assert waiter.returncode is None
        assert _client_backends(box) == cap

        _say(holders[-1], 'COMMIT;')
        out, err = waiter.communicate(timeout=COMMAND_TIMEOUT)

    assert (waiter.returncode, out.strip()) == (0, '42'), err


def test_an_idle_server_process_of_one_role_yields_to_the_others_client(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path
) -> None:
    """The cap counts idle server processes too, and an idle one of one role's pool is closed for the other's.

    After `ci` statements leave the cap's worth of idle server processes in
    their pool, an `operator` session still gets one, rather than waiting
    for the idle ones to time out.
    """
    box = _appliance(start, ignition)
    cap = settings.POOL_CAP
    ci = _bundle(roots, box, clients, settings.CI_ROLE)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    with ExitStack() as stack:
        holders = [_session(stack, clients, ci) for _ in range(cap)]
        for holder in holders:
            _say(holder, 'BEGIN; SELECT 1;')
        _ = _until(lambda: _client_backends(box, 'idle in transaction'), lambda held: held == cap, 'the cap held')
        for holder in holders:
            _say(holder, 'COMMIT;')
        _ = _until(lambda: _client_backends(box, 'idle'), lambda idle: idle == cap, 'the idle server processes')

    assert _psql(clients, operator, 'SELECT current_user') == 'state_owner'


def test_a_dump_through_the_pooler_restores_through_the_pooler(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """`state-backend dump` and `restore` reach the box through the pooler, and the round trip keeps the stack.

    The dump is `state`'s own, over the operator's bundle, from one box; the
    restore is `state`'s own into a second, which `pulumi` has only opened.
    """
    source, target = _appliance(start, ignition), _appliance(start, ignition)
    _stack_init(_bundle(roots, source, clients, settings.CI_ROLE), tmp_path / 'project', STACK)
    archive = clients / 'through-the-pooler.dump'

    state.pg_dump(_bundle(roots, source, clients, settings.OPERATOR_ROLE), archive)
    operator = _bundle(roots, target, clients, settings.OPERATOR_ROLE)
    assert state.stacks(operator) == []
    state.pg_restore(operator, archive)

    for role in CLIENT_ROLES:
        assert _names(state.stacks(_bundle(roots, target, clients, role))) == [STACK], role


def _pooler_start_check(ignition: dict[str, Any], name: str) -> list[str]:
    """The pooler unit's wait for Postgres, as the argument vector it runs inside the Postgres container `name`."""
    (line,) = [line for line in _unit_lines(ignition, POOLER_UNIT) if 'podman exec' in line]
    found = re.search(r'podman exec (\S+) (.+?); do', line)
    assert found is not None, line
    postgres = _unit_command(ignition, UNIT)
    assert found.group(1) == postgres[postgres.index('--name') + 1], 'the wait names another container'
    return ['exec', name, *shlex.split(found.group(2))]


def _sockets(ignition: dict[str, Any]) -> list[str]:
    """The directories the Postgres unit has the server put its sockets in."""
    (setting,) = [arg for arg in _unit_command(ignition, UNIT) if arg.startswith('unix_socket_directories=')]
    return setting.removeprefix('unix_socket_directories=').split(',')


#: An init script that holds the image's initialization until the case
#: releases it, and says when it has started holding.
HOLD = File(
    PurePosixPath('/docker-entrypoint-initdb.d/zz-hold.sh'),
    b'echo kluster-test: holding the initialization\nuntil [ -e /tmp/release ]; do sleep 0.1; done\n',
    0o755,
)


def test_the_pooler_waits_for_the_serving_postgres_and_not_the_initialization(
    start: Start, ignition: dict[str, Any]
) -> None:
    """The pooler's unit starts it once the serving Postgres answers, which the image's initialization never does.

    While the initialization runs, its temporary server answers on the
    sockets -- the one the pooler's clients would be served on included --
    and the unit's check refuses. Once the serving Postgres is up, it
    passes. So the first handshake the box completes is with a pooler whose
    Postgres serves the state, which is what the replacement's restore hook
    takes the readiness wait to mean.
    """
    box = start.appliance(ignition, held=HOLD)
    check = _pooler_start_check(ignition, box.name)
    _await_log(box.name, ('kluster-test: holding the initialization',))

    sockets = [
        process_sessions.run(['podman', 'exec', box.name, 'pg_isready', f'--host={directory}'], timeout=COMMAND_TIMEOUT)
        for directory in _sockets(ignition)
    ]
    held = process_sessions.run(['podman', *check], timeout=COMMAND_TIMEOUT)
    _ = _podman('exec', box.name, 'touch', '/tmp/release')
    _await_ready(box.name)
    serving = process_sessions.run(['podman', *check], timeout=COMMAND_TIMEOUT)

    # The premise: the temporary server answers on every socket, the one the
    # pooler is served on among them.
    assert len(sockets) > 1
    assert [answer.returncode for answer in sockets] == [0] * len(sockets)
    assert held.returncode != 0
    assert serving.returncode == 0, serving.stderr.decode()


def test_a_handshake_with_no_credential_completes_against_the_pooler(box: Box, roots: config.Roots) -> None:
    """The readiness wait and the expiry probe read the server certificate with no client certificate of their own.

    The pooler answers the protocol's TLS request, presents the server
    certificate the CA issued for the box's address, and does not demand a
    client certificate in the handshake.
    """
    postgres_tls.handshake(ADDRESS, box.port, roots.ca.certificate().cert_pem.decode())


def test_the_pooler_runs_as_the_user_the_box_gives_its_files() -> None:
    """The server key and the shared directory are given to `PGBOUNCER_UID` and `PGBOUNCER_GID`; the image runs as them."""
    name = f'kluster-test-pooleruser-{uuid.uuid4().hex[:12]}'
    with _removing(name):
        ran = _podman(*_named(_bounded('run', '--entrypoint', 'id', POOLER_IMAGE, image=POOLER_IMAGE), name)).decode()
    found = re.match(r'uid=(\d+)\S* gid=(\d+)', ran)
    assert found is not None, ran

    assert (int(found.group(1)), int(found.group(2))) == (settings.PGBOUNCER_UID, settings.PGBOUNCER_GID)


# -- the wire protocol, at the grain the pooler lends a server process -------------


class Wire:
    """One client connection to the pooler, speaking the protocol's messages one by one.

    The two round trips `lib/pq` makes of a statement with parameters, unless
    told otherwise -- Parse and Describe, answered, then Bind and Execute --
    are the subject, and no client tool here sends them apart, so this sends
    the messages themselves: TLS on the protocol's own request, the bundle's
    certificate, the startup, and each message as the protocol frames it.
    """

    def __init__(self, port: int, target: state.Connection, user: str) -> None:
        raw = socket.create_connection((ADDRESS, port), timeout=COMMAND_TIMEOUT)
        raw.sendall(struct.pack('!ii', 8, 80877103))
        assert raw.recv(1) == b'S'
        context = ssl.create_default_context(cafile=target.env[CA_ENV])
        context.load_cert_chain(target.env[CERT_ENV], target.env[KEY_ENV])
        self.socket: ssl.SSLSocket = context.wrap_socket(raw, server_hostname=ADDRESS)
        body = b''.join(
            f'{key}\0{value}\0'.encode() for key, value in (('user', user), ('database', settings.DATABASE))
        )
        body += b'\0'
        self.socket.sendall(struct.pack('!ii', 8 + len(body), 196608) + body)
        _ = self.until_ready()

    def send(self, *messages: tuple[bytes, bytes]) -> None:
        self.socket.sendall(b''.join(kind + struct.pack('!i', 4 + len(body)) + body for kind, body in messages))

    def _exactly(self, count: int) -> bytes:
        read = b''
        while len(read) < count:
            chunk = self.socket.recv(count - len(read))
            assert chunk, 'the pooler closed the connection'
            read += chunk
        return read

    def until_ready(self) -> list[str]:
        """Every value of every row up to `ReadyForQuery`; an `ErrorResponse` fails, naming its message."""
        values: list[str] = []
        while True:
            kind = self._exactly(1)
            (length,) = struct.unpack('!i', self._exactly(4))
            body = self._exactly(length - 4)
            if kind == b'E':
                raise AssertionError(body.decode(errors='replace'))
            if kind == b'D':
                (columns,) = struct.unpack('!h', body[:2])
                at = 2
                for _ in range(columns):
                    (size,) = struct.unpack('!i', body[at : at + 4])
                    values.append(body[at + 4 : at + 4 + size].decode())
                    at += 4 + size
            if kind == b'Z':
                return values

    def close(self) -> None:
        self.socket.close()


def _parse(sql: str) -> tuple[bytes, bytes]:
    return b'P', f'\0{sql}\0'.encode() + struct.pack('!h', 0)


DESCRIBE = (b'D', b'S\0')
BIND = (b'B', b'\0\0' + struct.pack('!hhh', 0, 0, 0))
EXECUTE = (b'E', b'\0' + struct.pack('!i', 0))
SYNC = (b'S', b'')


def test_remove_binary_parameters_once_the_transaction_entry_keeps_a_statements_two_round_trips_together(
    start: Start, capped: dict[str, Any], roots: config.Roots, clients: Path
) -> None:
    """The tripwire on `binary_parameters=yes`: red once the pinned pooler serves a statement's two round trips alike.

    `pulumi` is given the parameter (`bundle.Purpose.PULUMI`) because a
    statement sent in two round trips -- Parse and Describe, answered, then
    Bind and Execute, which is how `lib/pq` sends one by default -- is
    executed as whatever its server process last parsed: on the transaction
    entry, at the one server process the cap leaves, another client's
    statement, parsed in between. `pulumi` keeps two connections open, so the
    first is what its reads meet on any server process the pooler lends both.
    The parameter makes `lib/pq` send each statement in one round trip.

    -   **Two round trips meet the other client's statement: the case
        passes.** The pooler lends the server process between them, and the
        parameter is needed.
    -   **Two round trips meet their own statement: the case fails**, naming
        the parameter to remove. The pooler is the box's pinned image, the
        same on every machine, so any run's finding is the verdict.

    The control is the one round trip the parameter makes `lib/pq` send,
    executed as its own statement.
    """
    box = start.appliance(capped)
    target = _bundle(roots, box, clients, settings.CI_ROLE)
    first, second = Wire(box.port, target, settings.CI_ROLE), Wire(box.port, target, settings.CI_ROLE)
    try:
        first.send(_parse("SELECT 'first'"), DESCRIBE, SYNC)
        _ = first.until_ready()
        second.send(_parse("SELECT 'second'"), DESCRIBE, SYNC)
        _ = second.until_ready()
        first.send(BIND, EXECUTE, SYNC)
        apart = first.until_ready()

        first.send(_parse("SELECT 'first'"), BIND, DESCRIBE, EXECUTE, SYNC)
        together = first.until_ready()
    finally:
        first.close()
        second.close()

    assert together == ['first']
    if apart == ['first']:
        pytest.fail(
            "the pinned pooler's transaction entry executes a statement sent in two round trips as its own: remove "
            "`binary_parameters=yes` where `pulumi`'s URL gains it (`bundle.pulumi_url`, `mise.toml`'s "
            '`PULUMI_BACKEND_URL`), and this case'
        )
    assert apart == ['second']


def test_a_pulumi_session_after_a_dump_and_a_restore_meets_the_default_search_path(
    start: Start, capped: dict[str, Any], roots: config.Roots, clients: Path, tmp_path: Path
) -> None:
    """A restore empties `search_path` in its session, and that session is the session entry's, never a `pulumi` one.

    `pg_restore` sets it for the whole session, so it outlasts the
    restore's transaction on whichever server process ran it. At a cap of
    one, a `pulumi` session or a query is served by the one server process
    the transaction entry has, so a restore through that entry would leave
    the setting there for the next. Through the session entry, the next
    `pulumi` run creates its table and reads its stack.
    """
    box = start.appliance(capped)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    _stack_init(operator, tmp_path / 'project', STACK)
    archive = clients / 'leak.dump'

    state.pg_dump(operator, archive)
    state.pg_restore(operator, archive)

    assert _psql(clients, operator, 'SHOW search_path') == '"$user", public'
    assert _names(state.stacks(operator)) == [STACK]


def test_the_session_entry_admits_operator_alone(box: Box, roots: config.Roots, clients: Path) -> None:
    ci = _bundle(roots, box, clients, settings.CI_ROLE)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    dumping = state.Connection(url=ci.form(bundle.Purpose.DUMP), env=ci.env)

    with pytest.raises(state.StateError, match=re.escape('no authentication method is found')):
        _ = _psql(clients, dumping, 'SELECT 1')
    assert _psql(clients, state.Connection(url=operator.form(bundle.Purpose.DUMP), env=operator.env), 'SELECT 1') == '1'


def test_a_certificate_names_the_one_role_it_logs_in_as_on_both_entries(
    box: Box, roots: config.Roots, clients: Path
) -> None:
    """A `ci` certificate asking to be `operator` is refused on the transaction entry and on the session entry.

    The pooler maps a certificate's CN to the role, which is the line between
    `ci` and the session entry `operator` alone may use. The control: the
    same certificate, as `ci`, is admitted.
    """
    ci = _bundle(roots, box, clients, settings.CI_ROLE)
    posing = state.Connection(url=ci.url.replace(f'{settings.CI_ROLE}@', f'{settings.OPERATOR_ROLE}@', 1), env=ci.env)
    assert urllib_parse.urlsplit(posing.url).username == settings.OPERATOR_ROLE

    assert _psql(clients, ci, 'SELECT current_user') == 'state_owner'
    for purpose in (bundle.Purpose.QUERY, bundle.Purpose.DUMP):
        with pytest.raises(state.StateError, match='certificate authentication failed'):
            _ = _psql(clients, state.Connection(url=posing.form(purpose), env=posing.env), 'SELECT 1')


# -- workaround: pasta drops the data a reset follows ------------------------------
#
# Everything between this marker and the one that ends the layer is a
# workaround for one defect of pasta's. On a connection pasta splices from
# host loopback, data the namespace's side sends just before it resets the
# connection is dropped whenever pasta handles the data and the reset in one
# wakeup: `tcp_splice_sock_handler` (passt's `tcp_splice.c`) tests `EPOLLERR`
# before anything else, and `tcp_splice_rst` closes both sides with
# `SO_LINGER` 0 and the data unread. The client then reads the connection's
# end and nothing before it, which load makes more likely. The pooler refuses a
# certificate exactly that way: under TLS 1.3 the client's startup follows
# its handshake before the pooler has judged its certificate, so the pooler
# sends its alert and closes with the startup unread, and its kernel resets.
#
# So a case whose evidence is data the pooler sends before it resets reaches
# the pooler inside the box's own network namespace, where no relay sits
# between the two and the kernel delivers the data before the reset:
# `_psql_in_namespace`, which the stranger-CA case below runs. The layer is
# that function, that case's call of it in place of `_psql` over `_bundle`,
# and the tripwire with its helpers. They reach the box's network container
# through `_network_name`, which `Launcher` names it with. framework/testing.md
# §1.2 describes the tripwire in the sentences, beside the container
# paragraph, on a pasta a case has stopped; those go with the layer. Its
# §1 sentence on the tripwire of each workaround covers this one without
# naming it.
#
# **It is removed once the pasta on CI's runner forwards what a socket has
# queued before it propagates that socket's reset**; the tripwire fails there
# then, and skips anywhere else, since a workstation's pasta moves on a
# schedule of its own. The stranger-CA case goes back to `_psql` over
# `_bundle`, and everything else between the markers goes whole.


def _psql_in_namespace(box: Box, directory: Path, url: str, env: Mapping[str, str], sql: str) -> str:
    """The image's `psql`, run in `box`'s network container, over the bundle in `directory`, at `url`.

    The network container runs the box's image, so its `psql` is the
    server's own release, and it shares the pooler's network namespace, so
    `url` is a bundle's own, naming `settings.PORT` on `ADDRESS`. The bundle
    is copied in at the same path, the way `tools` hands its tools a
    directory, so `env`'s paths mean the same inside.
    """
    network = _network_name(box.name)
    _ = _podman('exec', network, 'mkdir', '-p', str(directory))
    _ = _podman('cp', f'{directory}/.', f'{network}:{directory}')
    done = process_sessions.run(
        ['podman', 'exec', *(f'--env={name}={value}' for name, value in env.items()), network]  # noqa: RUF005 -- keeps -c beside its value
        + ['psql', '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-At', f'--dbname={url}', '-c', sql],
        text=True,
        timeout=COMMAND_TIMEOUT,
    )
    if done.returncode != 0:
        raise state.StateError(done.stderr.strip())
    return done.stdout.strip()


def test_a_certificate_another_authority_signed_is_refused_in_the_handshake(
    box: Box, roots: config.Roots, clients: Path
) -> None:
    """A client role's name, in a certificate the box's CA did not sign, gets no further than the handshake.

    The refusal is the pooler's TLS alert, read inside the box's namespace,
    where nothing between the client and the pooler drops it.
    """
    stranger = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT,))
    issued = config.client_bundle(stranger.ca, name=settings.CI_ROLE, address=ADDRESS)
    directory = clients / settings.CI_ROLE
    config.write_client_bundle(issued, directory)
    env = ssl_env(directory)
    # The client trusts the box's CA, so the refusal is the box's and not the client's.
    _ = Path(env[CA_ENV]).write_bytes(roots.ca.certificate().cert_pem)

    with pytest.raises(state.StateError, match=r'SSL error|alert'):
        _ = _psql_in_namespace(box, directory, issued.url(), env, 'SELECT 1')


#: The states of a TCP socket that `/proc/net/tcp` names, in its own
#: hexadecimal: one end of a connection still open, and the states a close
#: with a FIN leaves the two ends in.
ESTABLISHED = 0x01
CLOSED_WITH_A_FIN = {0x04: 'FIN_WAIT1', 0x05: 'FIN_WAIT2', 0x08: 'CLOSE_WAIT', 0x09: 'LAST_ACK', 0x0B: 'CLOSING'}

#: The protocol's request for TLS, which the pooler answers with `S`.
SSL_REQUEST = struct.pack('!ii', 8, 80877103)

#: The watchdog that resumes a stopped pasta once this process is gone. It
#: ignores the signals that end a session's processes together -- SIGTERM,
#: SIGHUP, SIGINT -- and reads its stdin, which only this process holds open,
#: until EOF, which comes when the block that started it ends, or when this
#: process dies however it dies. Then it sends SIGCONT to the process
#: `argv[1]` names, if that process is still the one that started at
#: `argv[2]`.
WATCHDOG = """
import signal
import sys
import psutil
for ending in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(ending, signal.SIG_IGN)
sys.stdin.read()
process = psutil.Process(int(sys.argv[1]))
if process.create_time() == float(sys.argv[2]):
    process.resume()
"""


@dataclass(frozen=True)
class TcpSocket:
    """One TCP socket in a network namespace, as `/proc/net/tcp` lists it: its two ports, its state, and its unread bytes."""

    local: int
    remote: int
    state: int
    unread: int


def _namespace_sockets(box: Box) -> list[TcpSocket]:
    """Every IPv4 TCP socket listed in `box`'s network namespace, read from inside it.

    IPv4 alone, because the pooler listens on `0.0.0.0` and pasta reaches it
    from the namespace's IPv4 loopback for a client on the host's.
    """
    listed = _podman('exec', _network_name(box.name), 'cat', '/proc/net/tcp').decode().splitlines()[1:]
    found: list[TcpSocket] = []
    for line in listed:
        _, local, remote, state_, queues, *_ = line.split()
        found.append(
            TcpSocket(
                local=int(local.rsplit(':', 1)[1], 16),
                remote=int(remote.rsplit(':', 1)[1], 16),
                state=int(state_, 16),
                unread=int(queues.split(':')[1], 16),
            )
        )
    return found


def _pasta(netns: str) -> psutil.Process | None:
    """The pasta that forwards the ports of the network namespace at `netns`, where this process can see it.

    podman records no pid for the pasta it starts, and pasta hides its own
    sockets (it is not dumpable, so no other process reads its `/proc/<pid>/fd`),
    so the process cannot be found as the listener on the box's host port.
    What ties it to the box is the namespace podman hands it, `--netns` and
    the network container's own `SandboxKey`: one argument compared whole,
    which no other box's pasta carries, and a second process that carries it
    fails the case rather than being stopped. The command is told by the name it
    was started as, since pasta goes on to run a build of `passt` that names
    the process after itself.
    """
    found: list[psutil.Process] = []
    for process in psutil.process_iter(['cmdline']):
        argv: list[str] = process.info['cmdline'] or []
        named = bool(argv) and PurePosixPath(argv[0]).name == 'pasta'
        if named and ('--netns', netns) in itertools.pairwise(argv):
            found.append(process)
    assert len(found) <= 1, f'more than one pasta names {netns}: {[process.pid for process in found]}'
    return found[0] if found else None


def _pasta_release(pasta: psutil.Process) -> str:
    """The first line `--version` prints for the command `pasta` was started as, which names its release."""
    printed = process_sessions.run([pasta.cmdline()[0], '--version'], text=True, timeout=COMMAND_TIMEOUT)
    return (printed.stdout.splitlines() or [f'pasta ({printed.stderr.strip()})'])[0]


def _unobserved(why: str) -> NoReturn:
    """The tripwire cannot hold pasta still here: skip, except on CI's runner, where its verdict is read, and fail."""
    if _on_ci_runner():
        pytest.fail(f"{why}, on CI's runner, where the tripwire's verdict is read")
    pytest.skip(why)


@contextmanager
def _stopped(pasta: psutil.Process) -> Generator[None]:
    """`pasta` stopped with SIGSTOP for the block, and resumed with SIGCONT on every way out of it.

    The `finally` covers every way out the test process lives through: a
    failure, the case bound. The watchdog (`WATCHDOG`) covers this process
    ended without one, killed outright or by a signal it does not handle:
    its stdin closes with this process, and it sends the SIGCONT. It leads a
    POSIX session of its own, so the outer `timeout`'s signal to the run's
    process group does not reach it, and it ignores SIGTERM, SIGHUP and
    SIGINT, so a stop of the cgroup it shares with this process -- systemd's
    `KillMode=control-group` sends SIGTERM to every process in it -- ends
    this process and leaves the watchdog to read the EOF. pasta runs in the
    container service's cgroup and is not reached by that stop.

    What remains is a SIGKILL that reaches the watchdog before it reads the
    EOF: a SIGKILL of the cgroup, or an OOM kill of the whole group. That
    leaves `pasta` stopped, and a stopped pasta does not end with its
    container, since only its own event loop notices the namespace going.
    It keeps listening on the box's reserved port, accepting connections it
    never serves, until something kills it; `host_ports` passes over that
    port.
    """
    with process_sessions.started(
        [sys.executable, '-c', WATCHDOG, str(pasta.pid), repr(pasta.create_time())], stdin=process_sessions.PIPE
    ):
        try:
            try:
                pasta.suspend()
            except psutil.AccessDenied as denied:
                _unobserved(f'this process may not stop pasta {pasta.pid}: {denied}')
            yield
        finally:
            # A SIGCONT to a pasta that was never stopped does nothing; one
            # this process may not signal, or one that is gone, needs none.
            with suppress(psutil.AccessDenied, psutil.NoSuchProcess):
                pasta.resume()


def _startup(user: str) -> bytes:
    """The protocol's startup message, for `user` on the state's database."""
    body = b''.join(f'{key}\0{value}\0'.encode() for key, value in (('user', user), ('database', settings.DATABASE)))
    body += b'\0'
    return struct.pack('!ii', 8 + len(body), 196608) + body


def _drained(raw: socket.socket) -> bytes:
    """Everything `raw` reads before it ends, with a reset or with the end of the stream."""
    read = b''
    with suppress(ConnectionResetError):
        while chunk := raw.recv(4096):
            read += chunk
    return read


def test_remove_the_in_namespace_client_once_pasta_forwards_the_data_a_reset_follows(
    start: Start, ignition: dict[str, Any], roots: config.Roots, clients: Path
) -> None:
    """The tripwire on the layer above: red on CI's runner once its pasta forwards the pooler's alert ahead of the reset.

    The loss is held still rather than raced. The case completes a TLS 1.3
    handshake with a certificate from a stranger CA through the box's host
    port, and holds back its final flight and its startup. It pauses the
    pooler, sends what it held, and waits until it is all in the pooler's
    receive queue. It stops the box's own pasta, unpauses the pooler, and
    waits until neither end of the connection is established in the
    namespace: the pooler has refused, and pasta's end has received the
    alert and the reset behind it, with pasta asleep. Then pasta runs again
    and handles both at once, and the client reads what pasta forwards until
    the connection ends: with `ECONNRESET` from a pasta that passes the
    reset on, and with the end of the stream from one that closes instead.

    -   **Nothing: the case passes.** pasta drops the alert, and the layer
        is needed.
    -   **The alert, on CI's runner: the case fails**, naming the layer to
        remove. The runner's run is the verdict, being the pasta every
        change runs this module on.
    -   **The alert anywhere else: the case skips**, naming what it found.
        A workstation's pasta moves on a schedule of its own, so its finding
        cannot clear the workaround for the runner.

    The premise is that the pooler reset the connection: no end of it is
    left in the namespace, which a close with a FIN would leave in
    `CLOSE_WAIT` or a `FIN_WAIT`. The control for the refusal itself is the
    stranger-CA case, which reads the alert inside the namespace. Every wait
    is a condition read from `/proc/net/tcp`, with `COMMAND_TIMEOUT` as the
    stop-loss. Where the box's pasta is not visible from this process, as
    where podman's service runs on another machine, or this process may not
    stop it, the case skips, and fails on CI's runner, where its verdict is
    read.
    """
    box = start.appliance(ignition)
    assert box.pooler is not None
    netns = (
        _podman('container', 'inspect', '--format', '{{.NetworkSettings.SandboxKey}}', _network_name(box.name))
        .decode()
        .strip()
    )
    pasta = _pasta(netns)
    if pasta is None:
        _unobserved(f'no pasta that names {netns} is visible from this process')
    stranger = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT,))
    target = _bundle(stranger, box, clients, settings.CI_ROLE)
    context = ssl.create_default_context(cadata=roots.ca.certificate().cert_pem.decode())
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(target.env[CERT_ENV], target.env[KEY_ENV])

    with socket.create_connection((ADDRESS, box.port), timeout=COMMAND_TIMEOUT) as raw:
        raw.sendall(SSL_REQUEST)
        assert raw.recv(1) == b'S'
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = context.wrap_bio(incoming, outgoing, server_hostname=ADDRESS)
        while True:
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                raw.sendall(outgoing.read())
                answer = raw.recv(65536)
                assert answer, 'the pooler closed the connection inside the handshake'
                incoming.write(answer)
        _ = tls.write(_startup(settings.CI_ROLE))
        held = outgoing.read()

        _ = _podman('pause', box.pooler)
        paused = True
        try:
            raw.sendall(held)
            queued = _until(
                lambda: _namespace_sockets(box),
                lambda found: any(
                    end.local == settings.PORT and end.state == ESTABLISHED and end.unread == len(held) for end in found
                ),
                "the held bytes in the pooler's receive queue",
            )
            (pooled,) = [end for end in queued if end.local == settings.PORT and end.state == ESTABLISHED]
            with _stopped(pasta):
                _ = _podman('unpause', box.pooler)
                paused = False
                left = _until(
                    lambda: [
                        end
                        for end in _namespace_sockets(box)
                        if {end.local, end.remote} == {settings.PORT, pooled.remote}
                    ],
                    lambda ends: all(end.state != ESTABLISHED for end in ends),
                    'both ends of the connection out of the established state',
                )
        finally:
            if paused:
                _ = _podman('unpause', box.pooler)
        read = _drained(raw)

    assert left == [], (
        f'the pooler closed rather than reset: {[CLOSED_WITH_A_FIN.get(end.state, end.state) for end in left]}'
    )
    if read:
        _runners_verdict(
            f'{_pasta_release(pasta)} forwarded the {len(read)} bytes the pooler sent ahead of its reset',
            "the workaround for pasta's dropped data and this case, as the comment that opens it says",
        )


# -- end of the workaround for pasta's dropped data ---------------------------------


def test_a_client_that_presents_no_certificate_is_refused(box: Box, roots: config.Roots, clients: Path) -> None:
    """The pooler asks for a certificate without demanding one in the handshake; its `hba.conf` refuses the client that sent none."""
    target = _bundle(roots, box, clients, settings.CI_ROLE)
    unsigned = state.Connection(url=target.url, env={**target.env, CERT_ENV: '/nonexistent', KEY_ENV: '/nonexistent'})

    with pytest.raises(state.StateError, match='certificate authentication failed'):
        _ = _psql(clients, unsigned, 'SELECT 1')


def test_the_expiry_probe_reads_the_pooler_certificate_with_no_credential(box: Box) -> None:
    """The probe's own handshake, `openssl s_client`, completes and reads the leaf though the pooler demands a client certificate."""
    verdict = probe.certificate(address=ADDRESS, port=box.port)

    assert verdict.passed, str(verdict)


@pytest.mark.parametrize('argv', [['census', '--yes'], ['census', '--ceiling', '--yes']])
def test_census_yes_is_refused_without_prune_superseded(capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        _ = cli.main(argv)

    assert refused.value.code == 2
    assert '--prune-superseded' in capsys.readouterr().err


#: A stack's keys under `<database>/.pulumi/`, and which of them are
#: superseded. Each group is one stack.
SUPERSEDED: list[tuple[str, list[str], list[str]]] = [
    (
        'compressed after plain',
        ['stacks/p/a.json.zst', 'stacks/p/a.json.zst.bak', 'stacks/p/a.json.bak'],
        ['stacks/p/a.json.bak'],
    ),
    ('zstd after gzip', ['stacks/p/b.json.zst', 'stacks/p/b.json.gz.bak'], ['stacks/p/b.json.gz.bak']),
    (
        'plain after zstd: writers that disagree',
        [
            'stacks/p/c.json',
            'stacks/p/c.json.zst.bak',
            'history/p/c/c-1.checkpoint.json.zst',
            'backups/p/c/c.1.json.zst',
        ],
        ['stacks/p/c.json.zst.bak', 'history/p/c/c-1.checkpoint.json.zst', 'backups/p/c/c.1.json.zst'],
    ),
    (
        'a removed stack: its copies are all there is',
        ['stacks/p/d.json.bak', 'history/p/d/d-1.checkpoint.json', 'backups/p/d/d.1.json'],
        [],
    ),
    (
        'two live suffixes: a copy in either is its checkpoint copy',
        [
            'stacks/p/e.json',
            'stacks/p/e.json.zst',
            'stacks/p/e.json.bak',
            'history/p/e/e-1.checkpoint.json',
            'backups/p/e/e.1.json',
        ],
        [],
    ),
    (
        'plain throughout',
        [
            'stacks/p/f.json',
            'stacks/p/f.json.bak',
            'history/p/f/f-1.history.json',
            'history/p/f/f-1.checkpoint.json',
            'backups/p/f/f.1.json',
        ],
        [],
    ),
    (
        'compressed throughout',
        [
            'stacks/p/g.json.zst',
            'stacks/p/g.json.zst.bak',
            'history/p/g/g-1.history.json.zst',
            'history/p/g/g-1.checkpoint.json.zst',
        ],
        [],
    ),
    (
        'compressed after plain: the history and backups copies, and no update record',
        [
            'stacks/p/h.json.zst',
            'history/p/h/h-1.history.json',
            'history/p/h/h-1.checkpoint.json',
            'history/p/h/h-2.history.json.gz',
            'history/p/h/h-2.checkpoint.json.gz',
            'history/p/h/h-3.history.json.zst',
            'history/p/h/h-3.checkpoint.json.zst',
            'backups/p/h/h.1.json',
            'backups/p/h/h.2.json.gz',
        ],
        [
            'history/p/h/h-1.checkpoint.json',
            'history/p/h/h-2.checkpoint.json.gz',
            'backups/p/h/h.1.json',
            'backups/p/h/h.2.json.gz',
        ],
    ),
    (
        "the backend's project-less layout",
        ['stacks/i.json.zst', 'stacks/i.json.bak', 'history/i/i-1.checkpoint.json', 'backups/i/i.1.json'],
        ['stacks/i.json.bak', 'history/i/i-1.checkpoint.json', 'backups/i/i.1.json'],
    ),
    (
        'a name with dots and dashes',
        ['stacks/p/k-8s.x.json.zst', 'history/p/k-8s.x/k-8s.x-17.checkpoint.json', 'backups/p/k-8s.x/k-8s.x.17.json'],
        ['history/p/k-8s.x/k-8s.x-17.checkpoint.json', 'backups/p/k-8s.x/k-8s.x.17.json'],
    ),
    (
        'names the backend does not write',
        [
            'stacks/p/j.json.zst',
            'history/p/j/k-1.checkpoint.json',
            'history/p/j/j-1.checkpoint.json.bak',
            'history/p/j-1.checkpoint.json',
            'backups/p/j/j.json',
            'backups/p/j/k.1.json',
            'backups/p/j/j.1.json.bak',
            'stacks/p/j.json.1790591454000000000',
        ],
        [],
    ),
]


def _superseded_rows(keys: list[str]) -> list[table.Row]:
    """`keys` under the database's own prefix, and the same names under another database's, which nothing selects."""
    layout = f'{settings.DATABASE}/.pulumi/'
    return [
        table.Row(key=key, size=1)
        for key in [*(layout + key for key in keys), *(f'other/.pulumi/{key}' for key in keys)]
    ]


@pytest.mark.parametrize(('keys', 'stale'), [case[1:] for case in SUPERSEDED], ids=[case[0] for case in SUPERSEDED])
def test_a_copy_is_superseded_when_its_stack_has_a_checkpoint_and_none_under_its_suffix(
    keys: list[str], stale: list[str]
) -> None:
    layout = f'{settings.DATABASE}/.pulumi/'

    assert sorted(row.key for row in table.superseded(_superseded_rows(keys), settings.DATABASE)) == sorted(
        layout + key for key in stale
    )


def test_the_delete_takes_what_the_listing_selects_and_nothing_else_it_is_handed(
    start: Start,
    ignition: dict[str, Any],
    roots: config.Roots,
    clients: Path,
    tmp_path: Path,
) -> None:
    """`PRUNE_STATEMENT` reads `superseded`'s definition in Postgres's dialect, and agrees with it on every case above.

    Every group's keys go into one table, and the delete is handed all of
    them: what it deletes is what `superseded` selects from the same rows.
    """
    box = _appliance(start, ignition)
    operator = _bundle(roots, box, clients, settings.OPERATOR_ROLE)
    _stack_init(operator, tmp_path / 'project', STACK)
    rows = _superseded_rows([key for case in SUPERSEDED for key in case[1]])
    values = ', '.join(f'(\'{row.key}\', \'{{"data":""}}\')' for row in rows)
    _ = box.local(f'INSERT INTO {BACKEND_TABLE} (key, data) VALUES {values}')
    selected = sorted(row.key for row in table.superseded(rows, settings.DATABASE))
    assert selected

    assert sorted(table.prune(operator, [row.key for row in rows], database=settings.DATABASE)) == selected


@pytest.mark.parametrize(
    ('path', 'kind'),
    [
        ('.pulumi/meta.yaml', table.Kind.META),
        ('.pulumi/stacks/project/a.json', table.Kind.CHECKPOINT),
        ('.pulumi/stacks/project/a.json.gz', table.Kind.CHECKPOINT),
        ('.pulumi/stacks/project/a.json.zst', table.Kind.CHECKPOINT),
        ('.pulumi/stacks/project/a.json.bak', table.Kind.PREVIOUS),
        ('.pulumi/stacks/project/a.json.zst.bak', table.Kind.PREVIOUS),
        ('.pulumi/stacks/project/a.pulumi-tags', table.Kind.OTHER),
        ('.pulumi/stacks/project/a.json.1790591454000000000', table.Kind.OTHER),
        ('.pulumi/backups/project/a/a.1790591454000000000.json', table.Kind.BACKUPS),
        ('.pulumi/history/project/a/a-1790591454000000000.history.json', table.Kind.HISTORY),
        ('.pulumi/history/project/a/a-1790591454000000000.checkpoint.json.zst', table.Kind.HISTORY),
        ('.pulumi/locks/organization/project/a/id.json', table.Kind.LOCKS),
        ('elsewhere', table.Kind.OTHER),
    ],
)
def test_a_key_is_the_kind_its_place_in_the_layout_says(path: str, kind: table.Kind) -> None:
    assert table.kind(f'{settings.DATABASE}/{path}', settings.DATABASE) is kind
    assert table.kind(f'other/{path}', settings.DATABASE) is table.Kind.OTHER
