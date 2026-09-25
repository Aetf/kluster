"""Fenced code in markdown, as far as the suites need to tell it from prose.

A test that reads the repository's prose has to know which lines are code: a
`# ` line inside a fence is a comment rather than a heading, and a command in
a fence is a command rather than a sentence. `lines` is that one reading.

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


def lines(text: str) -> Iterator[Line]:
    """Every line of `text` but the fence delimiters, each with its block."""
    fence: Fence | None = None
    marker = ''
    for number, line in enumerate(text.splitlines(), start=1):
        if fence is None:
            if opened := OPENING.match(line):
                marker = opened.group(1)
                fence = Fence(number, opened.group(2))
                continue
        elif re.fullmatch(rf'\s*{re.escape(marker[0])}{{{len(marker)},}}\s*', line):
            fence = None
            continue
        yield Line(line, fence)
