"""Addresses read out of another stack's outputs, refused unless they are addresses.

A machine fact crosses a stack boundary as a stack output read through a
`StackReference` (framework/pulumi.md §3.1), and what the reference reads back
is not always the value the producer meant. Measured at the pinned SDK
(framework/pulumi.md §1.4, §3.1), in place of an output it can hand over:

-   `None`, for an output the producer has not published -- the state of a
    stack that has never been applied -- and, in an update, for one a targeted
    apply of the producer wrote as Pulumi's unknown sentinel;
-   an unknown, in a preview, for every output of a producer that holds that
    sentinel in any of them;
-   `{}`, for a secret the reading stack cannot decrypt.

Carried on unchecked, each of them becomes something a resource takes as an
address: `str(None)` is `"None"`. So a program that hands an address output to
a resource reads it through here, and anything but an address of the family it
needs stops the run at the read, naming the output and what it held and never
the value itself. The check runs unknowns included (`run_with_unknowns`), so a
preview refuses what an update would.

Two shapes of output are read: one address (`address`), and a mapping from a
name the producer chose -- a node's -- to one address each
(`addresses_by_name`). The second refuses an empty mapping too, since an
elided secret reads back as one and no reader of a set of node addresses wants
none.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Literal, cast

import pulumi
from pulumi.output import Unknown

__all__ = ('Family', 'UnusableAddressOutput', 'address', 'addresses_by_name', 'usable_address', 'usable_addresses')

#: An IP version, as `ipaddress` numbers them.
type Family = Literal[4, 6]

#: Why a value can be absent or unknown, said once for every refusal that finds one.
_UNAPPLIED = (
    "a targeted apply of the producing stack wrote it as Pulumi's unknown sentinel (framework/pulumi.md §1.4), "
    'and the rest of that stack has to be applied first'
)


class UnusableAddressOutput(ValueError):
    """A stack output read for an address holds something that is not one."""


def address(reference: pulumi.StackReference, output: str, family: Family) -> pulumi.Output[str]:
    """The output `output` of the referenced stack, refused unless it is an IPv`family` address."""
    return reference.get_output(output).apply(
        lambda value: usable_address(output, family, value), run_with_unknowns=True
    )


def addresses_by_name(reference: pulumi.StackReference, output: str, family: Family) -> pulumi.Output[dict[str, str]]:
    """The output `output` of the referenced stack, refused unless it maps names to IPv`family` addresses."""
    return reference.get_output(output).apply(
        lambda value: usable_addresses(output, family, value), run_with_unknowns=True
    )


def usable_address(output: str, family: Family, value: object) -> str:
    """`value` if it is an IPv`family` address, else a refusal saying what it is -- never the value itself."""
    found = _not_an_address(family, value)
    if found is None:
        return cast('str', value)
    raise UnusableAddressOutput(
        f'the {output!r} output is {found}; an IPv{family} address is read from it, and nothing is declared with '
        'what it holds'
    )


def usable_addresses(output: str, family: Family, value: object) -> dict[str, str]:
    """`value` if it maps names to IPv`family` addresses, else a refusal saying what it is -- never a value in it."""
    found = _not_a_mapping(value)
    if found is None:
        entries = cast('Mapping[object, object]', value)
        for name, entry in entries.items():
            if not isinstance(name, str):
                found = f'a mapping with a {type(name).__name__} key'
                break
            entry_found = _not_an_address(family, entry)
            if entry_found is not None:
                found = f'a mapping whose {name!r} entry is {entry_found}'
                break
        else:
            return {cast('str', name): cast('str', entry) for name, entry in entries.items()}
    raise UnusableAddressOutput(
        f'the {output!r} output is {found}; a name-to-IPv{family}-address mapping is read from it, and nothing is '
        'declared with what it holds'
    )


def _not_a_mapping(value: object) -> str | None:
    """What `value` is, if it is not a non-empty mapping; `None` if it is one."""
    if isinstance(value, Mapping):
        if cast('Mapping[object, object]', value):
            return None
        return (
            'an empty mapping, which is also how a StackReference reads back a secret it could not decrypt: '
            "either the producing stack published no entries, or this stack cannot open the producer's secrets"
        )
    return _absent_or_other(value)


def _not_an_address(family: Family, value: object) -> str | None:
    """What `value` is, if it is not an IPv`family` address; `None` if it is one."""
    if isinstance(value, str):
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            return 'a string that is not an address' if value else 'an empty string'
        return None if parsed.version == family else f'an IPv{parsed.version} address'
    if isinstance(value, Mapping):
        return (
            'a mapping, which is how a StackReference reads back a secret it could not decrypt: this stack '
            "cannot open the producing stack's secrets"
        )
    return _absent_or_other(value)


def _absent_or_other(value: object) -> str:
    """What `value` is, for a value that is neither a string nor a mapping."""
    if value is None:
        return f'absent: the producing stack has not published it, or {_UNAPPLIED}'
    if isinstance(value, Unknown):
        return (
            "unknown, which is how a preview reads every output of a stack holding Pulumi's unknown sentinel in any "
            f'of them: {_UNAPPLIED}'
        )
    return f'a {type(value).__name__}'
