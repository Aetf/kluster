"""Reading stack configuration that is not a plain string.

Most of what this program needs is a decision, and decisions are code. A few
things are not: an address was written into every lease on the LAN long before
this program existed, a machine reported a global address one boot after it was
declared, an operator supplies the list of people an alert goes to. Those
arrive as configuration.

Configuration arrives untyped — `require_object` hands back whatever the YAML
held — so it crosses into the program through here, at one narrow place that
turns it into something the type checker can see and reports a shape mistake by
name instead of by traceback (rfc-002 §10.4).
"""

from __future__ import annotations

from typing import cast

__all__ = ('strings',)


def strings(value: object, what: str) -> tuple[str, ...]:
    """`value` as a list of non-empty strings, or a `TypeError` saying what it is.

    The whole list is described in the refusal rather than the offending entry
    alone: a configured list is short, an operator reads it as one value, and
    what they have to correct is the line they wrote.
    """
    if not isinstance(value, list):
        raise TypeError(f'{what} must be a list, not {type(value).__name__}')
    entries = cast('list[object]', value)
    if not all(isinstance(entry, str) and entry for entry in entries):
        raise TypeError(f'{what} must be a list of non-empty strings, and is {entries!r}')
    return tuple(cast('list[str]', entries))
