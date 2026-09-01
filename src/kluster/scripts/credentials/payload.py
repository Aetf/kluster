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

from dataclasses import dataclass
from typing import cast

from .masters import CredentialRejected


class ResponseRejected(CredentialRejected):
    """A provider's answer is not the shape the code reading it needs.

    A subclass because the two are one event to everything that handles them:
    a run that cannot go on against a provider, reported to the operator as one
    line rather than as a traceback (`cli.py`). What separates them is the
    sentence — a rejected credential names the credential, this names the
    field.
    """


def _describe(value: object) -> str:
    """A value in a refusal: what it is, and what it says if that is short."""
    shown = repr(value)
    return shown if len(shown) <= 60 else f'a {type(value).__name__} of {len(shown)} characters'


@dataclass(frozen=True)
class Payload:
    """One JSON object from a provider, read field by field.

    `where` is the call that answered plus the path walked into its answer, so
    a refusal says which entry of which response is wrong rather than that
    something somewhere was.
    """

    where: str
    fields: dict[str, object]

    @classmethod
    def of(cls, answer: object, where: str) -> Payload:
        """The answer as an object, or a refusal saying what arrived instead."""
        if not isinstance(answer, dict):
            raise ResponseRejected(f'{where}: expected an object, and the answer is {_describe(answer)}')
        return cls(where=where, fields=cast('dict[str, object]', answer))

    @classmethod
    def each(cls, answer: object, where: str) -> tuple[Payload, ...]:
        """The answer as an array of objects, each carrying its own index."""
        if not isinstance(answer, list):
            raise ResponseRejected(f'{where}: expected a list, and the answer is {_describe(answer)}')
        listed = cast('list[object]', answer)
        return tuple(cls.of(entry, f'{where}[{index}]') for index, entry in enumerate(listed))

    def value(self, field: str) -> object:
        """A field that must be there, as it arrived.

        For the one caller that hands a value straight to another parser: a
        Cloudflare envelope's `result` is a different shape per call, so what
        it holds is the call's business rather than the envelope's.
        """
        if field not in self.fields:
            raise ResponseRejected(f'{self.where}: the answer carries no {field}')
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
            raise ResponseRejected(f'{self.where}: {field} is {_describe(value)}, not a non-empty string')
        return value

    def string(self, field: str) -> str:
        """A field that must be there, whose empty value means something.

        A B2 lifecycle rule's prefix is the case: empty is the rule that
        governs the whole bucket, which is a different rule from the one this
        program writes rather than a missing answer.
        """
        value = self.value(field)
        if not isinstance(value, str):
            raise ResponseRejected(f'{self.where}: {field} is {_describe(value)}, not a string')
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
            raise ResponseRejected(f'{self.where}: {field} is {_describe(value)}, not a string')
        return value

    def optional_whole(self, field: str) -> int | None:
        """A count a provider may send as null, meaning "no limit"."""
        value = self.fields.get(field)
        if value is None:
            return None
        # `True` is an `int` in Python, and is not a number of days.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ResponseRejected(f'{self.where}: {field} is {_describe(value)}, not a whole number')
        return value

    def texts(self, field: str) -> tuple[str, ...]:
        """A field that must be a list of strings, such as a capability set."""
        value = self.value(field)
        if not isinstance(value, list):
            raise ResponseRejected(f'{self.where}: {field} is {_describe(value)}, not a list')
        found: list[str] = []
        for index, entry in enumerate(cast('list[object]', value)):
            if not isinstance(entry, str):
                raise ResponseRejected(f'{self.where}: {field}[{index}] is {_describe(entry)}, not a string')
            found.append(entry)
        return tuple(found)

    def nested(self, field: str) -> Payload:
        """A field that must be an object, read the same way as its parent."""
        return Payload.of(self.value(field), f'{self.where}.{field}')

    def objects(self, field: str) -> tuple[Payload, ...]:
        """A field that must be a list of objects, each carrying its index."""
        return Payload.each(self.value(field), f'{self.where}.{field}')
