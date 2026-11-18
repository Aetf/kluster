"""`state-backend release-lock` against a scratch Postgres, over locks the pinned `pulumi` left.

Each lock here is the real thing: a `pulumi up` run as a role that may write
the state table but may not delete from it. That is how a backend that stops
answering at the end of a run leaves one -- `pulumi` cannot delete its lock,
says so, and exits as the update made it -- so the value the command reads is
the one the pinned CLI writes, and the pid and start time it is held to are
the ones the case recorded of the `pulumi` it started. The command is run the way a deploy's `Up` step runs it, through
`cli.main`, over a client bundle whose connection string reaches the box.

The box is the image the appliance pins, with nothing in front of it: what is
held here is the lock's format and the proof read from it, and the role the
command connects as is the image's superuser. How the appliance's own roles
and pooler carry a `psql` statement is `tests/test_state_roles.py`'s.

Skipped where the image is not local, or `pulumi`, `psql` or `podman` is not on
`PATH`, with the reason named.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import socket
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import host_ports
import process_sessions
import pytest
from scratch_projects import PASSPHRASE, cli_directories, scrubbed

from kluster.lib import bundle, stack_environment
from kluster.lib.state_backend import settings, state, table
from kluster.scripts.state_backend import cli, locks

IMAGE = settings.POSTGRES_IMAGE
ADDRESS = '127.0.0.1'

#: The role a case's `pulumi` writes the state as, which may not delete.
WRITER = 'writer'

#: The bound on each command a case starts: a stop-loss, which nothing
#: asserts on. Every one is local.
COMMAND_TIMEOUT = 60
#: The bound on a case, the module's container start included.
CASE_TIMEOUT = 240
#: How long the module's container may run: the run's outer bound.
CONTAINER_TIMEOUT = 1200
#: How often the start reads whether the server answers. The answer decides;
#: `COMMAND_TIMEOUT` is the stop-loss.
POLL_INTERVAL = 0.2

#: The label the module's container carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=<module>`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: What `pulumi` prints when its delete of its own lock failed (`Unlock` in
#: `pkg/backend/diy/lock.go` at the pinned CLI).
UNLOCK_FAILED = 'there was a problem deleting the lock'

pytestmark = [
    pytest.mark.skipif(shutil.which('pulumi') is None, reason='pulumi is not on PATH (mise x -- ...)'),
    pytest.mark.skipif(shutil.which(table.PSQL) is None, reason='psql is not on PATH (mise x -- ...)'),
    pytest.mark.timeout(CASE_TIMEOUT),
]


@dataclass(frozen=True)
class Box:
    """The scratch Postgres: its container, and the host port it is published on."""

    name: str
    port: int

    def url(self, role: str) -> str:
        return f'postgres://{role}@{ADDRESS}:{self.port}/{settings.DATABASE}?sslmode=disable'

    def sql(self, statement: str) -> str:
        """`statement` as the superuser, its rows printed unaligned."""
        done = process_sessions.run(
            [table.PSQL, '--no-psqlrc', '-v', 'ON_ERROR_STOP=1', '-At', f'--dbname={self.url("postgres")}'],
            input=statement,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        if done.returncode != 0:
            raise state.StateError(done.stderr.strip())
        return done.stdout.strip()

    def state_keys(self) -> set[str]:
        return set(self.sql(f'SELECT key FROM {state.STATE_TABLE}').splitlines())


def _podman(*args: str) -> str:
    done = process_sessions.run(['podman', *args], text=True, timeout=COMMAND_TIMEOUT)
    if done.returncode != 0:
        raise AssertionError(f'podman {" ".join(args[:2])} failed: {done.stderr.strip()}')
    return done.stdout


def _answers(box: Box) -> bool:
    try:
        _ = box.sql('SELECT 1')
    except state.StateError:
        return False
    return True


@pytest.fixture(scope='module')
def box() -> Iterator[Box]:
    """The pinned image, serving on a port this module reserved, with a role that writes the state and cannot delete it.

    Once started, `--timeout` has conmon end the container at
    `CONTAINER_TIMEOUT` and `--rm` removes it then, however this process
    ended. One a `podman run` killed before the start leaves `Created` is
    removed by name in the teardown, armed before the `podman run`
    (`process_sessions.removing`), which a test process killed outright
    never runs. The data directory, the
    image's one `VOLUME`, is a tmpfs, so the container has no anonymous
    volume to leave. `podman run` creates and starts it in one command, so
    the port it publishes is bound before the reservation is let go: the
    port is one `host_ports` reserved, never one podman picks
    (framework/testing.md §1.2).
    """
    if shutil.which('podman') is None:
        pytest.skip('podman is not on PATH')
    if process_sessions.run(['podman', 'image', 'exists', IMAGE], timeout=COMMAND_TIMEOUT).returncode != 0:
        pytest.skip(f'there is no local copy of {IMAGE}')
    name = f'kluster-test-release-lock-{uuid.uuid4().hex[:12]}'
    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        with host_ports.reserved(ADDRESS) as port:
            _ = _podman(
                'run',
                '--detach',
                '--rm',
                '--timeout',
                str(CONTAINER_TIMEOUT),
                '--label',
                LABEL,
                '--name',
                name,
                '--tmpfs=/var/lib/postgresql/data',
                '--network',
                'pasta',
                '--publish',
                f'{ADDRESS}:{port}:{settings.PORT}',
                '--env',
                f'POSTGRES_DB={settings.DATABASE}',
                '--env',
                'POSTGRES_HOST_AUTH_METHOD=trust',
                IMAGE,
            )
        found = Box(name=name, port=port)
        # The entrypoint's initialization runs a server that listens on its
        # socket alone, so an answer over TCP is the serving one.
        stop_loss = time.monotonic() + COMMAND_TIMEOUT
        while not _answers(found):
            assert time.monotonic() < stop_loss, f'{name} did not answer within {COMMAND_TIMEOUT}s'
            time.sleep(POLL_INTERVAL)
        _ = found.sql(f'CREATE ROLE {WRITER} LOGIN; GRANT CREATE ON SCHEMA public TO {WRITER}')
        yield found


@dataclass(frozen=True)
class Left:
    """A lock `pulumi` left, and what a step would have recorded of the `pulumi` that took it.

    `started` is the time read before that `pulumi` started, in whole
    seconds, as the step reads it; `taken` is the time the lock names, read
    off the stored value here rather than through the code under test.
    """

    stack: str
    pid: int
    started: int
    key: str
    taken: dt.datetime


def _leave_lock(box: Box, root: Path) -> Left:
    """Run `pulumi up` on a stack of its own as `WRITER`, which leaves the run's lock behind.

    The revoke between the stack's creation and its update is what makes the
    lock's delete the one statement of the update that fails.
    """
    stack = f'release-{uuid.uuid4().hex[:8]}'
    project = root / stack
    project.mkdir(parents=True)
    _ = (project / 'Pulumi.yaml').write_text('name: probe\nruntime: yaml\n')
    env = (
        scrubbed(os.environ)
        | cli_directories(root / f'{stack}-cli')
        | stack_environment.state_switches()
        | {
            'PULUMI_BACKEND_URL': bundle.pulumi_url(box.url(WRITER)),
            'PULUMI_CONFIG_PASSPHRASE': PASSPHRASE,
            'PULUMI_SKIP_UPDATE_CHECK': 'true',
        }
    )
    pulumi = shutil.which('pulumi')
    assert pulumi is not None
    # `stack init` takes the stack's lock too, so it runs while the role may
    # still delete; the table is the role's once its first `pulumi` has
    # created it.
    _ = box.sql(f'GRANT DELETE ON ALL TABLES IN SCHEMA public TO {WRITER}')
    _ = process_sessions.run(
        [pulumi, 'stack', 'init', stack], cwd=project, env=env, timeout=COMMAND_TIMEOUT, check=True
    )
    _ = box.sql(f'REVOKE DELETE ON {state.STATE_TABLE} FROM {WRITER}')
    started = int(time.time())
    with process_sessions.started(
        [pulumi, 'up', '--yes', '--skip-preview', '--stack', stack],
        cwd=project,
        env=env,
        stdout=process_sessions.PIPE,
        stderr=process_sessions.STDOUT,
        text=True,
    ) as up:
        output, _ = up.communicate(timeout=COMMAND_TIMEOUT)
        pid = up.pid
    # The behavior the step is built on: a run whose own delete failed says
    # so and exits 0, the status its update earned.
    assert up.returncode == 0, output
    assert UNLOCK_FAILED in output, output
    held = [
        key for key in box.state_keys() if key.startswith(f'{settings.DATABASE}/{locks.LOCKS}') and f'/{stack}/' in key
    ]
    assert len(held) == 1, held
    taken = box.sql(
        f"SELECT convert_from(decode(data->>'data', 'base64'), 'UTF8')::json->>'timestamp' "
        f"FROM {state.STATE_TABLE} WHERE key = '{held[0]}'"
    )
    return Left(stack=stack, pid=pid, started=started, key=held[0], taken=dt.datetime.fromisoformat(taken))


@pytest.fixture
def release(box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The bundle `release-lock` reads the box over, and each of its `psql` statements bounded below the case."""
    directory = tmp_path / 'bundle'
    directory.mkdir()
    _ = (directory / bundle.URL_FILE).write_text(f'{box.url("postgres")}\n')
    monkeypatch.setattr(table, 'LISTING_TIMEOUT', COMMAND_TIMEOUT)
    return directory


def _start_file(path: Path, pid: int, started: int) -> Path:
    """The file a step writes before its `pulumi` starts: the pid, then the time in seconds."""
    _ = path.write_text(f'{pid} {started}\n')
    return path


def _run(bundle_dir: Path, stack: str, start_file: Path) -> int:
    return cli.main(['release-lock', '--stack', stack, '--start-file', str(start_file), '--bundle', str(bundle_dir)])


#: An image the store does not hold. A `podman create` of it with
#: `--pull=never` fails before it creates a container.
ABSENT_IMAGE = 'localhost/kluster-test-absent:none'


@pytest.mark.parametrize(
    ('named', 'refusal'),
    [(False, 'names no container'), (True, 'before the teardown that removes it is armed')],
    ids=['no-name', 'unarmed'],
)
def test_a_container_no_armed_teardown_removes_by_name_is_refused_before_podman_runs(named: bool, refusal: str) -> None:
    """A `podman create` this module issues with no name, or with one no `removing` block is armed to remove, fails before podman runs.

    The module's containers are started through `_podman`, so this is the
    path the box's own create takes. The image is one the store does not
    hold, so a create that reached podman fails there, naming podman rather
    than the refusal, and leaves no container behind.
    """
    name = ['--name', f'kluster-test-unarmed-{uuid.uuid4().hex[:12]}'] if named else []
    with pytest.raises(AssertionError, match=refusal):
        _ = _podman('create', '--pull=never', '--label', LABEL, *name, ABSENT_IMAGE)


def test_the_lock_the_runs_own_pulumi_left_is_deleted_and_nothing_else(box: Box, release: Path, tmp_path: Path) -> None:
    """Beside it, another stack's lock, and one another process holds on the same stack.

    The second is the row a `pulumi` whose `Lock` raced the run's would hold
    until it read the directory again: the value another process's `pulumi`
    wrote, under the run's stack.
    """
    mine, theirs = _leave_lock(box, tmp_path), _leave_lock(box, tmp_path)
    racing = f'{mine.key.rsplit("/", 1)[0]}/{uuid.uuid4()}.json'
    _ = box.sql(
        f"INSERT INTO {state.STATE_TABLE} (key, data) SELECT '{racing}', data FROM {state.STATE_TABLE} "
        f"WHERE key = '{theirs.key}'"
    )
    before = box.state_keys()

    assert _run(release, mine.stack, _start_file(tmp_path / 'start', mine.pid, mine.started)) == 0

    assert box.state_keys() == before - {mine.key}


def test_a_lock_another_process_took_on_the_stack_is_left(box: Box, release: Path, tmp_path: Path) -> None:
    theirs, other = _leave_lock(box, tmp_path), _leave_lock(box, tmp_path)
    before = box.state_keys()

    assert _run(release, theirs.stack, _start_file(tmp_path / 'start', other.pid, theirs.started)) == 0

    assert box.state_keys() == before


def test_a_lock_naming_the_pid_on_another_machine_is_left(box: Box, release: Path, tmp_path: Path) -> None:
    left = _leave_lock(box, tmp_path)
    before = box.state_keys()

    status = locks.release(
        state.connection(release),
        stack=left.stack,
        start=_start(left),
        hostname=f'not-{socket.gethostname()}',
        wait=0,
    )

    assert status == 0
    assert box.state_keys() == before


def test_a_lock_taken_before_the_step_started_is_left(box: Box, release: Path, tmp_path: Path) -> None:
    """An earlier run's lock, on a machine that shared this one's hostname and drew the same pid."""
    left = _leave_lock(box, tmp_path)
    before = box.state_keys()

    after = math.floor(left.taken.timestamp()) + 1
    assert _run(release, left.stack, _start_file(tmp_path / 'start', left.pid, after)) == 0

    assert box.state_keys() == before


def test_a_step_that_recorded_no_start_proves_no_lock(box: Box, release: Path, tmp_path: Path) -> None:
    left = _leave_lock(box, tmp_path)
    before = box.state_keys()

    assert _run(release, left.stack, tmp_path / 'never-written') == 0

    assert box.state_keys() == before


def _start(left: Left) -> locks.Start:
    """What the step that ran `left`'s `pulumi` recorded."""
    return locks.Start(pid=left.pid, at=dt.datetime.fromtimestamp(left.started, tz=dt.UTC))


class Clock:
    """A monotonic clock that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_backend_that_answers_after_refusing_has_the_lock_deleted(
    box: Box, release: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = _leave_lock(box, tmp_path)
    answered = locks.listing
    refusals = [state.StateError('the database system is not yet accepting connections')]

    def refusing_once(target: state.Connection) -> list[tuple[str, str]]:
        if refusals:
            raise refusals.pop()
        return answered(target)

    monkeypatch.setattr(locks, 'listing', refusing_once)
    clock = Clock()

    status = locks.release(
        state.connection(release),
        stack=left.stack,
        start=_start(left),
        hostname=socket.gethostname(),
        clock=clock,
        sleep=clock.sleep,
    )

    assert status == 0
    assert left.key not in box.state_keys()


def test_a_backend_that_never_answers_fails_once_the_wait_is_spent(tmp_path: Path) -> None:
    with socket.socket() as bound:
        # Bound and never listening: every connection to it is refused.
        bound.bind((ADDRESS, 0))
        port = bound.getsockname()[1]
        target = state.Connection(url=f'postgres://postgres@{ADDRESS}:{port}/{settings.DATABASE}', env={})
        clock = Clock()

        status = locks.release(
            target,
            stack='any',
            start=locks.Start(pid=1, at=dt.datetime.now(dt.UTC)),
            hostname=socket.gethostname(),
            wait=60,
            clock=clock,
            sleep=clock.sleep,
        )

    assert status == 1
    assert clock.now >= 60
