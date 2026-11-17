"""A referenced stack's outputs, as a reader of a `StackReference` receives them.

Its own named module rather than a `conftest`, for the reason `mock_monitor`
is one: test modules import it.

-   `reading_back` has every `StackReference` read back each output it is
    handed as given, and every other output through the mock monitor. A
    stack the monitor publishes reads back as the SDK reads one: in a
    preview an unknown anywhere in it makes every output unknown
    (`kluster.lib.stack_outputs.Held.UNKNOWN`), and a `None` reads as an
    absence. Answering one output here is what lets a case meet that one
    read with an unknown while every other read meets its value.
-   `read_through` runs a reader of a referenced stack's output -- a function
    of the reference, as `kluster.lib.stack_addresses.address` is once its
    other arguments are bound -- against outputs `reading_back` answers, in
    a preview or an update, and hands back what the reader made of them. The
    referenced stack publishes nothing else, so an output not among them is
    answered by the SDK's own `get_output`, which reads it back `None`.
-   `deserialized` is what the SDK makes of a producer's outputs on the way
    to a reference, with the SDK's own deserializer: the step that drops a
    null's key.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from typing import Any

import pulumi
import pytest
from google.protobuf import struct_pb2
from mock_monitor import Recorder, declaring, run_with
from pulumi.runtime import rpc

__all__ = ('deserialized', 'read_through', 'reading_back')


@contextmanager
def reading_back(outputs: Mapping[str, object]) -> Generator[None]:
    """Every `StackReference` reads each of `outputs` back as given, and every other output through the mock monitor."""
    through = pulumi.StackReference.get_output

    def get_output(reference: pulumi.StackReference, name: pulumi.Input[str]) -> pulumi.Output[object]:
        if isinstance(name, str) and name in outputs:
            return pulumi.Output.from_input(outputs[name])
        return through(reference, name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.StackReference, 'get_output', get_output)
        yield


class _Unpublished(Recorder):
    """A monitor whose referenced stacks publish nothing."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': {}}
        return {}


async def read_through[T](
    read: Callable[[pulumi.StackReference], pulumi.Output[T]], outputs: Mapping[str, object], *, preview: bool
) -> T | None:
    """What `read` makes of a referenced stack whose outputs are `outputs`, in a preview or an update."""
    _ = await run_with(_Unpublished(), stack='reader', preview=preview)
    with reading_back(outputs):
        checked = read(pulumi.StackReference('producer'))
        async with declaring():
            return await checked.future()


def deserialized(outputs: Mapping[str, Any]) -> dict[str, object]:
    """`outputs`, values a protobuf `Struct` can carry, as a reference holds them once the SDK has deserialized them."""
    published = struct_pb2.Struct()
    published.update(outputs)
    return rpc.deserialize_properties(published)
