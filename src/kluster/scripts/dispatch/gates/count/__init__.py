"""`gate: count`: does a count the change writes have its members in view, and is it its own document's to keep?

The canon is style/README.md's "A count stands beside the members it
counts", which holds the count rules. The gate is shown each file the
change writes that can hold prose (`gates.prose.TRIGGER`), and is asked of
each count the change writes whether the rules allow it where it stands.
Its extractor (`extract`), which draws the items `harvest` records a
review's tags as, reads the prose a change writes into a file this
repository writes
(`extract.written_here`), which is markdown outside code, comments and
Python docstrings (`gates.prose`), and draws each place where a number is
used as a count (`COUNT`): the count's lines, and as context the paragraph
they sit in with what follows it, which is where the rule allows the
members to be.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..extract import Change, Item, written_here
from ..gate import Canon, Spec
from ..prose import TRIGGER, Block, blocks

#: A number word. A count written in words is the common form in prose, and
#: `one` is left out, since it is far more often a pronoun than a count.
_WORDS = (
    r'two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen'
    r'|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|dozen|both'
)
#: A number: a word, or digits that are no part of a version, a path, a place or an identifier.
_NUMBER = rf'(?:(?<![\w.§/:#-])\d+(?![\w.:/-]|\.\d)|\b(?:{_WORDS})\b)'
#: A plural noun: a word ending in `s`, its possessive, or a code span that names one (`GeneratedMember`s).
_PLURAL = r"(?:`[^`\s]+`s\b|\b[A-Za-z][\w-]*(?<![su])s'?(?!\w))"

#: A number used as a count. Either it counts a plural noun at most two words
#: on, `two machine scripts`, `four images`; or it stands for its members
#: itself after a determiner or a possessive, `the chart's four`, `all three`.
COUNT = re.compile(
    rf"{_NUMBER}(?:[ ]+(?:[\w`'-]+[ ]+){{0,2}}{_PLURAL})"
    rf"|(?:\b(?:the|these|those|its|their|all|our|each\s+of\s+the)\b|\w(?:'s|s'))[ ]+(?:{_WORDS})\b",
    re.I,
)

#: How many lines after a count's paragraph go into its context: where a list
#: or the subsections that hold its members would start.
FOLLOWING = 12


def _counts(block: Block, written: set[int]) -> Iterator[tuple[int, int]]:
    """The first and last line of each count in `block` that the change writes at least one line of."""
    for match in COUNT.finditer(block.text):
        first, last = block.line_of(match.start()), block.line_of(match.end() - 1)
        if written.intersection(range(first, last + 1)):
            yield first, last


def extract(change: Change) -> list[Item]:
    """Each count the change writes into prose, a comment or a docstring, with the paragraph it sits in and what follows."""
    items: list[Item] = []
    for changed in change.files:
        if not changed.written or not written_here(changed.path):
            continue
        text = change.text(changed.path)
        if text is None:
            continue
        lines = text.split('\n')
        written = {n for first, last in changed.written for n in range(first, last + 1)}
        for block in blocks(changed.path, text):
            places = sorted(set(_counts(block, written)))
            merged: list[tuple[int, int]] = []
            for first, last in places:
                if merged and first <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
                else:
                    merged.append((first, last))
            following = '\n'.join(lines[block.last : block.last + FOLLOWING])
            context = f'{block.raw(lines)}\n\nWhat follows it:\n{following}' if following.strip() else block.raw(lines)
            items += [
                Item(changed.path, first, last, '\n'.join(lines[first - 1 : last]), context) for first, last in merged
            ]
    return items


GATE = Spec(
    canon=Canon('docs/style/README.md', 'A count stands beside the members it counts'),
    trigger=TRIGGER,
    extract=extract,
)
