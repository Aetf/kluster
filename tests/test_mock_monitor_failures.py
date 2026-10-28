"""A failure in a mocked run's RPC fails the case that caused it, unless the case received it.

A registration is carried out in a task of its own, and under the mocks on an
executor thread, so its failure -- the recorder's refusal of a duplicate URN,
a component's refusal reaching a resource's inputs -- reaches a case only if
the case waits for it. `conftest`'s `pytest_runtest_call` hook does that wait
for every case through `mock_monitor.fail_on_unreceived_failures`, and the
property is about how a case *ends*, so it is held in a child run under this
directory's `conftest`, the way `tests/test_tracked_outputs.py` holds its
own: a case cannot watch its own outcome.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import process_sessions

ROOT = Path(__file__).parent.parent

#: The child's cases. Three must fail: one registers a URN twice and awaits
#: nothing, which is the failure the hook exists to catch, and two register
#: it twice inside a block whose exception -- the block's own, or a task's
#: that failed first -- the case catches, with the duplicate only a note on
#: it. Each of the others meets a failure the case does receive, or none, and
#: must pass.
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
async def test_a_run_that_fails_nothing() -> None:
    _ = pulumi.CustomResource(THING, 'once', {})
    _ = pulumi.CustomResource(THING, 'other', {})
"""

#: The child's configuration: none of `pyproject.toml`'s, so that it runs its
#: cases in one process whatever the parent's options are.
CONFIGURATION = '[pytest]\n'

#: A stop-loss on the child run, below the per-case bound.
TIMEOUT = 50


def test_a_failure_no_case_received_fails_the_case_that_caused_it_and_no_other(tmp_path: Path) -> None:
    (tmp_path / 'test_child.py').write_text(CHILD)
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
        'test_a_run_that_fails_nothing': [],
    }, run.stdout + run.stderr
