"""The section numbers a markdown document offers a `§` reference to land on.

Every suite that resolves a `§N` against a document reads the rule here, so
they agree on what a number lands on; `test_docs_sections`' docstring is where
it is argued.

`sections` reads prose, not markdown: its caller blanks the fenced code first
(`fences.prose`), so a `# ` comment in a fence is not a heading.
"""

from __future__ import annotations

import re

HEADING = re.compile(r'^#{1,6}\s+(?:§?(\d+(?:\.\d+)*)\.?(?:\s|$))?')
INLINE_LABEL = re.compile(r'\*\*§(\d+(?:\.\d+)*)\b')
LIST_ITEM = re.compile(r'^(\d+)\.\s')


def sections(text: str) -> set[str]:
    """Every number a reference into this prose can land on."""
    found: set[str] = set()
    items: set[str] = set()
    heading: str | None = None
    for line in text.splitlines():
        if match := HEADING.match(line):
            heading = match.group(1)
            if heading:
                found.add(heading)
        elif heading and (match := LIST_ITEM.match(line)):
            items.add(f'{heading}.{match.group(1)}')
    found |= {match.group(1) for match in INLINE_LABEL.finditer(text)}
    # A list item is `§N.M` only where nothing heading-numbered stands beneath
    # §N; where a `### N.x` or `**§N.x**` exists, `§N.x` is that and nothing else.
    beneath = {'.'.join(number.split('.')[:depth]) for number in found for depth in range(1, number.count('.') + 1)}
    found |= {item for item in items if item.rsplit('.', 1)[0] not in beneath}
    for number in list(found):
        parts = number.split('.')
        found |= {'.'.join(parts[:depth]) for depth in range(1, len(parts))}
    return found
