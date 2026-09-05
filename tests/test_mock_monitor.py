"""What the recorder can and cannot tell one registration from another by.

Every declaration suite reads its run back through `tests/mock_monitor.py`, so
a question the recorder answers wrongly is answered wrongly in all of them at
once. The shape here is the one this repository builds everywhere: a component
and the resource inside it carry the same logical name, which makes a logical
name a thing several registrations answer to rather than an identifier. A
recorder that resolved such a name anyway would let a case asserting about the
component pass on the child's registration -- green whichever of the two
carried the option, and green if only one of them existed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

COMPONENT = 'test:index:Holder'
INNER = 'test:index:Held'
LONE = 'test:index:Lone'

#: The name the component and the resource inside it share.
SHARED = 'shared'


class Holder(pulumi.ComponentResource):
    """A component and the resource inside it, under one name.

    The option is on the child alone, which is what makes the two
    distinguishable by something a case can assert on.
    """

    def __init__(self, name: str) -> None:
        super().__init__(COMPONENT, name, None, None)
        self.held = pulumi.CustomResource(INNER, name, {}, pulumi.ResourceOptions(parent=self, protect=True))
        self.register_outputs({})


@pytest_asyncio.fixture(scope='module')
async def monitor() -> AsyncGenerator[Recorder]:
    """One run, read by every case below."""
    recorder = await run_with(Recorder(), stack='recorder', project='mock-monitor')
    async with declaring():
        _ = Holder(SHARED)
        _ = pulumi.CustomResource(LONE, 'lone', {}, None)
    yield recorder


@pytest.mark.asyncio
async def test_a_component_and_the_resource_inside_it_keep_separate_options(monitor: Recorder) -> None:
    """The two registrations are reachable apart, and each carries its own options.

    Asserting both halves is the point: an answer that named only the
    protected one would be as wrong as an answer that named only the other.
    """
    holder = monitor.options_of(SHARED, COMPONENT)
    held = monitor.options_of(SHARED, INNER)

    assert (holder.type, held.type) == (COMPONENT, INNER)
    assert holder.protect is False
    assert held.protect is True


@pytest.mark.asyncio
async def test_a_name_several_registrations_answer_to_is_refused(monitor: Recorder) -> None:
    """Refused, rather than resolved to whichever registered last.

    The refusal is the whole guard: a case that means the component and does
    not say so gets an error naming both candidates, instead of an assertion
    that quietly reads the child.
    """
    with pytest.raises(AssertionError, match='registered 2 times, not once'):
        _ = monitor.options_of(SHARED)


@pytest.mark.asyncio
async def test_a_name_one_registration_answers_to_needs_no_type(monitor: Recorder) -> None:
    """Most resources are named once in a run, and those cases say only the name."""
    assert monitor.options_of('lone').type == LONE


@pytest.mark.asyncio
async def test_a_name_nothing_registered_is_refused_with_the_run_beside_it(monitor: Recorder) -> None:
    """A typo'd name and an ambiguous one are the same failure, and both name the run."""
    with pytest.raises(AssertionError, match='registered 0 times, not once'):
        _ = monitor.options_of('never-declared')
