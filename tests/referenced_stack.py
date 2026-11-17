"""A referenced stack's outputs, as a reader of a `StackReference` receives them.

Its own named module rather than a `conftest`, for the reason `mock_monitor`
is one: test modules import it.

-   `read_through` runs a reader of a referenced stack's output -- a function
    of the reference, as `kluster.lib.stack_addresses.address` is once its
    other arguments are bound -- against outputs this module answers, in a
    preview or an update, and hands back what the reader made of them. The
    reference's `get_output` is answered here rather than by the mock
    monitor, because the mock drops an output that is `None` or unknown on
    its way to the reader; it answers as the SDK's does, with the named
    output or `None` where there is none.
-   `deserialized` is what the SDK makes of a producer's outputs on the way
    to a reference, with the SDK's own deserializer: the step that drops a
    null's key.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pulumi
import pytest
from google.protobuf import struct_pb2
from mock_monitor import Recorder, declaring, run_with
from pulumi.runtime import rpc

__all__ = ('deserialized', 'read_through')


async def read_through[T](
    read: Callable[[pulumi.StackReference], pulumi.Output[T]], outputs: Mapping[str, object], *, preview: bool
) -> T | None:
    """What `read` makes of a referenced stack whose outputs are `outputs`, in a preview or an update."""
    _ = await run_with(Recorder(), stack='reader', preview=preview)

    def get_output(reference: pulumi.StackReference, name: pulumi.Input[str]) -> pulumi.Output[object]:
        assert isinstance(name, str), name
        return pulumi.Output.from_input(outputs.get(name))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.StackReference, 'get_output', get_output)
        checked = read(pulumi.StackReference('producer'))
        async with declaring():
            return await checked.future()


def deserialized(outputs: Mapping[str, Any]) -> dict[str, object]:
    """`outputs`, values a protobuf `Struct` can carry, as a reference holds them once the SDK has deserialized them."""
    published = struct_pb2.Struct()
    published.update(outputs)
    return rpc.deserialize_properties(published)
