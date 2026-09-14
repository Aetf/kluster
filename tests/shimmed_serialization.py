"""A provider serialized the way a resource's `__provider` property holds it.

Its own module because more than one suite pickles a provider to see what
lands in state, and because the guarantee the helper rests on -- that the
engine's `serialize_provider` is reached through the shim
`kluster.providers.serialization` installs -- should travel with the helper
rather than with each suite's import list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pulumi.dynamic.dynamic as dynamic_module

# For its side effect: importing the package installs the shim on the module
# attribute `serialized` reads, so a suite that imports this helper has the
# shim in place whatever else it imports. Both checkers read an import nobody
# names as unused; this one is used at import.
import kluster.providers  # noqa: F401  # pyright: ignore[reportUnusedImport]

if TYPE_CHECKING:
    from collections.abc import Callable

    from pulumi.dynamic import ResourceProvider


def serialized(instance: ResourceProvider) -> str:
    """What a `__provider` property holds for `instance`: the engine's own serialization.

    It lives beside the base class rather than in the package's exports, and it
    is looked up on the module at each call rather than bound by a from-import:
    the shim replaces that module attribute, and a name bound before
    `kluster.providers` was imported would be the bare function, which leaves
    the process's pickler patched for every case after the caller.
    """
    # Cast because upstream marks the function `@no_type_check`.
    serialize = cast('Callable[[ResourceProvider], str]', dynamic_module.serialize_provider)
    return serialize(instance)
