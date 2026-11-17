"""What a StackReference hands back in place of a value, classified once for every reader of one.

The SDK's half first: the deserializer a reference's outputs go through drops
a null's key at every depth, which is why a reader looks for a missing key
rather than for a null. Then the classification each reader is built from,
against a reader's `Held` of the test's own, so the kinds are shown to come
back as the reader's members; the check that a reader's `Held` carries every
shared kind, made where it is defined; and the read, under mocks, in a preview
and an update.
"""

from __future__ import annotations

import pytest
from pulumi.output import UNKNOWN
from referenced_stack import deserialized, read_through

from kluster.lib import stack_outputs


class Held(stack_outputs.Kinds):
    """A reader's own kinds, as a reader declares them: every shared kind, and one of its own."""

    UNKNOWN = stack_outputs.Held.UNKNOWN
    ABSENT = stack_outputs.Held.ABSENT
    EMPTY_MAPPING = stack_outputs.Held.EMPTY_MAPPING
    MAPPING = stack_outputs.Held.MAPPING
    NOT_A_NAME = stack_outputs.Held.NOT_A_NAME
    EMPTY_STRING = stack_outputs.Held.EMPTY_STRING
    OTHER = stack_outputs.Held.OTHER
    OWN = 'own'


def kind(found: tuple[Held, str] | None) -> Held | None:
    """The kind a classification found, `None` where it found the value usable."""
    return None if found is None else found[0]


def test_a_null_reads_back_as_no_key_at_every_depth() -> None:
    read_back = deserialized({'published': {'node-a': {'kept': 'x', 'null': None}}, 'null': None})

    assert read_back == {'published': {'node-a': {'kept': 'x'}}}


#: What can stand in where a non-empty mapping goes, with the kind it is found to be.
NOT_A_MAPPING: dict[str, tuple[object, Held]] = {
    'absent': (None, Held.ABSENT),
    'unknown': (UNKNOWN, Held.UNKNOWN),
    'elided-secret': ({}, Held.EMPTY_MAPPING),
    'a-string': ('x', Held.OTHER),
    'a-list': ([], Held.OTHER),
}

#: What can stand in where a non-empty string goes, with the kind it is found to be.
NOT_A_STRING: dict[str, tuple[object, Held]] = {
    'absent': (None, Held.ABSENT),
    'unknown': (UNKNOWN, Held.UNKNOWN),
    'elided-secret': ({}, Held.MAPPING),
    'a-mapping': ({'a': 'x'}, Held.MAPPING),
    'empty': ('', Held.EMPTY_STRING),
    'a-number': (7, Held.OTHER),
}


@pytest.mark.parametrize('found', NOT_A_MAPPING)
def test_anything_but_a_non_empty_mapping_is_found_as_the_readers_kind(found: str) -> None:
    value, held = NOT_A_MAPPING[found]

    assert kind(stack_outputs.not_a_mapping(Held, value)) is held


@pytest.mark.parametrize('found', NOT_A_STRING)
def test_anything_but_a_non_empty_string_is_found_as_the_readers_kind(found: str) -> None:
    value, held = NOT_A_STRING[found]

    assert kind(stack_outputs.not_a_string(Held, value)) is held


def test_a_key_that_is_not_a_name_is_found_as_the_readers_kind() -> None:
    assert kind(stack_outputs.not_a_name(Held, 1)) is Held.NOT_A_NAME


def test_a_value_of_its_shape_is_found_to_be_nothing() -> None:
    assert stack_outputs.not_a_mapping(Held, {'a': None}) is None
    assert stack_outputs.not_a_string(Held, 'x') is None
    assert stack_outputs.not_a_name(Held, 'a') is None


def test_a_readers_held_without_every_shared_kind_is_refused_where_it_is_defined() -> None:
    with pytest.raises(TypeError) as refused:

        class Short(stack_outputs.Kinds):  # pyright: ignore[reportUnusedClass] -- defining it is the case
            UNKNOWN = stack_outputs.Held.UNKNOWN
            # A shared kind's name, holding something other than the shared kind.
            ABSENT = 'absent'

    lacking = {kind.name for kind in stack_outputs.Held} - {'UNKNOWN'}
    assert all(repr(name) in str(refused.value) for name in lacking)
    assert repr('UNKNOWN') not in str(refused.value)


async def classified(outputs: dict[str, object], *, preview: bool) -> Held | None:
    """The kind of what `stack_outputs.read` hands its check for `the_output` of a stack publishing `outputs`."""
    return await read_through(
        lambda reference: stack_outputs.read(
            reference, 'the_output', lambda value: kind(stack_outputs.not_a_string(Held, value))
        ),
        outputs,
        preview=preview,
    )


@pytest.mark.asyncio
async def test_a_preview_hands_the_check_an_unknown_rather_than_skipping_it() -> None:
    """A plain `apply` would not run on an unknown in a preview, and the preview would go on over nothing."""
    assert await classified({'the_output': UNKNOWN}, preview=True) is Held.UNKNOWN


@pytest.mark.asyncio
async def test_an_update_hands_the_check_the_named_output_or_its_absence() -> None:
    assert await classified({'the_output': 'x'}, preview=False) is None

    assert await classified({'another_output': 'x'}, preview=False) is Held.ABSENT


@pytest.mark.asyncio
async def test_a_preview_hands_the_check_the_absence_of_an_output_the_stack_does_not_publish() -> None:
    """An output the stack does not publish is absent in a preview too, never unknown."""
    assert await classified({'another_output': 'x'}, preview=True) is Held.ABSENT
