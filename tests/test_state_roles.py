"""The appliance's Postgres roles, run in the image the box runs.

The rendered Ignition's own files -- `pg_hba.conf`, the init script, the TLS
material -- go into the pinned image under the Postgres unit's own `podman
run` arguments. What changes is only what has to: the address the server
certificate names, the port it is published on, the delivered directories,
which are copied into the container rather than mounted from a host that does
not have them, and a data directory on a tmpfs, which starts empty and goes
away with the container. So what is exercised is the image's own
initialization mechanism (`/docker-entrypoint-initdb.d`), the server's own
authentication, and real clients: `pulumi` from this workstation, and the
Postgres client tools from the same image, which are the server's own
release. The workstation's client, `mise.toml`'s `postgres` pin, is held only
to the server's major (`tests/test_postgres_client.py`).

Nothing here mounts a host path. A remote `podman` -- one whose service runs
in another mount namespace than this process -- sees none of this process's
temporary files, and `podman cp` is the channel that reaches it either way.

Every container here ends by itself (`_bounded`), and every command a case
starts -- `podman`, the client tools, the log follower -- runs through
`process_sessions` under a bound below the case's (framework/testing.md §1.2).

The properties held here are the ones the box's roles exist for: the bootstrap
superuser answers on the container's local socket and to no certificate, the
client roles are not superusers, `pulumi` writes as one client role and reads
as the other, a dump from a box whose client roles are superusers restores
into one whose roles are not, and the appliance's dump script and the
operator's `state.verify_dump` count the stack checkpoints the pinned `pulumi`
writes -- none on a box the backend has only opened, one after `stack init`.

**Skipped where the image cannot be run without fetching it.** A test that
pulls an image needs the network. This one runs where the pinned image is
already local, which a workstation that has run it once satisfies, and is
skipped elsewhere with the reason named.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import shlex
import shutil
import subprocess as sp
import tarfile
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import process_sessions
import pytest
from root_credentials import fake
from scratch_projects import cli_directories

from kluster.lib import pulumi_cli
from kluster.lib.bundle import CA_ENV, CERT_ENV, KEY_ENV, ssl_env
from kluster.lib.state_backend import render, settings, state
from kluster.scripts.credentials import pki, pulumi_config
from kluster.scripts.state_backend import config

ADDRESS = '127.0.0.1'
IMAGE = settings.POSTGRES_IMAGE
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
#: every command bound, so a stalled command fails as a `TimeoutExpired`
#: naming it rather than as the case's bound (framework/testing.md §8). The
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

#: The image's own `VOLUME`s. A container is given an anonymous volume for
#: each one nothing is mounted on, so each is mounted as a tmpfs instead;
#: `test_every_container_ends_by_itself_and_mounts_no_volume` holds this to
#: the image.
IMAGE_VOLUMES = (PurePosixPath('/var/lib/postgresql/data'),)

#: The unit whose `podman run` this repeats.
UNIT = 'pgstate.service'

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

#: Where the dumps of a box these cases start would go; none is taken.
RECIPIENT = 'age1exampleexampleexampleexampleexampleexampleexampleexamplezzzz'

#: The stack a first client role writes and the other must then read.
STACK = 'roles'

#: The status coreutils `timeout` exits with when its command ran out of time.
TIMED_OUT = 124


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
    found = process_sessions.run(['podman', 'image', 'exists', IMAGE], timeout=COMMAND_TIMEOUT)
    if found.returncode != 0:
        pytest.skip(f'there is no local copy of {IMAGE}')


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
    """A tar stream of `files`, rooted at `/`, with their parent directories.

    Ownership travels inside the stream, and `podman cp --archive=false`
    keeps it: that is how a server key reaches the uid the image runs
    Postgres as, which refuses a key it does not own.
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
    return buffer.getvalue()


def _delivered(ignition: dict[str, Any]) -> list[File]:
    """Every file the Ignition writes, at the path it writes it to."""
    files: list[File] = []
    for entry in ignition['storage']['files']:
        head, _, body = str(entry['contents']['source']).partition(',')
        raw = base64.b64decode(body) if head.endswith(';base64') else urllib.parse.unquote_to_bytes(body)
        if entry['contents'].get('compression') == 'gzip':
            raw = gzip.decompress(raw)
        user = int(entry['user']['id']) if 'user' in entry else 0
        group = int(entry['group']['id']) if 'group' in entry else user
        files.append(File(PurePosixPath(entry['path']), raw, int(entry.get('mode', 0o644)), user, group))
    return files


def _unit_command(ignition: dict[str, Any]) -> list[str]:
    """The Postgres unit's `ExecStart`, as an argument vector."""
    unit = next(entry for entry in ignition['systemd']['units'] if entry['name'] == UNIT)
    lines = str(unit['contents']).replace('\\\n', ' ').splitlines()
    return shlex.split(next(line for line in lines if line.startswith('ExecStart=')).removeprefix('ExecStart='))


def _unit_env(ignition: dict[str, Any]) -> dict[str, str]:
    argv = _unit_command(ignition)
    pairs = (argv[at + 1].split('=', 1) for at, arg in enumerate(argv) if arg == '--env')
    return dict(pairs)


def _bounded(verb: str, *args: str) -> list[str]:
    """`podman <verb>` of a container that ends and goes by itself, and is given no volume.

    `--timeout` has conmon end the container at `CONTAINER_TIMEOUT` and
    `--rm` removes it then. conmon runs under the container service, not
    under the test process, so this holds however the test process ended,
    an outright kill that runs no teardown included; until then the label
    lists it. A container created and never started is not reached: conmon
    counts from the start.

    Each of the image's `VOLUME`s is a tmpfs. Otherwise each would be an
    anonymous volume, which holds one of the host's podman locks and
    outlives a container `podman rm` removes without `--volumes`.
    """
    tmpfs = [f'--tmpfs={volume}' for volume in IMAGE_VOLUMES]
    return [verb, '--rm', '--timeout', str(CONTAINER_TIMEOUT), '--label', LABEL, *tmpfs, *args]


def _unit_container(ignition: dict[str, Any]) -> tuple[list[str], list[File]]:
    """The Postgres unit's `podman run` as a `podman create`, and what to copy in.

    Every argument is the unit's except these: `--replace` and the name,
    which the caller gives; the published port, which becomes a free one on
    the loopback interface; each volume a delivered file sits under, which
    becomes a copy of those files at the path the volume mounts them on; and
    the one volume nothing is delivered into, which is the box's own state and
    starts here empty, as the tmpfs `_bounded` mounts on the path the image's
    own `VOLUME` names. `_bounded`'s arguments are added.
    """
    argv = _unit_command(ignition)
    assert argv[:2] == ['/usr/bin/podman', 'run'], argv
    delivered = _delivered(ignition)
    created = _bounded('create')
    copies: list[File] = []
    rest = iter(argv[2:])
    for arg in rest:
        match arg:
            case '--replace':
                pass
            case '--name':
                _ = next(rest)
            case '--publish':
                _ = next(rest)
                created += ['--publish', f'{ADDRESS}::{settings.PORT}']
            case '--volume':
                host, inside, *_options = next(rest).split(':')
                under = [file for file in delivered if file.path.is_relative_to(host)]
                if not under:
                    assert PurePosixPath(inside) in IMAGE_VOLUMES, (inside, IMAGE_VOLUMES)
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
    """One running Postgres container, and the name its superuser goes by."""

    name: str
    port: int
    superuser: str

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


def _await_ready(name: str, *, timeout: float = COMMAND_TIMEOUT) -> None:
    """Follow the container's log until the serving Postgres says it is up.

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
        initialized = False
        for text in follow.stdout:
            seen.append(text)
            initialized = initialized or INIT_DONE in text
            if initialized and READY in text:
                return
        if follow.wait(timeout=COMMAND_TIMEOUT) == TIMED_OUT:
            raise sp.TimeoutExpired(follower, timeout, output=''.join(seen))
        raise AssertionError(f'{name} stopped before it served:\n{"".join(seen)}')


def _remove(name: str) -> None:
    """Remove a container these cases started, and any anonymous volume it mounted.

    `_bounded` gives a container none, and `--volumes` is the guard for one
    the image's `VOLUME`s would add if `IMAGE_VOLUMES` fell behind them: a
    container that `podman rm` removes keeps its anonymous volumes without
    it, `--rm` notwithstanding.
    """
    _ = process_sessions.run(['podman', 'rm', '--force', '--volumes', '--time', '0', name], timeout=COMMAND_TIMEOUT)


def _named(created: list[str], name: str) -> list[str]:
    """A `podman create` built without a name, given `name`."""
    return [*created[:1], '--name', name, *created[1:]]


def _client(name: str) -> list[str]:
    """The `podman run` of the container that serves the image's client tools."""
    return _bounded('run', '--detach', '--network', 'host', '--name', name, '--entrypoint', 'sleep', IMAGE, 'infinity')


def _superusers() -> list[str]:
    """The `podman create` of a box whose image superuser is `ci`, which is a client role's name."""
    return _bounded(
        'create',
        *('--publish', f'{ADDRESS}::{settings.PORT}'),
        *('--env', f'POSTGRES_DB={settings.DATABASE}'),
        *('--env', f'POSTGRES_USER={settings.CI_ROLE}'),
        *('--env', 'POSTGRES_HOST_AUTH_METHOD=trust'),
        IMAGE,
    )


Start = Callable[[list[str], list[File], str], Box]


@pytest.fixture(scope='module')
def start() -> Iterator[Start]:
    """Create, fill and start containers, and remove every one when the module is done."""
    names: list[str] = []

    def run(created: list[str], copies: list[File], superuser: str) -> Box:
        name = f'kluster-test-pgstate-{uuid.uuid4().hex[:12]}'
        names.append(name)
        _ = _podman(*_named(created, name))
        if copies:
            _ = _podman('cp', '--archive=false', '-', f'{name}:/', stdin=_archive(copies))
        _ = _podman('start', name)
        _await_ready(name)
        published = _podman('port', name, f'{settings.PORT}/tcp').decode().split()[0]
        return Box(name=name, port=int(published.rsplit(':', 1)[1]), superuser=superuser)

    yield run
    for name in names:
        _remove(name)


@pytest.fixture(scope='module')
def roots() -> config.Roots:
    return config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT,))


@pytest.fixture(scope='module')
def ignition(roots: config.Roots) -> dict[str, Any]:
    built = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')
    return json.loads(render.render_ignition(built))


def _appliance(start: Start, ignition: dict[str, Any]) -> Box:
    """The appliance's Postgres, as its Ignition builds it."""
    created, copies = _unit_container(ignition)
    return start(created, copies, _unit_env(ignition)['POSTGRES_USER'])


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
    inside as out.
    """
    directory = tmp_path_factory.mktemp('clients')
    name = f'kluster-test-pgclient-{uuid.uuid4().hex[:12]}'
    _ = _podman(*_client(name))
    try:
        _ = _podman('exec', name, 'mkdir', '-p', str(directory))
        for tool in ('psql', state.PG_RESTORE):
            script = directory / tool
            _ = script.write_text(
                '#!/bin/sh\n'
                f'podman cp {shlex.quote(f"{directory}/.")} {shlex.quote(f"{name}:{directory}")} || exit 1\n'
                f'exec podman exec --interactive --env PGSSLROOTCERT --env PGSSLCERT --env PGSSLKEY {name} {tool} "$@"\n'
            )
            script.chmod(0o755)
        yield directory
    finally:
        _remove(name)


@pytest.fixture
def clients(tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where a case keeps its bundles and archives, with `state` and `pulumi` pointed there.

    `state` runs the image's `pg_restore`. `pulumi` gets a home of its own,
    and none of the operator's `PGSSL*` variables: a case names its bundle's
    files or connects without TLS, never through what the operator's shell
    happened to hold. Each command either one starts is bounded by
    `COMMAND_TIMEOUT` rather than by its runner's own bound, which is set for
    a transfer over the network and, for `state`'s, sits above the run's.
    """
    monkeypatch.setattr(state, 'PG_RESTORE', str(tools / state.PG_RESTORE))
    monkeypatch.setattr(pulumi_cli, 'TIMEOUT', COMMAND_TIMEOUT)
    monkeypatch.setattr(state, 'LISTING_TIMEOUT', COMMAND_TIMEOUT)
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
    _ = pulumi_config.run_pulumi(
        ['stack', 'init', stack],
        cwd=project,
        env={
            pulumi_config.BACKEND_URL_ENV: target.url,
            pulumi_config.PASSPHRASE_ENV: fake(pulumi_config.PASSPHRASE_ENV),
            **target.env,
            **(env or {}),
        },
        stdin=None,
    )


@pytest.mark.parametrize('container', ['appliance', 'superusers', 'client'])
def test_every_container_ends_by_itself_and_mounts_no_volume(ignition: dict[str, Any], container: str) -> None:
    """Each container these cases start is bounded, removed when it ends, labelled, and given no volume.

    Read from the container podman made of the arguments rather than from
    the arguments: what podman does with them is what leaves a lock or a
    volume behind.
    """
    name = f'kluster-test-volumes-{uuid.uuid4().hex[:12]}'
    argv = {
        'appliance': lambda: _named(_unit_container(ignition)[0], name),
        'superusers': lambda: _named(_superusers(), name),
        'client': lambda: _client(name),
    }[container]()
    try:
        _ = _podman(*argv)
        made = json.loads(_podman('container', 'inspect', name))[0]
    finally:
        _remove(name)
    declared: dict[str, Any] = json.loads(_podman('image', 'inspect', IMAGE))[0]['Config'].get('Volumes') or {}

    # The premise: the image declares volumes, so a container given none is
    # one whose tmpfs covers each, and `IMAGE_VOLUMES` names them all.
    assert sorted(declared) == sorted(str(volume) for volume in IMAGE_VOLUMES)
    assert [mount['Destination'] for mount in made['Mounts'] if mount['Type'] == 'volume'] == []
    assert made['Config']['Timeout'] == CONTAINER_TIMEOUT
    assert made['HostConfig']['AutoRemove'] is True
    assert LABEL in {f'{key}={value}' for key, value in made['Config']['Labels'].items()}


def test_a_box_silent_before_it_serves_fails_the_wait_naming_the_follower() -> None:
    """A container that stays up and prints nothing ends the readiness wait at the follower's bound.

    The client container is that container: it sleeps and never writes its
    log. The bound is the event here, since nothing else ends the follow.
    """
    name = f'kluster-test-silent-{uuid.uuid4().hex[:12]}'
    try:
        _ = _podman(*_client(name))
        with pytest.raises(sp.TimeoutExpired) as stalled:
            _await_ready(name, timeout=1)
    finally:
        _remove(name)

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
    superuser is a role that exists. What refuses it is that no line of
    `pg_hba.conf` admits that role over TCP.
    """
    monkeypatch.setattr(pki, 'CLIENT_NAMES', (*pki.CLIENT_NAMES, box.superuser))
    target = _bundle(roots, box, clients, box.superuser)

    with pytest.raises(state.StateError, match=re.escape('no pg_hba.conf entry')):
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
