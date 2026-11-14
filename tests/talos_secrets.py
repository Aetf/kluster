"""What the Talos provider generates for `Secrets`, and the names a structure travels under.

A helper module of its own, for the reason `mock_monitor` is one: every suite
that declares a `TalosCluster` answers `Secrets` for the provider, and test
modules do not import each other.

Every name here is read from the generated SDK's type metadata rather than
listed, so an answer carries each field the pinned SDK declares, spelled the
way the provider spells it on the wire. A field a later SDK adds is in the
answer without anyone writing it down.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Mapping
from typing import Any, cast

# The SDK's type metadata -- which name each field travels under -- lives in
# this private module, and nowhere public.
from pulumi import _types  # pyright: ignore[reportPrivateUsage]
from pulumiverse_talos import machine


def declared(hint: Any, is_declared: Callable[[type], bool]) -> type | None:
    """The one class within `hint` -- an `Input`, an `Optional`, a union -- that `is_declared` accepts.

    `None` where there is none, which is a field holding a plain value.
    """
    if isinstance(hint, type) and is_declared(hint):
        return hint
    found = {each for arg in typing.get_args(hint) if (each := declared(arg, is_declared)) is not None}
    assert len(found) <= 1, f'{hint} declares more than one type: {found}'
    return found.pop() if found else None


def provider_answer(output_type: type, path: str) -> dict[str, Any]:
    """Every field `output_type` declares, under the provider's own name, each leaf holding its own path.

    The names are the generated SDK's, so the answer is spelled the way the
    provider spells it on the wire, whatever the program reads it back as.
    """
    answer: dict[str, Any] = {}
    for name, hint in _types.output_type_types(output_type).items():
        nested = declared(hint, _types.is_output_type)
        answer[name] = provider_answer(nested, f'{path}.{name}') if nested else f'{path}.{name}'
    return answer


def generated_by(resource: type) -> dict[str, Any]:
    """What the provider generates for `resource`: every output of it whose type is a structure, filled in."""
    answer: dict[str, Any] = {}
    for name, hint in _types.resource_types(resource).items():
        nested = declared(hint, _types.is_output_type)
        if nested is not None:
            answer[name] = provider_answer(nested, name)
    return answer


def wire_names(input_type: type) -> dict[str, Any]:
    """The names `input_type` carries its fields under on the wire, nested fields as nested maps."""
    return {
        name: wire_names(nested) if (nested := declared(hint, _types.is_input_type)) else None
        for name, hint in _types.input_type_types(input_type).items()
    }


def names_in(value: Any) -> Any:
    """The keys of a received structure, nested as `wire_names` nests them."""
    if isinstance(value, Mapping):
        return {str(key): names_in(item) for key, item in cast('Mapping[Any, Any]', value).items()}
    return None


def generated_secrets() -> dict[str, Any]:
    """What the provider generates for `talos.machine.Secrets`: the secrets bundle and the client configuration."""
    return generated_by(machine.Secrets)


def declared_input_type(hint: Any) -> type:
    """The input type `hint` declares -- a parameter's or a field's -- which must be exactly one."""
    found = declared(hint, _types.is_input_type)
    assert found is not None, f'{hint} declares no input type'
    return found


def field_hint(input_type: type, name: str) -> Any:
    """The type a field of `input_type` is declared with, by the name it travels under."""
    return _types.input_type_types(input_type)[name]
