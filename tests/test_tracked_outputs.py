"""The tripwire on `conftest.mocked_runs_release_their_tasks`, the suite's one workaround for the Pulumi SDK.

The SDK keeps a cancelled output's task in its set of tracked outputs, and the
set outlives the case whose loop the task belonged to. The workaround drops
such tasks after each case; this module holds that it does, and is what says
when it can go.

The property is about one case's leftovers as the next case sees them, so it
is held in a child run of two cases in order, under this directory's
`conftest`, rather than across this run's own cases: their order and their
process are the scheduler's to decide.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

#: The child's two cases: the first leaves an output its loop will cancel,
#: and the second reads what the set holds afterwards.
CHILD = """
import asyncio

import pulumi
import pytest
from pulumi.runtime.settings import SETTINGS


@pytest.mark.asyncio
async def test_a_case_leaves_an_output_its_loop_will_cancel() -> None:
    never = asyncio.get_running_loop().create_future()
    output = pulumi.Output.from_input(never)
    # The premise: the output's task is tracked, and nothing will finish it.
    assert any(task.get_loop() is asyncio.get_running_loop() for task in SETTINGS.outputs)
    del output


def test_the_next_case_finds_no_task_of_a_closed_loop() -> None:
    stale = [task for task in SETTINGS.outputs if task.get_loop().is_closed()]
    assert stale == [], f'{len(stale)} tracked tasks belong to loops that have closed'
"""

#: The child's configuration: none of `pyproject.toml`'s, so that it runs its
#: two cases in order in one process whatever the parent's options are.
CONFIGURATION = '[pytest]\n'

#: A stop-loss on the child run, below the per-case bound.
TIMEOUT = 50


def test_a_cases_cancelled_outputs_are_not_tracked_into_the_next(tmp_path: Path) -> None:
    (tmp_path / 'test_child.py').write_text(CHILD)
    (tmp_path / 'pytest.ini').write_text(CONFIGURATION)
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'tests')}

    run = subprocess.run(
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
            str(tmp_path / 'test_child.py'),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert '2 passed' in run.stdout, run.stdout
