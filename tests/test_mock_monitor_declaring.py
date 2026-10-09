"""The barrier waits for what its block declared, however the block ends.

`declaring` is what makes a read of the recorder after it mean something: a
declaration only schedules its registration, so a read that runs before the
registration lands sees nothing, and a case asserting that something was *not*
declared passes whether or not it was. A case whose subject is a refusal ends
its block with an exception, and the read after that block is exactly such an
absence read. So an exception leaving the block is the case this module holds:
the registrations the block started still land before the exception reaches
the case, and the exception the case receives is the block's own.
"""

from __future__ import annotations

import asyncio

import pulumi
import pytest
from mock_monitor import Recorder, declaring, run_with

THING = 'test:index:Thing'


class Refused(Exception):
    """What the block raises: the refusal a case's subject would end its block with."""


class DrainFailure(Exception):
    """What a task the block started fails with, after the block has already raised."""


@pytest.mark.asyncio
async def test_a_registration_the_block_started_lands_before_its_exception_leaves() -> None:
    """The read after a refused block is answered by the run, not by the race.

    Without the drain on this path, the registration is still queued when the
    exception arrives, and the read below finds the run empty: the shape in
    which an assertion that the refusal declared nothing passes vacuously.
    """
    monitor = await run_with(Recorder(), stack='declaring', project='mock-monitor')

    with pytest.raises(Refused):
        async with declaring():
            _ = pulumi.CustomResource(THING, 'declared-before-the-refusal', {}, None)
            raise Refused

    assert monitor.names(THING) == {'declared-before-the-refusal'}


@pytest.mark.asyncio
async def test_the_exception_that_leaves_is_the_blocks_even_when_the_drain_fails_too() -> None:
    """A failure the drain meets is carried on the block's exception, not in its place.

    The block's exception is the one a case's `pytest.raises` waits for, so a
    drain that replaced it would turn every refusal case red over a task the
    refused block left behind.
    """
    _ = await run_with(Recorder(), stack='declaring', project='mock-monitor')

    async def fails() -> None:
        raise DrainFailure('the task the refused block left behind')

    left_behind: list[asyncio.Task[None]] = []
    with pytest.raises(Refused) as refused:
        async with declaring():
            left_behind.append(asyncio.ensure_future(fails()))
            raise Refused

    assert [task.done() for task in left_behind] == [True]
    assert any('the task the refused block left behind' in note for note in getattr(refused.value, '__notes__', []))


#: How many turns of the event loop the slower task takes to settle: more than
#: the one a bare gather's failure takes to reach the block's caller. Counted
#: in turns, which no machine's load changes.
SLOWER_TURNS = 10


@pytest.mark.asyncio
async def test_a_failure_a_normal_exit_meets_is_raised_once_everything_the_block_started_has_settled() -> None:
    """A case that catches what the block raised reads a run with nothing of the block still in flight.

    The failure leaves only after every task the block started has settled.
    A wait that raised the first failure as it happened would leave the
    slower task still running when the case reads the run.
    """
    _ = await run_with(Recorder(), stack='declaring', project='mock-monitor')

    async def fails() -> None:
        raise DrainFailure('the failure the block meets')

    async def settles_later() -> None:
        for _ in range(SLOWER_TURNS):
            await asyncio.sleep(0)

    started: list[asyncio.Task[None]] = []
    with pytest.raises(DrainFailure):
        async with declaring():
            started.append(asyncio.ensure_future(fails()))
            started.append(asyncio.ensure_future(settles_later()))

    assert [task.done() for task in started] == [True, True]


@pytest.mark.asyncio
async def test_of_two_failures_a_normal_exit_meets_the_first_to_happen_is_raised() -> None:
    """The raised failure is the first to happen, and the other is a note on it.

    The order is the one the tasks finish in, set here by events: the task
    started second fails first. An order taken from the tasks themselves
    rather than from their finishing would raise either.
    """
    _ = await run_with(Recorder(), stack='declaring', project='mock-monitor')
    first_may_fail = asyncio.Event()
    second_may_fail = asyncio.Event()

    async def fails(may: asyncio.Event, said: str) -> None:
        await may.wait()
        raise DrainFailure(said)

    async def in_order(second: asyncio.Task[None]) -> None:
        second_may_fail.set()
        while not second.done():
            await asyncio.sleep(0)
        first_may_fail.set()

    started: list[asyncio.Task[None]] = []
    with pytest.raises(DrainFailure) as raised:
        async with declaring():
            started.append(asyncio.ensure_future(fails(first_may_fail, 'started first, failed second')))
            started.append(asyncio.ensure_future(fails(second_may_fail, 'started second, failed first')))
            started.append(asyncio.ensure_future(in_order(started[1])))

    assert str(raised.value) == 'started second, failed first'
    assert any('started first, failed second' in note for note in getattr(raised.value, '__notes__', []))
