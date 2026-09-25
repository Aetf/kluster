"""The appliance's Postgres roles, run in the image the box runs.

The rendered Ignition's own files -- `pg_hba.conf`, the init script, the TLS
material -- go into the pinned image under the Postgres unit's own `podman
run` arguments. What changes is only what has to: the address the server
certificate names, the port it is published on, the delivered directories,
which are copied into the container rather than mounted from a host that does
not have them, and a data directory that starts empty and goes away with the
container. So what is exercised is the image's own initialization mechanism
(`/docker-entrypoint-initdb.d`), the server's own authentication, and real
clients: `pulumi` from this workstation, and the Postgres client tools from the
same image, since a workstation need not carry them.

Nothing here mounts a host path. A remote `podman` -- one whose service runs
in another mount namespace than this process -- sees none of this process's
temporary files, and `podman cp` is the channel that reaches it either way.

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

import pytest
from root_credentials import fake

from kluster.scripts.credentials import pki, pulumi_config
from kluster.scripts.state_backend import config, settings, state

ADDRESS = '127.0.0.1'
IMAGE = settings.POSTGRES_IMAGE
CLIENT_ROLES = (settings.CI_ROLE, settings.OPERATOR_ROLE)

#: Stop-losses for a subprocess that never answers, not budgets: every call
#: here is local, and the slowest is a container's first start.
PODMAN_TIMEOUT = 120

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
DUMP_SCRIPT = config.DEPLOY_DIR / config.DUMP_SCRIPT

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


def _image_is_local() -> bool:
    if shutil.which('podman') is None:
        return False
    found = sp.run(['podman', 'image', 'exists', IMAGE], capture_output=True, timeout=PODMAN_TIMEOUT, check=False)
    return found.returncode == 0


pytestmark = [
    pytest.mark.skipif(shutil.which('butane') is None, reason='butane is not on PATH (mise x -- ...)'),
    pytest.mark.skipif(shutil.which('pulumi') is None, reason='pulumi is not on PATH (mise x -- ...)'),
    pytest.mark.skipif(not _image_is_local(), reason=f'podman, or a local copy of {IMAGE}, is missing'),
]


def _podman(*args: str, stdin: bytes | None = None) -> bytes:
    done = sp.run(['podman', *args], input=stdin, capture_output=True, timeout=PODMAN_TIMEOUT, check=False)
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
    return {key: value for key, value in pairs}


def _unit_container(ignition: dict[str, Any]) -> tuple[list[str], list[File]]:
    """The Postgres unit's `podman run` as a `podman create`, and what to copy in.

    Every argument is the unit's except these: `--replace` and the name,
    which the caller gives; the published port, which becomes a free one on
    the loopback interface; each volume a delivered file sits under, which
    becomes a copy of those files at the path the volume mounts them on; and
    the one volume nothing is delivered into, which is the box's own state and
    starts here as an anonymous volume that `--rm` removes with the container.
    """
    argv = _unit_command(ignition)
    assert argv[:2] == ['/usr/bin/podman', 'run'], argv
    delivered = _delivered(ignition)
    created = ['create', '--rm']
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
                    created += ['--volume', inside]
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
        done = sp.run(
            ['podman', 'exec', self.name, 'psql', '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-At']
            + ['-U', self.superuser, '-d', settings.DATABASE, '-c', sql],
            capture_output=True,
            text=True,
            timeout=PODMAN_TIMEOUT,
            check=False,
        )
        if done.returncode != 0:
            raise state.StateError(done.stderr.strip())
        return done.stdout.strip()


def _await_ready(name: str) -> None:
    """Follow the container's log until the serving Postgres says it is up.

    The log is the event: the entrypoint prints each stage as it reaches it,
    and a container that dies ends the stream, which ends the wait with the
    log in hand rather than with a timeout.
    """
    seen: list[str] = []
    with sp.Popen(['podman', 'logs', '--follow', name], stdout=sp.PIPE, stderr=sp.STDOUT, text=True) as follow:
        assert follow.stdout is not None
        initialized = False
        for text in follow.stdout:
            seen.append(text)
            initialized = initialized or INIT_DONE in text
            if initialized and READY in text:
                follow.terminate()
                return
    raise AssertionError(f'{name} stopped before it served:\n{"".join(seen)}')


Start = Callable[[list[str], list[File], str], Box]


@pytest.fixture(scope='module')
def start() -> Iterator[Start]:
    """Create, fill and start containers, and remove every one when the module is done."""
    names: list[str] = []

    def run(created: list[str], copies: list[File], superuser: str) -> Box:
        name = f'kluster-test-pgstate-{uuid.uuid4().hex[:12]}'
        names.append(name)
        _ = _podman(*created[:1], '--name', name, *created[1:])
        if copies:
            _ = _podman('cp', '--archive=false', '-', f'{name}:/', stdin=_archive(copies))
        _ = _podman('start', name)
        _await_ready(name)
        published = _podman('port', name, f'{settings.PORT}/tcp').decode().split()[0]
        return Box(name=name, port=int(published.rsplit(':', 1)[1]), superuser=superuser)

    yield run
    for name in names:
        _ = sp.run(['podman', 'rm', '--force', '--time', '0', name], capture_output=True, timeout=PODMAN_TIMEOUT)


@pytest.fixture(scope='module')
def roots() -> config.Roots:
    return config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT,))


@pytest.fixture(scope='module')
def ignition(roots: config.Roots) -> dict[str, Any]:
    built = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')
    return json.loads(config.render_ignition(built))


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
    _ = _podman(
        'run', '--detach', '--rm', '--network', 'host', '--name', name, '--entrypoint', 'sleep', IMAGE, 'infinity'
    )
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
        _ = sp.run(['podman', 'rm', '--force', '--time', '0', name], capture_output=True, timeout=PODMAN_TIMEOUT)


@pytest.fixture
def clients(tools: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where a case keeps its bundles and archives, with `state` and `pulumi` pointed there.

    `state` runs the image's `pg_restore`. `pulumi` gets a home of its own,
    and none of the operator's `PGSSL*` variables: a case names its bundle's
    files or connects without TLS, never through what the operator's shell
    happened to hold.
    """
    monkeypatch.setattr(state, 'PG_RESTORE', str(tools / state.PG_RESTORE))
    monkeypatch.setenv('PULUMI_HOME', str(tmp_path / 'pulumi-home'))
    monkeypatch.setenv('PULUMI_SKIP_UPDATE_CHECK', 'true')
    for variable in (config.CA_ENV, config.CERT_ENV, config.KEY_ENV):
        monkeypatch.delenv(variable, raising=False)
    return tools


def _bundle(roots: config.Roots, box: Box, clients: Path, role: str) -> state.Connection:
    """A client bundle for `role`, written the way `state-backend bundle` writes one."""
    bundle = config.client_bundle(roots.ca, name=role, address=ADDRESS)
    directory = clients / role
    config.write_client_bundle(bundle, directory)
    url = bundle.url().replace(f'@{ADDRESS}:{settings.PORT}/', f'@{ADDRESS}:{box.port}/')
    return state.Connection(url=url, env=config.ssl_env(directory))


def _psql(clients: Path, target: state.Connection, sql: str) -> str:
    done = sp.run(
        [str(clients / 'psql'), '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-At', f'--dbname={target.url}', '-c', sql],
        env={**os.environ, **target.env},
        capture_output=True,
        text=True,
        timeout=PODMAN_TIMEOUT,
        check=False,
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

    with pytest.raises(state.StateError, match='no pg_hba.conf entry'):
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
        ['create', '--rm', '--publish', f'{ADDRESS}::{settings.PORT}']
        + ['--env', f'POSTGRES_DB={settings.DATABASE}', '--env', f'POSTGRES_USER={settings.CI_ROLE}']
        + ['--env', 'POSTGRES_HOST_AUTH_METHOD=trust', IMAGE],
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
    listing = sp.run(
        [state.PG_RESTORE, '--list', str(archive)], capture_output=True, text=True, timeout=PODMAN_TIMEOUT, check=True
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
    counted = sp.run(
        [str(DUMP_SCRIPT), 'count-stacks'],
        input=printed,
        env={'PATH': os.environ['PATH'], 'PG_DATABASE': env['PG_DATABASE']},
        capture_output=True,
        timeout=PODMAN_TIMEOUT,
        check=False,
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
