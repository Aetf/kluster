"""A mocked run's resource hooks and transforms: kept, with no callback server started (framework/testing.md §3.1).

The SDK registers a hook with a gRPC server of its own, which it starts the
first time a program registers one and stops only at the end of a real
program. A run here never reaches that end, so a server it started would
outlive it, and `mock_monitor.run_with` hands every run a stand-in instead.
This holds both halves of what the stand-in is for: the run starts no
server, and the hook is on the `Recorder` under its name, and each transform
under the token the registrations bind, where a case calls either as the
engine would.
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


def _keep(_args: pulumi.ResourceTransformArgs) -> pulumi.ResourceTransformResult | None:
    return None


def _also_keep(_args: pulumi.ResourceTransformArgs) -> pulumi.ResourceTransformResult | None:
    return None


class _Holder:
    """Something whose method is a transform: each read of it is a new object, equal to the last."""

    def keep(self, _args: pulumi.ResourceTransformArgs) -> pulumi.ResourceTransformResult | None:
        return None


@pytest.mark.asyncio
async def test_a_run_that_binds_transforms_starts_no_callback_server_and_keeps_each_with_its_registrations() -> None:
    """Each registration reads back the transforms it bound, in its order, and
    a transform bound by two registrations is one transform, as the SDK keys
    its table by the transform: a function bound twice, and a bound method
    read twice, which is a new object each time and equal to the last."""
    servers = list(_CallbackServicer._servicers)  # pyright: ignore[reportPrivateUsage]
    recorder = await run_with(Recorder(), stack='callbacks')
    holder = _Holder()

    async with declaring():
        _ = pulumi.CustomResource(
            'test:index:Thing', 'first', {}, opts=pulumi.ResourceOptions(transforms=[_keep, _also_keep])
        )
        _ = pulumi.CustomResource('test:index:Thing', 'second', {}, opts=pulumi.ResourceOptions(transforms=[_keep]))
        _ = pulumi.CustomResource(
            'test:index:Thing', 'method', {}, opts=pulumi.ResourceOptions(transforms=[holder.keep])
        )
        _ = pulumi.CustomResource(
            'test:index:Thing', 'method-again', {}, opts=pulumi.ResourceOptions(transforms=[holder.keep])
        )
        _ = pulumi.CustomResource('test:index:Thing', 'bare', {})

    assert _CallbackServicer._servicers == servers  # pyright: ignore[reportPrivateUsage]
    assert recorder.transforms_of('first', 'test:index:Thing') == [_keep, _also_keep]
    assert recorder.transforms_of('second', 'test:index:Thing') == [_keep]
    assert recorder.transforms_of('method', 'test:index:Thing') == [holder.keep]
    assert recorder.transforms_of('method-again', 'test:index:Thing') == [holder.keep]
    assert recorder.transforms_of('bare', 'test:index:Thing') == []
    assert len(recorder.transforms) == 3
