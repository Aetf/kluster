"""A failure in a mocked run's RPC fails the case that caused it, unless the case received it.

A registration is carried out in a task of its own, and under the mocks on an
executor thread, so its failure -- the recorder's refusal of a duplicate URN,
a component's refusal reaching a resource's inputs -- reaches a case only if
the case waits for it. `conftest`'s `pytest_runtest_call` hook does that wait
for every case through `mock_monitor.fail_on_unreceived_failures`, and the
property is about how a case *ends*, so it is held in a child run under this
directory's `conftest`, the way `tests/test_tracked_outputs.py` holds its
own: a case cannot watch its own outcome.

Two shapes of run are held. A case's own run, where each failure is judged on
its own, so a receipt of one refusal receives that refusal and nothing beside
it; and a module's shared run, whose later cases are judged as its first is,
each by what it started.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import process_sessions

ROOT = Path(__file__).parent.parent

#: The child's cases on runs of their own. Five must fail: one registers a
#: URN twice and awaits nothing, which is the failure the hook exists to
#: catch; two register it twice inside a block whose exception -- the block's
#: own, or a task's that failed first -- the case catches, with the duplicate
#: only a note on it; and two register it twice once a refusal has failed and
#: then receive the refusal, which receives nothing beside it. Each of the
#: others meets a failure the case does receive, or none, and must pass.
CHILD = """
import asyncio

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with, the_run_failed_with

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
"""

#: The child's cases on one run the module shares, started by a module-scoped
#: fixture on the module's loop and so set up at the first case alone. The
#: duplicate a later case registers must fail that case and only that one:
#: the cases on either side of it, one of which receives a refusal of its own
#: on the same run, must pass. A duplicate declared by a case on a loop of
#: its own, which the run never started on, must fail that case too, even
#: declared by a task the case leaves behind: that task declares on the
#: loop's last turn, so no RPC of it has begun when the case ends. A receipt
#: is the case's own: a later case reading an output whose refusal an earlier
#: case received must fail. And the shared run is judged after a case on a
#: loop of its own has started a run of its own: a duplicate on the shared
#: run after that must fail too.
SHARED = """
import asyncio

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
    monitor = await run_with(Recorder(), stack='shared', project='mock-monitor')
    HELD['refused'] = pulumi.Output.from_input(refuses_once())
    return monitor


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


async def test_a_case_after_a_failed_one_on_a_shared_run() -> None:
    _ = pulumi.CustomResource(THING, 'after', {})


@pytest.mark.asyncio(loop_scope='function')
async def test_a_duplicate_a_case_on_a_loop_of_its_own_declares_on_a_shared_run() -> None:
    async def declares() -> None:
        _ = pulumi.CustomResource(THING, 'twice-elsewhere', {})
        _ = pulumi.CustomResource(THING, 'twice-elsewhere', {})

    _ = asyncio.ensure_future(declares())


async def test_a_refusal_of_a_shared_output_received_on_a_shared_run() -> None:
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
        'test_the_first_case_on_a_shared_run': [],
        'test_a_duplicate_a_later_case_declares_on_a_shared_run': ['mock_monitor.UnreceivedFailure'],
        'test_a_refusal_a_later_case_receives_on_a_shared_run': [],
        'test_a_case_after_a_failed_one_on_a_shared_run': [],
        'test_a_duplicate_a_case_on_a_loop_of_its_own_declares_on_a_shared_run': ['mock_monitor.UnreceivedFailure'],
        'test_a_refusal_of_a_shared_output_received_on_a_shared_run': [],
        'test_a_later_reader_of_that_refusal_on_a_shared_run': ['mock_monitor.UnreceivedFailure'],
        'test_a_run_of_its_own_beside_a_shared_run': [],
        'test_a_duplicate_on_a_shared_run_after_another_run_started': ['mock_monitor.UnreceivedFailure'],
    }, run.stdout + run.stderr
