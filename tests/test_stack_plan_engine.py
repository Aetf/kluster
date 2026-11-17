"""`stack-plan` against the pinned `pulumi`: a change no step carries reads as one an `up` would write.

The command's comparison reads two surfaces of a real preview -- the JSON
document's states and the event log's outputs -- whose shape only the engine
can produce, so each case runs the CLI `mise.toml` pins over a `file://`
backend and a `PULUMI_HOME` of the case's own (framework/testing.md §8),
against a program of stand-ins: two dynamic resources, `first` and `second`,
a component, `holder`, that registers an output, a stack output, and a
resource, `gathered`, that depends on several `part`s, some through
`depends_on` and some through its input, whose state therefore lists several
members in each of its dependency fields, and one, `read`, that the program
reads by its id rather than creates, so a preview carries a `read` step beside
the `same` ones. The
program reads the change a case makes from `PROBE_CHANGE`, so a baseline `up`
and the changed program are the same file under two environments.

Each change below is one an `up` writes and the preview plans no step for
(the program registers it, the state does not hold it yet), and each is
expressed without a provider plugin from the registry. After an `up` of the
changed program, the command reads it as nothing to write: a field the
comparison could never settle would otherwise ask for an approval on every
run.

The module also holds the layer's tripwire (style/README.md, "A workaround
for an upstream defect is a layer of its own"): the bare preview under
`--expect-no-changes` still exits 0 on each of those changes, which is the
defect the command answers. A release that refuses one turns that case red,
and the removal condition in `kluster.lib.state_only`'s docstring is then
what to read. The tripwire is the operator driver's too: the driver counts
the steps of its refreshed preview as the engine's `HasChanges` does, which
is what `--expect-no-changes` refuses on.

One case runs another program, `WAITING`, whose provider call never returns,
and holds that a signal to the command leaves no process of the run behind:
only the real CLI shows what a run asked to stop does with its plugins.

The command runs as its console script, the one `pyproject.toml` installs,
and every command leads a POSIX session of its own that ends with it
(`process_sessions`).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess as sp
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import process_sessions
import pytest
from scratch_projects import scratch_project

from kluster.scripts.stack_plan import cli

#: How long one case may take: its project's set-up and every command it runs
#: against the engine. On 24 cores at a load average between 5 and 12, with the
#: module's cases spread over six workers, the slowest case took 20 s, its
#: set-up included, but for the signal cases, which wait out `GRACE` by design
#: and took 40 s. Twice `COMMAND_TIMEOUT`, so a stalled command fails naming
#: itself before this fires; a stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one command -- a `pulumi` run, or the whole of `stack-plan` -- may
#: take before the case fails naming it: a stop-loss, many times the whole case
#: as measured, and below the case bound.
COMMAND_TIMEOUT = 120

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

#: The console script, installed beside the interpreter the suite runs on.
STACK_PLAN = Path(sys.executable).parent / 'stack-plan'

STACK = 'probe'

#: The secrets the program registers, as an input and as a stack output, and
#: the one its component registers under `PROBE_CHANGE=component output made
#: secret`, which nothing the command prints may carry.
SECRET_INPUT = 'an-input-nobody-may-read'
SECRET_OUTPUT = 'an-output-nobody-may-read'
SECRET_COMPONENT_OUTPUT = 'a-component-output-nobody-may-read'

PROGRAM = f"""\
import os

import pulumi
from pulumi.dynamic import CreateResult, Resource, ResourceProvider

CHANGE = os.environ.get('PROBE_CHANGE', '')


class Provider(ResourceProvider):
    def create(self, props):
        return CreateResult(id_='stand-in', outs=dict(props))


class Stand(Resource):
    def __init__(self, name, props, opts=None):
        super().__init__(Provider(), name, props, opts)


class Holder(pulumi.ComponentResource):
    def __init__(self, name):
        super().__init__('probe:index:Holder', name)
        held = {{
            'component output': 'changed',
            'component output made secret': pulumi.Output.secret({SECRET_COMPONENT_OUTPUT!r}),
        }}.get(CHANGE, 'as it was')
        self.register_outputs({{'held': held}})


holder = Holder('holder')

first_options = {{
    'protect': pulumi.ResourceOptions(protect=True),
    'retain on delete': pulumi.ResourceOptions(retain_on_delete=True),
    'renamed by an alias': pulumi.ResourceOptions(aliases=[pulumi.Alias(name='first')]),
    'moved under a parent by an alias': pulumi.ResourceOptions(
        parent=holder, aliases=[pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)]
    ),
    'ignore changes': pulumi.ResourceOptions(ignore_changes=['value']),
    'replace on changes': pulumi.ResourceOptions(replace_on_changes=['value']),
    'additional secret outputs': pulumi.ResourceOptions(additional_secret_outputs=['value']),
    'custom timeouts': pulumi.ResourceOptions(custom_timeouts=pulumi.CustomTimeouts(create='5m')),
}}.get(CHANGE)
first = Stand(
    'renamed' if CHANGE == 'renamed by an alias' else 'first',
    {{
        'value': 'changed' if CHANGE == 'input' else 'as it was',
        'token': pulumi.Output.secret({SECRET_INPUT!r}),
    }},
    first_options,
)

second_options = {{
    'depends on': pulumi.ResourceOptions(depends_on=[first]),
    'deleted with': pulumi.ResourceOptions(deleted_with=first),
}}.get(CHANGE)
Stand('second', {{'value': 'as it was'}}, second_options)

Stand('read', {{'value': 'as it was'}}, pulumi.ResourceOptions(id='stand-in'))

parts = [Stand(f'part-{{number}}', {{'value': str(number)}}) for number in range(8)]
Stand(
    'gathered',
    {{'value': pulumi.Output.all(*(part.urn for part in parts[4:])).apply(' '.join)}},
    pulumi.ResourceOptions(depends_on=parts[:4]),
)

pulumi.export('exported', 'changed' if CHANGE == 'stack output' else 'as it was')
pulumi.export('token', pulumi.Output.secret({SECRET_OUTPUT!r}))
if CHANGE == 'stack output added':
    pulumi.export('added', 'new')
"""

#: Every change of the program that a `same` step carries, by the name the
#: program reads it under.
STATE_ONLY = [
    'protect',
    'retain on delete',
    'depends on',
    'deleted with',
    'renamed by an alias',
    'moved under a parent by an alias',
    'ignore changes',
    'replace on changes',
    'additional secret outputs',
    'custom timeouts',
    'stack output',
    'stack output added',
    'component output',
]


@dataclass
class Engine:
    """One scratch project and its backend, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]

    def pulumi(self, *args: str, change: str = '') -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args],
            cwd=self.project,
            env=self.env | {'PROBE_CHANGE': change},
            text=True,
            timeout=COMMAND_TIMEOUT,
        )

    def up(self, change: str = '') -> None:
        applied = self.pulumi('up', '--yes', '--skip-preview', '--stack', STACK, change=change)
        assert applied.returncode == 0, applied.stdout + applied.stderr

    def stack_plan(self, change: str = '') -> sp.CompletedProcess[str]:
        return process_sessions.run(
            [str(STACK_PLAN), '--stack', STACK],
            cwd=self.project,
            env=self.env | {'PROBE_CHANGE': change},
            text=True,
            timeout=COMMAND_TIMEOUT,
        )


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def launched(tmp_path: Path) -> Engine:
    """A fresh stack after its first `up` of the unchanged program."""
    scratch = scratch_project(tmp_path, PROGRAM)
    engine = Engine(project=scratch.directory, env=scratch.env)
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = engine.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    engine.up()
    return engine


def test_an_unchanged_program_would_write_nothing_whatever_order_its_dependencies_come_in(launched: Engine) -> None:
    """`gathered`'s dependencies come in a fresh order on every registration, and are the same members."""
    planned = launched.stack_plan()

    assert planned.returncode == cli.NOTHING, _ran(planned)


def test_an_input_change_would_write(launched: Engine) -> None:
    planned = launched.stack_plan('input')

    assert planned.returncode == cli.WOULD_WRITE, _ran(planned)


@pytest.mark.parametrize('change', STATE_ONLY)
def test_a_change_no_step_carries_would_write_until_an_up_writes_it(launched: Engine, change: str) -> None:
    planned = launched.stack_plan(change)

    assert planned.returncode == cli.WOULD_WRITE, _ran(planned)

    launched.up(change)
    again = launched.stack_plan(change)

    assert again.returncode == cli.NOTHING, _ran(again)


@pytest.mark.parametrize('change', STATE_ONLY)
def test_the_bare_preview_still_exits_0_on_a_change_no_step_carries(launched: Engine, change: str) -> None:
    """The defect `stack-plan` and the operator driver answer: the preview's own exit counts steps alone."""
    previewed = launched.pulumi('preview', '--stack', STACK, '--expect-no-changes', change=change)

    assert previewed.returncode == 0, (
        f'the pinned `pulumi preview --expect-no-changes` now refuses {change!r} (exit {previewed.returncode}): '
        'read the removal condition in the docstring of kluster.lib.state_only. A release that refuses '
        'every change of STATE_ONLY is the one that removes the layer; one that refuses some of them leaves '
        'the command answering for the rest, and the tripwire lists only those.'
    )


def test_a_plan_that_rewrites_a_secret_prints_no_secret(launched: Engine) -> None:
    """A component output turned secret is a line of the plan, beside a secret input and a secret stack output."""
    planned = launched.stack_plan('component output made secret')

    assert planned.returncode == cli.WOULD_WRITE, _ran(planned)
    for secret in (SECRET_INPUT, SECRET_OUTPUT, SECRET_COMPONENT_OUTPUT):
        assert secret not in _ran(planned)


#: A program whose one resource's provider, asked to diff it while
#: `PROBE_WAITING` names a file, writes that file and never answers: a
#: preview of it waits on a provider call that does not return, the run a
#: single SIGINT or SIGTERM cancels without ending.
WAITING = """\
import os
import pathlib
import time

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider


class Provider(ResourceProvider):
    def create(self, props):
        return CreateResult(id_='stand-in', outs=dict(props))

    def diff(self, _id, olds, news):
        if waiting := os.environ.get('PROBE_WAITING'):
            pathlib.Path(waiting).write_text('waiting')
            while True:
                time.sleep(60)
        return DiffResult(changes=False)


class Stand(Resource):
    def __init__(self, name, props):
        super().__init__(Provider(), name, props)


Stand('waits', {'value': 'as it was'})
"""


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGHUP], ids=lambda signum: signum.name)
def test_a_signal_to_the_command_leaves_no_process_of_the_run_behind(tmp_path: Path, signum: signal.Signals) -> None:
    """The run waits on a provider call, so it ends only on a second asking, and stops its plugins as it does.

    Whatever is left in the command's POSIX session once the command has died
    is a process of the run: `pulumi` itself, its language host or its
    provider.
    """
    scratch = scratch_project(tmp_path, WAITING)
    engine = Engine(project=scratch.directory, env=scratch.env)
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    engine.up()
    waiting = tmp_path / 'waiting'
    # outside process_sessions -- the case reads what is left of the session
    # once its leader has died, which `process_sessions.members` reads from the
    # leader's own `Popen`; `process_sessions.end` ends the session after it.
    command = sp.Popen(
        # SIGHUP at its default whatever this process inherited: a suite
        # launched under `nohup` hands its children SIGHUP ignored, which the
        # command keeps ignoring.
        ['env', '--default-signal=HUP', str(STACK_PLAN), '--stack', STACK],
        cwd=engine.project,
        env=engine.env | {'PROBE_WAITING': str(waiting)},
        stdin=sp.DEVNULL,
        stdout=sp.DEVNULL,
        stderr=sp.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while not waiting.exists():
            assert time.monotonic() < deadline, 'the preview never reached the provider call'
            time.sleep(0.1)

        os.kill(command.pid, signum)

        assert command.wait(timeout=COMMAND_TIMEOUT) == -signum
        assert [str(member) for member in process_sessions.members(command)] == []
    finally:
        _ = process_sessions.end(command)
