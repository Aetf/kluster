"""Serializing a dynamic provider without leaving the pickler patched.

Every dynamic resource pickles its provider into state, and Pulumi sorts
dictionaries while it does so, so that an unchanged provider serializes to
unchanged bytes. It sorts them by replacing two methods on the pickler for
the duration of the call, and it restores neither: `serialize_provider` rebinds
the `pickle.Pickler` name to `pickle._Pickler` first, so the replacements land
on the Python implementation -- which is the one `dill` derives from and so the
one that does the work -- while its `finally` puts back only the name.

Two things follow:

-   The replacement of `_batch_setitems` closes over whatever that attribute
    held, so each call wraps the previous wrapper. The chain is as long as the
    number of providers serialized in the process so far and is walked once per
    dictionary pickled, which costs a stack frame per dynamic resource and
    makes the time quadratic in their number.
-   `save_dict` stays replaced too, so every later `dill` or `pickle._Pickler`
    user in the process keeps sorting, whoever they are.

`install_pickler_restore` wraps the leaking function so that both methods are
put back after each call. It is installed when `kluster.providers` is imported,
which is before any dynamic resource declared here can be constructed, so no
provider has to remember it and a new one cannot forget it.

The leak is not reported upstream: the analysis lives in
`Aetf/kluster-ops#165` and the repair lives here. Nothing watches Pulumi's
releases for it either. The shim stays until Pulumi restores what it patches,
and the canary case in `tests/test_provider_serialization.py` is what says that
day has come -- it fails then, naming what to delete.
"""

from __future__ import annotations

import functools
import pickle
from typing import TYPE_CHECKING, Any, cast

import pulumi.dynamic.dynamic as dynamic_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from pulumi.dynamic import ResourceProvider

__all__ = ('LEAKED_METHODS', 'install_pickler_restore')

#: The pickler `dill` builds on, and the one Pulumi patches. Private to
#: `pickle` and typed as nothing but a class, because the methods below are an
#: implementation detail no stub describes.
PICKLER: type[Any] = pickle._Pickler  # pyright: ignore[reportPrivateUsage]

#: What `serialize_provider` replaces on the pickler and never restores.
LEAKED_METHODS = ('_batch_setitems', 'save_dict')


def install_pickler_restore() -> None:
    """Make Pulumi's provider serialization leave the pickler as it found it.

    Installed once: an already-wrapped function is left alone, so a second
    call -- a module reload, say -- adds no second wrapper.
    """
    # Cast because upstream marks the function `@no_type_check`, which leaves
    # its parameter untyped; the signature is the one below names.
    serialize = cast('Callable[[ResourceProvider], str]', dynamic_module.serialize_provider)
    if hasattr(serialize, '__wrapped__'):
        return
    dynamic_module.serialize_provider = _with_pickler_restored(serialize)


def _with_pickler_restored(serialize: Callable[[ResourceProvider], str]) -> Callable[[ResourceProvider], str]:
    """`serialize`, with the pickler's leaked methods saved and put back around it."""

    @functools.wraps(serialize)
    def serialize_and_restore(provider: ResourceProvider) -> str:
        found = {name: getattr(PICKLER, name) for name in LEAKED_METHODS}
        try:
            return serialize(provider)
        finally:
            for name, method in found.items():
                setattr(PICKLER, name, method)

    return serialize_and_restore
