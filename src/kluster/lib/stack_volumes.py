"""Volume OCIDs read out of another stack's outputs, refused unless they are a node-to-volume-to-OCID map.

A stack that publishes its volumes' OCIDs keys them by node and then by the
volume's name, every node's boot volume under `conventions.outputs.BOOT_VOLUME`,
and a consumer reads the map through a `StackReference` (framework/pulumi.md
§3.1). What the reference reads back in place of a value
(`kluster.lib.stack_outputs`, which says what each is and why) becomes, carried
on unchecked, a map with no volume in it, a node without its boot volume --
the SDK drops the key of an entry the producer holds as null -- or a volume
whose id is `"None"`, and whatever reads the map by a volume's OCID quietly
finds nothing.

So a program that hands the map to a resource reads it through here, and
anything but a non-empty map of maps of OCIDs, each holding its node's boot
volume, stops the run at the read, naming the output and what it held, and
never the value itself. A missing key is caught only where the reader knows to
expect it, so the boot volume is the one volume every node is held to. The
check runs unknowns included, so a preview refuses what an update would.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pulumi

from kluster.conventions.outputs import BOOT_VOLUME
from kluster.lib import stack_outputs

__all__ = (
    'OCID_PREFIX',
    'Held',
    'UnusableVolumeOutput',
    'usable_volume_ids',
    'volume_ids',
)

#: What every OCID starts with: the version of its format, which is the only one
#: OCI issues (`ocid1.<resource type>.<realm>.[region].<unique id>`).
OCID_PREFIX = 'ocid1.'


class Held(stack_outputs.Kinds):
    """What a volume-id output held where its map, a node's map, or an OCID goes.

    The shared kinds mean what `kluster.lib.stack_outputs.Held` says they do;
    the rest are the map's own.
    """

    UNKNOWN = stack_outputs.Held.UNKNOWN
    ABSENT = stack_outputs.Held.ABSENT
    EMPTY_MAPPING = stack_outputs.Held.EMPTY_MAPPING
    MAPPING = stack_outputs.Held.MAPPING
    NOT_A_NAME = stack_outputs.Held.NOT_A_NAME
    EMPTY_STRING = stack_outputs.Held.EMPTY_STRING
    OTHER = stack_outputs.Held.OTHER
    #: A string that is not an OCID.
    NOT_AN_OCID = 'not an OCID'
    #: A node's entry without its boot volume, which is how a null reads back.
    NO_BOOT_VOLUME = 'no boot volume'


class UnusableVolumeOutput(ValueError):
    """A stack output read for volume OCIDs holds something that is not a map of them.

    `output` names it, `held` says what it held, and `node` and `volume` are
    the names of the entries at fault, each `None` where the fault is above it:
    both for the output as a whole, `volume` alone for a node's whole entry.
    """

    def __init__(
        self, message: str, *, output: str, held: Held, node: str | None = None, volume: str | None = None
    ) -> None:
        super().__init__(message)
        self.output: str = output
        self.held: Held = held
        self.node: str | None = node
        self.volume: str | None = volume


def volume_ids(reference: pulumi.StackReference, output: str) -> pulumi.Output[dict[str, dict[str, str]]]:
    """The output `output` of the referenced stack, refused unless it maps nodes to volume names to OCIDs."""
    return stack_outputs.read(reference, output, lambda value: usable_volume_ids(output, value))


def usable_volume_ids(output: str, value: object) -> dict[str, dict[str, str]]:
    """`value` if it maps nodes to volume names to OCIDs, else a refusal saying what it is -- never a value in it."""
    found = stack_outputs.not_a_mapping(Held, value)
    if found is not None:
        raise _refusal(output, found)
    usable: dict[str, dict[str, str]] = {}
    for node, volumes in cast('Mapping[object, object]', value).items():
        found = stack_outputs.not_a_name(Held, node)
        if found is not None:
            held, said = found
            raise _refusal(output, (held, f'a mapping with {said}'))
        node = cast('str', node)
        found = stack_outputs.not_a_mapping(Held, volumes)
        if found is not None:
            held, said = found
            raise _refusal(output, (held, f'a mapping whose {node!r} entry is {said}'), node=node)
        usable[node] = {}
        for volume, ocid in cast('Mapping[object, object]', volumes).items():
            found = stack_outputs.not_a_name(Held, volume)
            if found is not None:
                held, said = found
                raise _refusal(output, (held, f'a mapping whose {node!r} entry has {said}'), node=node)
            volume = cast('str', volume)
            found = _not_an_ocid(ocid)
            if found is not None:
                held, said = found
                raise _refusal(
                    output,
                    (held, f'a mapping whose {node!r} entry maps {volume!r} to {said}'),
                    node=node,
                    volume=volume,
                )
            usable[node][volume] = cast('str', ocid)
        if BOOT_VOLUME not in usable[node]:
            raise _refusal(
                output,
                (
                    Held.NO_BOOT_VOLUME,
                    f'a mapping whose {node!r} entry holds no {BOOT_VOLUME!r} volume, which is how a StackReference '
                    'reads back a null: the producing stack holds no OCID for that boot volume',
                ),
                node=node,
            )
    return usable


def _refusal(output: str, found: tuple[Held, str], *, node: str | None = None, volume: str | None = None) -> Exception:
    held, said = found
    return UnusableVolumeOutput(
        f'the {output!r} output is {said}; a node-to-volume-to-OCID mapping is read from it, and nothing is '
        'declared with what it holds',
        output=output,
        held=held,
        node=node,
        volume=volume,
    )


def _not_an_ocid(value: object) -> tuple[Held, str] | None:
    """What `value` is, if it is not an OCID; `None` if it is one."""
    found = stack_outputs.not_a_string(Held, value)
    if found is not None:
        return found
    return None if cast('str', value).startswith(OCID_PREFIX) else (Held.NOT_AN_OCID, 'a string that is not an OCID')
