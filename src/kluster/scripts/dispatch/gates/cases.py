"""A gate's labelled set: `cases.jsonl`, one case per line.

A case is an item a review ruled on, and how it ruled: the pull request and
the head the review read, the place, the verdict, the comment the verdict
came from, and the item's text and context as the gate's extractor drew
them at that head. The text travels with the case, so `calibrate` asks the
model about it as an item, with no store and no forge behind it. The first two cases are the gate's falsifier pair: the one
it must catch, then the one it must pass (dispatch.md §3).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .extract import Item
from .verdict import Verdict


class CasesUnreadable(ValueError):
    """A line of a gate's cases that is not a case."""


@dataclass(frozen=True)
class Case:
    """One labelled item."""

    pr: int
    head: str
    path: str
    line: int
    end_line: int
    verdict: Verdict
    #: The URL of the comment whose verdict this is.
    source: str
    text: str
    context: str = ''

    @property
    def item(self) -> Item:
        return Item(self.path, self.line, self.end_line, self.text, self.context)

    @property
    def key(self) -> tuple[int, str, str, int, int]:
        """What makes two cases the same case, whatever either says of it."""
        return (self.pr, self.head, self.path, self.line, self.end_line)

    def to_json(self) -> str:
        return json.dumps({**asdict(self), 'verdict': self.verdict.value}, ensure_ascii=False, sort_keys=True)


def _case(raw: object, where: str) -> Case:
    if not isinstance(raw, dict):
        raise CasesUnreadable(f'{where} is not an object')
    fields = cast(dict[str, Any], raw)
    try:
        case = Case(
            pr=int(fields['pr']),
            head=str(fields['head']),
            path=str(fields['path']),
            line=int(fields['line']),
            end_line=int(fields['end_line']),
            verdict=Verdict(fields['verdict']),
            source=str(fields['source']),
            text=str(fields['text']),
            context=str(fields.get('context', '')),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise CasesUnreadable(f'{where}: {exc!r}') from exc
    if case.end_line < case.line:
        raise CasesUnreadable(f'{where} ends at line {case.end_line}, before it starts at {case.line}')
    return case


def load(path: Path) -> list[Case]:
    """The cases of `path`, in order. Raises `CasesUnreadable` naming the line."""
    found: list[Case] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CasesUnreadable(f'{path}:{number} is not JSON: {exc}') from exc
        found.append(_case(raw, f'{path}:{number}'))
    return found


def append(path: Path, new: Iterable[Case]) -> list[Case]:
    """Append to `path` each case of `new` it does not hold yet; the ones appended."""
    held = {case.key for case in load(path)} if path.is_file() else set[tuple[int, str, str, int, int]]()
    added: list[Case] = []
    for case in new:
        if case.key not in held:
            held.add(case.key)
            added.append(case)
    if added:
        # A file whose last line has no newline would take the first case onto that line.
        held_text = path.read_text() if path.is_file() else ''
        lead = '\n' if held_text and not held_text.endswith('\n') else ''
        with path.open('a') as sink:
            _ = sink.write(lead + ''.join(f'{case.to_json()}\n' for case in added))
    return added
