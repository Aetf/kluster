"""A nested unknown reads back as the run in hand says, whatever run came before it.

The mock monitor deserializes a registration's inputs on an executor thread
that has no Python context of its own, where the runtime's `dry_run` answers
with a process-wide default the SDK fixes at the first value set in a context
(`mock_monitor._capture_request`). An unknown nested in those inputs is an
`Unknown` under a preview and a dropped key otherwise, so without the patch
the second run of a different kind in one context reads its nested unknown
back the first run's way.

Both runs happen inside one case here, in each order, because that is the
condition the SDK's default is fixed under: the test runner hands every case a
fresh context, which would hide the artifact if the two runs were two cases.
"""

from __future__ import annotations

from typing import Any

import pulumi
import pytest
from mock_monitor import Recorder, declaring, run_with
from pulumi.output import Unknown


async def details_read_back(*, preview: bool) -> dict[str, Any]:
    """What the recorder holds for a nested property the program handed over unknown."""
    monitor = await run_with(Recorder(), stack='physical', preview=preview)
    # The sentinel is what an unknown `Output` serializes to, so handing it
    # over directly puts on the wire exactly what the mock deserializes.
    details = {'sourceUri': pulumi.UNKNOWN, 'sourceType': 'objectStorageUri'}
    async with declaring():
        _ = pulumi.CustomResource('kluster:test:Thing', 'thing', props={'details': details})
    return monitor.inputs_of('thing')['details']


def assert_read_back_as_a_preview(details: dict[str, Any]) -> None:
    assert isinstance(details.get('sourceUri'), Unknown), details
    assert details['sourceType'] == 'objectStorageUri', 'the known sibling still reads back'


def assert_read_back_as_an_update(details: dict[str, Any]) -> None:
    assert 'sourceUri' not in details, details
    assert details['sourceType'] == 'objectStorageUri', 'the known sibling still reads back'


@pytest.mark.asyncio
async def test_an_update_after_a_preview_in_one_context_reads_a_nested_unknown_back_its_own_way() -> None:
    assert_read_back_as_a_preview(await details_read_back(preview=True))
    assert_read_back_as_an_update(await details_read_back(preview=False))


@pytest.mark.asyncio
async def test_a_preview_after_an_update_in_one_context_reads_a_nested_unknown_back_its_own_way() -> None:
    assert_read_back_as_an_update(await details_read_back(preview=False))
    assert_read_back_as_a_preview(await details_read_back(preview=True))
