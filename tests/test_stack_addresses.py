"""Addresses read across a StackReference: each thing it can hand back in place of one is refused by name.

What a StackReference reads back in place of an output's value
(`kluster.lib.stack_outputs`), and the ordinary mistakes besides, a string
that is not an address and an address of the other family, for each shape of
output the module reads. A refusal says what it found by its fields, names
the output and the entry at fault, and never quotes what it found: the string
values below carry a marker the message must not contain.

The last cases read through a StackReference under mocks, a preview and an
update, so the check is shown to run where a plain `apply` would skip it.
"""

from __future__ import annotations

import pytest
from pulumi.output import UNKNOWN
from referenced_stack import read_through

from kluster.lib.stack_addresses import (
    Family,
    Held,
    UnusableAddressOutput,
    address,
    addresses_by_name,
    usable_address,
    usable_addresses,
)

#: In the refusals' place of the value, which the message must never quote.
MARKER = 'marker'

#: What can stand in for one address, with what the refusal says it held.
NOT_AN_ADDRESS: dict[str, tuple[object, Held]] = {
    'absent': (None, Held.ABSENT),
    'unknown': (UNKNOWN, Held.UNKNOWN),
    'elided-secret': ({}, Held.MAPPING),
    'empty': ('', Held.EMPTY_STRING),
    'hostname': (f'{MARKER}.example', Held.NOT_AN_ADDRESS),
    'not-a-string': ([], Held.OTHER),
}

#: What can stand in for a name-to-address mapping, with what the refusal says
#: it held and the entry it blames.
NOT_A_MAPPING: dict[str, tuple[object, Held, str | None]] = {
    'absent': (None, Held.ABSENT, None),
    'unknown': (UNKNOWN, Held.UNKNOWN, None),
    'elided-secret': ({}, Held.EMPTY_MAPPING, None),
    'a-single-address': ('192.0.2.1', Held.OTHER, None),
    'entry-unknown': ({'node-a': UNKNOWN}, Held.UNKNOWN, 'node-a'),
    'entry-not-an-address': ({'node-a': f'{MARKER}.example'}, Held.NOT_AN_ADDRESS, 'node-a'),
    'entry-wrong-family': ({'node-a': '2001:db8::1'}, Held.WRONG_FAMILY, 'node-a'),
    'key-not-a-string': ({1: '192.0.2.1'}, Held.NOT_A_NAME, None),
    # The fault is found wherever it is, not only in the first entry.
    'second-entry-at-fault': ({'node-a': '192.0.2.1', 'node-b': ''}, Held.EMPTY_STRING, 'node-b'),
}


@pytest.mark.parametrize('found', NOT_AN_ADDRESS)
def test_anything_but_an_address_is_refused_naming_the_output_and_what_it_is(found: str) -> None:
    value, held = NOT_AN_ADDRESS[found]

    with pytest.raises(UnusableAddressOutput) as refused:
        _ = usable_address('the_output', 4, value)

    # A program tells refusals apart by their fields, not by their message.
    assert (refused.value.output, refused.value.family, refused.value.held, refused.value.entry) == (
        'the_output',
        4,
        held,
        None,
    )
    # A person reads the message, which names the output and never quotes a value.
    assert "'the_output'" in str(refused.value)
    assert MARKER not in str(refused.value)


@pytest.mark.parametrize(('family', 'other'), [(4, '2001:db8::1'), (6, '192.0.2.1')])
def test_an_address_of_the_other_family_is_refused(family: Family, other: str) -> None:
    with pytest.raises(UnusableAddressOutput) as refused:
        _ = usable_address('the_output', family, other)

    assert (refused.value.held, refused.value.family) == (Held.WRONG_FAMILY, family)


@pytest.mark.parametrize(('family', 'value'), [(4, '192.0.2.1'), (6, '2001:db8::1')])
def test_an_address_of_its_family_is_handed_on_as_it_is(family: Family, value: str) -> None:
    assert usable_address('the_output', family, value) == value


@pytest.mark.parametrize('found', NOT_A_MAPPING)
def test_anything_but_a_mapping_of_addresses_is_refused_naming_the_output_and_what_it_is(found: str) -> None:
    value, held, entry = NOT_A_MAPPING[found]

    with pytest.raises(UnusableAddressOutput) as refused:
        _ = usable_addresses('the_output', 4, value)

    assert (refused.value.output, refused.value.family, refused.value.held, refused.value.entry) == (
        'the_output',
        4,
        held,
        entry,
    )
    # The message names the output and the entry at fault, and never quotes a value.
    message = str(refused.value)
    assert "'the_output'" in message
    assert entry is None or repr(entry) in message
    assert MARKER not in message


@pytest.mark.parametrize('family', [4, 6])
@pytest.mark.parametrize('mapping', [False, True], ids=['address', 'mapping'])
def test_a_refusal_carries_the_family_it_was_read_for(mapping: bool, family: Family) -> None:
    with pytest.raises(UnusableAddressOutput) as refused:
        _ = usable_addresses('the_output', family, None) if mapping else usable_address('the_output', family, None)

    assert refused.value.family == family


def test_a_mapping_of_addresses_of_its_family_is_handed_on_as_it_is() -> None:
    nodes = {'node-a': '2001:db8::1', 'node-b': '2001:db8::2'}

    assert usable_addresses('the_output', 6, nodes) == nodes


async def read(value: object, *, preview: bool, mapping: bool) -> object:
    """`value`, read back as the output `the_output` of a referenced stack through the module."""
    return await read_through(
        lambda reference: (
            addresses_by_name(reference, 'the_output', 4) if mapping else address(reference, 'the_output', 4)
        ),
        {'the_output': value},
        preview=preview,
    )


@pytest.mark.parametrize('mapping', [False, True], ids=['address', 'mapping'])
@pytest.mark.asyncio
async def test_a_preview_refuses_an_unknown_rather_than_skipping_the_check(mapping: bool) -> None:
    """A plain `apply` would not run on an unknown in a preview, and the preview would go on over nothing."""
    with pytest.raises(UnusableAddressOutput) as refused:
        _ = await read(UNKNOWN, preview=True, mapping=mapping)

    assert refused.value.held is Held.UNKNOWN


@pytest.mark.parametrize('mapping', [False, True], ids=['address', 'mapping'])
@pytest.mark.asyncio
async def test_an_update_refuses_an_absent_output(mapping: bool) -> None:
    with pytest.raises(UnusableAddressOutput) as refused:
        _ = await read(None, preview=False, mapping=mapping)

    assert refused.value.held is Held.ABSENT


@pytest.mark.asyncio
async def test_a_published_mapping_reads_through_unchanged() -> None:
    nodes = {'node-a': '192.0.2.1'}

    assert await read(nodes, preview=False, mapping=True) == nodes
