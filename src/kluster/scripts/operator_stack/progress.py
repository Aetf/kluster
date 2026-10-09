"""What a `pulumi` run the driver starts is still waiting on, said at a display rate.

The driver starts two runs whose progress `pulumi` itself does not show:

-   **The refreshed preview.** Its standard output is the engine's events,
    which the driver reads for the plan (`driver.read_preview`), so `pulumi`
    draws no display of it at all, at a terminal or not.
-   **The apply, where `pulumi`'s display is not live** (`live`): under a pipe,
    a terminal that reports no size, or `TERM=dumb`. The non-interactive
    display prints a step when it starts and when it ends, and nothing in
    between, so a step that takes minutes reads as a stopped run.

For each such run the driver hands `pulumi` an event log of its own
(`observed`): `--event-log` into a fresh directory outside the checkout, with
`PULUMI_DEBUG_COMMANDS=true`, the variable the flag is registered under. At
every `INTERVAL` it reads what the engine appended (`EventLog`) and logs the
line due (`Narrator`, `Line`): the custom resources' steps in flight and for
how long, or that the engine has said nothing. It goes to the driver's log, on
standard error, never to standard output. **Nothing the driver acts on reads
the log**: the plan is read from the preview's standard output, the checks
from the state, the answers from each process's exit code. The layer is a
display, and fails open: a failure inside it is one warning, and the run
goes on unnarrated.

**The apply's narration is a workaround for an upstream defect**, and stands
apart so that it can go: pulumi/pulumi#11139 asks the non-interactive display
to reprint a running step's elapsed time. Its boundary is this module, the
`doing` keyword of `driver.Pulumi.stream`, whatever exists only to hand
`stream` a `doing` (today `Run.up`'s argument and `Run._write`'s `doing`
parameter), and the wait in `driver.Cli._run`; past those, the driver's
`Run`, its readers, its checks and the gate know nothing of it. Once a
pinned release reprints the elapsed time, the apply stops being observed:
`shown=True`, `live`, `--suppress-progress`, the keyword on `stream` and
everything that only hands it a `doing` go. The preview's narration
stays, since the preview's silence is the driver's own choice of standard
output. The real-engine apply case in `tests/test_operator_stack.py` trips
when a bump drops the debug-gated flag or its file form: the line naming the
held step never comes, and the case fails at its bound naming what it waited
for.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)

#: How long a run may look still before the driver says what it is waiting on,
#: in seconds: a display rate. Nothing asserts on it, or on elapsed time.
INTERVAL = 30

#: How many steps in flight a line names, longest-running first; the rest are
#: counted.
NAMED = 3

#: The variable `pulumi` registers `--event-log` under, and the flag.
DEBUG_COMMANDS_ENV = 'PULUMI_DEBUG_COMMANDS'
EVENT_LOG_FLAG = '--event-log'
#: What takes the non-interactive display's dots away and leaves its lines.
SUPPRESS_PROGRESS_FLAG = '--suppress-progress'

#: The step events: the step's start, its end, and its failure.
STARTED = 'resourcePreEvent'
ENDED = ('resOutputsEvent', 'resOpFailedEvent')


def duration(seconds: float) -> str:
    """Whole seconds in Go's form, the form of `pulumi`'s own `(12s)` and `Duration:`: `45s`, `4m30s`, `1h0m5s`."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}h{minutes}m{secs}s'
    if minutes:
        return f'{minutes}m{secs}s'
    return f'{secs}s'


def _metadata(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return cast('Mapping[str, Any]', body.get('metadata') or {})


def _custom(metadata: Mapping[str, Any]) -> bool:
    """Whether a step's resource is a custom one: a component's step stays open until the run's end, and says nothing."""
    for side in ('new', 'old'):
        state = metadata.get(side)
        if isinstance(state, dict):
            return bool(cast('Mapping[str, Any]', state).get('custom'))
    return False


class Branch(enum.Enum):
    """Which state of the run a line says."""

    #: Custom resources' steps are in flight.
    IN_FLIGHT = 'in flight'
    #: No step is in flight, and the engine has been quiet since its last event.
    QUIET = 'quiet'
    #: The engine has written no event since `pulumi` started.
    NO_EVENT_YET = 'no event yet'
    #: The engine has summarized the run, and `pulumi` has not exited.
    SUMMARIZED = 'summarized'


@dataclass(frozen=True)
class Step:
    """A custom resource's step in flight, and how long it has run, in seconds."""

    op: str
    urn: str
    seconds: float


@dataclass(frozen=True)
class Line:
    """A line due, as what it says; `str()` is the line the driver logs."""

    doing: str
    branch: Branch
    #: The steps in flight a line names, longest-running first, at most `NAMED`.
    named: tuple[Step, ...] = ()
    #: How many more steps are in flight than are named.
    more: int = 0
    #: How many steps have ended, said where the display does not print them
    #: and the line is about steps (in flight, or quiet); otherwise `None`.
    done: int | None = None
    #: How long ago the branch's own moment was, in seconds: the last event
    #: (quiet), `pulumi`'s start (no event yet), or the summary (summarized).
    #: `None` in flight, where each step has its own.
    age: float | None = None
    #: Lines of the event log that were not events, since the line before.
    skipped: int = 0

    def __str__(self) -> str:
        line = f'still {self.doing}: {self._state()}'
        if self.skipped:
            line += (
                ' (1 line of the event log was not an event)'
                if self.skipped == 1
                else f' ({self.skipped} lines of the event log were not events)'
            )
        return line

    def _state(self) -> str:
        age = duration(self.age or 0)
        if self.branch is Branch.SUMMARIZED:
            return f'the engine summarized the run {age} ago, and pulumi has not exited'
        if self.branch is Branch.NO_EVENT_YET:
            return f'no event from the engine yet, {age} after pulumi started'
        finished = '' if self.done is None else f'{self.done} step{"" if self.done == 1 else "s"} done; '
        if self.branch is Branch.QUIET:
            return f'{finished}no step in flight, and no event from the engine for {age}'
        named = ', '.join(f'{step.op} {step.urn} for {duration(step.seconds)}' for step in self.named)
        return finished + named + (f', and {self.more} more in flight' if self.more else '')


@dataclass
class Narrator:
    """What is in flight in a run, from its engine events, and the line due at a moment.

    `doing` is what the run is doing, as the driver says it before it starts
    the run (`applying the github stack`). `shown` says whether `pulumi`'s own
    display prints the run: where it does, a line is due only when nothing the
    display prints has arrived since the last tick, so the narration does not
    talk over it; where it does not, a line is due at every tick, and leads
    with what has finished. Every time is the injected clock's or an event's
    own `timestamp`, in Unix seconds.
    """

    doing: str
    shown: bool
    started: float
    #: Each custom resource's step in flight, by URN and operation, with the time it started.
    in_flight: dict[tuple[str, str], float] = field(default_factory=dict[tuple[str, str], float])
    done: int = 0
    last_event: float | None = None
    summarized: float | None = None
    skipped: int = 0
    _printed: bool = False

    def feed(self, event: Mapping[str, Any]) -> None:
        """Take one engine event."""
        at = event.get('timestamp')
        if isinstance(at, int | float):
            self.last_event = float(at)
        if (body := event.get(STARTED)) is not None:
            metadata = _metadata(cast('Mapping[str, Any]', body))
            op, urn = str(metadata.get('op')), str(metadata.get('urn'))
            self._printed |= op != 'same'
            if _custom(metadata):
                self.in_flight[(urn, op)] = self.last_event if self.last_event is not None else self.started
        for kind in ENDED:
            if (body := event.get(kind)) is not None:
                metadata = _metadata(cast('Mapping[str, Any]', body))
                op, urn = str(metadata.get('op')), str(metadata.get('urn'))
                self._printed |= op != 'same'
                if self.in_flight.pop((urn, op), None) is not None:
                    self.done += 1
        if (diagnostic := event.get('diagnosticEvent')) is not None:
            self._printed |= cast('Mapping[str, Any]', diagnostic).get('severity') != 'debug'
        if event.get('summaryEvent') is not None:
            self.summarized = self.last_event if self.last_event is not None else self.started

    def skip(self, count: int) -> None:
        """Count lines of the log that were not events, to say in the next line due."""
        self.skipped += count

    def due(self, now: float) -> Line | None:
        """The line due at `now`, or `None`; either way the tick is taken."""
        printed, self._printed = self._printed, False
        if self.shown and printed:
            return None
        line = self._line(now)
        self.skipped = 0
        return line

    def _line(self, now: float) -> Line:
        said = Line(doing=self.doing, branch=Branch.IN_FLIGHT, skipped=self.skipped)
        if self.summarized is not None:
            return replace(said, branch=Branch.SUMMARIZED, age=now - self.summarized)
        if self.last_event is None:
            return replace(said, branch=Branch.NO_EVENT_YET, age=now - self.started)
        said = replace(said, done=None if self.shown else self.done)
        if not self.in_flight:
            return replace(said, branch=Branch.QUIET, age=now - self.last_event)
        longest = sorted(self.in_flight.items(), key=lambda item: item[1])
        return replace(
            said,
            named=tuple(Step(op, urn, now - since) for (urn, op), since in longest[:NAMED]),
            more=max(0, len(longest) - NAMED),
        )


@dataclass
class EventLog:
    """The engine events appended to `path` since the last read, one JSON object per complete line.

    A partial last line waits for the next read. A line that is not a JSON
    object is skipped and counted. It never raises: a file that does not
    exist yet, or cannot be read, reads as no events.
    """

    path: Path
    _offset: int = 0
    _partial: bytes = b''

    def read(self) -> tuple[list[dict[str, Any]], int]:
        """The new events, and how many new lines were not events."""
        try:
            with self.path.open('rb') as handle:
                _ = handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return [], 0
        self._offset += len(chunk)
        *lines, self._partial = (self._partial + chunk).split(b'\n')
        events: list[dict[str, Any]] = []
        skipped = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(event, dict):
                events.append(cast('dict[str, Any]', event))
            else:
                skipped += 1
        return events, skipped


def live(env: Mapping[str, str], *, stdin: int = 0, stdout: int = 1) -> bool:
    """Whether `pulumi`, started with `env` on these descriptors, draws its live display.

    Its own conditions (`cmdutil.Interactive`, and the terminal's open), on
    the descriptors it inherits rather than on `sys.stdin`: both are
    terminals, `TERM` is not `dumb` in any case, and standard output's
    terminal reports a width and a height above zero. Its detection of a CI system from that
    system's variables is not mirrored: no CI job runs an operator stack.
    """
    if env.get('TERM', '').lower() == 'dumb':
        return False
    try:
        if not (os.isatty(stdin) and os.isatty(stdout)):
            return False
        size = os.get_terminal_size(stdout)
    except OSError:
        return False
    return size.columns > 0 and size.lines > 0


@dataclass
class Observation:
    """What an observed run gains: its extra arguments and variables, and the tick that narrates it."""

    args: list[str] = field(default_factory=list[str])
    env: dict[str, str] = field(default_factory=dict[str, str])
    beat: Callable[[], None] | None = None


@contextmanager
def observed(doing: str, *, shown: bool, clock: Callable[[], float], cwd: Path) -> Generator[Observation]:
    """A run of `pulumi` doing `doing`, narrated at every `beat` from an event log in a directory of its own.

    The directory is fresh, private to its owner (`tempfile.mkdtemp`), and
    removed on every way out the driver lives through. A driver ended by a
    signal it does not handle (SIGKILL, SIGTERM, a hang-up) leaves it, and
    the log in it holds every value the program does not mark secret. One
    that resolves inside the checkout `cwd` is not used, and the run goes
    without progress lines: `jj` would snapshot the log there. `shown` says
    `pulumi`'s display prints the run, and adds `--suppress-progress`, whose
    dots would otherwise wait on a newline.
    """
    directory = Path(tempfile.mkdtemp(prefix='operator-stack-events-'))
    try:
        if directory.resolve().is_relative_to(cwd.resolve()):
            log.warning(
                'the temporary directory %s is inside %s: %s goes without progress lines', directory, cwd, doing
            )
            yield Observation()
            return
        events = EventLog(directory / 'events.jsonl')
        narrator = Narrator(doing=doing, shown=shown, started=clock())
        failed = False

        def beat() -> None:
            nonlocal failed
            if failed:
                return
            try:
                read, skipped = events.read()
                for event in read:
                    narrator.feed(event)
                narrator.skip(skipped)
                if (line := narrator.due(clock())) is not None:
                    log.info('%s', line)
            except Exception as exc:  # noqa: BLE001 -- a display fails open: one warning, and the run goes on
                failed = True
                log.warning('progress lines for %s stopped: %s: %s', doing, type(exc).__name__, exc)

        yield Observation(
            args=[EVENT_LOG_FLAG, str(events.path), *([SUPPRESS_PROGRESS_FLAG] if shown else [])],
            env={DEBUG_COMMANDS_ENV: 'true'},
            beat=beat,
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)
