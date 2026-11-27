"""The tags a review or a ruling rules with, and the places each binds: what `harvest` reads of a comment.

A tag is `[gate:<name>]`, `[gate:<name> clean]`, either with `operator`
last (`TAG`), and it binds to the places its unit names (`units`): a list
item with the paragraphs indented under it, or any other line alone. A place
is named at the comment's own head, `path:line` or `path:first–last`
(`PLACE`), a commit's message among them as `:commit/<sha>:<line>`; or at a
pull request and head the comment names, in the form `calibrate` prints a
finding no case confirms, `#<pr> <path>:<lines> at <head>` (`AT`). `read`
gives every tag of a text with the places it binds. What a tag rules is
`harvest`'s, which records each as a case; the examples a gate's prompt
carries cut the reason a case's comment gives as the unit that binds its tag
(`examples.cut`), so both read a comment one way.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from .change import COMMITS
from .verdict import Verdict

#: A tag: the gate, whether it holds the place clean, and whether the ruling is the operator's.
TAG = re.compile(r'\[gate:(?P<gate>[a-z0-9-]+)(?P<clean> clean)?(?P<operator> operator)?\]')
#: Where a tag opens, in any case: `[gate` and then a colon or a space. Each is a tag or is reported as one that
#: does not read; a link to `gateway.md` or a subscript `Sequence[Gate]` opens none.
TAG_OPENING = re.compile(r'\[gate(?=[ \t]*:|[ \t])', re.I)
#: A place at the comment's own head: a path, which holds a letter, or a commit's message by its SHA; a line; and
#: a last line after a dash.
PLACE = re.compile(
    rf'(?<![\w/.-])(?P<path>{COMMITS}/[0-9a-f]{{7,40}}|(?=[\w./-]*[A-Za-z])[\w.-][\w./-]*)'
    r':(?P<line>\d+)(?:[ \t]*[–-][ \t]*(?P<end>\d+))?'
)
#: A place at a pull request and head it names, as `calibrate` prints an unconfirmed finding: the path is any
#: run of characters but spaces, bounded by the pull request ahead of it and the head after it.
AT = re.compile(
    r'#(?P<pr>\d+)[ \t]+`?(?P<path>\S+?):(?P<line>\d+)(?:[–-](?P<end>\d+))?`?'
    r'[ \t]+at[ \t]+(?P<head>[0-9a-f]{40})(?![0-9a-f])'
)
#: Where a list item opens, at any depth, or a heading.
_OPENS = re.compile(r'^[ \t]*(?:[-*+]|\d+[.)])[ \t]|^#{1,6}[ \t]')


@dataclass(frozen=True)
class Named:
    """A place as a comment names it: a path, its first line and its last, and the pull request and head it gives."""

    path: str
    line: int
    end_line: int
    pr: int | None = None
    head: str | None = None


@dataclass(frozen=True)
class Tagged:
    """One tag of a comment, with every place its unit names (`units`)."""

    gate: str
    verdict: Verdict
    operator: bool
    places: tuple[Named, ...]


def units(text: str) -> Iterator[str]:
    """What a tag in `text` binds within: a list item with its indented paragraphs, or any other line alone.

    An item nested under another is a unit of its own, and ends its parent's.
    """
    block: list[str] = []
    item = False
    gap = False

    def done() -> Iterator[str]:
        if item:
            yield ' '.join(block)
        else:
            yield from block

    for line in text.split('\n'):
        if not line.strip():
            gap = bool(block)
            continue
        opens = _OPENS.match(line) is not None
        continues = not gap or (item and line[:1] in (' ', '\t'))
        if block and (opens or not continues):
            yield from done()
            block = []
        if not block:
            item = opens and not line.lstrip().startswith('#')
        block.append(line.strip())
        gap = False
    if block:
        yield from done()


def _named(match: re.Match[str], *, at: bool) -> Named:
    line = int(match['line'])
    end = int(match['end'] or line)
    if at:
        return Named(match['path'].strip('`'), line, end, int(match['pr']), match['head'])
    return Named(match['path'], line, end)


@dataclass(frozen=True)
class Read:
    """A comment's tags, with the places each binds, and each unit whose tags of one gate disagree."""

    tagged: list[Tagged]
    #: What makes a unit ambiguous, and its text, which binds none of its tags.
    ambiguous: list[tuple[str, str]]
    #: Each text that opens like a tag and is none.
    malformed: list[str]


def named(unit: str) -> tuple[Named, ...]:
    """Every place `unit` names, at a pull request and head it gives (`AT`) or at the comment's own (`PLACE`)."""
    at = list(AT.finditer(unit))
    spans = [m.span() for m in at]
    plain = [m for m in PLACE.finditer(unit) if not any(a <= m.start() < b for a, b in spans)]
    return tuple(sorted([_named(m, at=True) for m in at] + [_named(m, at=False) for m in plain], key=_order))


def read(text: str) -> Read:
    """Every tag of `text`, with the places its unit names (`units`); a tag whose unit names none has none.

    A unit in which one gate's tags give two rulings, two verdicts or one the
    operator's and one not, binds none of them, and so does one that names
    several places with the tags of several gates beside them: nothing says
    which ruling is which place's. One place takes every gate's tag beside it.
    """
    tagged: list[Tagged] = []
    ambiguous: list[tuple[str, str]] = []
    for unit in units(text):
        places = named(unit)
        rulings: dict[str, set[tuple[Verdict, bool]]] = {}
        for tag in TAG.finditer(unit):
            ruled = (Verdict.PASS if tag['clean'] else Verdict.FINDING, tag['operator'] is not None)
            rulings.setdefault(tag['gate'], set()).add(ruled)
        if len(places) > 1 and len(rulings) > 1:
            ambiguous.append((f'tags of {", ".join(sorted(rulings))} beside its places', unit))
            continue
        for gate, ruled in rulings.items():
            if len(ruled) > 1:
                ambiguous.append((f'[gate:{gate}] tags that disagree', unit))
                continue
            ((verdict, operator),) = ruled
            tagged.append(Tagged(gate, verdict, operator, places))
    malformed = [_opening(text, m.start()) for m in TAG_OPENING.finditer(text) if TAG.match(text, m.start()) is None]
    return Read(tagged, ambiguous, malformed)


def _opening(text: str, start: int) -> str:
    """The text a tag opening at `start` runs to: its closing bracket, or the end of its line."""
    end = min(i for i in (text.find(']', start), text.find('\n', start), len(text)) if i >= 0)
    return text[start : end + 1 if end < len(text) and text[end] == ']' else end]


def _order(named: Named) -> tuple[str, int, int]:
    return (named.path, named.line, named.end_line)
