"""The stack dispatch is the one place a run's blast radius is decided, so an
unknown stack must fail rather than silently declare nothing."""

import pytest
import pytest_asyncio
from mock_monitor import Recorder, run_with

from kluster import stacks

#: A stack name the register does not carry, which is what the refusal is about.
UNKNOWN_STACK = 'dev'


@pytest_asyncio.fixture
async def selected_stack_is_unknown() -> Recorder:
    return await run_with(Recorder(), stack=UNKNOWN_STACK, preview=True)


def test_every_stack_is_registered() -> None:
    assert set(stacks.STACKS) == {'physical', 'dns', 'k8s-base', 'apps', 'github'}


@pytest.mark.asyncio
async def test_an_unknown_stack_is_refused(selected_stack_is_unknown: Recorder) -> None:
    with pytest.raises(ValueError, match='no program for stack'):
        await stacks.run_selected()
