"""Every component class and every dynamic resource class the installation defines, and the type token each states.

The census imports every module under `kluster`, so a class no suite builds
is found too: `test_component_tokens.py` holds each class's token to its form,
and `test_apps_placement.py` holds its run to every application component the
census finds.
"""

import importlib
import pkgutil

from pulumi import dynamic

import kluster
from putils import Component


def _below[T](cls: type[T]) -> list[type[T]]:
    return [each for sub in cls.__subclasses__() for each in (sub, *_below(sub))]


def _defined[T](base: type[T]) -> list[type[T]]:
    """Every subclass of `base` the installation defines, found by importing every module under `kluster`."""
    for module in pkgutil.walk_packages(kluster.__path__, f'{kluster.__name__}.'):
        if module.name.rpartition('.')[2] != '__main__':
            importlib.import_module(module.name)
    found = dict.fromkeys(cls for cls in _below(base) if cls.__module__.startswith(f'{kluster.__name__}.'))
    return list(found)


def components() -> list[type[Component]]:
    """Every component class the installation defines."""
    return _defined(Component)


def dynamic_resources() -> list[type[dynamic.Resource]]:
    """Every dynamic resource class the installation defines."""
    return _defined(dynamic.Resource)


def stated(cls: type[Component]) -> str | None:
    """The token `cls` states in its own class statement, never one a base states."""
    token = vars(cls).get('__pulumi_type__')
    return token if isinstance(token, str) else None
