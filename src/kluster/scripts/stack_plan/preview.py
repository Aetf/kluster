"""One run of the pinned `pulumi`, bounded, that ends with the command whatever ends the command.

**`pulumi` stops its plugins only when it is asked twice.** It handles SIGINT
and SIGTERM and no other signal: the first cancels the run, which finishes
what is in flight and may wait on a provider call that never returns; the
second ends it, stopping every plugin it started, within seconds. Each
plugin leads a process group of its own, so nothing but `pulumi` reaches it,
and a `pulumi` killed outright leaves its language host and its providers
running for good. Two askings close together may count as one, so nothing here
kills `pulumi` before it has been asked twice, `GRACE` apart, and given
`GRACE` after the second asking.

**The run leads a process group of its own**, so the command decides what
reaches it, and reads `/dev/null`: a run that would ask the terminal for
something -- a passphrase that is not set -- fails at once with its own
error, rather than stopping as a background process that reads the terminal.

**A signal the command is sent is passed on**, SIGINT as SIGINT and SIGTERM
and SIGHUP as SIGTERM, since `pulumi` would die of a SIGHUP without stopping
its plugins. A second signal is passed on at once too. The run is asked
again once `GRACE` has passed without its answering the first asking, and
its group is killed once `GRACE` has passed after that: the kill waits on
the asking sent at the deadline, never on one a signal brought. When the
run has ended, `run` raises `Signalled`, and the command dies of the signal
it was sent once it has left what it holds (`die_of`). A signal the command
was started with ignored -- SIGHUP under `nohup` -- stays ignored.

**The handler of a signal only records it, and raises nothing.** The wait
on the run is `communicate` in steps of `STEP`, each of which reads what was
recorded and the deadline; an exception a handler raised into the wait could
land inside `Popen`'s own locking and leave the run unreapable, which is the
hazard the `signal` documentation's note on handlers and exceptions names.

**A run still going at `TIMEOUT` is stopped** the same way, asked with
SIGTERM, and refused as `Stopped`; so is a run the wait is left by anything
else, an exception in the command, before the exception goes on. Every
bound is a stop-loss, not a measure of a run.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess as sp
import sys
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from types import FrameType
from typing import NoReturn

log = logging.getLogger(__name__)

#: How long one preview may run before it is stopped. A preview of the
#: largest stack here takes about a minute; both runs at this bound, each with
#: its two `GRACE`s, stay inside the 30 minutes a CI step gives the command.
TIMEOUT = 600

#: How long a run asked to stop has to answer before it is asked again, and
#: after the second asking before its group is killed.
GRACE = 30

#: How long one step of the wait on a run may last before the wait reads
#: the signals recorded and the deadline again: how late a signal is passed
#: on, at most. A stop-loss on that latency, not a measure of a run.
STEP = 0.5

#: The signals the command passes on to the run it is waiting on, each as the
#: signal `pulumi` is sent for it.
PASSED_ON = {signal.SIGINT: signal.SIGINT, signal.SIGTERM: signal.SIGTERM, signal.SIGHUP: signal.SIGTERM}


class Stopped(RuntimeError):
    """A run still going at its bound, and stopped there: no answer came back."""


class Signalled(BaseException):
    """The command was sent `signum` while a run was going; the run has ended, and the command dies of it next."""

    def __init__(self, signum: int) -> None:
        super().__init__(f'received {signal.Signals(signum).name}')
        self.signum: int = signum


def run(args: Sequence[str], *, env: Mapping[str, str] | None = None, captured: bool = False) -> tuple[int, str]:
    """`pulumi args`: its exit status, and its standard output where `captured`, which is otherwise the terminal's.

    `env` replaces the command's own environment where it is given. Raises
    `Stopped` for a run stopped at its bound and `Signalled` for one the
    command was told to stop, in either case once the run has ended.
    """
    sys.stdout.flush()
    with (
        _received() as received,
        sp.Popen(
            ['pulumi', *args],
            stdin=sp.DEVNULL,
            stdout=sp.PIPE if captured else None,
            text=True,
            env=None if env is None else dict(env),
            process_group=0,
        ) as child,
    ):
        try:
            status, printed = _wait(child, received, ' '.join(args[:3]))
        except BaseException:
            _stop(child, signal.SIGTERM, asked=0)
            raise
    if received.signals:
        raise Signalled(received.signals[0])
    return status, printed


class Received:
    """The signals of `PASSED_ON` the command was sent: a handler that records each one and does nothing else."""

    def __init__(self) -> None:
        self.signals: list[int] = []
        self.handled = 0

    def __call__(self, signum: int, _frame: FrameType | None) -> None:
        self.signals.append(signum)

    def new(self) -> list[int]:
        """The signals received since the last call."""
        fresh = self.signals[self.handled :]
        self.handled = len(self.signals)
        return fresh


@contextmanager
def _received() -> Generator[Received]:
    """The signals of `PASSED_ON` taken by a `Received`, but those the command was started with ignored."""
    received = Received()
    previous = {
        signum: signal.signal(signum, received)
        for signum in PASSED_ON
        if signal.getsignal(signum) is not signal.SIG_IGN
    }
    try:
        yield received
    finally:
        for signum, handler in previous.items():
            _ = signal.signal(signum, handler)


def _wait(child: sp.Popen[str], received: Received, named: str) -> tuple[int, str]:
    """Wait on the run: pass each signal on, ask again at `GRACE` and at the bound, and kill `GRACE` after that.

    `asked` counts the askings the kill waits on: the first, a signal's or
    the bound's, and the one sent at the `GRACE` deadline after it. A signal
    after the first is passed on and counts for nothing, since `pulumi` may
    take it and the asking before it for one.
    """
    asked = 0
    sent = signal.SIGTERM
    due = time.monotonic() + TIMEOUT
    at_bound = False
    while True:
        for signum in received.new():
            sent = PASSED_ON[signal.Signals(signum)]
            _signal(child, sent)
            if asked == 0:
                asked = 1
                due = time.monotonic() + GRACE
        try:
            printed, _ = child.communicate(timeout=min(STEP, max(0.0, due - time.monotonic())))
        except sp.TimeoutExpired:
            if time.monotonic() < due:
                continue
            if asked < 2:
                if asked == 0:
                    log.error('stopping `pulumi %s`, still running at its bound of %ss', named, TIMEOUT)
                    at_bound = True
                _signal(child, sent)
                asked += 1
                due = time.monotonic() + GRACE
                continue
            _signal(child, signal.SIGKILL)
            printed, _ = child.communicate()
        if at_bound:
            raise Stopped(f'`pulumi {named}` did not finish within {TIMEOUT}s and was stopped')
        return child.returncode, printed or ''


def _stop(child: sp.Popen[str], signum: int, *, asked: int) -> None:
    """Ask the run to stop until it has been asked twice, `GRACE` apart, and kill its group past the last."""
    while child.poll() is None:
        if asked >= 2:
            _signal(child, signal.SIGKILL)
            _ = child.communicate()
            return
        _signal(child, signum)
        asked += 1
        with suppress(sp.TimeoutExpired):
            _ = child.communicate(timeout=GRACE)


def _signal(child: sp.Popen[str], signum: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signum)


def die_of(signum: int) -> NoReturn:
    """End the command by `signum`, as it would have ended had it not waited for the run."""
    _ = signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    raise SystemExit(128 + signum)
