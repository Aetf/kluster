"""A gate's history: `cases.jsonl`, one case per line.

A case is a place in a recorded change and how it was ruled: the pull
request and the head the ruling read, the path and lines at that head, the
verdict, and the comment the verdict came from (dispatch.md §3). A
`finding` is one the gate should find there; a `pass` is a place held to the
rule, which a gate finding there is false against. A case holds no text: the
head is the forge's, and the change is that head against its merge base
with the base the forge records for the pull request (`calibrate`). So the
place is public, a commit of this repository, and `harvest` records cases
from the reviews and rulings on pull requests of this repository.

A place holds one case (`record`). Every session writes as the operator's
account, so a ruling the operator gave in so many words is marked
`operator`, which the dispatcher writes only when it relays the operator's
own word. At a place, the ruling that stands is the operator's over any
other, and among rulings of one kind the one written later (`Case.rank`):
when its comment was written, as the forge gives it, its id breaking a
tie. So the history a place ends with turns on the rulings and never on
the order they are read in, and recording rulings the history already
reflects writes nothing. A replacement is returned for the harvest to
report, and so is a ruling written after an operator's at its place, which
the operator's keeps out.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from .calibration import near
from .verdict import Verdict

#: A comment's id at the end of its URL: an issue or pull-request comment's, or a review comment's.
_COMMENT_ID = re.compile(r'(?:issuecomment-|discussion_r)(\d+)$')


class CasesUnreadable(ValueError):
    """A line of a gate's history that is not a case."""


@dataclass(frozen=True)
class Case:
    """One ruled place."""

    pr: int
    head: str
    path: str
    line: int
    end_line: int
    verdict: Verdict
    #: The URL of the comment whose verdict this is.
    source: str
    #: When the comment of `source` was written, as the forge gives it (`forge.PullComment.created`); empty where
    #: it is not known, which ranks below any time.
    written: str = ''
    #: Whether the ruling is the operator's own, relayed in so many words.
    operator: bool = False

    @property
    def key(self) -> tuple[int, str, str, int, int]:
        """What makes two cases the same case, whatever either says of it."""
        return (self.pr, self.head, self.path, self.line, self.end_line)

    @property
    def when(self) -> tuple[str, int]:
        """The order its ruling was written in: when its comment was, and that comment's id."""
        found = _COMMENT_ID.search(self.source)
        return (self.written, int(found[1]) if found else 0)

    @property
    def rank(self) -> tuple[bool, str, int]:
        """How its ruling stands against another at its place: the operator's first, then the one written later."""
        return (self.operator, *self.when)

    @property
    def place(self) -> str:
        return f'{self.path}:{self.line}' if self.line == self.end_line else f'{self.path}:{self.line}–{self.end_line}'

    def near(self, path: str, line: int, end_line: int) -> bool:
        """Whether lines `line`–`end_line` of `path` at this case's head name its place (`calibration.near`)."""
        return path == self.path and near(self.line, self.end_line, line, end_line)

    def to_json(self) -> str:
        fields = {**asdict(self), 'verdict': self.verdict.value}
        if not self.operator:
            del fields['operator']
        if not self.written:
            del fields['written']
        return json.dumps(fields, ensure_ascii=False, sort_keys=True)


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
            written=str(fields.get('written', '')),
            operator=bool(fields.get('operator', False)),
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


@dataclass
class Recorded:
    """What `record` did with each ruling it was handed."""

    appended: list[Case] = field(default_factory=list[Case])
    #: Each case replaced, and the ruling that replaced it.
    replaced: list[tuple[Case, Case]] = field(default_factory=list[tuple[Case, Case]])
    #: Each operator's case kept, and the ruling written after it that it kept out.
    kept: list[tuple[Case, Case]] = field(default_factory=list[tuple[Case, Case]])


def record(path: Path, new: Iterable[Case]) -> Recorded:
    """Each ruling of `new` recorded in `path` at its place: appended, replacing the case held, or left out.

    A ruling replaces the case held where it ranks above it (`Case.rank`), or
    where it comes from the same comment and says otherwise, the comment
    having been edited. One that ranks below changes nothing, and is
    returned only where it was written after an operator's case it is kept
    out by. A ruling the place holds as it stands changes nothing.
    """
    held = load(path) if path.is_file() else []
    at = {case.key: index for index, case in enumerate(held)}
    done = Recorded()
    for case in new:
        index = at.get(case.key)
        if index is None:
            at[case.key] = len(held)
            held.append(case)
            done.appended.append(case)
            continue
        standing = held[index]
        if (standing.verdict, standing.source, standing.operator) == (case.verdict, case.source, case.operator):
            continue
        if case.source == standing.source or case.rank > standing.rank:
            held[index] = case
            done.replaced.append((standing, case))
        elif standing.operator and not case.operator and case.when > standing.when:
            done.kept.append((standing, case))
    if done.appended or done.replaced:
        _ = path.write_text(''.join(f'{case.to_json()}\n' for case in held))
    return done
