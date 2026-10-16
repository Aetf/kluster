"""The untyped side of a provider's JSON answer, read one field at a time.

`b2.py` and `cloudflare.py` speak to JSON APIs, so everything they learn
arrives as an `object` whose shape is the provider's promise rather than this
program's. Each response shape has one parser that turns that answer into a
frozen record, and `Payload` is what a parser reads it through: nothing outside
a parser holds one, and every field crosses into a typed value exactly once.

What is refused, by name and with the path to the offending entry: an answer
that is not the object or array the call returns, a field a parser needs and
the answer does not carry, and a field whose value is of another type. What is
not refused: a field nobody reads, so a provider that adds one to a response
does not stop a mint.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Annotated, cast

from kluster.lib.secret import Secret

from .masters import CredentialRejected


class Expected(enum.Enum):
    """What the reader of an answer needed where it found something else, as a refusal says it."""

    OBJECT = 'an object'
    LIST = 'a list'
    #: The field there at all.
    PRESENT = 'present'
    NON_EMPTY_STRING = 'a non-empty string'
    STRING = 'a string'
    WHOLE_NUMBER = 'a whole number'


class ResponseRejected(CredentialRejected):
    """A provider's answer is not the shape the code reading it needs.

    A subclass because the two are one event to everything that handles them:
    a run that cannot go on against a provider, reported to the operator as one
    line rather than as a traceback (`cli.py`). What separates them is the
    type, and the fields say where: `where` is the call and the path walked
    into its answer, `field` the field at fault or `None` for the answer
    itself, `expected` what was needed, and `found` what arrived instead as
    its kind and size, never its content -- or `None` for a field that is not
    there.
    """

    def __init__(self, where: str, *, field: str | None, expected: Expected, found: str | None) -> None:
        if field is None:
            message = f'{where}: expected {expected.value}, and the answer is {found}'
        elif found is None:
            message = f'{where}: the answer carries no {field}'
        else:
            message = f'{where}: {field} is {found}, not {expected.value}'
        super().__init__(message)
        self.where: str = where
        self.field: str | None = field
        self.expected: Expected = expected
        self.found: str | None = found


def _describe(value: object) -> str:
    """A value in a refusal: its JSON kind and its size, and never its content.

    The answers read here are the ones that carry a credential -- the key
    `b2_create_key` discloses once, the token Cloudflare discloses once -- and a
    refusal is logged. What the operator has to act on is the shape the
    provider sent, which the kind and the size say; the content would put a
    secret into the run log exactly when the provider misbehaved.
    """
    match value:
        case None:
            return 'null'
        case bool():
            return 'a boolean'
        case int() | float():
            return 'a number'
        case str():
            return f'a string of {len(value)} characters'
        case list():
            return f'a list of {len(cast("list[object]", value))} entries'
        case dict():
            return f'an object of {len(cast("dict[object, object]", value))} fields'
        case _:
            return f'a {type(value).__name__}'


@dataclass(frozen=True)
class Payload:
    """One JSON object from a provider, read field by field.

    `where` is the call that answered plus the path walked into its answer, so
    a refusal says which entry of which response is wrong rather than that
    something somewhere was.
    """

    where: str
    #: The answer as it arrived, which for a mint includes the credential the
    #: provider discloses once. Kept out of the repr for that reason; `where`
    #: still prints, and is what a refusal is read by.
    fields: Annotated[dict[str, object], Secret] = field(repr=False, compare=False)

    @classmethod
    def of(cls, answer: object, where: str) -> Payload:
        """The answer as an object, or a refusal saying what arrived instead."""
        if not isinstance(answer, dict):
            raise ResponseRejected(where, field=None, expected=Expected.OBJECT, found=_describe(answer))
        return cls(where=where, fields=cast('dict[str, object]', answer))

    @classmethod
    def each(cls, answer: object, where: str) -> tuple[Payload, ...]:
        """The answer as an array of objects, each carrying its own index."""
        if not isinstance(answer, list):
            raise ResponseRejected(where, field=None, expected=Expected.LIST, found=_describe(answer))
        listed = cast('list[object]', answer)
        return tuple(cls.of(entry, f'{where}[{index}]') for index, entry in enumerate(listed))

    def value(self, field: str) -> object:
        """A field that must be there, as it arrived.

        For the one caller that hands a value straight to another parser: a
        Cloudflare envelope's `result` is a different shape per call, so what
        it holds is the call's business rather than the envelope's.
        """
        if field not in self.fields:
            raise ResponseRejected(self.where, field=field, expected=Expected.PRESENT, found=None)
        return self.fields[field]

    def truth(self, field: str) -> bool:
        """A flag, where anything an answer does not spell as true is false.

        Cloudflare's `success` is the case: a refused call may carry it as
        false or not carry it at all, and both mean the call did not happen.
        """
        return self.fields.get(field) is True

    def text(self, field: str) -> str:
        """A field that must be there and must say something."""
        value = self.value(field)
        if not isinstance(value, str) or not value:
            raise ResponseRejected(self.where, field=field, expected=Expected.NON_EMPTY_STRING, found=_describe(value))
        return value

    def string(self, field: str) -> str:
        """A field that must be there, whose empty value means something.

        A B2 lifecycle rule's prefix is the case: empty is the rule that
        governs the whole bucket, which is a different rule from the one this
        program writes rather than a missing answer.
        """
        value = self.value(field)
        if not isinstance(value, str):
            raise ResponseRejected(self.where, field=field, expected=Expected.STRING, found=_describe(value))
        return value

    def optional_text(self, field: str) -> str | None:
        """A field an answer may leave out or send as null, but not as a number.

        Absent and null are one answer here: both are the provider saying the
        key is unscoped, the page is the last one, or the value does not apply.
        """
        value = self.fields.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ResponseRejected(self.where, field=field, expected=Expected.STRING, found=_describe(value))
        return value

    def optional_whole(self, field: str) -> int | None:
        """A count a provider may send as null, meaning "no limit"."""
        value = self.fields.get(field)
        if value is None:
            return None
        # `True` is an `int` in Python, and is not a number of days.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ResponseRejected(self.where, field=field, expected=Expected.WHOLE_NUMBER, found=_describe(value))
        return value

    def texts(self, field: str) -> tuple[str, ...]:
        """A field that must be a list of strings, such as a capability set."""
        value = self.value(field)
        if not isinstance(value, list):
            raise ResponseRejected(self.where, field=field, expected=Expected.LIST, found=_describe(value))
        found: list[str] = []
        for index, entry in enumerate(cast('list[object]', value)):
            if not isinstance(entry, str):
                raise ResponseRejected(
                    self.where, field=f'{field}[{index}]', expected=Expected.STRING, found=_describe(entry)
                )
            found.append(entry)
        return tuple(found)

    def optional_texts(self, field: str) -> tuple[str, ...]:
        """A list of strings an answer may leave out or send as null.

        Absent is the empty list, which is what a schema marking the field
        optional means: an entry that names no scope is in no scope. A value
        that is there and is not a list of strings is still refused.
        """
        if self.fields.get(field) is None:
            return ()
        return self.texts(field)

    def nested(self, field: str) -> Payload:
        """A field that must be an object, read the same way as its parent."""
        return Payload.of(self.value(field), f'{self.where}.{field}')

    def objects(self, field: str) -> tuple[Payload, ...]:
        """A field that must be a list of objects, each carrying its index."""
        return Payload.each(self.value(field), f'{self.where}.{field}')
