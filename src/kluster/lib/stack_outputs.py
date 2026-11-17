"""What a `StackReference` hands back in place of an output's value, and the read that refuses it.

A machine fact crosses a stack boundary as a stack output read through a
`StackReference` (framework/pulumi.md §3.1), and what the reference reads back
is not always what the producer published. Measured at the pinned SDK
(framework/pulumi.md §1.4, §3.1), in place of a value it can hand over:

-   `None`, for an output the producer has not published -- the state of a
    stack that has never been applied, or was applied before it exported that
    output -- and, in an update, for one a targeted apply of the producer wrote
    as Pulumi's unknown sentinel;
-   an unknown, in a preview, for every output of a producer that holds that
    sentinel in any of them;
-   `{}`, for a secret the reading stack cannot decrypt;
-   no key at all, for an entry the producer holds as null, at any depth: the
    SDK drops every key whose value reads back as `None`, so a mapping whose
    entry the producer holds as null arrives without that entry, and an entry
    holding the unknown sentinel does the same in an update.

Carried on unchecked, each of them becomes something a resource takes as a
value -- `str(None)` is `"None"`, a mapping without the entry a reader needs
quietly finds nothing -- so a program that hands an output to a resource reads
it through a reader built on this module, and anything but a usable value
stops the run at the read, naming the output and what it held and never the
value itself.

What every such reader shares is here: `read`, which hands the output to the
reader's check unknowns included (`run_with_unknowns`), so a preview refuses
what an update would; `Held`, the kinds of thing a reader finds in place of a
value whatever the value's shape; and the classification that finds them --
`not_a_mapping` where a mapping goes, `not_a_string` where a string goes,
`not_a_name` where a mapping's key goes. A missing key is caught only where
the reader knows to expect it, so that check stays with each reader.

A reader's own `Held` is a `Kinds`: it carries the shared kinds and adds the
ones only its values have, and `Kinds` holds it to carrying every shared kind
at its definition. The classification takes nothing else. Today's readers are
`kluster.lib.stack_addresses` and `kluster.lib.stack_volumes`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from typing import cast

import pulumi
from pulumi.output import Unknown

__all__ = (
    'Held',
    'Kinds',
    'not_a_mapping',
    'not_a_name',
    'not_a_string',
    'read',
)

#: Why a value can be absent or unknown, said once for every refusal that finds one.
_UNAPPLIED = (
    "a targeted apply of the producing stack wrote it as Pulumi's unknown sentinel (framework/pulumi.md §1.4), "
    'and the rest of that stack has to be applied first'
)


class Held(enum.Enum):
    """What a stack output, or an entry of one, held in place of a value, whatever shape the value has."""

    #: Unknown, as a preview reads every output of a stack holding the unknown sentinel.
    UNKNOWN = enum.auto()
    #: Absent: not published, or written as the sentinel by a targeted apply.
    ABSENT = enum.auto()
    #: An empty mapping, which is also how an undecryptable secret reads back.
    EMPTY_MAPPING = enum.auto()
    #: A mapping where a string goes, which is how an undecryptable secret reads back.
    MAPPING = enum.auto()
    #: A mapping keyed by something other than a name.
    NOT_A_NAME = enum.auto()
    #: An empty string.
    EMPTY_STRING = enum.auto()
    #: A value of another type altogether.
    OTHER = enum.auto()


class Kinds(enum.Enum):
    """A reader's own kinds, held to carrying every shared kind under its own name.

    A reader's `Held` subclasses this and names each shared kind with the
    shared member as its value (`UNKNOWN = stack_outputs.Held.UNKNOWN`), so the
    classification here hands a reader its own member. One that leaves a
    shared kind out, or names it with another value, is refused where it is
    defined, rather than at the first run that finds that kind.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        members = cast('Mapping[str, enum.Enum]', cls.__members__)
        missing = [kind.name for kind in Held if kind.name not in members or members[kind.name].value is not kind]
        if missing:
            raise TypeError(f'{cls.__qualname__} does not carry the shared kinds {missing} (kluster.lib.stack_outputs)')


def read[T](reference: pulumi.StackReference, output: str, check: Callable[[object], T]) -> pulumi.Output[T]:
    """The output `output` of the referenced stack, passed through `check`, in a preview as in an update.

    A plain `apply` does not run on an unknown in a preview, which would go on
    over nothing; this one does, so `check` refuses an unknown where it finds
    one.
    """
    return reference.get_output(output).apply(check, run_with_unknowns=True)


def not_a_mapping[H: Kinds](held: type[H], value: object) -> tuple[H, str] | None:
    """What `value` is, as `held`'s kind and in words, if it is not a non-empty mapping; `None` if it is one."""
    if isinstance(value, Mapping):
        if cast('Mapping[object, object]', value):
            return None
        return held(Held.EMPTY_MAPPING), (
            'an empty mapping, which is also how a StackReference reads back a secret it could not decrypt: '
            "either the producing stack published no entries, or this stack cannot open the producer's secrets"
        )
    return _absent_or_other(held, value)


def not_a_string[H: Kinds](held: type[H], value: object) -> tuple[H, str] | None:
    """What `value` is, as `held`'s kind and in words, if it is not a non-empty string; `None` if it is one.

    A non-empty string is what a reader parses further, as the address or the
    identifier it reads.
    """
    if isinstance(value, str):
        return None if value else (held(Held.EMPTY_STRING), 'an empty string')
    if isinstance(value, Mapping):
        return held(Held.MAPPING), (
            'a mapping, which is how a StackReference reads back a secret it could not decrypt: this stack '
            "cannot open the producing stack's secrets"
        )
    return _absent_or_other(held, value)


def not_a_name[H: Kinds](held: type[H], key: object) -> tuple[H, str] | None:
    """What `key`, a mapping's key, is, as `held`'s kind and in words, if it is not a name; `None` if it is one."""
    return None if isinstance(key, str) else (held(Held.NOT_A_NAME), f'a {type(key).__name__} key')


def _absent_or_other[H: Kinds](held: type[H], value: object) -> tuple[H, str]:
    """What `value` is, for a value that is neither a string nor a mapping."""
    if value is None:
        return held(Held.ABSENT), f'absent: the producing stack has not published it, or {_UNAPPLIED}'
    if isinstance(value, Unknown):
        return held(Held.UNKNOWN), (
            "unknown, which is how a preview reads every output of a stack holding Pulumi's unknown sentinel in any "
            f'of them: {_UNAPPLIED}'
        )
    return held(Held.OTHER), f'a {type(value).__name__}'
