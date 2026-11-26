"""`gate: narration`: does a passage the change writes narrate history, attempts, verification or provenance instead of stating what is?

The rule is AGENTS.md's "Every artifact is as-built", the section the gate
quotes. Every path can carry a comment, so every change triggers the gate,
which is shown each file the change writes and each of its commits'
messages. The items its extractor draws, which `harvest` records a review's
tags as, are the passages of prose the change writes into
(`gates.prose`), one item to a passage: its lines are the first to the last
the change writes, its text those lines, and its context the whole passage
where it runs past them. A passage is the least a question about narration
can be answered over, since a sentence that narrates is often soft-wrapped
across lines and only some of them rewritten. Text this repository holds but
did not write gives no item (`extract.written_here`). Each paragraph of the
body of each of the change's commits is an item too, whole, at the place
that names the commit (`extract.message_path`, `gates.prose.message`), since
a commit message is an artifact the rule holds as it holds a file.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence

from ..extract import Change, Item, message_path, written_here
from ..gate import Canon, Spec
from ..prose import blocks, message


def extract(change: Change) -> list[Item]:
    """An item for each passage of prose `change` writes into, with the lines it writes as the item's, and for each paragraph of its commits' bodies."""
    items: list[Item] = []
    for changed in change.files:
        if not changed.written or not written_here(changed.path):
            continue
        text = change.text(changed.path)
        if text is None:
            continue
        lines = text.split('\n')
        written = {n for first, last in changed.written for n in range(first, last + 1)}
        for block in sorted(blocks(changed.path, text), key=lambda b: (b.first, b.last)):
            touched = [n for n in range(block.first, block.last + 1) if n in written]
            if not touched:
                continue
            line, end_line = touched[0], touched[-1]
            whole = (line, end_line) == (block.first, block.last)
            context = '' if whole else _block(lines, block.first, block.last)
            items.append(Item(changed.path, line, end_line, _block(lines, line, end_line), context))
    for commit in change.commits:
        lines = commit.message.split('\n')
        items += [
            Item(message_path(commit.sha), b.first, b.last, _block(lines, b.first, b.last))
            for b in message(commit.message)
        ]
    return items


def _block(lines: Sequence[str], first: int, last: int) -> str:
    return textwrap.dedent('\n'.join(lines[first - 1 : last]))


GATE = Spec(canon=Canon('AGENTS.md', 'Every artifact is as-built'), trigger=('**',), extract=extract)
