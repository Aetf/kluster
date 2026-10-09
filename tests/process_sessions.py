"""Each command a case starts leads a POSIX session of its own, and the session dies with the call.

`subprocess.run` kills the process it started and nothing else, and `with
Popen` waits on it with no bound. A command's own children outlive both:
the `pulumi` CLI starts each plugin in a process group of its own, which
only a living `pulumi` closes, so a `pulumi` killed at its bound leaves a
language host and a dynamic provider running for good; a shell killed at
its bound leaves a `cmp` blocked on a FIFO nobody will write. What every one
of them keeps is the session: nothing leaves a POSIX session except by
calling `setsid()`, and Linux hands a session's id to no other process while
one member lives (`__change_pid` in `kernel/pid.c`).

So a command started here calls `setsid()` before it execs, and every way
out of the call -- a clean exit, `TimeoutExpired`, an assertion, the case
bound, KeyboardInterrupt -- ends it the same way. Every process on the host
whose session is the command's is found, killed with SIGKILL, and waited on
until it is gone, and the scan is repeated until it finds nothing. Nothing is
chosen by name, command line, user or process group, so a process outside
the session -- another worker's, another agent's, the operator's -- is never
signalled. `psutil` pins each member by its start time before signalling it
and does the bounded wait. The leader is the one process here that is this
process's child, and it is reaped through its own `Popen` and never through
`psutil`, which would reap it and leave the `Popen` reading status 0.

What this does not reach is a process outside the session of a command
started here -- a descendant that called `setsid()`, a command code under
test starts through a runner of its own -- and anything at all once the test
process can no longer run a `finally`: framework/testing.md §1.2.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import psutil

#: How long a member SIGKILLed may take to be gone, its reaping included: a
#: stop-loss, which nothing asserts on. A process that outlives SIGKILL is in
#: an uninterruptible wait, or has no parent that reaps it, and either fails
#: the call naming the process rather than hanging it.
KILL_TIMEOUT = 10.0

PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
DEVNULL = subprocess.DEVNULL

#: What a stream argument may be: a file, or one of `PIPE`, `STDOUT` and `DEVNULL`.
Stream = int | IO[Any]


class SessionSurvived(RuntimeError):
    """A member of a command's session was still there `KILL_TIMEOUT` after its SIGKILL."""


@dataclass(frozen=True)
class Member:
    """A process found in a session, pinned by `psutil` to the one that had its pid when it was found."""

    process: psutil.Process
    cmdline: str

    def __str__(self) -> str:
        return f'{self.process.pid} {self.cmdline}'


class Command:
    """A command running as the leader of a POSIX session of its own.

    Neither a `Popen` nor a context manager: every wait takes a bound, and
    the session ends when the `started` block that made it does.
    """

    def __init__(self, popen: subprocess.Popen[Any]) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        """The leader's pid, which is the session's id and the leader's process group."""
        return self._popen.pid

    @property
    def args(self) -> Any:
        return self._popen.args

    @property
    def stdin(self) -> IO[Any] | None:
        return self._popen.stdin

    @property
    def stdout(self) -> IO[Any] | None:
        return self._popen.stdout

    @property
    def stderr(self) -> IO[Any] | None:
        return self._popen.stderr

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    def wait(self, *, timeout: float) -> int:
        """The leader's exit status, or `TimeoutExpired` naming the command once `timeout` passes."""
        return self._popen.wait(timeout=timeout)

    def communicate(self, input: str | bytes | None = None, *, timeout: float) -> tuple[Any, Any]:  # noqa: A002 -- `Popen.communicate`'s own name
        """`Popen.communicate`, bounded: the output once every writer of the pipes is gone."""
        return self._popen.communicate(input, timeout=timeout)


def members(leader: subprocess.Popen[Any]) -> list[Member]:
    """Every living process in `leader`'s session, each pinned as the process that is in it.

    The session's id is read twice around pinning a pid, so a pid reused
    between the two reads drops out; so does a zombie, which is dead. Once the
    leader is reaped its pid can be reused, and a scan that finds a process
    with that pid takes the number as reused and finds nothing at all. That
    covers the one scan an end makes after it reaps the leader, which a reused
    number reaches only if the host allocated every other pid in between;
    `started` makes no scan after an end that finished, which covers a reused
    session whose own leader has gone too.
    """
    session = leader.pid
    found: list[Member] = []
    for pid in psutil.pids():
        try:
            if os.getsid(pid) != session:
                continue
            process = psutil.Process(pid)
            if os.getsid(pid) != session or process.status() == psutil.STATUS_ZOMBIE:
                continue
            cmdline = ' '.join(process.cmdline())
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        if pid == session and leader.returncode is not None:
            return []
        found.append(Member(process, cmdline))
    return found


def _kill(member: Member) -> None:
    """SIGKILL `member`, which `psutil` refuses once its pid names another process.

    A member this process may not signal -- one whose real and saved user ids
    are no longer the caller's, as under `sudo` -- is passed over rather than
    stopping the round, so every other member is still signalled; the wait
    after the round names it if it is still there.
    """
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        member.process.send_signal(signal.SIGKILL)


def _reap(leader: subprocess.Popen[Any]) -> None:
    """Reap the leader through its own `Popen`, so that its status is the one it exited with."""
    try:
        _ = leader.wait(timeout=KILL_TIMEOUT)
    except subprocess.TimeoutExpired as expired:
        raise SessionSurvived(
            f'POSIX session {leader.pid}: the leader was not gone {KILL_TIMEOUT} s after SIGKILL'
        ) from expired


def end(leader: subprocess.Popen[Any]) -> list[Member]:
    """Kill every process in `leader`'s session, wait until each is gone, and return what was killed.

    Each round kills before it waits: a grace period would be spent on
    exactly the commands that need the kill, a `pulumi` with a call in
    flight among them. Then it waits on every member but the leader through
    `psutil`, which on Linux waits on a pidfd for the exit and then for the
    reaping, and reaps the leader through its `Popen`. A member forked
    between a round's scan and its kill is the next round's, and the rounds
    stop at the first scan that finds nothing. Every wait is bounded by
    `KILL_TIMEOUT`.
    """
    killed: list[Member] = []
    while found := members(leader):
        for member in found:
            _kill(member)
        killed += found
        others = [member.process for member in found if member.process.pid != leader.pid]
        _, alive = psutil.wait_procs(others, timeout=KILL_TIMEOUT)
        if alive:
            named = ', '.join(str(member) for member in found if member.process in alive)
            raise SessionSurvived(f'POSIX session {leader.pid}: alive {KILL_TIMEOUT} s after SIGKILL: {named}')
        _reap(leader)
    _reap(leader)
    return killed


@contextmanager
def started(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: Stream = DEVNULL,
    stdout: Stream | None = None,
    stderr: Stream | None = None,
    text: bool = False,
) -> Generator[Command]:
    """Start `argv` as the leader of a POSIX session of its own, and end the session with the block.

    The block owns everything between the start and the end: a blocking read
    of `stdout`, which meets EOF once every writer is gone; a signal to the
    leader's process group, `os.killpg(command.pid, …)`, which is what a
    terminal sends; bounded waits. The command reads `stdin`, which is
    `/dev/null` unless the call hands it another, and never this process's.

    However the block is left, every process in the session is killed and
    gone before the call returns or raises, and an exception leaving the
    block carries a note naming the session and each process the kill took.
    The end runs twice, the second time in a `finally` of its own: the case
    bound fires once per case, so if it lands inside the first the second
    runs to the end, and after an uninterrupted first it does nothing at all.
    """
    popen = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        start_new_session=True,
    )
    ended = False

    def ending() -> list[Member]:
        # Once an end has run to its last scan, which found the session
        # empty, no scan runs again: the leader is reaped by then, so its pid
        # -- the session's number -- may be another process's, or another
        # session's that a process outside this one leads or led.
        nonlocal ended
        if ended:
            return []
        killed = end(popen)
        ended = True
        return killed

    try:
        try:
            yield Command(popen)
        except BaseException as error:
            named = '; '.join(str(member) for member in ending()) or 'nothing'
            error.add_note(f'POSIX session {popen.pid} of {popen.args!r}: killed {named}')
            raise
        _ = ending()
    finally:
        _ = ending()
        # Standard input last, and a reader already gone ignored, as
        # `Popen.communicate` does: a write the block left unflushed meets
        # the killed reader, and a `BrokenPipeError` here would stand in for
        # whatever the block raised.
        for stream in (popen.stdout, popen.stderr):
            if stream is not None:
                stream.close()
        if popen.stdin is not None:
            with suppress(BrokenPipeError):
                popen.stdin.close()


def run(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,  # noqa: A002 -- `subprocess.run`'s own name
    stdin: IO[Any] | None = None,
    text: bool = False,
    check: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """`subprocess.run` with the output captured, its command leading a POSIX session of its own.

    Every process in the session is gone when this returns or raises. The
    command reads `input`, or the file `stdin`, or `/dev/null`. A command
    still running at `timeout`, or a member still holding its output open,
    raises `TimeoutExpired` naming the command and its bound, with the
    output read so far; `check` raises `CalledProcessError` on a non-zero
    status, after the session has ended.
    """
    if input is not None and stdin is not None:
        raise ValueError('input and stdin are two stdins; hand one')
    with started(
        argv,
        cwd=cwd,
        env=env,
        stdin=PIPE if input is not None else stdin if stdin is not None else DEVNULL,
        stdout=PIPE,
        stderr=PIPE,
        text=text,
    ) as command:
        output, errors = command.communicate(input, timeout=timeout)
    status = command.returncode
    assert status is not None, 'communicate returned before the leader was reaped'
    if check and status != 0:
        raise subprocess.CalledProcessError(status, command.args, output, errors)
    return subprocess.CompletedProcess(command.args, status, output, errors)
