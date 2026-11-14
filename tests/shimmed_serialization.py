"""A provider serialized the way a resource's `__provider` property holds it.

Its own module because more than one suite pickles a provider to see what
lands in state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pulumi.dynamic.dynamic as dynamic_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from pulumi.dynamic import ResourceProvider


def serialized(instance: ResourceProvider) -> str:
    """What a `__provider` property holds for `instance`: the engine's own serialization."""
    # Cast because upstream marks the function `@no_type_check`.
    serialize = cast('Callable[[ResourceProvider], str]', dynamic_module.serialize_provider)
    return serialize(instance)
