"""A mocked run's resource hooks: kept by name, with no callback server started (framework/testing.md §3.1).

The SDK registers a hook with a gRPC server of its own, which it starts the
first time a program registers one and stops only at the end of a real
program. A run here never reaches that end, so a server it started would
outlive it, and `mock_monitor.run_with` hands every run a stand-in instead.
This holds both halves of what the stand-in is for: the run starts no
server, and the hook is on the `Recorder` under its name, where a case calls
it as the engine would.
"""

from __future__ import annotations

import pulumi
import pytest
from mock_monitor import Recorder, declaring, run_with
from pulumi.runtime._callbacks import _CallbackServicer  # pyright: ignore[reportPrivateUsage]


def _refuse(_args: pulumi.ResourceHookArgs) -> None:
    raise RuntimeError('a mocked run calls no hook')


@pytest.mark.asyncio
async def test_a_run_that_binds_a_hook_starts_no_callback_server_and_keeps_the_hook_by_name() -> None:
    servers = list(_CallbackServicer._servicers)  # pyright: ignore[reportPrivateUsage]
    recorder = await run_with(Recorder(), stack='callbacks')
    hook = pulumi.ResourceHook('the-hook', _refuse)

    async with declaring():
        _ = pulumi.CustomResource(
            'test:index:Thing',
            'thing',
            {},
            opts=pulumi.ResourceOptions(hooks=pulumi.ResourceHookBinding(before_delete=[hook])),
        )

    assert _CallbackServicer._servicers == servers  # pyright: ignore[reportPrivateUsage]
    bound = list(recorder.options_of('thing', 'test:index:Thing').hooks.before_delete)
    assert [recorder.hooks[name] for name in bound] == [hook]
