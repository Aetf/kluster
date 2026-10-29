"""A failure in a mocked run's RPC or output fails the case that caused it, unless the case received it.

A registration is carried out in a task of its own, and under the mocks on an
executor thread, so its failure -- the recorder's refusal of a duplicate URN,
a component's refusal reaching a resource's inputs -- reaches a case only if
the case waits for it; so does the failure of an output nothing declared
reads, which no RPC carries at all. `conftest`'s `pytest_runtest_call` hook
does that wait for every case through
`mock_monitor.fail_on_unreceived_failures`, and the property is about how a
case *ends*, so it is held in a child run under this directory's `conftest`,
the way `tests/test_tracked_outputs.py` holds its own: a case cannot watch its
own outcome.

Two shapes of run are held. A case's own run, where each failure is judged on
its own, so a receipt of one refusal receives that refusal and nothing beside
it; and a module's shared run, whose later cases are judged as its first is,
each by what it started. And the child run as a whole logs no failure as
never retrieved: every failure it met was read by the judgment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import process_sessions

ROOT = Path(__file__).parent.parent

#: The child's cases on runs of their own. Eight must fail: one registers a
#: URN twice and awaits nothing, which is the failure the hook exists to
#: catch; two register it twice inside a block whose exception -- the block's
#: own, or a task's that failed first -- the case catches, with the duplicate
#: only a note on it; two register it twice once a refusal has failed and
#: then receive the refusal, which receives nothing beside it; one leaves an
#: output failed that nothing declared reads, so no RPC carries the failure;
#: one leaves such an output failing only once everything else has settled,
#: which the judgment waits for; and one leaves such an output beside a
#: refusal it receives, of another root. Each of the others meets a failure the case does receive, or none,
#: or the one a real run passes over -- an output faulted as the engine
#: faults a resource that failed to register -- and must pass. One of those
#: receives a refusal that failed several outputs and no RPC, and the receipt
#: covers them all; another receives a refusal whose resource's outputs fail
#: with the SDK's exception around it, which the receipt covers by its root.
CHILD = """
import asyncio

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with, the_run_failed_with
from pulumi.runtime.stack import ResourceRegistrationFailed

THING = 'test:index:Thing'


class Refused(ValueError):
    '''What a component raises on a bad argument, reaching a resource's inputs.'''


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Recorder:
    return await run_with(Recorder(), stack='failures', project='mock-monitor')


@pytest.mark.asyncio
async def test_a_duplicate_nothing_awaits() -> None:
    _ = pulumi.CustomResource(THING, 'twice', {})
    _ = pulumi.CustomResource(THING, 'twice', {})


@pytest.mark.asyncio
async def test_a_duplicate_the_barrier_raises() -> None:
    with pytest.raises(AssertionError):
        async with declaring():
            _ = pulumi.CustomResource(THING, 'twice', {})
            _ = pulumi.CustomResource(THING, 'twice', {})


@pytest.mark.asyncio
async def test_a_duplicate_inside_a_block_whose_refusal_is_caught() -> None:
    with pytest.raises(Refused):
        async with declaring():
            _ = pulumi.CustomResource(THING, 'twice', {})
            _ = pulumi.CustomResource(THING, 'twice', {})
            raise Refused('the component refuses')


@pytest.mark.asyncio
async def test_a_duplicate_beside_a_task_that_failed_first() -> None:
    async def refuses() -> None:
        raise Refused('the task refuses')

    with pytest.raises(Refused):
        async with declaring():
            _ = asyncio.ensure_future(refuses())
            _ = pulumi.CustomResource(THING, 'twice', {})
            _ = pulumi.CustomResource(THING, 'twice', {})


@pytest.mark.asyncio
async def test_a_refusal_the_case_receives() -> None:
    refusal = Refused('no')

    async def refuses() -> str:
        raise refusal

    _ = pulumi.CustomResource(THING, 'refused', {'value': pulumi.Output.from_input(refuses())})
    assert await the_run_failed_with(Refused) is refusal


@pytest.mark.asyncio
async def test_a_duplicate_received_once_its_own_output_has_failed() -> None:
    first = pulumi.CustomResource(THING, 'twice', {})
    _ = await first.urn.future()
    second = pulumi.CustomResource(THING, 'twice', {})
    with pytest.raises(AssertionError) as refused:
        _ = await second.urn.future()
    assert await the_run_failed_with(AssertionError) is refused.value


@pytest.mark.asyncio
async def test_a_duplicate_beside_a_refusal_the_case_receives() -> None:
    refusal = Refused('no')

    async def refuses() -> str:
        raise refusal

    refused = pulumi.CustomResource(THING, 'refused', {'value': pulumi.Output.from_input(refuses())})
    with pytest.raises(ValueError):
        _ = await refused.urn.future()
    _ = pulumi.CustomResource(THING, 'twice', {})
    _ = pulumi.CustomResource(THING, 'twice', {})
    assert await the_run_failed_with(Refused) is refusal


@pytest.mark.asyncio
async def test_a_duplicate_beside_a_refusal_the_barrier_raises() -> None:
    async def refuses() -> str:
        raise Refused('no')

    refused = pulumi.CustomResource(THING, 'refused', {'value': pulumi.Output.from_input(refuses())})
    with pytest.raises(ValueError):
        _ = await refused.urn.future()
    with pytest.raises(ValueError):
        async with declaring():
            _ = pulumi.CustomResource(THING, 'twice', {})
            _ = pulumi.CustomResource(THING, 'twice', {})


@pytest.mark.asyncio
async def test_a_run_that_fails_nothing() -> None:
    _ = pulumi.CustomResource(THING, 'once', {})
    _ = pulumi.CustomResource(THING, 'other', {})


@pytest.mark.asyncio
async def test_an_output_that_fails_with_nothing_reading_it() -> None:
    async def refuses() -> str:
        raise Refused('no')

    _ = pulumi.Output.from_input(refuses())


@pytest.mark.asyncio
async def test_a_refusal_that_fails_several_outputs_received_once() -> None:
    refusal = Refused('no')

    async def refuses() -> str:
        raise refusal

    refused = pulumi.Output.from_input(refuses())
    _ = refused.apply(str.upper)
    _ = refused.apply(len)
    assert await the_run_failed_with(Refused) is refusal


@pytest.mark.asyncio
async def test_an_output_failed_beside_a_refusal_the_case_receives() -> None:
    refusal = Refused('no')

    async def refuses() -> str:
        raise refusal

    async def fails() -> str:
        raise RuntimeError('of a root of its own')

    _ = pulumi.Output.from_input(fails())
    _ = pulumi.Output.from_input(refuses())
    assert await the_run_failed_with(Refused) is refusal


@pytest.mark.asyncio
async def test_an_output_that_fails_only_once_everything_else_has_settled() -> None:
    settled = pulumi.CustomResource(THING, 'settled', {})
    _ = await settled.urn.future()

    async def refuses_later() -> str:
        for _ in range(5):
            await asyncio.sleep(0)
        raise Refused('late')

    _ = pulumi.Output.from_input(refuses_later())


@pytest.mark.asyncio
async def test_an_output_faulted_as_a_resource_that_failed_to_register() -> None:
    async def failed() -> str:
        raise ResourceRegistrationFailed('resource thing [test:index:Thing] failed to register')

    _ = pulumi.Output.from_input(failed())
"""

#: The child's cases on one run the module shares, started by a module-scoped
#: fixture on the module's loop and so set up at the first case alone. The
#: duplicate a later case registers must fail that case and only that one:
#: the cases on either side of it, one of which receives a refusal of its own
#: on the same run, must pass. So must an output a later case leaves failed,
#: and the case after it, which the output still tracked does not fail. A
#: duplicate declared by a case on a loop of its own, which the run never
#: started on, must fail that case too, even declared by a task the case
#: leaves behind: that task declares on the loop's last turn, so no RPC of it
#: has begun when the case ends. A receipt is the case's own: a later case
#: reading an output whose refusal an earlier case received must fail. And
#: the shared run is judged after a case on a loop of its own has started a
#: run of its own: a duplicate on the shared run after that must fail too.
#: The last case collects what every case before it left, so that a failure
#: no judgment read is logged inside the child run.
SHARED = """
import asyncio
import gc

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, run_with, the_run_failed_with

THING = 'test:index:Thing'

pytestmark = pytest.mark.asyncio(loop_scope='module')


class Refused(ValueError):
    '''What a component raises on a bad argument, reaching a resource's inputs.'''


REFUSAL = Refused('no')
HELD: dict[str, pulumi.Output[str]] = {}


async def refuses_once() -> str:
    raise REFUSAL


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def shared() -> Recorder:
    return await run_with(Recorder(), stack='shared', project='mock-monitor')


async def test_the_first_case_on_a_shared_run() -> None:
    _ = pulumi.CustomResource(THING, 'once', {})


async def test_a_duplicate_a_later_case_declares_on_a_shared_run() -> None:
    _ = pulumi.CustomResource(THING, 'twice', {})
    _ = pulumi.CustomResource(THING, 'twice', {})


async def test_a_refusal_a_later_case_receives_on_a_shared_run() -> None:
    refusal = Refused('no')

    async def refuses() -> str:
        raise refusal

    _ = pulumi.CustomResource(THING, 'refused', {'value': pulumi.Output.from_input(refuses())})
    assert await the_run_failed_with(Refused) is refusal


async def test_an_output_a_later_case_leaves_failed_on_a_shared_run() -> None:
    async def refuses() -> str:
        raise Refused('no')

    _ = pulumi.Output.from_input(refuses())


async def test_a_case_after_a_failed_one_on_a_shared_run() -> None:
    _ = pulumi.CustomResource(THING, 'after', {})


@pytest.mark.asyncio(loop_scope='function')
async def test_a_duplicate_a_case_on_a_loop_of_its_own_declares_on_a_shared_run() -> None:
    async def declares() -> None:
        _ = pulumi.CustomResource(THING, 'twice-elsewhere', {})
        _ = pulumi.CustomResource(THING, 'twice-elsewhere', {})

    _ = asyncio.ensure_future(declares())


async def test_a_refusal_of_a_held_output_received_on_a_shared_run() -> None:
    HELD['refused'] = pulumi.Output.from_input(refuses_once())
    _ = pulumi.CustomResource(THING, 'first-reader', {'value': HELD['refused']})
    assert await the_run_failed_with(Refused) is REFUSAL


async def test_a_later_reader_of_that_refusal_on_a_shared_run() -> None:
    _ = pulumi.CustomResource(THING, 'second-reader', {'value': HELD['refused']})


@pytest.mark.asyncio(loop_scope='function')
async def test_a_run_of_its_own_beside_a_shared_run() -> None:
    _ = await run_with(Recorder(), stack='own', project='mock-monitor')
    _ = pulumi.CustomResource(THING, 'own', {})


async def test_a_duplicate_on_a_shared_run_after_another_run_started() -> None:
    _ = pulumi.CustomResource(THING, 'twice-after', {})
    _ = pulumi.CustomResource(THING, 'twice-after', {})


def test_what_every_case_left_is_collected() -> None:
    _ = gc.collect()
"""

#: An output failed the way the SDK fails a refused resource's: its value,
#: whether it is known and whether it is secret, failed together with one
#: exception. Printed is how many of its futures asyncio reports as never
#: retrieved once the output's task is no longer tracked.
UNRETRIEVED = """
import asyncio
import gc
import logging

import pulumi
from pulumi.runtime.settings import SETTINGS

reports = []


class Reports(logging.Handler):
    def emit(self, record):
        reports.append(record.getMessage())


logging.getLogger('asyncio').addHandler(Reports())


async def fail_an_output():
    loop = asyncio.get_running_loop()
    value, known, secret = (loop.create_future() for _ in range(3))
    refusal = ValueError('refused')
    for future in (value, known, secret):
        future.set_exception(refusal)
    try:
        _ = await pulumi.Output(set(), value, known, secret).future()
    except ValueError:
        pass


asyncio.run(fail_an_output())
# What the SDK's wait at a program's end takes, and what the suite's
# `mocked_runs_release_their_tasks` drops once the loop has closed.
SETTINGS.outputs.clear()
_ = gc.collect()
print(sum('was never retrieved' in report for report in reports))
"""

#: Both halves of `mock_monitor._ReadFailures` on one loop: the report of a
#: failure whose root the judgment read is dropped, and any other goes on to
#: asyncio's log. Printed is what reached the log.
HALVES = """
import asyncio
import gc
import logging

import mock_monitor

reports = []


class Reports(logging.Handler):
    def emit(self, record):
        reports.append(str(record.exc_info[1]) if record.exc_info else record.getMessage())


logging.getLogger('asyncio').addHandler(Reports())


async def leave_two_unretrieved():
    loop = asyncio.get_running_loop()
    mock_monitor._ReadFailures.install(loop)
    read, unread = ValueError('read'), RuntimeError('unread')
    mock_monitor._ReadFailures.read([loop], [read])
    for failure in (read, unread):
        future = loop.create_future()
        future.set_exception(failure)
        del future


asyncio.run(leave_two_unretrieved())
_ = gc.collect()
print(sorted(reports))
"""

#: The child's configuration: none of `pyproject.toml`'s, so that it runs its
#: cases in one process whatever the parent's options are.
CONFIGURATION = '[pytest]\n'

#: A stop-loss on the child run, below the per-case bound.
TIMEOUT = 50


def test_a_failure_no_case_received_fails_the_case_that_caused_it_and_no_other(tmp_path: Path) -> None:
    (tmp_path / 'test_child.py').write_text(CHILD)
    (tmp_path / 'test_shared.py').write_text(SHARED)
    (tmp_path / 'pytest.ini').write_text(CONFIGURATION)
    report = tmp_path / 'report.xml'
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'tests')}

    # Neither output capture nor log capture, so that what asyncio logs about
    # a failure never retrieved reaches the child's stderr, wherever in the
    # run it is logged.
    run = process_sessions.run(
        [
            sys.executable,
            '-m',
            'pytest',
            '-c',
            str(tmp_path / 'pytest.ini'),
            '-p',
            'conftest',
            '-p',
            'no:cacheprovider',
            '-p',
            'no:logging',
            '-s',
            '-q',
            f'--junit-xml={report}',
            str(tmp_path / 'test_child.py'),
            str(tmp_path / 'test_shared.py'),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    outcomes: dict[str, list[str]] = {
        case.attrib['name']: [
            outcome.attrib.get('message', '').split(':', 1)[0]
            for outcome in case
            if outcome.tag in {'failure', 'error', 'skipped'}
        ]
        for case in ET.parse(report).getroot().iter('testcase')
    }
    assert outcomes == {
        'test_a_duplicate_nothing_awaits': ['mock_monitor.UnreceivedFailure'],
        'test_a_duplicate_the_barrier_raises': [],
        'test_a_duplicate_inside_a_block_whose_refusal_is_caught': ['mock_monitor.UnreceivedFailure'],
        'test_a_duplicate_beside_a_task_that_failed_first': ['mock_monitor.UnreceivedFailure'],
        'test_a_refusal_the_case_receives': [],
        'test_a_duplicate_received_once_its_own_output_has_failed': [],
        'test_a_duplicate_beside_a_refusal_the_case_receives': ['mock_monitor.UnreceivedFailure'],
        'test_a_duplicate_beside_a_refusal_the_barrier_raises': ['mock_monitor.UnreceivedFailure'],
        'test_a_run_that_fails_nothing': [],
        'test_an_output_that_fails_with_nothing_reading_it': ['mock_monitor.UnreceivedFailure'],
        'test_a_refusal_that_fails_several_outputs_received_once': [],
        'test_an_output_failed_beside_a_refusal_the_case_receives': ['mock_monitor.UnreceivedFailure'],
        'test_an_output_that_fails_only_once_everything_else_has_settled': ['mock_monitor.UnreceivedFailure'],
        'test_an_output_faulted_as_a_resource_that_failed_to_register': [],
        'test_the_first_case_on_a_shared_run': [],
        'test_a_duplicate_a_later_case_declares_on_a_shared_run': ['mock_monitor.UnreceivedFailure'],
        'test_a_refusal_a_later_case_receives_on_a_shared_run': [],
        'test_an_output_a_later_case_leaves_failed_on_a_shared_run': ['mock_monitor.UnreceivedFailure'],
        'test_a_case_after_a_failed_one_on_a_shared_run': [],
        'test_a_duplicate_a_case_on_a_loop_of_its_own_declares_on_a_shared_run': ['mock_monitor.UnreceivedFailure'],
        'test_a_refusal_of_a_held_output_received_on_a_shared_run': [],
        'test_a_later_reader_of_that_refusal_on_a_shared_run': ['mock_monitor.UnreceivedFailure'],
        'test_a_run_of_its_own_beside_a_shared_run': [],
        'test_a_duplicate_on_a_shared_run_after_another_run_started': ['mock_monitor.UnreceivedFailure'],
        'test_what_every_case_left_is_collected': [],
    }, run.stdout + run.stderr
    assert 'never retrieved' not in run.stderr, run.stderr


def test_the_sdk_still_leaves_a_failed_outputs_futures_unretrieved(tmp_path: Path) -> None:
    """The tripwire on `mock_monitor._ReadFailures`, the workaround that drops their reports.

    Red once the SDK's output awaits every future it was given, and then the
    workaround goes: nothing would be left for it to drop. In a child
    process, because what is observed is the collection of what the SDK's
    process-global set of tracked outputs holds, which this process's other
    cases share.
    """
    run = process_sessions.run(
        [sys.executable, '-c', UNRETRIEVED], cwd=tmp_path, text=True, timeout=TIMEOUT, check=False
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() != '0', (
        'the SDK retrieves every future of an output it failed: delete `_ReadFailures` from tests/mock_monitor.py'
    )


def test_a_report_is_dropped_only_once_its_root_was_read(tmp_path: Path) -> None:
    """The workaround hides no failure the judgment did not read: that report reaches the log, and only that one."""
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'tests')}

    run = process_sessions.run(
        [sys.executable, '-c', HALVES], cwd=tmp_path, env=environment, text=True, timeout=TIMEOUT, check=False
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "['unread']", run.stdout + run.stderr
