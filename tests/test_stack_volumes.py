"""Volume OCIDs read across a StackReference: each thing it can hand back in place of the map is refused by name.

What a StackReference reads back in place of an output's value
(`kluster.lib.stack_outputs`) -- a node without its boot volume among them,
for a boot volume the producer holds as null, since the SDK drops a null's
key -- and the ordinary mistakes of shape besides: a node or a volume keyed by
something other than a name, a node mapped straight to an OCID, and a volume
mapped to a string that is not one. A refusal says what it found by its
fields, names the output and the entries at fault, and never quotes what it
found: the string values below carry a marker the message must not contain.

The last cases read through the SDK: a null boot volume deserialized as a
StackReference's outputs are, and a StackReference under mocks in a preview
and an update, so the check is shown to run where a plain `apply` would skip
it.
"""

from __future__ import annotations

import pytest
from pulumi.output import UNKNOWN
from referenced_stack import deserialized, read_through

from kluster.conventions.outputs import BOOT_VOLUME
from kluster.lib.stack_volumes import Held, UnusableVolumeOutput, usable_volume_ids, volume_ids

#: In the refusals' place of the value, which the message must never quote.
MARKER = 'marker'

#: An OCID of each kind the map holds, as OCI spells them.
BOOT = 'ocid1.bootvolume.oc1.phx.boot-a'
BLOCK = 'ocid1.volume.oc1.phx.block-a'

#: What can stand in for the map, with what the refusal says it held and the
#: node and volume entries it blames.
NOT_A_MAP: dict[str, tuple[object, Held, str | None, str | None]] = {
    'absent': (None, Held.ABSENT, None, None),
    'unknown': (UNKNOWN, Held.UNKNOWN, None, None),
    'elided-secret': ({}, Held.EMPTY_MAPPING, None, None),
    'a-single-ocid': (BOOT, Held.OTHER, None, None),
    'node-key-not-a-string': ({1: {BOOT_VOLUME: BOOT}}, Held.NOT_A_NAME, None, None),
    'node-unknown': ({'node-a': UNKNOWN}, Held.UNKNOWN, 'node-a', None),
    'node-elided-secret': ({'node-a': {}}, Held.EMPTY_MAPPING, 'node-a', None),
    'node-to-an-ocid': ({'node-a': f'ocid1.{MARKER}'}, Held.OTHER, 'node-a', None),
    'node-with-only-a-block-volume': ({'node-a': {'cache': BLOCK}}, Held.NO_BOOT_VOLUME, 'node-a', None),
    'second-node-without-its-boot-volume': (
        {'node-a': {BOOT_VOLUME: BOOT}, 'node-b': {'cache': BLOCK}},
        Held.NO_BOOT_VOLUME,
        'node-b',
        None,
    ),
    'volume-key-not-a-string': ({'node-a': {2: BOOT}}, Held.NOT_A_NAME, 'node-a', None),
    'volume-unknown': ({'node-a': {BOOT_VOLUME: UNKNOWN}}, Held.UNKNOWN, 'node-a', BOOT_VOLUME),
    'volume-elided-secret': ({'node-a': {BOOT_VOLUME: {}}}, Held.MAPPING, 'node-a', BOOT_VOLUME),
    'volume-empty': ({'node-a': {BOOT_VOLUME: ''}}, Held.EMPTY_STRING, 'node-a', BOOT_VOLUME),
    'volume-display-name': ({'node-a': {BOOT_VOLUME: f'{MARKER}-boot'}}, Held.NOT_AN_OCID, 'node-a', BOOT_VOLUME),
    'volume-not-a-string': ({'node-a': {BOOT_VOLUME: 7}}, Held.OTHER, 'node-a', BOOT_VOLUME),
    # The fault is found wherever it is, not only in the first entry.
    'second-node-at-fault': (
        {'node-a': {BOOT_VOLUME: BOOT}, 'node-b': {BOOT_VOLUME: BOOT, 'cache': f'{MARKER}-cache'}},
        Held.NOT_AN_OCID,
        'node-b',
        'cache',
    ),
}


@pytest.mark.parametrize('found', NOT_A_MAP)
def test_anything_but_a_map_of_volume_ocids_is_refused_naming_the_output_and_what_it_is(found: str) -> None:
    value, held, node, volume = NOT_A_MAP[found]

    with pytest.raises(UnusableVolumeOutput) as refused:
        _ = usable_volume_ids('the_output', value)

    # A program tells refusals apart by their fields, not by their message.
    assert (refused.value.output, refused.value.held, refused.value.node, refused.value.volume) == (
        'the_output',
        held,
        node,
        volume,
    )
    # A person reads the message, which names the output and every entry at
    # fault, and never quotes a value.
    message = str(refused.value)
    assert "'the_output'" in message
    assert all(repr(entry) in message for entry in (node, volume) if entry is not None)
    assert MARKER not in message


def test_a_map_of_volume_ocids_is_handed_on_as_it_is() -> None:
    ids = {
        'node-a': {BOOT_VOLUME: BOOT, 'cache': BLOCK},
        'node-b': {BOOT_VOLUME: 'ocid1.bootvolume.oc1.phx.boot-b'},
    }

    assert usable_volume_ids('the_output', ids) == ids


def test_a_null_boot_volume_reads_back_as_a_node_without_it_and_is_refused() -> None:
    """What a StackReference hands over for a producer whose state holds a node's boot volume as null.

    The SDK drops the null's key on the way (`kluster.lib.stack_outputs`), so
    the node arrives holding only its block volume, and the reader looks for
    the missing key rather than for a null.
    """
    read_back = deserialized({'the_output': {'node-a': {BOOT_VOLUME: None, 'cache': BLOCK}}})['the_output']

    with pytest.raises(UnusableVolumeOutput) as refused:
        _ = usable_volume_ids('the_output', read_back)

    assert (refused.value.held, refused.value.node) == (Held.NO_BOOT_VOLUME, 'node-a')


async def read(value: object, *, preview: bool) -> object:
    """`value`, read back as the output `the_output` of a referenced stack through the module."""
    return await read_through(
        lambda reference: volume_ids(reference, 'the_output'), {'the_output': value}, preview=preview
    )


@pytest.mark.asyncio
async def test_a_preview_refuses_an_unknown_rather_than_skipping_the_check() -> None:
    """A plain `apply` would not run on an unknown in a preview, and the preview would go on over nothing."""
    with pytest.raises(UnusableVolumeOutput) as refused:
        _ = await read(UNKNOWN, preview=True)

    assert refused.value.held is Held.UNKNOWN


@pytest.mark.asyncio
async def test_an_update_refuses_an_absent_output() -> None:
    with pytest.raises(UnusableVolumeOutput) as refused:
        _ = await read(None, preview=False)

    assert refused.value.held is Held.ABSENT


@pytest.mark.asyncio
async def test_a_published_map_reads_through_unchanged() -> None:
    ids = {'node-a': {BOOT_VOLUME: BOOT, 'cache': BLOCK}}

    assert await read(ids, preview=False) == ids
