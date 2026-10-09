"""What the driver says of a `pulumi` run `pulumi` does not show (`operator_stack.progress`).

The narrator is held by the fields of the line it makes due (`progress.Line`),
over events built in the shape the pinned CLI writes to `--event-log`, with
chosen `timestamp`s and `now` passed in: no case reads the wall clock. One
case holds a line's text to its fields, and no case holds its wording. The event log's reader is held over
a file written in pieces; `live` over the descriptors a run inherits.
`Cli`'s observation of a run is held against a stand-in `pulumi`, run with
the driver in a POSIX session of its own; the real engine's is held in
`tests/test_operator_stack.py`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import re
import struct
import sys
import tempfile
import termios
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import process_sessions
import pytest

from kluster.scripts.operator_stack import progress

BOX = 'urn:pulumi:probe::kluster-py::pulumi-python:dynamic:Resource::box'
REPO = 'urn:pulumi:probe::kluster-py::github:index/repository:Repository::repo'
STACK = 'urn:pulumi:probe::kluster-py::pulumi:pulumi:Stack::kluster-py-probe'

APPLYING = 'applying the probe stack'
PREVIEWING = 'previewing the probe stack with a refresh'


def _step(kind: str, op: str, urn: str, at: float, *, custom: bool = True) -> dict[str, Any]:
    """A step event as the event log carries it: the state's `custom` marks a custom resource."""
    state = {'urn': urn, 'custom': custom}
    side = {'old': state, 'new': None} if op == 'delete' else {'old': None, 'new': state}
    return {'sequence': int(at), 'timestamp': at, kind: {'metadata': {'op': op, 'urn': urn, **side}}}


def _diagnostic(severity: str, at: float) -> dict[str, Any]:
    return {'sequence': int(at), 'timestamp': at, 'diagnosticEvent': {'severity': severity, 'message': 'x'}}


def _fed(narrator: progress.Narrator, *events: dict[str, Any], ticked: float | None = None) -> progress.Narrator:
    """`narrator` fed `events`, and where `ticked` is given, past a tick at that time.

    Where the display prints the run, the events that start a step are ones it
    prints, so the tick after them says nothing; a case about the line after
    that takes the tick first.
    """
    for event in events:
        narrator.feed(event)
    if ticked is not None:
        _ = narrator.due(ticked)
    return narrator


# --------------------------------------------------------------------------
# The narrator.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('seconds', 'said'),
    [(0, '0s'), (45, '45s'), (59.9, '59s'), (60, '1m0s'), (270, '4m30s'), (3600, '1h0m0s'), (3725, '1h2m5s')],
)
def test_a_duration_is_said_in_gos_form(seconds: float, said: str) -> None:
    assert progress.duration(seconds) == said


def _due(narrator: progress.Narrator, now: float) -> progress.Line:
    line = narrator.due(now)
    assert line is not None, f'no line due at {now}'
    return line


def test_a_step_in_flight_is_named_with_how_long_it_has_run() -> None:
    narrator = _fed(
        progress.Narrator(APPLYING, shown=True, started=0), _step('resourcePreEvent', 'create', BOX, 30), ticked=31
    )

    line = _due(narrator, 300)

    assert line.branch is progress.Branch.IN_FLIGHT
    assert line.named == (progress.Step('create', BOX, 300 - 30),)
    assert (line.more, line.done) == (0, None)


def test_several_in_flight_are_named_longest_first_and_the_rest_counted() -> None:
    # Fed in the reverse of the order they started, so the order a line
    # names them in is their age's, not their arrival's.
    begun = {f'{BOX}{n}': 10.0 * (n + 1) for n in range(progress.NAMED + 2)}
    narrator = _fed(
        progress.Narrator(APPLYING, shown=True, started=0),
        *(_step('resourcePreEvent', 'update', urn, at) for urn, at in reversed(begun.items())),
        ticked=max(begun.values()) + 1,
    )

    line = _due(narrator, 100)

    longest = sorted(begun, key=begun.__getitem__)[: progress.NAMED]
    assert line.named == tuple(progress.Step('update', urn, 100 - begun[urn]) for urn in longest)
    assert line.more == len(begun) - progress.NAMED


def test_a_finished_or_failed_step_leaves_the_list_and_a_components_step_is_never_named() -> None:
    narrator = _fed(
        progress.Narrator(PREVIEWING, shown=False, started=0),
        _step('resourcePreEvent', 'create', STACK, 1, custom=False),
        _step('resourcePreEvent', 'refresh', BOX, 2),
        _step('resourcePreEvent', 'refresh', REPO, 3),
        _step('resOutputsEvent', 'refresh', BOX, 4),
        _step('resourcePreEvent', 'update', BOX, 5),
        _step('resOpFailedEvent', 'update', BOX, 6),
    )

    line = _due(narrator, 15)

    assert line.named == (progress.Step('refresh', REPO, 15 - 3),)
    # The display does not print this run, so the line counts what ended:
    # the refresh that finished and the update that failed.
    assert (line.done, line.more) == (2, 0)


def test_with_no_step_in_flight_the_engines_silence_is_measured_from_its_last_event() -> None:
    narrator = _fed(
        progress.Narrator(APPLYING, shown=True, started=0),
        _step('resourcePreEvent', 'create', BOX, 1),
        _step('resOutputsEvent', 'create', BOX, 20),
        ticked=20,
    )

    line = _due(narrator, 80)

    assert line.branch is progress.Branch.QUIET
    assert (line.age, line.named, line.done) == (80 - 20, (), None)


def test_before_any_event_the_time_since_pulumi_started_is_measured() -> None:
    line = _due(progress.Narrator(PREVIEWING, shown=False, started=1000), 1030)

    assert line.branch is progress.Branch.NO_EVENT_YET
    assert line.age == 1030 - 1000


def test_after_the_summary_the_time_since_the_summary_is_measured() -> None:
    # An event after the summary, so the summary's age is not the last event's.
    narrator = _fed(
        progress.Narrator(APPLYING, shown=True, started=0),
        _step('resourcePreEvent', 'create', BOX, 1),
        {'sequence': 9, 'timestamp': 10, 'summaryEvent': {'resourceChanges': {'create': 1}}},
        {'sequence': 10, 'timestamp': 12, 'cancelEvent': {}},
        ticked=12,
    )

    line = _due(narrator, 55)

    assert line.branch is progress.Branch.SUMMARIZED
    assert line.age == 55 - 10


def test_lines_that_were_not_events_are_counted_in_the_next_line_and_only_there() -> None:
    narrator = progress.Narrator(PREVIEWING, shown=False, started=0)
    narrator.skip(2)

    assert _due(narrator, 5).skipped == 2
    assert _due(narrator, 6).skipped == 0


@pytest.mark.parametrize(
    'line',
    [
        progress.Line(
            APPLYING,
            progress.Branch.IN_FLIGHT,
            named=(progress.Step('create', BOX, 270), progress.Step('update', REPO, 12)),
            more=4,
            done=7,
            skipped=3,
        ),
        progress.Line(PREVIEWING, progress.Branch.QUIET, done=5, age=61, skipped=1),
        progress.Line(APPLYING, progress.Branch.NO_EVENT_YET, age=30),
        progress.Line(APPLYING, progress.Branch.SUMMARIZED, age=45),
    ],
    ids=lambda line: line.branch.name,
)
def test_a_lines_text_carries_each_of_its_fields(line: progress.Line) -> None:
    said = str(line)

    assert line.doing in said
    for step in line.named:
        assert {step.op, step.urn, progress.duration(step.seconds)} <= set(said.replace(',', ' ').split()), said
    counts = [count for count in (line.more, line.done, line.skipped) if count]
    assert all(re.search(rf'\b{count}\b', said) for count in counts), said
    if line.age is not None:
        assert progress.duration(line.age) in said.split(), said


@pytest.mark.parametrize(
    'printed',
    [
        _step('resourcePreEvent', 'update', REPO, 50),
        _step('resOutputsEvent', 'update', REPO, 50),
        _diagnostic('info', 50),
        _diagnostic('warning', 50),
    ],
    ids=['step-start', 'step-end', 'info', 'warning'],
)
def test_where_the_display_prints_the_run_what_it_printed_since_the_last_tick_makes_nothing_due(
    printed: dict[str, Any],
) -> None:
    narrator = _fed(
        progress.Narrator(APPLYING, shown=True, started=0), _step('resourcePreEvent', 'create', BOX, 1), ticked=2
    )
    assert narrator.due(40) is not None
    narrator.feed(printed)

    assert narrator.due(70) is None
    # The tick is taken: the next one, with nothing printed since, speaks.
    assert narrator.due(100) is not None


@pytest.mark.parametrize(
    'unprinted',
    [_diagnostic('debug', 50), _step('resourcePreEvent', 'same', REPO, 50)],
    ids=['debug', 'same-step'],
)
def test_what_the_display_does_not_print_does_not_hold_a_line(unprinted: dict[str, Any]) -> None:
    narrator = _fed(
        progress.Narrator(APPLYING, shown=True, started=0), _step('resourcePreEvent', 'create', BOX, 1), ticked=2
    )
    assert narrator.due(40) is not None
    narrator.feed(unprinted)

    assert narrator.due(70) is not None


def test_where_the_display_does_not_print_the_run_a_line_is_due_at_every_tick() -> None:
    narrator = progress.Narrator(PREVIEWING, shown=False, started=0)
    lines: list[progress.Line | None] = []
    for at in (10, 20, 30):
        narrator.feed(_step('resourcePreEvent', 'refresh', f'{REPO}{at}', at))
        lines.append(narrator.due(at + 5))

    assert all(line is not None for line in lines), lines


# --------------------------------------------------------------------------
# The event log.
# --------------------------------------------------------------------------


def test_the_log_is_read_in_complete_lines_across_writes(tmp_path: Path) -> None:
    path = tmp_path / 'events.jsonl'
    events = progress.EventLog(path)
    first, second, third = (json.dumps({'sequence': n}) for n in range(3))

    assert events.read() == ([], 0)  # a file that does not exist yet
    _ = path.write_text(first + '\n' + second[:5])
    assert events.read() == ([{'sequence': 0}], 0)
    with path.open('a') as handle:
        _ = handle.write(second[5:] + '\nnot an event\n' + third + '\n')
    assert events.read() == ([{'sequence': 1}, {'sequence': 2}], 1)
    assert events.read() == ([], 0)


def test_a_log_that_never_appears_reads_as_no_events(tmp_path: Path) -> None:
    events = progress.EventLog(tmp_path / 'missing' / 'events.jsonl')

    assert [events.read() for _ in range(3)] == [([], 0)] * 3


# --------------------------------------------------------------------------
# Whether pulumi's display is live.
# --------------------------------------------------------------------------


@pytest.fixture
def terminal() -> Iterator[tuple[int, int]]:
    """A pty of 40x120: its controlling side, and the side a run would inherit."""
    controller, inherited = pty.openpty()
    fcntl.ioctl(inherited, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    yield controller, inherited
    os.close(controller)
    os.close(inherited)


def test_a_terminal_of_real_dimensions_on_both_descriptors_is_live(terminal: tuple[int, int]) -> None:
    _, inherited = terminal

    assert progress.live({'TERM': 'xterm-256color'}, stdin=inherited, stdout=inherited)


@pytest.mark.parametrize('term', ['dumb', 'DUMB'])
def test_a_dumb_terminal_is_not_live(terminal: tuple[int, int], term: str) -> None:
    # pulumi lowercases `TERM` before it compares (`cmdutil.InteractiveTerminal`).
    _, inherited = terminal

    assert not progress.live({'TERM': term}, stdin=inherited, stdout=inherited)


def test_a_terminal_that_reports_no_size_is_not_live() -> None:
    controller, inherited = pty.openpty()
    try:
        assert not progress.live({'TERM': 'xterm-256color'}, stdin=inherited, stdout=inherited)
    finally:
        os.close(controller)
        os.close(inherited)


def test_a_pipe_on_either_descriptor_is_not_live(terminal: tuple[int, int]) -> None:
    _, inherited = terminal
    read, write = os.pipe()
    try:
        assert not progress.live({'TERM': 'xterm-256color'}, stdin=read, stdout=inherited)
        assert not progress.live({'TERM': 'xterm-256color'}, stdin=inherited, stdout=write)
    finally:
        os.close(read)
        os.close(write)


# --------------------------------------------------------------------------
# The driver's `Cli`, observing a stand-in `pulumi`.
# --------------------------------------------------------------------------

#: A `pulumi` that records how it was started, writes events to the log it is
#: handed and a plan to its standard output, says so on a FIFO of the case's
#: (its own standard error may be the driver's to capture), and blocks until
#: the case opens another; then exits with `STAND_IN_EXIT`.
STAND_IN = """\
#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ['STAND_IN_RECORD'], 'w') as record:
    json.dump({{'args': args, 'debug': os.environ.get('PULUMI_DEBUG_COMMANDS')}}, record)
if '--event-log' in args:
    with open(args[args.index('--event-log') + 1], 'a') as log:
        for event in json.loads(os.environ.get('STAND_IN_EVENTS', '[]')):
            log.write(json.dumps(event) + '\\n')
sys.stdout.write(os.environ.get('STAND_IN_PRINTS', ''))
sys.stdout.flush()
with open(os.environ['STAND_IN_READY'], 'w') as ready:
    ready.write('waiting\\n')
open(os.environ['STAND_IN_RELEASE']).read()
sys.exit(int(os.environ.get('STAND_IN_EXIT', '0')))
"""

#: The driver's `Cli`, in a process of its own: it runs one command the way
#: the driver does, its clock fixed at `NOW`, and writes what the command
#: returned to `STAND_IN_RESULT`.
DRIVING = """\
import json, logging, os, sys
from pathlib import Path
from kluster.scripts.operator_stack import driver, progress
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
progress.INTERVAL = float(os.environ['STAND_IN_INTERVAL'])
cli = driver.Cli(clock=lambda: float(os.environ['STAND_IN_NOW']))
how, doing = sys.argv[1], sys.argv[2] or None
cwd, env = Path.cwd(), dict(os.environ)
if how == 'events':
    result = cli.events(['preview', '--refresh', '--json', '--stack', 'probe'], cwd=cwd, env=env, doing=doing)
elif how == 'stream':
    result = cli.stream(['up', '--yes', '--stack', 'probe'], cwd=cwd, env=env, doing=doing)
else:
    result = cli.capture(['stack', 'export', '--stack', 'probe'], cwd=cwd, env=env)
Path(os.environ['STAND_IN_RESULT']).write_text(json.dumps(result))
"""

#: A tick that comes at once, so that several come while the stand-in blocks.
FAST = 0.01

#: How long a case waits on the driver's process once released: a stop-loss.
WAIT = 30


def _driver(
    tmp_path: Path, *, events: list[dict[str, Any]], prints: str = '', exit_code: int = 0
) -> tuple[dict[str, str], Path]:
    """The environment that runs `DRIVING` against `STAND_IN`, and the FIFO that releases the stand-in."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    stand_in = bin_dir / 'pulumi'
    _ = stand_in.write_text(STAND_IN.format(python=sys.executable))
    stand_in.chmod(0o755)
    release, ready = tmp_path / 'release', tmp_path / 'ready'
    os.mkfifo(release)
    os.mkfifo(ready)
    env = os.environ | {
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'STAND_IN_RECORD': str(tmp_path / 'record.json'),
        'STAND_IN_RESULT': str(tmp_path / 'result.json'),
        'STAND_IN_RELEASE': str(release),
        'STAND_IN_READY': str(ready),
        'STAND_IN_EVENTS': json.dumps(events),
        'STAND_IN_PRINTS': prints,
        'STAND_IN_EXIT': str(exit_code),
        'STAND_IN_INTERVAL': str(FAST),
        'STAND_IN_NOW': '1030',
        'TMPDIR': str(tmp_path / 'tmp'),
    }
    (tmp_path / 'tmp').mkdir()
    # The checkout the driver runs from, which the temporary directory is outside.
    (tmp_path / 'checkout').mkdir()
    return env, release


def _stand_in_waits(tmp_path: Path) -> None:
    """Read the stand-in's line saying it blocks: an event, which the stand-in sends once it has done the rest."""
    with (tmp_path / 'ready').open() as ready:
        assert ready.readline() == 'waiting\n', 'the stand-in never reached its wait'


def _run_driver(
    tmp_path: Path, how: str, doing: str | None, *, until: tuple[str, ...] | None, **stand_in: Any
) -> tuple[list[str], dict[str, Any], Any]:
    """Run `DRIVING`, read its standard error until a line holds every needle in `until`, then release.

    With no `until`, the release follows the stand-in's wait. The read is an
    event: the driver's own line, or the stand-in's; a line
    that never comes is the case's bound to end. Returns the lines read, how
    the stand-in was started, and what the command returned.
    """
    env, release = _driver(tmp_path, **stand_in)
    lines: list[str] = []
    with process_sessions.started(
        [sys.executable, '-c', DRIVING, how, doing or ''],
        cwd=tmp_path / 'checkout',
        env=env,
        stdout=process_sessions.DEVNULL,
        stderr=process_sessions.PIPE,
        text=True,
    ) as command:
        assert command.stderr is not None
        _stand_in_waits(tmp_path)
        while until is not None:
            line = command.stderr.readline()
            assert line, 'the driver ended before the line the case waits for:\n' + ''.join(lines)
            lines.append(line)
            if all(needle in line for needle in until):
                break
        with release.open('w'):
            pass
        _ = command.wait(timeout=WAIT)
        lines += command.stderr.readlines()
    record = json.loads((tmp_path / 'record.json').read_text())
    result = json.loads((tmp_path / 'result.json').read_text()) if (tmp_path / 'result.json').exists() else None
    return lines, record, result


def test_an_observed_preview_is_narrated_from_its_log_with_the_clock_it_is_handed(tmp_path: Path) -> None:
    events = [_step('resourcePreEvent', 'refresh', REPO, 1018)]

    awaited = (PREVIEWING, REPO, progress.duration(1030 - 1018))
    lines, record, _ = _run_driver(tmp_path, 'events', PREVIEWING, events=events, until=awaited)

    assert [line for line in lines if all(needle in line for needle in awaited) and line.startswith('INFO: ')], lines
    flag = record['args'].index('--event-log')
    assert record['debug'] == 'true'
    assert '--suppress-progress' not in record['args']
    # Ahead of nothing past it, and gone afterwards with its directory.
    assert not Path(record['args'][flag + 1]).parent.exists()


def test_the_previews_standard_output_is_what_pulumi_printed_and_never_the_log(tmp_path: Path) -> None:
    printed = '{"summaryEvent": {"resourceChanges": {"same": 3}}}\n{"cancelEvent": {}}\n'
    # A summary at 1020 and an event after it: the line awaited is the one
    # only the summary's age, 10 s at the stand-in clock's 1030, can produce.
    logged: list[dict[str, Any]] = [
        {'sequence': 1, 'timestamp': 1020, 'summaryEvent': {'resourceChanges': {'create': 9}}},
        {'sequence': 2, 'timestamp': 1025, 'cancelEvent': {}},
    ]

    _, _, result = _run_driver(
        tmp_path, 'events', PREVIEWING, events=logged, prints=printed, until=(PREVIEWING, progress.duration(10))
    )

    assert result == [0, printed]


@pytest.mark.parametrize('exit_code', [0, 3])
def test_an_observed_runs_exit_code_passes_through_and_its_directory_goes(tmp_path: Path, exit_code: int) -> None:
    _, record, result = _run_driver(tmp_path, 'stream', APPLYING, events=[], exit_code=exit_code, until=(APPLYING,))

    assert result == exit_code
    assert '--suppress-progress' in record['args']
    assert not Path(record['args'][record['args'].index('--event-log') + 1]).parent.exists()
    assert os.listdir(tmp_path / 'tmp') == []


@pytest.mark.parametrize(
    ('how', 'doing'),
    [('events', None), ('stream', None), ('capture', None)],
    ids=['a-preview-not-asked', 'a-passthrough', 'a-query'],
)
def test_a_run_not_asked_to_be_observed_gets_neither_the_flag_nor_the_variable(
    tmp_path: Path, how: str, doing: str | None
) -> None:
    lines, record, _ = _run_driver(tmp_path, how, doing, events=[], until=None)

    assert '--event-log' not in record['args']
    assert record['debug'] is None
    assert lines == []


def test_an_apply_at_a_live_terminal_is_not_observed(tmp_path: Path) -> None:
    env, release = _driver(tmp_path, events=[])
    controller, inherited = pty.openpty()
    fcntl.ioctl(inherited, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    try:
        with process_sessions.started(
            [sys.executable, '-c', DRIVING, 'stream', APPLYING],
            cwd=tmp_path / 'checkout',
            env=env | {'TERM': 'xterm-256color'},
            stdin=inherited,
            stdout=inherited,
            stderr=process_sessions.PIPE,
            text=True,
        ) as command:
            _stand_in_waits(tmp_path)
            with release.open('w'):
                pass
            _ = command.wait(timeout=WAIT)
    finally:
        os.close(controller)
        os.close(inherited)
    record = json.loads((tmp_path / 'record.json').read_text())

    assert '--event-log' not in record['args']
    assert record['debug'] is None


def test_a_temporary_directory_inside_the_checkout_is_not_used(tmp_path: Path) -> None:
    # `jj` would snapshot a log there, and it holds every value the program
    # does not mark secret: the run goes unnarrated, and says so.
    env, release = _driver(tmp_path, events=[])
    with process_sessions.started(
        [sys.executable, '-c', DRIVING, 'stream', APPLYING],
        cwd=tmp_path,
        env=env,
        stdout=process_sessions.DEVNULL,
        stderr=process_sessions.PIPE,
        text=True,
    ) as command:
        assert command.stderr is not None
        said = command.stderr.readline()
        _stand_in_waits(tmp_path)
        with release.open('w'):
            pass
        assert command.wait(timeout=WAIT) == 0
    record = json.loads((tmp_path / 'record.json').read_text())

    assert said.startswith('WARNING:') and APPLYING in said, said
    assert '--event-log' not in record['args']
    assert os.listdir(tmp_path / 'tmp') == []


#: An event the narrator cannot read: a step whose body is not an object.
UNREADABLE = {'sequence': 1, 'timestamp': 1020, 'resourcePreEvent': [1]}


def test_an_event_the_narrator_cannot_read_stops_the_narration_and_not_the_run(tmp_path: Path) -> None:
    lines, record, result = _run_driver(
        tmp_path, 'stream', APPLYING, events=[UNREADABLE], exit_code=3, until=('WARNING:',)
    )

    warned = [at for at, line in enumerate(lines) if line.startswith('WARNING:')]
    assert len(warned) == 1 and APPLYING in lines[warned[0]], lines
    assert not [line for line in lines[warned[0] + 1 :] if line.startswith('INFO:')], lines
    assert result == 3
    assert not Path(record['args'][record['args'].index('--event-log') + 1]).parent.exists()
    assert os.listdir(tmp_path / 'tmp') == []


def test_after_an_event_the_narrator_cannot_read_no_tick_narrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The ticks are called here, so "no more" is every tick after the
    # unreadable event rather than whichever came before a release.
    (tmp_path / 'tmp').mkdir()
    (tmp_path / 'checkout').mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'tmp'))
    with (
        caplog.at_level(logging.INFO, logger=progress.__name__),
        progress.observed(APPLYING, shown=False, clock=lambda: 1030.0, cwd=tmp_path / 'checkout') as observation,
    ):
        assert observation.beat is not None
        log_path = Path(observation.args[observation.args.index(progress.EVENT_LOG_FLAG) + 1])
        _ = log_path.write_text(json.dumps(UNREADABLE) + '\n')
        observation.beat()
        with log_path.open('a') as log:
            _ = log.write(json.dumps(_step('resourcePreEvent', 'create', BOX, 1025)) + '\n')
        for _ in range(3):
            observation.beat()

    said = [(record.levelno, record.getMessage()) for record in caplog.records]
    assert [message for level, message in said if level == logging.WARNING and APPLYING in message], said
    assert [level for level, _ in said] == [logging.WARNING], said
    assert os.listdir(tmp_path / 'tmp') == []
