"""Volume OCIDs read out of another stack's outputs, refused unless they are a node-to-volume-to-OCID map.

A stack that publishes its volumes' OCIDs keys them by node and then by the
volume's name, every node's boot volume under `conventions.outputs.BOOT_VOLUME`,
and a consumer reads the map through a `StackReference` (framework/pulumi.md
§3.1). What the reference reads back is not always what the producer published.
Measured at the pinned SDK (framework/pulumi.md §1.4, §3.1), in place of a value
it can hand over:

-   `None`, for an output the producer has not published -- the state of a
    stack applied before it exported the map -- and, in an update, for one a
    targeted apply of the producer wrote as Pulumi's unknown sentinel;
-   an unknown, in a preview, for every output of a producer that holds that
    sentinel in any of them;
-   `{}`, for a secret the reading stack cannot decrypt;
-   no key at all, for an entry the producer holds as null, at any depth: the
    SDK drops every key whose value reads back as `None`, so a node whose boot
    volume the producer holds no OCID for arrives as a node without the key,
    and an entry holding the unknown sentinel does the same in an update.

Carried on unchecked, each of them becomes a map with no volume in it, a node
without its boot volume, or a volume whose id is `"None"`, and whatever reads
the map by a volume's OCID quietly finds nothing. So a program that hands the
map to a resource reads it through here, and anything but a non-empty map of
maps of OCIDs, each holding its node's boot volume, stops the run at the read,
naming the output and what it held, and never the value itself. A missing key
is caught only where the reader knows to expect it, so the boot volume is the
one volume every node is held to. The check runs unknowns included
(`run_with_unknowns`), so a preview refuses what an update would.

`kluster.lib.stack_addresses` reads the address outputs under the same rule.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import cast

import pulumi
from pulumi.output import Unknown

from kluster.conventions.outputs import BOOT_VOLUME

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

#: Why a value can be absent or unknown, said once for every refusal that finds one.
_UNAPPLIED = (
    "a targeted apply of the producing stack wrote it as Pulumi's unknown sentinel (framework/pulumi.md §1.4), "
    'and the rest of that stack has to be applied first'
)


class Held(enum.Enum):
    """What a volume-id output held where its map, a node's map, or an OCID goes."""

    #: Unknown, as a preview reads every output of a stack holding the unknown sentinel.
    UNKNOWN = enum.auto()
    #: Absent: not published, or written as the sentinel by a targeted apply.
    ABSENT = enum.auto()
    #: An empty mapping, which is also how an undecryptable secret reads back.
    EMPTY_MAPPING = enum.auto()
    #: A mapping where an OCID goes.
    MAPPING = enum.auto()
    #: A mapping keyed by something other than a name.
    NOT_A_NAME = enum.auto()
    #: A string that is not an OCID.
    NOT_AN_OCID = enum.auto()
    #: A node's entry without its boot volume, which is how a null reads back.
    NO_BOOT_VOLUME = enum.auto()
    #: An empty string.
    EMPTY_STRING = enum.auto()
    #: A value of another type altogether.
    OTHER = enum.auto()


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
    return reference.get_output(output).apply(lambda value: usable_volume_ids(output, value), run_with_unknowns=True)


def usable_volume_ids(output: str, value: object) -> dict[str, dict[str, str]]:
    """`value` if it maps nodes to volume names to OCIDs, else a refusal saying what it is -- never a value in it."""
    found = _not_a_map(value)
    if found is not None:
        raise _refusal(output, found)
    usable: dict[str, dict[str, str]] = {}
    for node, volumes in cast('Mapping[object, object]', value).items():
        if not isinstance(node, str):
            raise _refusal(output, (Held.NOT_A_NAME, f'a mapping with a {type(node).__name__} key'))
        found = _not_a_map(volumes)
        if found is not None:
            held, said = found
            raise _refusal(output, (held, f'a mapping whose {node!r} entry is {said}'), node=node)
        usable[node] = {}
        for volume, ocid in cast('Mapping[object, object]', volumes).items():
            if not isinstance(volume, str):
                raise _refusal(
                    output,
                    (Held.NOT_A_NAME, f'a mapping whose {node!r} entry has a {type(volume).__name__} key'),
                    node=node,
                )
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


def _not_a_map(value: object) -> tuple[Held, str] | None:
    """What `value` is, if it is not a non-empty mapping; `None` if it is one."""
    if isinstance(value, Mapping):
        if cast('Mapping[object, object]', value):
            return None
        return Held.EMPTY_MAPPING, (
            'an empty mapping, which is also how a StackReference reads back a secret it could not decrypt: '
            "either the producing stack published no entries, or this stack cannot open the producer's secrets"
        )
    return _absent_or_other(value)


def _not_an_ocid(value: object) -> tuple[Held, str] | None:
    """What `value` is, if it is not an OCID; `None` if it is one."""
    if isinstance(value, str):
        if value.startswith(OCID_PREFIX):
            return None
        return (Held.NOT_AN_OCID, 'a string that is not an OCID') if value else (Held.EMPTY_STRING, 'an empty string')
    if isinstance(value, Mapping):
        return Held.MAPPING, (
            'a mapping, which is how a StackReference reads back a secret it could not decrypt: this stack '
            "cannot open the producing stack's secrets"
        )
    return _absent_or_other(value)


def _absent_or_other(value: object) -> tuple[Held, str]:
    """What `value` is, for a value that is neither a string nor a mapping."""
    if value is None:
        return Held.ABSENT, f'absent: the producing stack has not published it, or {_UNAPPLIED}'
    if isinstance(value, Unknown):
        return Held.UNKNOWN, (
            "unknown, which is how a preview reads every output of a stack holding Pulumi's unknown sentinel in any "
            f'of them: {_UNAPPLIED}'
        )
    return Held.OTHER, f'a {type(value).__name__}'
