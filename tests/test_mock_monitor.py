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

from collections.abc import AsyncGenerator, Callable

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


class FixedChild(pulumi.ComponentResource):
    """A component whose child is named the same whatever the component is called.

    The mistake the engine's identity catches and a by-name one cannot: a URN
    carries the parent's *type* and not its name, so two of these are two
    components and one child.
    """

    def __init__(self, name: str) -> None:
        super().__init__(COMPONENT, name, None, None)
        self.held = pulumi.CustomResource(INNER, 'held', {}, pulumi.ResourceOptions(parent=self))
        self.register_outputs({})


async def refusal_of(declare: Callable[[], None]) -> str:
    """The message a run that `declare` makes illegal is stopped with.

    Its own run every time: a refused registration is a run that did not
    finish, so it is never one the cases above can also read.
    """
    _ = await run_with(Recorder(), stack='refused', project='mock-monitor')
    with pytest.raises(AssertionError) as refused:
        async with declaring():
            declare()
    return str(refused.value)


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

    The refusal is the whole guard, and it carries the remedy: a case that
    means the component and does not say so is told which types to choose
    between, rather than shown a run to search.
    """
    with pytest.raises(AssertionError) as refusal:
        _ = monitor.options_of(SHARED)

    message = str(refusal.value)
    assert f'{SHARED} was registered 2 times, not once' in message
    assert f"name the type it means, one of ['{INNER}', '{COMPONENT}']" in message


@pytest.mark.asyncio
async def test_a_type_the_name_never_registered_under_echoes_both(monitor: Recorder) -> None:
    """A typo is in one of the two arguments, and the refusal doubts neither for the reader.

    So it echoes the type asked for beside the types the name does have. The
    rest of the run is not printed: the name matched something, which is the
    handle the reader already has.
    """
    with pytest.raises(AssertionError) as refusal:
        _ = monitor.options_of(SHARED, 'test:index:Typo')

    message = str(refusal.value)
    assert f'{SHARED} was never registered as test:index:Typo' in message
    assert INNER in message
    assert LONE not in message


@pytest.mark.asyncio
async def test_a_name_one_registration_answers_to_needs_no_type(monitor: Recorder) -> None:
    """Most resources are named once in a run, and those cases say only the name."""
    assert monitor.options_of('lone').type == LONE


@pytest.mark.asyncio
async def test_a_name_nothing_registered_is_refused_with_the_whole_run(monitor: Recorder) -> None:
    """The one refusal the whole run belongs in, and the one row it leaves out.

    A name that matched nothing leaves the reader no handle, so here the
    listing is the help rather than the noise. The engine's own root resource
    is not in it: every run registers one and no case asks about it.
    """
    with pytest.raises(AssertionError) as refusal:
        _ = monitor.options_of('never-declared')

    message = str(refusal.value)
    assert 'never-declared was never registered' in message
    assert LONE in message
    assert 'pulumi:pulumi:Stack' not in message


@pytest.mark.asyncio
async def test_a_component_and_the_resource_inside_it_are_two_records_not_one(monitor: Recorder) -> None:
    """Keyed by identity rather than by logical name, which is what makes them countable.

    A record keyed by the name alone holds one of these two, and a case cannot
    then say which -- the shape this repository builds everywhere, since a
    component, the resource inside it and that resource's own sub-resource all
    answer to the component's name.
    """
    shared = {urn: request for urn, request in monitor.registrations.items() if request.name == SHARED}

    assert len(shared) == 2
    assert {request.type for request in shared.values()} == {COMPONENT, INNER}


@pytest.mark.asyncio
async def test_a_second_registration_of_one_identity_is_refused() -> None:
    """What the engine does with a duplicate URN, rather than what the mock does.

    Pulumi's mock monitor answers the second registration and overwrites the
    first in its own resource table, so without this the run reads back as a
    run with one resource in it -- a run no engine would have accepted.
    """

    def declare() -> None:
        for _ in range(2):
            _ = pulumi.CustomResource(LONE, 'twice', {}, None)

    message = await refusal_of(declare)

    assert 'duplicate resource URN' in message
    assert f'{LONE}::twice' in message


@pytest.mark.asyncio
async def test_two_components_of_one_type_cannot_hold_one_child_name_between_them() -> None:
    """The identity is the URN, which carries the parent's type and not its name.

    So naming a child after the component that holds it is a requirement
    rather than a house style, and a pair of arguments -- a type and a name --
    is not what the refusal is keyed on.
    """

    def declare() -> None:
        _ = FixedChild('one')
        _ = FixedChild('two')

    message = await refusal_of(declare)

    assert f'{COMPONENT}${INNER}::held' in message
