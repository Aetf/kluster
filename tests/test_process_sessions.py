"""Nothing a command started through `process_sessions` starts outlives the call, however the call ends.

Each case plants a process tree of the shape the `pulumi` CLI makes: a
leader whose child moves into a process group of its own and blocks opening
a FIFO nobody writes, so that only a kill ends it. The child writes its pid
and its start time before it blocks, and the case reads that line as the
event that it is there; while it is blocked it cannot exit, so the case pins
it as a `psutil.Process` at that moment. Once the helper has returned the
case asserts it is no longer running. No sleep is involved: the helper's exit
has already waited for every member, so the answer is settled when the
assertion runs. Where a case cannot pin the process while it is alive -- a
`run` call, a child `pytest` -- it holds the pid and start time instead,
which name one process for as long as the host is up.

A run that goes red leaves nothing behind either, and signals nothing to
get there. Every planted process blocks opening the case's FIFO, and the
fixture that made it opens it for writing once as the case ends, which lets
each of them read EOF and exit by itself; what a case pinned it also kills
through the pin. Nothing is chosen by name or pattern.

The last case holds the premise all of it rests on: that the `pulumi` CLI's
plugins stay in its session (framework/testing.md §1.2).
"""

from __future__ import annotations

import errno
import json
import os
import selectors
import shutil
import signal
import subprocess as sp
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import process_sessions
import psutil
import pytest
from scratch_projects import scratch_project

TESTS = Path(__file__).parent

#: How long a planted leader that ends by itself may take to end: a
#: stop-loss, which nothing asserts on.
WAIT = 30

#: The planted child: it says who it is, then blocks opening a FIFO that
#: nobody writes, which only a kill ends.
CHILD = """\
import os, pathlib, sys
stat = pathlib.Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
print(os.getpid(), stat[19], flush=True)
open(sys.argv[1]).read()
"""

#: The planted leader: it starts `CHILD` in a process group of the child's
#: own, as `pulumi` starts a plugin, with none of the leader's output open,
#: hands the child's line on once the child has written it, and then exits
#: (`exit`) or blocks on the same FIFO (`block`).
LEADER = f"""\
import subprocess, sys
fifo, then = sys.argv[1], sys.argv[2]
child = subprocess.Popen([sys.executable, '-c', {CHILD!r}, fifo], process_group=0, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
print(child.stdout.readline(), end='', flush=True)
if then == 'block':
    open(fifo).read()
"""


@dataclass(frozen=True)
class Identity:
    """A process as its pid and start time, which name it for as long as the host is up."""

    pid: int
    starttime: str

    @classmethod
    def parse(cls, line: str) -> Identity:
        pid, starttime = line.split()
        return cls(int(pid), starttime)

    def running(self) -> bool:
        """Whether the process is still there, a zombie included."""
        return _starttime(self.pid) == self.starttime


def _starttime(pid: int) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


class Planted:
    """What a case planted, pinned while it is alive, and killed through those pins if the case goes red."""

    def __init__(self) -> None:
        self._pinned: list[psutil.Process] = []
        self._held: list[Identity] = []

    def pin(self, identity: Identity) -> psutil.Process:
        """Pin a process that is alive and blocked, and so cannot have handed its pid on."""
        process = psutil.Process(identity.pid)
        assert identity.running(), f'{identity} was gone before it was pinned'
        self._pinned.append(process)
        return process

    def hold(self, identity: Identity) -> Identity:
        """Keep a process the case could not pin, to be killed by its identity if it is still there."""
        self._held.append(identity)
        return identity

    def kill(self) -> None:
        for process in self._pinned:
            with suppress(psutil.NoSuchProcess):
                process.kill()
        for identity in self._held:
            if identity.running():
                with suppress(psutil.NoSuchProcess):
                    process = psutil.Process(identity.pid)
                    if identity.running():
                        process.kill()


@pytest.fixture
def planted() -> Iterator[Planted]:
    made = Planted()
    yield made
    made.kill()


@pytest.fixture
def fifo(tmp_path: Path) -> Iterator[str]:
    """A FIFO nobody writes while the case runs, released as it ends."""
    path = tmp_path / 'never-written'
    os.mkfifo(path)
    yield str(path)
    _release(path)


def _release(path: Path) -> None:
    """Open `path` for writing once: each process blocked opening it goes on, and reads EOF."""
    try:
        written = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as error:
        if error.errno == errno.ENXIO:  # nobody is blocked opening it
            return
        raise
    os.close(written)


def _leader(fifo: str, then: str) -> list[str]:
    return [sys.executable, '-c', LEADER, fifo, then]


def _report(command: process_sessions.Command) -> Identity:
    """The planted child's line, read from the leader's output as the event that the child is there."""
    assert command.stdout is not None
    line = cast('str', command.stdout.readline())
    assert line, 'the planted leader ended before its child reported'
    return Identity.parse(line)


def test_a_member_left_by_a_clean_exit_is_gone(fifo: str, planted: Planted) -> None:
    with process_sessions.started(_leader(fifo, 'exit'), stdout=process_sessions.PIPE, text=True) as command:
        child = planted.pin(_report(command))
        assert command.wait(timeout=WAIT) == 0
        assert child.is_running(), 'the child must outlive its leader for the end to have anything to do'

    assert not child.is_running()


def test_a_member_left_by_a_clean_exit_is_gone_when_run_returns(fifo: str, planted: Planted) -> None:
    done = process_sessions.run(_leader(fifo, 'exit'), timeout=WAIT, text=True)

    assert done.returncode == 0, done.stderr
    assert not planted.hold(Identity.parse(cast('str', done.stdout))).running()


def test_a_member_is_gone_after_a_timeout_and_the_timeout_names_what_was_killed(fifo: str, planted: Planted) -> None:
    child: psutil.Process | None = None
    with (
        pytest.raises(sp.TimeoutExpired) as expired,
        process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as command,
    ):
        child = planted.pin(_report(command))
        _ = command.wait(timeout=0)

    assert child is not None
    assert not child.is_running()
    assert expired.value.cmd == _leader(fifo, 'block')
    (note,) = expired.value.__notes__
    assert f'POSIX session {command.pid}' in note
    assert f'{child.pid} {sys.executable} -c' in note


def test_a_command_still_running_at_its_bound_raises_naming_it(fifo: str) -> None:
    with pytest.raises(sp.TimeoutExpired) as expired:
        _ = process_sessions.run(_leader(fifo, 'block'), timeout=0)

    assert expired.value.cmd == _leader(fifo, 'block')


def test_a_child_blocked_on_a_fifo_under_a_shell_is_gone_after_the_shells_timeout(fifo: str, planted: Planted) -> None:
    cmp: psutil.Process | None = None
    with (
        pytest.raises(sp.TimeoutExpired),
        process_sessions.started(
            ['bash', '-c', f'cmp -s {fifo} /dev/null & echo "$!"; wait'], stdout=process_sessions.PIPE, text=True
        ) as command,
    ):
        assert command.stdout is not None
        pid = int(cast('str', command.stdout.readline()))
        starttime = _starttime(pid)
        assert starttime is not None, 'cmp was gone before it was pinned'
        cmp = planted.pin(Identity(pid, starttime))
        _ = command.wait(timeout=0)

    assert cmp is not None
    assert not cmp.is_running()


def test_a_member_is_gone_after_an_assertion_in_the_block(fifo: str, planted: Planted) -> None:
    child: psutil.Process | None = None
    with (
        pytest.raises(AssertionError, match='the case failed'),
        process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as command,
    ):
        child = planted.pin(_report(command))
        raise AssertionError('the case failed')

    assert child is not None
    assert not child.is_running()


def test_a_member_is_gone_after_a_keyboard_interrupt_in_the_block(fifo: str, planted: Planted) -> None:
    child: psutil.Process | None = None
    with (
        pytest.raises(KeyboardInterrupt),
        process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as command,
    ):
        child = planted.pin(_report(command))
        raise KeyboardInterrupt

    assert child is not None
    assert not child.is_running()


def test_a_leader_the_end_killed_reads_as_killed(fifo: str, planted: Planted) -> None:
    # Its status comes from its own `Popen`: `psutil` reaping it would leave
    # the `Popen` reading 0, a killed command that looks like a clean exit.
    with process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as command:
        child = planted.pin(_report(command))

    assert command.returncode == -signal.SIGKILL
    assert not child.is_running()


#: A command that says what its standard input is.
STDIN = 'import os; print(os.readlink("/proc/self/fd/0"))'


def test_a_command_reads_dev_null_and_never_this_processs_stdin() -> None:
    # pytest's capture and an xdist worker already hold /dev/null on fd 0,
    # so the case puts a pipe there for the length of the calls.
    read, write = os.pipe()
    saved = os.dup(0)
    os.dup2(read, 0)
    try:
        ran = process_sessions.run([sys.executable, '-c', STDIN], timeout=WAIT, text=True)
        with process_sessions.started(
            [sys.executable, '-c', STDIN], stdout=process_sessions.PIPE, text=True
        ) as command:
            output, _ = command.communicate(timeout=WAIT)
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read, write):
            os.close(fd)

    assert (ran.stdout.strip(), output.strip()) == ('/dev/null', '/dev/null')


def test_a_write_left_unflushed_to_the_killed_command_does_not_replace_the_blocks_failure(
    fifo: str, planted: Planted
) -> None:
    child: psutil.Process | None = None
    with (
        pytest.raises(AssertionError, match='the case failed'),
        process_sessions.started(
            _leader(fifo, 'block'), stdin=process_sessions.PIPE, stdout=process_sessions.PIPE, text=True
        ) as command,
    ):
        child = planted.pin(_report(command))
        assert command.stdin is not None
        _ = command.stdin.write('buffered, and never flushed while the command lives')
        raise AssertionError('the case failed')

    assert child is not None
    assert not child.is_running()


def test_a_member_this_process_may_not_signal_does_not_stop_the_round(
    fifo: str, planted: Planted, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The leader is the first member a scan meets, its pid being the lowest:
    # refused, it must not keep the kill from the rest, and the end names it.
    # The child dies all the same, and stays a zombie for as long as its
    # parent, the refused leader, lives to not reap it.
    send = psutil.Process.send_signal
    refused: list[int] = []

    def refusing(process: psutil.Process, sig: int) -> None:
        if process.pid == leader:
            refused.append(process.pid)
            raise psutil.AccessDenied(process.pid)
        send(process, sig)

    monkeypatch.setattr(psutil.Process, 'send_signal', refusing)
    monkeypatch.setattr(process_sessions, 'KILL_TIMEOUT', 1)
    leader = -1
    child: psutil.Process | None = None
    with (
        pytest.raises(process_sessions.SessionSurvived) as survived,
        process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as command,
    ):
        leader = command.pid
        child = planted.pin(_report(command))

    assert refused, 'the leader was never signalled, so the case held nothing'
    assert f'POSIX session {leader}' in str(survived.value)
    assert child is not None
    with suppress(psutil.NoSuchProcess):
        assert child.status() == psutil.STATUS_ZOMBIE, 'the member after the refused one was never killed'


def test_an_interruption_inside_the_end_is_finished_by_the_second(
    fifo: str, planted: Planted, monkeypatch: pytest.MonkeyPatch
) -> None:
    kill = process_sessions._kill  # pyright: ignore[reportPrivateUsage] -- the step the interruption lands in
    interrupted: list[process_sessions.Member] = []

    def interrupted_once(member: process_sessions.Member) -> None:
        if not interrupted:
            interrupted.append(member)
            raise KeyboardInterrupt
        kill(member)

    monkeypatch.setattr(process_sessions, '_kill', interrupted_once)
    child: psutil.Process | None = None
    with (
        pytest.raises(KeyboardInterrupt),
        process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as command,
    ):
        child = planted.pin(_report(command))

    assert interrupted, 'the end was never interrupted, so the case held nothing'
    assert child is not None
    assert not child.is_running()


def test_the_end_takes_its_own_session_and_nothing_outside_it(fifo: str, planted: Planted) -> None:
    # A child of this process outside any helper's session: the deliberate
    # exception to starting every process through `process_sessions`, since
    # what the case holds is that the helper leaves such a process alone.
    plain = sp.Popen([sys.executable, '-c', CHILD, fifo], stdout=sp.PIPE, text=True)
    try:
        assert plain.stdout is not None
        outside = planted.pin(Identity.parse(plain.stdout.readline()))
        with process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as second:
            neighbour = planted.pin(_report(second))
            with process_sessions.started(_leader(fifo, 'block'), stdout=process_sessions.PIPE, text=True) as first:
                child = planted.pin(_report(first))

            assert not child.is_running()
            assert neighbour.is_running(), 'the end of one session took a member of another'
            assert outside.is_running(), 'the end of a session took a process outside every session'

        assert not neighbour.is_running()
        assert outside.is_running()
    finally:
        plain.kill()
        _ = plain.wait(timeout=WAIT)


#: The child run's one case: it plants a tree through `started`, writes the
#: child's line where the outer case reads it, and has pytest-timeout's own
#: handler raise its failure inside the block. The marker's bound is far
#: above the run, so only that signal fires it.
BOUND_CASE = """\
import signal
import sys
from pathlib import Path

import process_sessions
import pytest


@pytest.mark.timeout(3600)
def test_the_case_bound_lands_in_the_block():
    with process_sessions.started({leader!r}, stdout=process_sessions.PIPE, text=True) as command:
        Path({report!r}).write_text(command.stdout.readline())
        signal.raise_signal(signal.SIGALRM)
"""

#: A stop-loss on the child run, below the per-case bound.
CHILD_RUN_TIMEOUT = 50


def test_a_member_is_gone_after_the_case_bound_fires_in_the_block(tmp_path: Path, fifo: str, planted: Planted) -> None:
    report = tmp_path / 'report'
    _ = (tmp_path / 'test_bound.py').write_text(BOUND_CASE.format(leader=_leader(fifo, 'block'), report=str(report)))
    _ = (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    # A plain `subprocess.run` rather than the helper under test, so that the
    # outer run does not depend on what it is checking; the child pytest
    # starts nothing outside the planted tree's own session.
    child_run = sp.run(
        [sys.executable, '-m', 'pytest', '-c', str(tmp_path / 'pytest.ini'), '-p', 'no:cacheprovider', '-q'],
        cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(TESTS)},
        capture_output=True,
        text=True,
        timeout=CHILD_RUN_TIMEOUT,
        check=False,
    )

    assert report.exists(), child_run.stdout + child_run.stderr
    child = planted.hold(Identity.parse(report.read_text()))
    assert child_run.returncode == 1, child_run.stdout + child_run.stderr
    assert 'Timeout (>3600.0s) from pytest-timeout' in child_run.stdout, child_run.stdout
    assert '1 failed' in child_run.stdout, child_run.stdout
    assert not child.running()


#: The premise case's program: its top level, and the dynamic provider's
#: `create`, each report their own process and its parent -- the language
#: host, and the provider's launcher shell -- with each one's session, and
#: the `create` then blocks on a FIFO nobody writes, as a stalled provider
#: call does. The top level does not block itself: it waits on the
#: registration, which waits on the `create`.
PREMISE_PROGRAM = """\
import json
import os
import pathlib

import pulumi
from pulumi.dynamic import CreateResult, Resource, ResourceProvider


def identity(pid):
    stat = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'starttime': stat[19], 'sid': os.getsid(pid)}


def report(where):
    line = json.dumps({'where': where, 'processes': [identity(os.getpid()), identity(os.getppid())]})
    with open(os.environ['PROBE_REPORT'], 'w') as fifo:
        fifo.write(line + '\\n')


class Stalling(ResourceProvider):
    def create(self, props):
        report('create')
        open(os.environ['PROBE_BLOCK']).read()
        return CreateResult(id_='never', outs={})


class Stalled(Resource):
    def __init__(self, name):
        super().__init__(Stalling(), name, {})


report('program')
Stalled('stalled')
"""

#: How long the premise case may take: its project's set-up, a `stack init`,
#: and an `up` that stalls in its provider, set from measured durations
#: rather than from the suite's per-case bound (framework/testing.md §8). On
#: four cores it took 0.8 s idle, 1.7 s with twice as many busy processes as
#: cores, and 2.1 s with four times as many. Above
#: `scratch_projects.SETUP_TIMEOUT` and twice `PREMISE_COMMAND_TIMEOUT`, so a
#: stalled command fails naming itself before this fires; a stop-loss, which
#: nothing asserts on.
PREMISE_CASE_TIMEOUT = 240

#: Each wait on a command the case runs -- the `stack init`, and the read of
#: the `up` until its plugins have reported -- so a stall fails as a
#: `TimeoutExpired` naming the command: a stop-loss below the case bound.
PREMISE_COMMAND_TIMEOUT = 120


def _reports(command: process_sessions.Command, report: Path, count: int, *, timeout: float) -> list[dict[str, Any]]:
    """`count` lines from the program's reporters, or a failure naming `pulumi`'s output once it ends first.

    The read waits on the report FIFO and on `pulumi`'s own output at once,
    so a `pulumi` that fails before its plugins report ends the case by its
    output rather than by the case bound, and one that stalls before them
    ends it as a `TimeoutExpired` naming the command once `timeout` passes.
    The case holds a writer of the FIFO itself, so the read end sees no
    hang-up between one reporter's close and the next one's open.
    """
    reader = os.open(report, os.O_RDONLY | os.O_NONBLOCK)
    writer = os.open(report, os.O_WRONLY | os.O_NONBLOCK)
    assert command.stdout is not None
    output = command.stdout.fileno()
    lines: list[dict[str, Any]] = []
    said: bytes = b''
    pending: bytes = b''
    try:
        with selectors.DefaultSelector() as selector:
            _ = selector.register(reader, selectors.EVENT_READ)
            _ = selector.register(output, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while len(lines) < count:
                ready: list[tuple[selectors.SelectorKey, int]] = selector.select(
                    timeout=max(0.0, deadline - time.monotonic())
                )
                if not ready:
                    raise sp.TimeoutExpired(command.args, timeout, output=said)
                for key, _events in ready:
                    chunk: bytes = os.read(key.fd, 65536)
                    if key.fd == output:
                        said += chunk
                        assert chunk, f'pulumi ended before its plugins reported:\n{said.decode()}'
                        continue
                    pending += chunk
                    while (end := pending.find(b'\n')) >= 0:
                        lines.append(cast('dict[str, Any]', json.loads(pending[:end])))
                        pending = pending[end + 1 :]
    finally:
        os.close(writer)
        os.close(reader)
    return lines


@pytest.mark.skipif(
    shutil.which('pulumi') is None or shutil.which('uv') is None, reason='the pinned pulumi CLI or uv is not on PATH'
)
@pytest.mark.timeout(PREMISE_CASE_TIMEOUT)
def test_the_pulumi_clis_plugins_stay_in_its_session_and_go_with_it(
    tmp_path: Path, fifo: str, planted: Planted
) -> None:
    scratch = scratch_project(tmp_path, PREMISE_PROGRAM)
    report = tmp_path / 'report'
    os.mkfifo(report)
    env = scratch.env | {'PROBE_REPORT': str(report), 'PROBE_BLOCK': fifo}
    initialized = process_sessions.run(
        ['pulumi', '--non-interactive', 'stack', 'init', 'probe'],
        cwd=scratch.directory,
        env=env,
        text=True,
        timeout=PREMISE_COMMAND_TIMEOUT,
    )
    assert initialized.returncode == 0, initialized.stderr

    pinned: dict[str, psutil.Process] = {}
    with (
        pytest.raises(sp.TimeoutExpired) as expired,
        process_sessions.started(
            ['pulumi', '--non-interactive', 'up', '--yes', '--skip-preview'],
            cwd=scratch.directory,
            env=env,
            stdout=process_sessions.PIPE,
            stderr=process_sessions.STDOUT,
        ) as command,
    ):
        reports = {
            line['where']: line['processes'] for line in _reports(command, report, 2, timeout=PREMISE_COMMAND_TIMEOUT)
        }
        assert set(reports) == {'program', 'create'}
        for where, processes in reports.items():
            for role, process in zip(('itself', 'its parent'), processes, strict=True):
                name = f'the {where} reporter, {role}'
                pinned[name] = planted.pin(Identity(int(process['pid']), str(process['starttime'])))
                # The premise, held on its own: a release that moves the
                # plugins out of the CLI's session fails here, by name, and
                # not as a leak below.
                assert process['sid'] == command.pid, f'{name} is not in the pulumi CLI session'
        _ = command.wait(timeout=0)

    # The way out is the wait's own bound, not the read's: a `pulumi` that
    # stalled before its plugins reported fails here naming the read.
    assert expired.value.timeout == 0, f'the up stalled before its plugins reported: {expired.value}'
    assert 'pulumi' in str(expired.value.cmd)
    assert len(pinned) == 4, pinned
    assert [name for name, process in pinned.items() if process.is_running()] == []
