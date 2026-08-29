"""Reading stack configuration that is not a plain string.

Most of what this program needs is a decision, and decisions are code. A few
things are not: an image digest is whatever the build produced, a node
identifier was minted by the device it belongs to, an address was written into
every lease on the LAN long before this program existed. Those arrive as
configuration.

Configuration arrives untyped — `require_object` hands back whatever the YAML
held — so it crosses into the program through here, at one narrow place that
turns it into something the type checker can see and reports a shape mistake by
name instead of by traceback (rfc-002 §10.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

__all__ = ('mapping', 'text')


def mapping(value: object, what: str) -> dict[str, object]:
    """`value` as a mapping keyed by name, or a `TypeError` saying what it is.

    Keys are stringified rather than required to be strings: YAML is happy to
    read an unquoted name as something else, and an entry called `true` is a
    configuration mistake to describe, not a crash to suffer.
    """
    if not isinstance(value, Mapping):
        raise TypeError(f'{what} must be a mapping, not {type(value).__name__}')
    return {str(key): item for key, item in cast('Mapping[object, object]', value).items()}


def text(entry: Mapping[str, object], key: str, what: str) -> str:
    """One non-empty string out of a configured entry."""
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'{what} carries no {key}')
    return value
