"""A pull request's description, held to the form dispatch.md §1.1 gives it.

`lint` reads a description against the dispatch it belongs to and returns
every way the description falls short of the form, each a sentence naming
what is missing, so a refusal reports all of them at once. The form is:

-   **the ledger line**: a closing keyword for the issue where the
    dispatch's `closes` is true, and `Refs Aetf/kluster-ops#<issue>` with
    no closing keyword for it where it is false, a closing keyword being any
    form GitHub reads as one (`ledger.CLOSES`; dispatch.md §1.1, §4);
-   **the sections** of `SECTIONS`, each a `##` heading of exactly that name
    with something under it, and each in its own shape (`Section`).

A section runs from its heading to the next heading of the same or a higher
level. Matching is over the text as written, apart from line endings: a
description saved with CRLF reads as the one written.
"""

from __future__ import annotations

import enum
import re

from . import ledger
from .forge import LEDGER

#: A markdown heading: its level and its text.
_HEADING = re.compile(r'^(?P<level>#{1,6})[ \t]+(?P<name>.*?)[ \t]*#*[ \t]*$', re.M)

#: A code span, which is how a section names a pattern or a test.
_CODE_SPAN = re.compile(r'`[^`\n]+`')
#: A test named in a code span: a case, or a module of them.
_TEST = re.compile(r'`[^`\n]*\btest_\w+[^`\n]*`')
#: A fenced block's opening line, which is how a transcript is quoted.
_FENCE = re.compile(r'^[ \t]*(?:```|~~~)', re.M)
#: A file count: a number, then the word `files` later in the same sentence,
#: which a period followed by whitespace ends; the one in `*.md` does not.
_FILE_COUNT = re.compile(r'\b\d[\d,]*\b(?:[^.]|\.(?=\S)){0,200}?\bfiles?\b')
#: A positive control, named as one.
_CONTROL = re.compile(r'\bcontrols?\b', re.I)


class Section(enum.StrEnum):
    """A section every description carries, by its heading."""

    #: A drill transcript in a fenced block, or a first line opening
    #: `Unproven live:` and saying what the first live run must confirm, or
    #: one opening `Not provider-facing` (dispatch.md §1.1 item 3).
    LIVE = 'Live'
    #: Each test that fails without the change, named in a code span, with
    #: the mutation that reddens it; or a first line opening `No test:` and
    #: saying why none is owed — no new behavior, or a design that holds it
    #: (dispatch.md §1.1 item 3).
    FAILS_WITHOUT = 'Fails without'
    #: The sweep for claims the change made false: its patterns in code
    #: spans, the positive control each was run against, and the number of
    #: files it read (docs/style/README.md, "Comments and docs").
    SWEEP = 'Sweep'
    #: The gate's results, the parts CI does not run included (AGENTS.md,
    #: "The gate").
    GATE = 'Gate'


#: The sections, in the order a description gives them.
SECTIONS = tuple(Section)

#: What an unproven change's `Live` section opens with.
UNPROVEN = 'Unproven live:'
#: What a change that reaches no provider says in its `Live` section.
NOT_PROVIDER_FACING = 'Not provider-facing'
#: What a `Fails without` section owing no test opens with.
NO_TEST = 'No test:'


def sections(body: str) -> dict[str, str]:
    """Each `##` section of `body`, by heading, with the text under it; a repeated heading's first.

    A line inside a fenced block is never a heading: a transcript's `# …`
    comment line stays in the section that quotes it.
    """
    found: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    fenced = False

    def close() -> None:
        if name is not None and name not in found:
            found[name] = '\n'.join(lines).strip()

    for line in body.replace('\r\n', '\n').split('\n'):
        heading = None if fenced else _HEADING.fullmatch(line)
        if _FENCE.match(line):
            fenced = not fenced
        if heading is not None and len(heading['level']) <= 2:
            close()
            name, lines = (heading['name'] if len(heading['level']) == 2 else None), []
        else:
            lines.append(line)
    close()
    return found


def _first_line(text: str) -> str:
    return text.split('\n', 1)[0].strip()


def _opens_with(text: str, opening: str) -> bool:
    """Whether `text`'s first line opens with `opening` and says something after it."""
    first = _first_line(text)
    return first.startswith(opening) and bool(first[len(opening) :].strip(' .:—-'))


def _live(text: str) -> list[str]:
    if _FENCE.search(text) or _opens_with(text, UNPROVEN) or _first_line(text).startswith(NOT_PROVIDER_FACING):
        return []
    return [
        f'`## {Section.LIVE}` holds no transcript in a fenced block, and its first line opens with neither '
        f'`{UNPROVEN} <what the first live run must confirm>` nor `{NOT_PROVIDER_FACING}`'
    ]


def _fails_without(text: str) -> list[str]:
    if _TEST.search(text) or _opens_with(text, NO_TEST):
        return []
    return [
        f'`## {Section.FAILS_WITHOUT}` names no test in a code span (`test_…`), '
        f'and its first line does not open with `{NO_TEST} <why no test is owed>`'
    ]


def _sweep(text: str) -> list[str]:
    folded = ' '.join(text.split())
    lacks = [
        what
        for what, present in (
            ('its patterns in code spans', _CODE_SPAN.search(folded)),
            ('the positive control the patterns ran against', _CONTROL.search(folded)),
            ('the number of files it read', _FILE_COUNT.search(folded)),
        )
        if not present
    ]
    return [f'`## {Section.SWEEP}` does not name {", or ".join(lacks)}'] if lacks else []


def _ledger_line(body: str, issue: int, closes: bool) -> list[str]:
    closed = {int(n) for n in ledger.CLOSES.findall(body)}
    referred = {int(n) for n in ledger.REFERS.findall(body)}
    others = sorted((closed | referred) - {issue})
    named = f'; it names {", ".join(f"{LEDGER}#{n}" for n in others)} instead' if others else ''
    if closes and issue not in closed:
        return [f'the dispatch closes its issue, and the description carries no `Closes {LEDGER}#{issue}`{named}']
    if not closes and issue in closed:
        return [f'the dispatch leaves its issue open, and the description closes {LEDGER}#{issue}']
    if not closes and issue not in referred:
        return [f'the dispatch leaves its issue open, and the description carries no `Refs {LEDGER}#{issue}`{named}']
    return []


def lint(body: str, *, issue: int, closes: bool) -> list[str]:
    """Every way `body` falls short of the form, for the dispatch of ledger issue `issue`."""
    problems = _ledger_line(body, issue, closes)
    found = sections(body)
    for section in SECTIONS:
        text = found.get(section.value)
        if text is None:
            problems.append(f'the description has no `## {section}` section')
        elif not text:
            problems.append(f'`## {section}` is empty')
        elif section is Section.LIVE:
            problems += _live(text)
        elif section is Section.FAILS_WITHOUT:
            problems += _fails_without(text)
        elif section is Section.SWEEP:
            problems += _sweep(text)
    return problems
