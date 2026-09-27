"""A token called more than once is answered for every call to it, not for the last.

One lookup is made once per node or per artifact, so a program that lost the
provider on one of those calls still makes the others through it. The
recorder keeps every call (`Recorder.called`), and the reader that asks about a
token (`Recorder.called_through`) refuses one whose calls went through two
providers rather than answering with either.

The calls are made through the engine rather than handed to the recorder
directly, so what is recorded is what the mock monitor actually receives for
an invoke that names a provider and one that names none.
"""

from __future__ import annotations

import pulumi
import pytest
from mock_monitor import Recorder, declaring, run_with

#: A lookup no suite serves: the empty answer is all it gets.
TOKEN = 'b2:index/getLookup:getLookup'
OTHER = 'b2:index/getOther:getOther'


async def calls_made(*providers: bool) -> Recorder:
    """A run that calls `TOKEN` once per entry, through the provider where the entry is true.

    Each call is answered before the next is made. An invoke naming a provider
    waits for that provider's registration and one naming none does not, so
    calls made together reach the monitor unsigned first whatever order the
    program made them in -- and the order is what one case turns on.
    """
    monitor = await run_with(Recorder(), stack='calls', project='mock-monitor')
    async with declaring():
        provider = pulumi.ProviderResource('b2', 'account', {})
        for through in providers:
            opts = pulumi.InvokeOutputOptions(provider=provider) if through else None
            _ = await pulumi.runtime.invoke_output(TOKEN, {}, opts).future()
    return monitor


@pytest.mark.asyncio
async def test_every_call_to_a_token_is_recorded_in_order() -> None:
    monitor = await calls_made(True, False)

    assert [call.token for call in monitor.called] == [TOKEN, TOKEN]
    assert 'account' in monitor.called[0].provider
    assert monitor.called[1].provider == ''


@pytest.mark.asyncio
async def test_a_token_whose_calls_all_went_through_one_provider_names_it() -> None:
    monitor = await calls_made(True, True)

    assert 'account' in monitor.called_through(TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize('order', [(True, False), (False, True)], ids=['last-unsigned', 'first-unsigned'])
async def test_a_token_one_of_whose_calls_lost_its_provider_is_refused(order: tuple[bool, bool]) -> None:
    """Either call may be the one that lost it, so both orders are cases.

    A record keeping one provider per token answers with whichever call came
    last, which is the signed one when the unsigned call came first.
    """
    monitor = await calls_made(*order)

    with pytest.raises(AssertionError, match='called through 2 providers'):
        _ = monitor.called_through(TOKEN)


@pytest.mark.asyncio
async def test_a_token_never_called_is_refused_with_the_tokens_the_run_called() -> None:
    monitor = await calls_made(True)

    with pytest.raises(AssertionError, match=f'never called.*{TOKEN}'):
        _ = monitor.called_through(OTHER)
