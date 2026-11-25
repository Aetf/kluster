"""The narration gate's extractor: the passages of prose a change writes, one item to a passage.

A passage is the unit a reader takes in at once, and the least a question
about narration can be answered over, since a sentence that narrates is
often soft-wrapped across lines and only some of them rewritten:

-   in a Markdown file, a paragraph, a list item without the items nested
    under it, or a line that stands alone, a heading or a table row; a
    block of code, fenced or indented, is no passage;
-   in a Python file, a docstring (module, class or function), or a run of
    comments on consecutive lines, a comment after code being a run of its
    own;
-   in any other file, a run of consecutive comment lines: a line that
    opens with `#` or `//`, or a Jinja comment, which runs from its `{#` to
    the line that closes it.

A `#!` line names an interpreter, and is no comment in either kind of file.

Each passage the change writes into is an item: its lines are the first to
the last the change writes, its text those lines, and its context the whole
passage where it runs past them. Text this repository holds but did not
write gives no item (`NOT_WRITTEN_HERE`).
"""

from __future__ import annotations

import ast
import io
import re
import textwrap
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from ..extract import Change, Item

#: Where the text is held here but written elsewhere, so that no passage of it is this repository's
#: prose: generated (the SDKs, the CRD package, mise's lockfiles), vendored, or recorded as test data.
#: Today's instances.
NOT_WRITTEN_HERE = ('sdks/', 'packages/crds/', 'mise.lock', 'mise.ci.lock', 'tests/vendor/', 'tests/data/')

#: A fence opening or closing a fenced block of markdown.
_FENCE = re.compile(r'^\s*(```|~~~)')
#: A list item's marker and the spaces after it: the item opens a passage, and its content starts past them.
_OPENER = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+')
#: A line of Markdown that is a passage by itself: a heading, or a table row.
_ALONE = re.compile(r'^#{1,6}\s|^\s*\|')
#: A line of dashes alone, with any colons and pipes: a table's header rule, a thematic break, or a
#: front-matter fence. None of them is prose.
_RULE = re.compile(r'^\s*\|?[\s:|-]*-[\s:|-]*$')
#: A comment line outside Markdown and Python; and a Jinja comment's opening and closing.
_COMMENT = re.compile(r'^\s*(?:#(?!!)|//)')
_JINJA_OPENS = re.compile(r'^\s*\{#')
_JINJA_CLOSES = '#}'


@dataclass(frozen=True)
class Passage:
    """A passage of a file, by its first and last line."""

    first: int
    last: int


def _code(lines: Sequence[str]) -> set[int]:
    """The lines of `lines` that are code: fenced, or an indented block.

    A block is indented code where it opens after a blank line four columns
    or more past the content of the list item it sits in, or past the
    margin outside any list, and it runs to the next line that is not.
    """
    code: set[int] = set()
    fenced = False
    content = 0
    indented = False
    blank = True
    for number, line in enumerate(lines, start=1):
        if _FENCE.match(line) is not None:
            code.add(number)
            fenced = not fenced
            continue
        if fenced:
            code.add(number)
            continue
        if not line.strip():
            blank = True
            continue
        indent = len(line) - len(line.lstrip())
        if blank or indented:
            indented = indent >= content + 4
        if indented:
            code.add(number)
        else:
            opener = _OPENER.match(line)
            if opener is not None:
                content = len(opener.group(0))
            elif blank:
                content = min(content, indent)
        blank = False
    return code


def _markdown(lines: Sequence[str]) -> Iterator[Passage]:
    code = _code(lines)
    start: int | None = None
    for number, line in enumerate(lines, start=1):
        alone = _ALONE.match(line) is not None
        prose = number not in code and bool(line.strip()) and _RULE.match(line) is None
        if start is not None and (not prose or alone or _OPENER.match(line) is not None):
            yield Passage(start, number - 1)
            start = None
        if not prose:
            continue
        if alone:
            yield Passage(number, number)
        elif start is None:
            start = number
    if start is not None:
        yield Passage(start, len(lines))


def _runs(numbers: Sequence[int]) -> Iterator[Passage]:
    """`numbers`, ascending, as runs of consecutive lines."""
    start: int | None = None
    previous = 0
    for number in numbers:
        if start is not None and number != previous + 1:
            yield Passage(start, previous)
            start = None
        if start is None:
            start = number
        previous = number
    if start is not None:
        yield Passage(start, previous)


def _comments(lines: Sequence[str]) -> Iterator[Passage]:
    numbers: list[int] = []
    jinja = False
    for number, line in enumerate(lines, start=1):
        jinja = jinja or _JINJA_OPENS.match(line) is not None
        if jinja or _COMMENT.match(line):
            numbers.append(number)
        if jinja and _JINJA_CLOSES in line:
            jinja = False
    return _runs(numbers)


def _python(text: str, lines: Sequence[str]) -> Iterator[Passage]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # A file Python cannot parse is read the way any other file is.
        yield from _comments(lines)
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
                and first.end_lineno is not None
            ):
                yield Passage(first.lineno, first.end_lineno)
    alone: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT or token.string.startswith('#!'):
            continue
        number = token.start[0]
        if lines[number - 1][: token.start[1]].strip():
            yield Passage(number, number)
        else:
            alone.append(number)
    yield from _runs(alone)


def passages(path: str, text: str) -> list[Passage]:
    """The passages of prose of the file at `path`, whose text is `text`."""
    lines = text.split('\n')
    if path.endswith('.md'):
        return list(_markdown(lines))
    if path.endswith('.py'):
        return list(_python(text, lines))
    return list(_comments(lines))


def extract(change: Change) -> list[Item]:
    """An item for each passage of prose `change` writes into, with the lines it writes as the item's."""
    items: list[Item] = []
    for changed in change.files:
        if not changed.written or changed.path.startswith(NOT_WRITTEN_HERE):
            continue
        text = change.text(changed.path)
        if text is None:
            continue
        lines = text.split('\n')
        written = {n for first, last in changed.written for n in range(first, last + 1)}
        for passage in sorted(set(passages(changed.path, text)), key=lambda p: p.first):
            touched = [n for n in range(passage.first, passage.last + 1) if n in written]
            if not touched:
                continue
            line, end_line = touched[0], touched[-1]
            context = (
                '' if (line, end_line) == (passage.first, passage.last) else _block(lines, passage.first, passage.last)
            )
            items.append(Item(changed.path, line, end_line, _block(lines, line, end_line), context))
    return items


def _block(lines: Sequence[str], first: int, last: int) -> str:
    return textwrap.dedent('\n'.join(lines[first - 1 : last]))
