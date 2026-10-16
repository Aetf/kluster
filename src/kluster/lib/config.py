"""Reading configuration that is not a plain string.

Most of what this program needs is a decision, and decisions are code. A few
things are not: an address was written into every lease on the LAN long before
this program existed, a machine reported a global address one boot after it was
declared, an operator supplies the list of people an alert goes to, an operator
supplies the public keys that may log in to an appliance. Those arrive as
configuration, from a stack's configuration object or from a file beside the
code that reads it.

Either way it arrives untyped — `require_object` hands back whatever the YAML
held, `require_secret_object` the same inside a secret output, and a file is
bytes — so it crosses into the program through here, at one narrow place that
turns it into something the type checker can see and reports a shape mistake
by name instead of by traceback (rfc-002 §10.4).
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import cast

__all__ = ('Shape', 'ShapeRefused', 'lines', 'strings', 'text')


class Shape(enum.Enum):
    """What a value was instead of the shape it was read as."""

    #: Not JSON at all: what the stack's configuration object holds would not parse.
    NOT_JSON = enum.auto()
    #: Of another type: a value, or an entry of a list, that is not what was asked.
    WRONG_TYPE = enum.auto()
    #: An empty string, where a value or an entry must say something.
    EMPTY = enum.auto()


class ShapeRefused(TypeError):
    """A configuration value refused for its shape, by name and never by value.

    A `TypeError`, as every refusal of a shape here is, and the fields say
    which: `what` is the value's name as the caller gave it, `shape` what it
    was instead, and `entry` the 1-based position of the list entry at fault,
    or `None` where the value as a whole is.
    """

    def __init__(self, message: str, *, what: str, shape: Shape, entry: int | None = None) -> None:
        super().__init__(message)
        self.what: str = what
        self.shape: Shape = shape
        self.entry: int | None = entry


def text(value: object, what: str) -> str:
    """`value` as a non-empty string, or a `ShapeRefused` saying what it is instead.

    The single-value case of `strings`, for the places a structured answer
    would otherwise be coerced into one silently — a `None` that becomes the
    four characters `null`, an object that becomes its JSON.

    **The refusal names the type and never the value.** The one caller is the
    slot map reading a stack output on its way into a GitHub secret
    (`credentials.slots.StateRead`), which logs what this raises, so an export
    that came out in the wrong shape would otherwise put a credential into the
    run log of the command that was pushing it. What the operator has to fix is
    the export's shape, which the type is, and `what` already names which
    output it was.
    """
    if not isinstance(value, str):
        raise ShapeRefused(
            f'{what} must be a non-empty string, and is {type(value).__name__}', what=what, shape=Shape.WRONG_TYPE
        )
    if not value:
        raise ShapeRefused(f'{what} must be a non-empty string, and is empty', what=what, shape=Shape.EMPTY)
    return value


def strings(value: object, what: str) -> tuple[str, ...]:
    """`value` as a list of non-empty strings, or a `ShapeRefused` saying what shape it is.

    **The refusal names the shape and never the value**, for `text`'s reason:
    what this raises is logged, and a list that arrives this way may be
    private — the budget alerts' recipients are personal mailboxes. What the
    operator has to correct is the line they wrote, so the refusal says which
    entry is wrong by its position and what it is instead: a type, or empty.
    """
    if not isinstance(value, list):
        raise ShapeRefused(f'{what} must be a list, not {type(value).__name__}', what=what, shape=Shape.WRONG_TYPE)
    entries = cast('list[object]', value)
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, str):
            raise ShapeRefused(
                f'{what} must be a list of non-empty strings, and entry {position} is {type(entry).__name__}',
                what=what,
                shape=Shape.WRONG_TYPE,
                entry=position,
            )
        if not entry:
            raise ShapeRefused(
                f'{what} must be a list of non-empty strings, and entry {position} is empty',
                what=what,
                shape=Shape.EMPTY,
                entry=position,
            )
    return tuple(cast('list[str]', entries))


#: What introduces a comment in the line-per-value files this repository reads
#: — SSH `authorized_keys`, an `age` identity as `age-keygen` prints it, a
#: recipient list. All three spell it the same way.
COMMENT = '#'


def lines(path: Path, what: str) -> tuple[str, ...]:
    """`path` as its non-empty, non-comment lines, or a `FileNotFoundError`/`ValueError` saying which file.

    The shape a line-per-value file has: blank lines and `#` comments are the
    author's, everything else is a value. An absent file and an empty one are
    both refused by name, because a caller asking for this asks for values —
    a silent empty answer is how a missing operator key becomes an appliance
    nobody can log in to.
    """
    if not path.is_file():
        raise FileNotFoundError(f'{what}: no such file {path}')
    found = tuple(
        stripped
        for line in path.read_text().splitlines()
        if (stripped := line.strip()) and not stripped.startswith(COMMENT)
    )
    if not found:
        raise ValueError(f'{what}: {path} holds no values, only blank and commented lines')
    return found
