"""`stack-plan`'s answer from each exit and each record of its two previews, against a stand-in `pulumi`.

The stand-in is an executable first on `PATH` that answers as the pinned CLI
does, from a scenario the case writes: the plain preview exits with the
scenario's status; the JSON preview prints the scenario's document, writes
its events to the `--event-log` path and exits with its status, and, as the
pinned CLI does, refuses `--event-log` as an unknown flag unless
`PULUMI_DEBUG_COMMANDS` is `true`. The document and the events are the shapes
`pulumi preview --json --show-sames` and its event log have at the pinned
release; `tests/test_stack_plan_engine.py` holds the command to the real ones.
A run the scenario gives a behavior rather than a status runs until it is
stopped, as the pinned CLI does on a program or a provider call that does
not return (`BEHAVIORS`), and writes its pid and its plugin's beside the
scenario once it is ready for a signal.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import process_sessions
import pytest

from kluster.scripts.stack_plan import cli, preview

STAND_IN = f"""\
#!{sys.executable}
import json
import os
import signal
import subprocess
import sys
import time

scenario = json.loads(open(os.environ['STACK_PLAN_SCENARIO']).read())
args = sys.argv[1:]
written = os.environ['STACK_PLAN_SCENARIO']


def behave(behavior):
    if behavior == 'reads the terminal':
        if not sys.stdin.read(1):
            print('error: passphrase must be set with PULUMI_CONFIG_PASSPHRASE', file=sys.stderr)
            sys.exit(255)
        sys.exit(0)
    plugin = None
    if behavior != 'stuck':
        plugin = subprocess.Popen(
            [sys.executable, '-c', 'import time\\nwhile True: time.sleep(60)'], process_group=0
        )
    asked = []

    def answer(signum, frame):
        asked.append(signum)
        if behavior in ('cancels on the second signal', 'drops the second asking') and len(asked) == 1:
            open(written + '.asked', 'w').close()
            return
        if behavior == 'drops the second asking' and len(asked) == 2:
            return
        os.killpg(plugin.pid, signal.SIGKILL)
        plugin.wait()
        sys.exit(1)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, signal.SIG_IGN if behavior == 'stuck' else answer)
    with open(written + '.pid.part', 'w') as pids:
        json.dump({{'pulumi': os.getpid(), 'plugin': None if plugin is None else plugin.pid}}, pids)
    os.rename(written + '.pid.part', written + '.pid')
    while True:
        time.sleep(60)


if '--json' not in args:
    if isinstance(scenario['plain'], str):
        behave(scenario['plain'])
    sys.exit(scenario['plain'])
if os.environ.get('PULUMI_DEBUG_COMMANDS') != 'true':
    print('error: unknown flag: --event-log', file=sys.stderr)
    sys.exit(1)
second = scenario['json']
if isinstance(second['status'], str):
    behave(second['status'])
if second['events'] is not None:
    with open(args[args.index('--event-log') + 1], 'w') as log:
        log.writelines(json.dumps(event) + '\\n' for event in second['events'])
sys.stdout.write(second['document'])
sys.exit(second['status'])
"""

STACK = 'physical'
URN = 'urn:pulumi:physical::kluster::pulumi-python:dynamic:Resource::first'
STACK_URN = 'urn:pulumi:physical::kluster::pulumi:pulumi:Stack::kluster-physical'

#: A resource's state as the JSON preview serializes it, with nothing that
#: differs between the two sides of a `same` step.
STATE: dict[str, object] = {
    'urn': URN,
    'custom': True,
    'type': 'pulumi-python:dynamic:Resource',
    'inputs': {'value': 'as it was', 'token': '[secret]'},
    'parent': STACK_URN,
    'provider': 'urn:pulumi:physical::kluster::pulumi:providers:pulumi-python::default::0d0c',
    'propertyDependencies': {'value': [], 'token': []},
    'created': '2026-11-17T10:03:00.787207002Z',
    'modified': '2026-11-17T10:03:00.787207002Z',
}


#: The key the pinned CLI's events mark a secret, an asset or an archive with.
SIG_KEY = '4dabf18193072939515e22adb298388d'


def secret(ciphertext: str = '[secret]') -> dict[str, str]:
    """A secret as the pinned CLI's event log carries it: the secret signature, and what it shows of the value."""
    return {SIG_KEY: '1b47061264138c4ac30d75fd1eb44270', 'ciphertext': ciphertext}


def outputs_event(urn: str, old: dict[str, object] | None, new: dict[str, object] | None) -> dict[str, object]:
    return {
        'resOutputsEvent': {
            'metadata': {
                'op': 'same',
                'urn': urn,
                'old': None if old is None else {'outputs': old},
                'new': None if new is None else {'outputs': new},
            }
        }
    }


#: The outputs event of the resource `STATE` is, the same on both sides.
RESOURCE_OUTPUTS = outputs_event(URN, {'value': 'as it was'}, {'value': 'as it was'})

SUMMARY: dict[str, object] = {'summaryEvent': {'resourceChanges': {'same': 2}}}


#: What a run the scenario gives no status does, as the pinned CLI does.
BEHAVIORS = {
    #: Ends on the first SIGINT or SIGTERM, stopping its plugin: a `pulumi`
    #: whose program does not return. SIGHUP, which it does not handle, kills
    #: it and leaves the plugin.
    'cancels on one signal',
    #: Cancels on the first SIGINT or SIGTERM and keeps waiting, writing
    #: `.asked` beside the scenario; ends on the second, stopping its plugin:
    #: a `pulumi` waiting on a provider call that does not return.
    'cancels on the second signal',
    #: As `cancels on the second signal`, but takes the second asking for
    #: part of the first and ends on the third: a `pulumi` sent two askings
    #: microseconds apart, which it counts as one.
    'drops the second asking',
    #: Ignores SIGINT and SIGTERM and starts no plugin: only a kill ends it.
    'stuck',
    #: Reads its standard input, and fails at once, naming what it wanted, at its end.
    'reads the terminal',
}


@dataclass
class Scenario:
    """What the stand-in answers: the plain preview's status, and the JSON preview's document, events and status."""

    #: The plain preview's exit status, or one of `BEHAVIORS`.
    plain: int | str = 0
    #: The JSON preview's exit status, or one of `BEHAVIORS`.
    status: int | str = 0
    old: dict[str, object] = field(default_factory=lambda: dict(STATE))
    new: dict[str, object] = field(default_factory=lambda: dict(STATE))
    #: The event log's lines, its summary included; `None` writes no log.
    events: list[dict[str, object]] | None = field(
        default_factory=lambda: [
            RESOURCE_OUTPUTS,
            outputs_event(STACK_URN, {'address': '10.0.5.1'}, {'address': '10.0.5.1'}),
            SUMMARY,
        ]
    )
    #: The JSON preview's document as printed; `None` prints the steps of `old` and `new`.
    document: str | None = None

    def write(self, path: Path) -> None:
        document = self.document
        if document is None:
            document = json.dumps(
                {
                    'steps': [{'op': 'same', 'urn': URN, 'oldState': self.old, 'newState': self.new}],
                    'changeSummary': {'same': 2},
                }
            )
        _ = path.write_text(
            json.dumps(
                {
                    'plain': self.plain,
                    'json': {'status': self.status, 'document': document, 'events': self.events},
                }
            )
        )


@dataclass
class Plan:
    """What one run of the command answered, and what it printed on each stream."""

    status: int
    out: str
    err: str


#: How long a case waits on the command or the stand-in: a stop-loss, which nothing asserts on.
WAIT = 30

#: The console script, installed beside the interpreter the suite runs on.
STACK_PLAN = Path(sys.executable).parent / 'stack-plan'

#: The command as a case that signals it starts it: with SIGHUP at its
#: default, whatever this process inherited. A suite launched under `nohup`
#: hands its children SIGHUP ignored, which the command keeps ignoring.
COMMAND = ['env', '--default-signal=HUP', str(STACK_PLAN), '--stack', STACK]


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The stand-in first on `PATH`, answering the scenario at the path returned."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    stand_in = bin_dir / 'pulumi'
    _ = stand_in.write_text(STAND_IN)
    stand_in.chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setenv('STACK_PLAN_SCENARIO', str(tmp_path / 'scenario.json'))
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.delenv('PULUMI_DEBUG_COMMANDS', raising=False)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    scenario = tmp_path / 'scenario.json'
    yield scenario
    # A case that fails leaves what it started: the run, and its plugin.
    written = scenario.with_name(scenario.name + '.pid')
    if written.exists():
        for pid in json.loads(written.read_text()).values():
            if pid is not None:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@pytest.fixture
def plan(stand_in: Path, capfd: pytest.CaptureFixture[str]) -> Callable[[Scenario], Plan]:
    """Run `stack-plan --stack physical` in this process against the stand-in answering `scenario`."""

    def run(scenario: Scenario) -> Plan:
        scenario.write(stand_in)
        _ = capfd.readouterr()
        status = cli.main(['--stack', STACK])
        captured = capfd.readouterr()
        return Plan(status=status, out=captured.out, err=captured.err)

    return run


def test_a_same_step_whose_copied_and_relocated_fields_alone_differ_would_write_nothing(
    plan: Callable[[Scenario], Plan],
) -> None:
    """`id` and `outputs` are copied from the old state, and a registration's position moves with every edit."""
    old = STATE | {'id': 'stand-in', 'outputs': {'value': 'as it was'}, 'sourcePosition': 'project:///a.py#1'}
    new = STATE | {'sourcePosition': 'project:///a.py#9', 'stackTrace': [{'sourcePosition': 'project:///a.py#9'}]}

    planned = plan(Scenario(old=old, new=new))

    assert planned.status == cli.NOTHING, planned.out + planned.err


def test_a_step_the_first_preview_plans_would_write_whatever_the_second_would_say(
    plan: Callable[[Scenario], Plan],
) -> None:
    planned = plan(Scenario(plain=cli.REFUSED, status=1, document='', events=None))

    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err


#: The pinned CLI's exits other than success and `--expect-no-changes`'s
#: refusal (sdk/go/common/util/cmdutil/exit.go at v3.274.1): an error, a
#: configuration error, an authentication error, a resource error, a policy
#: violation, a stack not found, a cancel, a timeout, an internal error.
FAILURES = [1, 2, 3, 4, 5, 6, 8, 9, 255]


@pytest.mark.parametrize('status', FAILURES)
def test_a_first_preview_that_fails_is_no_answer(plan: Callable[[Scenario], Plan], status: int) -> None:
    """Whatever it exits with, and whatever the second would say."""
    planned = plan(Scenario(plain=status, new=STATE | {'protect': True}))

    assert planned.status not in (cli.NOTHING, cli.WOULD_WRITE)


def test_a_step_only_the_json_preview_plans_would_write(plan: Callable[[Scenario], Plan]) -> None:
    planned = plan(Scenario(status=cli.REFUSED))

    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err


def test_a_json_preview_that_fails_is_no_answer_and_prints_its_diagnostics(plan: Callable[[Scenario], Plan]) -> None:
    """The pinned CLI writes such a failure into the document and nothing on standard error."""
    message = 'error: Program failed with an unhandled exception: RuntimeError: the program raised this'
    document = json.dumps({'steps': [], 'diagnostics': [{'message': message + '\n', 'severity': 'error'}]})

    planned = plan(Scenario(status=255, document=document, events=None))

    assert planned.status not in (cli.NOTHING, cli.WOULD_WRITE)
    assert message in planned.out + planned.err


@pytest.mark.parametrize(
    ('old', 'new'),
    [
        pytest.param({}, {'protect': True}, id='an option set'),
        pytest.param({'protect': True}, {}, id='an option cleared'),
        pytest.param({}, {'aFieldOfALaterRelease': 'anything'}, id='a field no release names today'),
        pytest.param({'retainOnDelete': 1}, {'retainOnDelete': True}, id='a value equal only in Python'),
        pytest.param({'deletedWith': None}, {}, id='a null against an absent field'),
    ],
)
def test_a_state_field_the_program_moved_would_write(
    plan: Callable[[Scenario], Plan], old: dict[str, object], new: dict[str, object]
) -> None:
    planned = plan(Scenario(old=STATE | old, new=STATE | new))

    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err


@pytest.mark.parametrize(
    ('old', 'new'),
    [
        pytest.param({'dependencies': ['a', 'b', 'c']}, {'dependencies': ['c', 'a', 'b']}, id='dependencies'),
        pytest.param(
            {'propertyDependencies': {'value': ['a', 'b'], 'token': []}},
            {'propertyDependencies': {'value': ['b', 'a'], 'token': []}},
            id='the dependencies of an input',
        ),
    ],
)
def test_the_same_dependencies_in_another_order_would_write_nothing(
    plan: Callable[[Scenario], Plan], old: dict[str, object], new: dict[str, object]
) -> None:
    """The engine lists a set's members in no fixed order, so a converged stack lists them afresh on every preview."""
    planned = plan(Scenario(old=STATE | old, new=STATE | new))

    assert planned.status == cli.NOTHING, planned.out + planned.err


@pytest.mark.parametrize(
    ('old', 'new'),
    [
        pytest.param({'dependencies': ['a', 'b']}, {'dependencies': ['b', 'c', 'a']}, id='a dependency added'),
        pytest.param({'dependencies': ['a', 'b', 'c']}, {'dependencies': ['c', 'a']}, id='a dependency removed'),
        pytest.param(
            {'propertyDependencies': {'value': ['a'], 'token': []}},
            {'propertyDependencies': {'value': ['b', 'a'], 'token': []}},
            id='a dependency of an input added',
        ),
        pytest.param(
            {'propertyDependencies': {'value': ['a', 'b'], 'token': []}},
            {'propertyDependencies': {'value': ['b'], 'token': []}},
            id='a dependency of an input removed',
        ),
    ],
)
def test_a_dependency_added_or_removed_would_write(
    plan: Callable[[Scenario], Plan], old: dict[str, object], new: dict[str, object]
) -> None:
    planned = plan(Scenario(old=STATE | old, new=STATE | new))

    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err


def test_the_plan_names_each_resource_and_each_field_it_rewrites(plan: Callable[[Scenario], Plan]) -> None:
    """A map field is opened a level, so its line names the key that moved."""
    planned = plan(
        Scenario(
            new=STATE | {'protect': True, 'customTimeouts': {'create': 300}},
            events=[
                RESOURCE_OUTPUTS,
                outputs_event(STACK_URN, {'address': '10.0.5.1'}, {'address': '10.0.5.2'}),
                SUMMARY,
            ],
        )
    )

    lines = planned.out.splitlines()
    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err
    stripped = [line.strip() for line in lines]
    at_resource, at_stack = stripped.index(URN), stripped.index(STACK_URN)
    under_resource = lines[at_resource + 1 : at_stack] if at_resource < at_stack else lines[at_resource + 1 :]
    under_stack = lines[at_stack + 1 : at_resource] if at_stack < at_resource else lines[at_stack + 1 :]
    assert any('protect' in line and 'true' in line for line in under_resource), lines
    assert any('customTimeouts.create' in line and '300' in line for line in under_resource), lines
    assert any('outputs.address' in line and '10.0.5.1' in line and '10.0.5.2' in line for line in under_stack), lines


def test_an_output_differs_by_the_last_event_of_its_resource(plan: Callable[[Scenario], Plan]) -> None:
    """The first event of a resource is its registration; the last holds the outputs the program registers."""
    planned = plan(
        Scenario(
            events=[
                RESOURCE_OUTPUTS,
                outputs_event(STACK_URN, {'address': '10.0.5.1'}, {'address': '10.0.5.1'}),
                outputs_event(STACK_URN, {'address': '10.0.5.1'}, {'address': '10.0.5.2'}),
                SUMMARY,
            ]
        )
    )

    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err


def test_an_output_that_differed_before_its_last_event_would_write_nothing(plan: Callable[[Scenario], Plan]) -> None:
    planned = plan(
        Scenario(
            events=[
                RESOURCE_OUTPUTS,
                outputs_event(STACK_URN, {'address': '10.0.5.1'}, {}),
                outputs_event(STACK_URN, {'address': '10.0.5.1'}, {'address': '10.0.5.1'}),
                SUMMARY,
            ]
        )
    )

    assert planned.status == cli.NOTHING, planned.out + planned.err


def test_a_secret_prints_as_the_clis_placeholder_whatever_its_event_carries(plan: Callable[[Scenario], Plan]) -> None:
    carried = 'what-the-event-carried-beside-the-signature'

    planned = plan(
        Scenario(
            events=[RESOURCE_OUTPUTS, outputs_event(STACK_URN, {'token': 'plain'}, {'token': secret(carried)}), SUMMARY]
        )
    )

    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err
    assert carried not in planned.out + planned.err
    assert SIG_KEY not in planned.out + planned.err
    assert any('outputs.token' in line and '[secret]' in line for line in planned.out.splitlines())


@pytest.mark.parametrize(
    'scenario',
    [
        pytest.param(Scenario(document='Previewing update (physical):'), id='a document that is not JSON'),
        pytest.param(Scenario(document=json.dumps({'changeSummary': {'same': 2}})), id='a document with no steps'),
        pytest.param(Scenario(events=None), id='no event log'),
        pytest.param(
            Scenario(document=json.dumps({'steps': [{'op': 'same', 'urn': URN}]})), id='a step with neither state'
        ),
        pytest.param(
            Scenario(events=[outputs_event(STACK_URN, {}, {}), SUMMARY]), id='a step whose outputs event the log lost'
        ),
        pytest.param(
            Scenario(
                events=[RESOURCE_OUTPUTS, outputs_event(STACK_URN, {'address': '10.0.5.1'}, {'address': '10.0.5.1'})]
            ),
            id='an event log cut short of its summary',
        ),
    ],
)
def test_a_record_that_does_not_say_what_an_up_writes_is_no_answer(
    plan: Callable[[Scenario], Plan], scenario: Scenario
) -> None:
    planned = plan(scenario)

    assert planned.status not in (cli.NOTHING, cli.WOULD_WRITE)


def test_a_state_only_plan_annotates_the_run_on_github_actions(
    plan: Callable[[Scenario], Plan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow command on a line of its own, which the runner turns into an annotation on the run page."""
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')

    planned = plan(Scenario(new=STATE | {'protect': True}))

    assert planned.status == cli.WOULD_WRITE, planned.out + planned.err
    assert any(line.startswith('::notice::') for line in planned.out.splitlines())


@dataclass(frozen=True)
class Hung:
    """A run of the stand-in that is waiting for a signal: its pid, and its plugin's where it started one."""

    pulumi: int
    plugin: int | None

    def gone(self) -> bool:
        return all(_gone(pid) for pid in (self.pulumi, self.plugin) if pid is not None)


def _written(path: Path) -> None:
    deadline = time.monotonic() + WAIT
    while not path.exists():
        assert time.monotonic() < deadline, f'the stand-in never wrote {path.name}'
        time.sleep(0.05)


def _hung(scenario: Path) -> Hung:
    """The stand-in's run, once it is ready for a signal."""
    written = scenario.with_name(scenario.name + '.pid')
    _written(written)
    pids = json.loads(written.read_text())
    return Hung(pulumi=pids['pulumi'], plugin=pids['plugin'])


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _started(scenario: Scenario, stand_in: Path) -> AbstractContextManager[process_sessions.Command]:
    scenario.write(stand_in)
    return process_sessions.started(COMMAND, env=os.environ)


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda signum: signum.name)
def test_a_signal_that_ends_the_command_ends_its_run_and_the_runs_plugins_first(
    stand_in: Path, signum: signal.Signals
) -> None:
    """SIGHUP among them, which the CLI does not handle and is passed on as SIGTERM."""
    with _started(Scenario(plain='cancels on one signal'), stand_in) as command:
        hung = _hung(stand_in)

        os.kill(command.pid, signum)

        assert command.wait(timeout=WAIT) == -signum
        assert hung.gone()


def test_a_second_signal_asks_the_run_again_rather_than_killing_it(stand_in: Path) -> None:
    """A run killed outright would leave its plugin; asked again, it stops the plugin itself."""
    with _started(Scenario(plain='cancels on the second signal'), stand_in) as command:
        hung = _hung(stand_in)
        os.kill(command.pid, signal.SIGTERM)
        _written(stand_in.with_name(stand_in.name + '.asked'))

        os.kill(command.pid, signal.SIGTERM)

        assert command.wait(timeout=WAIT) == -signal.SIGTERM
        assert hung.gone()


@pytest.mark.timeout(4 * preview.GRACE)
def test_a_second_signal_does_not_bring_the_kill_forward(stand_in: Path) -> None:
    """The run takes the second asking for part of the first, and ends on the one sent at `GRACE`, not on a kill."""
    with _started(Scenario(plain='drops the second asking'), stand_in) as command:
        hung = _hung(stand_in)
        os.kill(command.pid, signal.SIGTERM)
        _written(stand_in.with_name(stand_in.name + '.asked'))

        os.kill(command.pid, signal.SIGTERM)

        assert command.wait(timeout=3 * preview.GRACE) == -signal.SIGTERM
        assert hung.gone()


class _SignalInsideTheWaitLock:
    """`Popen`'s wait lock, with a SIGTERM handled the moment a timed wait has taken it.

    CPython's timed `Popen.wait` takes the lock with `acquire(False)` and
    releases it in a `finally` that starts on the next line, so a handler
    that raised there would leave it held and the child unreapable for good.
    This lock runs the handler installed for SIGTERM at exactly that point,
    once the stand-in is ready for the signal, as a signal landing there would.
    An exception the handler raises is recorded in `raised` and goes on, with
    the lock released first, so a handler that raises fails the case by name
    rather than leaving the run unreapable.
    """

    def __init__(self, ready: Path, raised: list[BaseException]) -> None:
        self._lock = threading.Lock()
        self._ready = ready
        self._fired = False
        self._raised = raised

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        taken = self._lock.acquire(blocking, timeout)
        if taken and not blocking and not self._fired and self._ready.exists():
            self._fired = True
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler), 'the run installed no handler for SIGTERM'
            try:
                handler(signal.SIGTERM, None)
            except BaseException as exc:
                self._lock.release()
                self._raised.append(exc)
                raise
        return taken

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def test_a_signal_handled_inside_the_wait_still_lets_the_run_be_reaped(
    stand_in: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler raises nothing into `Popen`'s locking, so the run ends on the signal and is reaped."""
    raised: list[BaseException] = []
    monkeypatch.setattr(
        subprocess,
        'threading',
        SimpleNamespace(Lock=lambda: _SignalInsideTheWaitLock(stand_in.with_name(stand_in.name + '.pid'), raised)),
    )
    Scenario(plain='cancels on one signal').write(stand_in)

    with pytest.raises(preview.Signalled) as signalled:
        _ = preview.run(['preview', '--stack', STACK])

    assert raised == []
    assert signalled.value.signum == signal.SIGTERM
    assert _hung(stand_in).gone()


@pytest.mark.timeout(4 * preview.GRACE)
def test_a_signal_the_run_does_not_answer_is_sent_again_once_the_grace_has_passed(stand_in: Path) -> None:
    """The run keeps waiting on the first asking, and ends on the one sent at `GRACE`."""
    with _started(Scenario(plain='cancels on the second signal'), stand_in) as command:
        hung = _hung(stand_in)

        os.kill(command.pid, signal.SIGTERM)

        assert command.wait(timeout=3 * preview.GRACE) == -signal.SIGTERM
        assert hung.gone()


def test_a_signal_the_command_was_started_ignoring_stays_ignored(stand_in: Path) -> None:
    """As `nohup` starts it: a SIGHUP then a SIGTERM, delivered in that order, and the command dies of the SIGTERM."""
    Scenario(plain='cancels on one signal').write(stand_in)
    with process_sessions.started(
        ['env', '--ignore-signal=HUP', str(STACK_PLAN), '--stack', STACK], env=os.environ
    ) as command:
        hung = _hung(stand_in)

        os.kill(command.pid, signal.SIGHUP)
        os.kill(command.pid, signal.SIGTERM)

        assert command.wait(timeout=WAIT) == -signal.SIGTERM
        assert hung.gone()


def test_a_signal_during_the_json_run_leaves_no_event_log_behind(stand_in: Path, tmp_path: Path) -> None:
    """The command dies of the signal once its temporary directory is gone."""
    temporary = tmp_path / 'tmp'
    temporary.mkdir()
    Scenario(status='cancels on one signal').write(stand_in)
    with process_sessions.started(COMMAND, env=os.environ | {'TMPDIR': str(temporary)}) as command:
        hung = _hung(stand_in)

        os.kill(command.pid, signal.SIGTERM)

        assert command.wait(timeout=WAIT) == -signal.SIGTERM
        assert hung.gone()
        assert list(temporary.iterdir()) == []


def test_a_run_that_reads_the_terminal_reads_nothing_and_fails_at_once(stand_in: Path) -> None:
    """Started with a terminal that never answers -- a pipe nothing writes to -- the command still ends, with no answer."""
    Scenario(plain='reads the terminal').write(stand_in)
    with process_sessions.started(COMMAND, env=os.environ, stdin=process_sessions.PIPE) as command:
        assert command.wait(timeout=WAIT) == cli.NO_ANSWER


@pytest.mark.parametrize('behavior', ['cancels on the second signal', 'stuck'])
def test_a_run_still_going_at_its_bound_is_stopped_and_no_answer(
    plan: Callable[[Scenario], Plan], monkeypatch: pytest.MonkeyPatch, stand_in: Path, behavior: str
) -> None:
    """Asked twice, `GRACE` apart, so it stops its plugins; killed past the second asking where it answers neither."""
    monkeypatch.setattr(preview, 'TIMEOUT', 0.5)
    monkeypatch.setattr(preview, 'GRACE', 0.5)

    planned = plan(Scenario(plain=behavior))

    assert planned.status == cli.NO_ANSWER, planned.out + planned.err
    assert _hung(stand_in).gone()
