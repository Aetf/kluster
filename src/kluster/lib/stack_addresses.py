"""Addresses read out of another stack's outputs, refused unless they are addresses.

A program that hands an address output to a resource reads it through here,
and anything a `StackReference` hands back in place of an address
(`kluster.lib.stack_outputs`, which says what each is and why), or anything
but an address of the family it needs, stops the run at the read, naming the
output and what it held and never the value itself. The check runs unknowns
included, so a preview refuses what an update would.

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

from kluster.lib import stack_outputs

__all__ = (
    'Family',
    'Held',
    'UnusableAddressOutput',
    'address',
    'addresses_by_name',
    'usable_address',
    'usable_addresses',
)

#: An IP version, as `ipaddress` numbers them.
type Family = Literal[4, 6]


class Held(stack_outputs.Kinds):
    """What an address output held instead of an address, or a mapping of them.

    The shared kinds mean what `kluster.lib.stack_outputs.Held` says they do;
    the rest are an address's own.
    """

    UNKNOWN = stack_outputs.Held.UNKNOWN
    ABSENT = stack_outputs.Held.ABSENT
    EMPTY_MAPPING = stack_outputs.Held.EMPTY_MAPPING
    MAPPING = stack_outputs.Held.MAPPING
    NOT_A_NAME = stack_outputs.Held.NOT_A_NAME
    EMPTY_STRING = stack_outputs.Held.EMPTY_STRING
    OTHER = stack_outputs.Held.OTHER
    #: An address of the other family.
    WRONG_FAMILY = 'wrong family'
    #: A string that is not an address.
    NOT_AN_ADDRESS = 'not an address'


class UnusableAddressOutput(ValueError):
    """A stack output read for an address holds something that is not one.

    `output` names it, `family` is the IP version read from it, `held` says
    what it held, and `entry` is the name of the mapping entry at fault, or
    `None` where the output as a whole is.
    """

    def __init__(self, message: str, *, output: str, family: Family, held: Held, entry: str | None = None) -> None:
        super().__init__(message)
        self.output: str = output
        self.family: Family = family
        self.held: Held = held
        self.entry: str | None = entry


def address(reference: pulumi.StackReference, output: str, family: Family) -> pulumi.Output[str]:
    """The output `output` of the referenced stack, refused unless it is an IPv`family` address."""
    return stack_outputs.read(reference, output, lambda value: usable_address(output, family, value))


def addresses_by_name(reference: pulumi.StackReference, output: str, family: Family) -> pulumi.Output[dict[str, str]]:
    """The output `output` of the referenced stack, refused unless it maps names to IPv`family` addresses."""
    return stack_outputs.read(reference, output, lambda value: usable_addresses(output, family, value))


def usable_address(output: str, family: Family, value: object) -> str:
    """`value` if it is an IPv`family` address, else a refusal saying what it is -- never the value itself."""
    found = _not_an_address(family, value)
    if found is None:
        return cast('str', value)
    held, said = found
    raise UnusableAddressOutput(
        f'the {output!r} output is {said}; an IPv{family} address is read from it, and nothing is declared with '
        'what it holds',
        output=output,
        family=family,
        held=held,
    )


def usable_addresses(output: str, family: Family, value: object) -> dict[str, str]:
    """`value` if it maps names to IPv`family` addresses, else a refusal saying what it is -- never a value in it."""
    found = stack_outputs.not_a_mapping(Held, value)
    at: str | None = None
    if found is None:
        entries = cast('Mapping[object, object]', value)
        for name, entry in entries.items():
            key_found = stack_outputs.not_a_name(Held, name)
            if key_found is not None:
                held, said = key_found
                found = (held, f'a mapping with {said}')
                break
            entry_found = _not_an_address(family, entry)
            if entry_found is not None:
                held, said = entry_found
                found, at = (held, f'a mapping whose {name!r} entry is {said}'), cast('str', name)
                break
        else:
            return {cast('str', name): cast('str', entry) for name, entry in entries.items()}
    held, said = found
    raise UnusableAddressOutput(
        f'the {output!r} output is {said}; a name-to-IPv{family}-address mapping is read from it, and nothing is '
        'declared with what it holds',
        output=output,
        family=family,
        held=held,
        entry=at,
    )


def _not_an_address(family: Family, value: object) -> tuple[Held, str] | None:
    """What `value` is, if it is not an IPv`family` address; `None` if it is one."""
    found = stack_outputs.not_a_string(Held, value)
    if found is not None:
        return found
    try:
        parsed = ipaddress.ip_address(cast('str', value))
    except ValueError:
        return Held.NOT_AN_ADDRESS, 'a string that is not an address'
    return None if parsed.version == family else (Held.WRONG_FAMILY, f'an IPv{parsed.version} address')
