"""Fenced code in markdown, as far as the suites need to tell it from prose.

A test that reads the repository's prose has to know which lines are code: a
`# ` line inside a fence is a comment rather than a heading, and a command in
a fence is a command rather than a sentence. `lines` is that one reading, and
`prose` is the same reading handed back as text for a reader that counts
offsets.

A fence opens on a line that starts, after any indentation, with three or
more backticks or tildes, and it closes on a line of the same character, at
least as long, with nothing else on it. So a four-backtick fence that quotes
a three-backtick one is one block, and a fence inside a list item is a fence.
A fence that never closes runs to the end of the document. Indentation is
where this parts from CommonMark, which reads a fence indented four spaces
past its container as indented code; nothing here tracks containers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import NamedTuple

OPENING = re.compile(r'^\s*(`{3,}|~{3,})\s*([^`\s]*)')


class Fence(NamedTuple):
    """One fenced block: where it opened, and the first word of its info string."""

    opened_at: int
    info: str


class Line(NamedTuple):
    text: str
    #: The block the line sits in, or `None` outside every fence.
    fence: Fence | None


class _Read(NamedTuple):
    body: str
    #: The line break `body` ended on, empty on a last line that has none.
    ending: str
    fence: Fence | None
    delimiter: bool


def _read(text: str) -> Iterator[_Read]:
    """Every line of `text`, delimiters included, each with the block it opens, closes or sits in."""
    fence: Fence | None = None
    marker = ''
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        body = line.splitlines()[0]
        ending = line[len(body) :]
        if fence is None:
            if opened := OPENING.match(body):
                marker = opened.group(1)
                fence = Fence(number, opened.group(2))
                yield _Read(body, ending, fence, delimiter=True)
                continue
        elif re.fullmatch(rf'\s*{re.escape(marker[0])}{{{len(marker)},}}\s*', body):
            yield _Read(body, ending, fence, delimiter=True)
            fence = None
            continue
        yield _Read(body, ending, fence, delimiter=False)


def lines(text: str) -> Iterator[Line]:
    """Every line of `text` but the fence delimiters, each with its block."""
    for read in _read(text):
        if not read.delimiter:
            yield Line(read.body, read.fence)


def prose(text: str) -> str:
    """The text with every fence blanked, delimiters included, its lengths kept so lines and offsets survive."""
    return ''.join((read.body if read.fence is None else ' ' * len(read.body)) + read.ending for read in _read(text))
