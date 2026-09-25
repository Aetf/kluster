"""The stack dispatch is the one place a run's blast radius is decided, so an
unknown stack must fail rather than silently declare nothing, and a known one
must run its own program and no other."""

import importlib

import pytest
import pytest_asyncio
from mock_monitor import Recorder, run_with

from kluster import stacks

#: A stack name the register does not carry, which is what the refusal is about.
UNKNOWN_STACK = 'dev'

#: The stacks of the project (docs/declarative/README.md §1).
REGISTERED = ('physical', 'dns', 'k8s-base', 'apps', 'github')


@pytest_asyncio.fixture
async def selected_stack_is_unknown() -> Recorder:
    return await run_with(Recorder(), stack=UNKNOWN_STACK, preview=True)


def test_every_stack_is_registered() -> None:
    assert set(stacks.STACKS) == set(REGISTERED)


@pytest.mark.parametrize('name', REGISTERED)
def test_each_stack_runs_the_program_named_after_it(name: str) -> None:
    """`pulumi up -s apps` declares the applications, and nothing that belongs to another stack.

    Each program is the module the stack is named for, so the dispatch is held
    to the package's own layout rather than to a second copy of the table: a
    row wired to a neighbour's program would run that neighbour's resources
    under the wrong stack's state.
    """
    program = importlib.import_module(f'{stacks.__name__}.{name.replace("-", "_")}')

    assert stacks.STACKS[name] is program.main


@pytest.mark.asyncio
async def test_an_unknown_stack_is_refused(selected_stack_is_unknown: Recorder) -> None:
    with pytest.raises(ValueError, match='no program for stack'):
        await stacks.run_selected()
