"""The stack dispatch is the one place a run's blast radius is decided, so an
unknown stack must fail rather than silently declare nothing."""

import pytest

from kluster import stacks


def test_every_stack_is_registered() -> None:
    assert set(stacks.STACKS) == {'physical', 'dns', 'k8s-base', 'apps', 'github'}


@pytest.mark.asyncio
async def test_an_unknown_stack_is_refused() -> None:
    # The test runtime selects a stack named 'dev', which is not one of ours.
    with pytest.raises(ValueError, match='no program for stack'):
        await stacks.run_selected()
