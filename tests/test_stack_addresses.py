"""Addresses read across a StackReference: each thing it can hand back in place of one is refused by name.

What a StackReference reads back in place of an output's value -- `None` for
an absent output, an unknown in a preview of a producer holding Pulumi's
unknown sentinel, `{}` for a secret the reader cannot decrypt
(framework/pulumi.md §1.4, §3.1) -- and the ordinary mistakes besides, a
string that is not an address and an address of the other family, for each
shape of output the module reads. A refusal names the output and what it
found, and never quotes what it found: the string values below carry a marker
the message must not contain.

The last cases read through a StackReference under mocks, a preview and an
update, so the check is shown to run where a plain `apply` would skip it.
"""

from __future__ import annotations

import pulumi
import pytest
from mock_monitor import Recorder, declaring, run_with
from pulumi.output import UNKNOWN

from kluster.lib.stack_addresses import (
    Family,
    UnusableAddressOutput,
    address,
    addresses_by_name,
    usable_address,
    usable_addresses,
)

#: In the refusals' place of the value, which the message must never quote.
MARKER = 'marker'

#: What can stand in for one address, with the part of the refusal that names it.
NOT_AN_ADDRESS: dict[str, tuple[object, str]] = {
    'absent': (None, 'absent'),
    'unknown': (UNKNOWN, 'unknown'),
    'elided-secret': ({}, 'a mapping'),
    'empty': ('', 'an empty string'),
    'stringified-none': ('None', 'not an address'),
    'hostname': (f'{MARKER}.example', 'not an address'),
    'not-a-string': ([], 'a list'),
}

#: What can stand in for a name-to-address mapping, with the part of the refusal that names it.
NOT_A_MAPPING: dict[str, tuple[object, str]] = {
    'absent': (None, 'absent'),
    'unknown': (UNKNOWN, 'unknown'),
    'elided-secret': ({}, 'empty mapping'),
    'a-single-address': ('192.0.2.1', 'a str'),
    'entry-unknown': ({'node-a': UNKNOWN}, "'node-a' entry is unknown"),
    'entry-absent': ({'node-a': None}, "'node-a' entry is absent"),
    'entry-not-an-address': ({'node-a': f'{MARKER}.example'}, "'node-a' entry is a string that is not an address"),
    'entry-wrong-family': ({'node-a': '2001:db8::1'}, "'node-a' entry is an IPv6 address"),
    'key-not-a-string': ({1: '192.0.2.1'}, 'int key'),
}


@pytest.mark.parametrize('found', NOT_AN_ADDRESS)
def test_anything_but_an_address_is_refused_naming_the_output_and_what_it_is(found: str) -> None:
    value, named = NOT_AN_ADDRESS[found]

    with pytest.raises(UnusableAddressOutput) as refused:
        _ = usable_address('the_output', 4, value)

    assert "'the_output'" in str(refused.value)
    assert named in str(refused.value)
    assert MARKER not in str(refused.value)


@pytest.mark.parametrize(('family', 'other'), [(4, '2001:db8::1'), (6, '192.0.2.1')])
def test_an_address_of_the_other_family_is_refused(family: Family, other: str) -> None:
    with pytest.raises(UnusableAddressOutput, match=f'an IPv{10 - family} address'):
        _ = usable_address('the_output', family, other)


@pytest.mark.parametrize(('family', 'value'), [(4, '192.0.2.1'), (6, '2001:db8::1')])
def test_an_address_of_its_family_is_handed_on_as_it_is(family: Family, value: str) -> None:
    assert usable_address('the_output', family, value) == value


@pytest.mark.parametrize('found', NOT_A_MAPPING)
def test_anything_but_a_mapping_of_addresses_is_refused_naming_the_output_and_what_it_is(found: str) -> None:
    value, named = NOT_A_MAPPING[found]

    with pytest.raises(UnusableAddressOutput) as refused:
        _ = usable_addresses('the_output', 4, value)

    assert "'the_output'" in str(refused.value)
    assert named in str(refused.value)
    assert MARKER not in str(refused.value)


def test_a_mapping_of_addresses_of_its_family_is_handed_on_as_it_is() -> None:
    nodes = {'node-a': '2001:db8::1', 'node-b': '2001:db8::2'}

    assert usable_addresses('the_output', 6, nodes) == nodes


async def read(value: object, *, preview: bool, mapping: bool) -> object:
    """`value`, read back as an output of a referenced stack through the module, in a preview or an update.

    The reference's output is answered here rather than by the mock monitor,
    because the mock drops an output that is `None` or unknown on its way to
    the reader.
    """
    _ = await run_with(Recorder(), stack='reader', preview=preview)

    def read_back(reference: pulumi.StackReference, name: pulumi.Input[str]) -> pulumi.Output[object]:
        return pulumi.Output.from_input(value)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.StackReference, 'get_output', read_back)
        reference = pulumi.StackReference('producer')
        checked = addresses_by_name(reference, 'the_output', 4) if mapping else address(reference, 'the_output', 4)
        async with declaring():
            return await checked.future()


@pytest.mark.parametrize('mapping', [False, True], ids=['address', 'mapping'])
@pytest.mark.asyncio
async def test_a_preview_refuses_an_unknown_rather_than_skipping_the_check(mapping: bool) -> None:
    """A plain `apply` would not run on an unknown in a preview, and the preview would go on over nothing."""
    with pytest.raises(UnusableAddressOutput, match='unknown'):
        _ = await read(UNKNOWN, preview=True, mapping=mapping)


@pytest.mark.parametrize('mapping', [False, True], ids=['address', 'mapping'])
@pytest.mark.asyncio
async def test_an_update_refuses_an_absent_output(mapping: bool) -> None:
    with pytest.raises(UnusableAddressOutput, match='absent'):
        _ = await read(None, preview=False, mapping=mapping)


@pytest.mark.asyncio
async def test_a_published_mapping_reads_through_unchanged() -> None:
    nodes = {'node-a': '192.0.2.1'}

    assert await read(nodes, preview=False, mapping=True) == nodes
