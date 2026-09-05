"""Declaring a dynamic resource leaves the process's pickler as it found it.

Pulumi sorts dictionaries while it pickles a provider into state, and it does
that by replacing two methods on `pickle._Pickler` without ever putting them
back. One of the replacements closes over the previous value of the attribute,
so unrepaired the wrappers stack: a stack frame per dynamic resource ever
serialized, walked for every dictionary pickled, and quadratic time. The repair
is `kluster.providers.serialization`, installed when the provider package is
imported.

The first two cases declare a real resource rather than calling the
serializer, because what has to hold is that a resource *this repository
declares* pays nothing -- the shim reaching that path is half of what is being
asserted. The third is the canary that says when the shim may be deleted.
"""

from __future__ import annotations

import functools
import inspect
import pickle
from typing import TYPE_CHECKING, Any

import pulumi.dynamic.dynamic as dynamic_module
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster.providers.adguard_rewrites import AdGuardRewrite

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The pickler `dill` derives from and the one Pulumi patches, and the two
#: methods it patches. Declared here rather than imported from the module under
#: test, so that the assertions below cannot agree with it by construction.
PICKLER: type[Any] = pickle._Pickler  # pyright: ignore[reportPrivateUsage]
LEAKED_METHODS = ('_batch_setitems', 'save_dict')

INSTANCE = 'adguard-test'
ENDPOINT = 'http://adguard.test:3000'


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Recorder:
    return await run_with(Recorder(), stack='dns')


async def declare(name: str) -> None:
    """One rewrite, which is one provider serialized."""
    async with declaring():
        _ = AdGuardRewrite(name, instance=INSTANCE, endpoint=ENDPOINT, domain=f'{name}.test', answer='192.0.2.1')


def methods() -> dict[str, Any]:
    """What the pickler carries under the patched names right now."""
    return {name: getattr(PICKLER, name) for name in LEAKED_METHODS}


@pytest.fixture
def depths(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """How deep the stack was each time the pickler batched a dictionary.

    Recorded from underneath: this sits at the bottom of whatever Pulumi puts
    on top of it, so a wrapper left over from an earlier serialization shows up
    as a frame here. The signature is open because the method takes a second
    argument from CPython 3.14 on, and this reads neither.
    """
    recorded: list[int] = []
    original = PICKLER._batch_setitems

    @functools.wraps(original)
    def record_depth(self: Any, *items: Any) -> None:
        recorded.append(len(inspect.stack(0)))
        original(self, *items)

    monkeypatch.setattr(PICKLER, '_batch_setitems', record_depth)
    yield recorded


@pytest.mark.asyncio
async def test_declaring_a_dynamic_resource_leaves_the_pickler_as_it_found_it() -> None:
    before = methods()
    assert all(method.__module__ == 'pickle' for method in before.values()), (
        f'a serialization earlier in this session already left a wrapper behind: {before}'
    )

    await declare('kept')

    assert methods() == before


@pytest.mark.asyncio
async def test_repeated_declarations_do_not_deepen_the_pickler(depths: list[int]) -> None:
    await declare('first')
    first = list(depths)
    depths.clear()

    await declare('second')

    assert depths == first
    assert first, 'the pickler batched no dictionary, so nothing was measured'


def test_pulumi_still_leaves_the_wrapper_behind() -> None:
    """The day this fails, Pulumi has fixed the leak and the shim can go.

    A shim over someone else's bug has to say when it is deletable, and nobody
    bumping Pulumi is going to read `dynamic.py` to find out. This calls the
    function the shim wraps, so the fix announces itself here rather than in a
    release note. The leak is not reported upstream; what is known about it is
    `Aetf/kluster-ops#165`.
    """
    # Untyped because upstream marks the function `@no_type_check` and
    # `__wrapped__` is not on a function until something sets it.
    unshimmed: Any = dynamic_module.serialize_provider.__wrapped__  # pyright: ignore[reportUnknownMemberType, reportFunctionMemberAccess]
    before = methods()
    try:
        _ = unshimmed(dynamic_module.ResourceProvider())
        once = methods()
        _ = unshimmed(dynamic_module.ResourceProvider())
        twice = methods()
    finally:
        for name, method in before.items():
            setattr(PICKLER, name, method)

    assert once != before and twice != once, (
        'Pulumi restores the pickler methods it patches now: delete '
        '`kluster.providers.serialization`, its call in `kluster/providers/__init__.py`, and this suite'
    )
