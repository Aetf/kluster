"""The child run and the tripwire of `conftest`'s workaround for the loops the SDK's mocks give threads.

Pulumi's mock monitor gives each executor thread that carries one of its RPCs
an event loop of its own and never closes it; collecting one can raise
`Invalid file descriptor: -1` in whichever case is running then. The
workaround closes each such loop once its thread has finished. What it
answers is how a case *ends* and what later cases meet, so it is held in a
child run under this directory's `conftest`, the way
`tests/test_tracked_outputs.py` holds its own; and the SDK's half of the
defect is held in a child process with no `conftest` at all, which is what
says when the workaround can go.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import process_sessions

ROOT = Path(__file__).parent.parent

#: The SDK's own mocks and nothing of this repository's, so that what the
#: loops are is the SDK's alone.
MOCKS = """
import pulumi
from pulumi.runtime import Mocks, set_mocks


class Answering(Mocks):
    def new_resource(self, args):
        return f'{args.name}-id', args.inputs

    def call(self, args):
        return {}, []


async def declare(prefix):
    things = [pulumi.CustomResource('test:index:Thing', f'{prefix}-{i}', {'index': i}) for i in range(5)]
    for thing in things:
        _ = await thing.urn.future()
"""

#: Forty cases on loops of their own, each beginning with a collection, so
#: that every loop an earlier case's threads were given is collected in a
#: later case -- the moment the defect's report lands -- rather than whenever
#: the collector next happens to run. Then cases on one module's shared loop,
#: whose executor threads outlive each case: the later cases declare through
#: threads whose loops an earlier case was given, and pass only if those
#: loops are still open.
CHILD = (
    MOCKS
    + """
import gc

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('case', range(40))
async def test_a_case_on_a_loop_of_its_own(case: int) -> None:
    _ = gc.collect()
    set_mocks(Answering(), project='loops', stack='own', preview=False)
    await declare(f'own-{case}')
"""
)

#: The cases on one module's shared loop.
SHARED = (
    MOCKS
    + """
import gc

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def shared_run() -> None:
    set_mocks(Answering(), project='loops', stack='shared', preview=False)


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('case', range(10))
async def test_a_case_on_a_shared_loop(case: int) -> None:
    _ = gc.collect()
    await declare(f'shared-{case}')


def test_what_every_case_left_is_collected() -> None:
    _ = gc.collect()
"""
)

#: A mocked run on a loop of its own, under no `conftest`. Printed is how many
#: loops the collector finds unclosed once the run's loop has closed and shut
#: its executor down.
UNCLOSED = (
    MOCKS
    + """
import asyncio
import gc
import warnings


async def run():
    set_mocks(Answering(), project='loops', stack='tripwire', preview=False)
    await declare('tripwire')


with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always', ResourceWarning)
    asyncio.run(run())
    _ = gc.collect()
print(sum(str(warning.message).startswith('unclosed event loop') for warning in caught))
"""
)

#: The child's configuration: none of `pyproject.toml`'s, so that it runs its
#: cases in one process whatever the parent's options are.
CONFIGURATION = '[pytest]\n'

#: A stop-loss on each child, below the per-case bound.
TIMEOUT = 50


def test_no_case_collects_a_loop_the_mocks_gave_a_thread(tmp_path: Path) -> None:
    """No case meets an unclosed loop of the SDK's, and no case on a shared loop loses its threads' loops.

    An unclosed loop collected in a case warns `unclosed event loop`, every
    time, and can raise `Invalid file descriptor: -1` as well, which fails
    that case here. Both are read from the child's whole output, since the
    collection lands in a case other than the one whose threads held the loop.
    """
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
            '-W',
            'error::pytest.PytestUnraisableExceptionWarning',
            '-W',
            'default::ResourceWarning',
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

    output = run.stdout + run.stderr
    assert run.returncode == 0, output
    cases = list(ET.parse(report).getroot().iter('testcase'))
    assert {case.attrib['classname'] for case in cases} == {'test_child', 'test_shared'}, output
    assert not [
        case.attrib['name'] for case in cases if any(it.tag in {'failure', 'error', 'skipped'} for it in case)
    ], output
    assert 'unclosed event loop' not in output, output
    assert 'Invalid file descriptor' not in output, output


def test_the_sdks_mocks_still_leave_a_threads_loop_unclosed(tmp_path: Path) -> None:
    """The tripwire on the workaround in `conftest`.

    Red once the SDK closes the loops its mocks give executor threads, or
    gives them none, and then the workaround goes: nothing would be left for
    it to close. In a child process with no `conftest`, because what is
    observed is what the collector finds after an unpatched run.
    """
    run = process_sessions.run([sys.executable, '-c', UNCLOSED], cwd=tmp_path, text=True, timeout=TIMEOUT, check=False)

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() != '0', (
        'the SDK leaves no loop of its mocks unclosed: delete the workaround for the per-thread loops '
        'from tests/conftest.py'
    )
