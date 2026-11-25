"""The dispatch record: a dispatch's evidence, a comment on the ledger issue.

A record is a comment whose first line is `HEADING`, followed by lines a
person reads and, last, a fenced `json` block the program reads. `dispatch
claim` and `dispatch queue` post one before they apply the label; `dispatch
abandon` and `dispatch merge` post an `end` record.

**A dispatch is one brief's**, and its `Name` is the workspace and branch
the brief's front matter names. An issue carries any number of dispatches
at once, each its own: a claim or queue record whose name a live dispatch
already has amends that dispatch, and one with another name starts another.
An `end` record ends the dispatches it names, and one that names none, as
every `end` written before records named them does, ends every dispatch
live at it. A record posted while the issue carried no claim label starts
a run: every dispatch before it ended when its label came off, record or
not. A dispatch is **live** while no `end` and no start has ended it and
the issue carries a claim label; each builder or design record of a live
dispatch still owns its paths.

Only comments the account `gh` runs as wrote are read: anyone else's comment
is data whatever its first line says. Within that account, who posted a
comment proves nothing (every session writes as the operator's account,
dispatch.md §1), so a record states its dispatcher session itself. An edited
record is read as it now stands. A record is read with what reading needs —
the event, the session, the issue, the role, the owned paths, the workspace
and the branch, and the names an `end` ends — and every other front-matter
field rides along unread, so a brief field added later leaves every earlier
record readable. A record whose `version` is newer than `VERSION`, whose
front matter names another issue than the one it sits on, or a claim or
queue record with no front matter, does not read.

The ledger repository is private, so a record may carry what a brief carries.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .brief import FrontMatter, Role
from .forge import Comment
from .paths import OwnedPath

#: The first line of every record comment.
HEADING = '**Dispatch record**'

#: The shape of the block this module writes. A reader refuses a newer one.
VERSION = 1

#: The record's block: the `json` block that ends the comment. A string in
#: JSON escapes its line breaks, so no line of the JSON can be an opener, while
#: a `done` or a quote above the block may hold blocks of its own.
_BLOCK = re.compile(r'(?:\A|\n)```json\n(?P<json>(?:(?!\n```json\n).)*)\n```\s*\Z', re.S)


class Event(enum.StrEnum):
    """What a record marks."""

    #: A dispatch, by `dispatch claim`.
    CLAIM = 'claim'
    #: A dispatch into the cloud queue, by `dispatch queue`.
    QUEUE = 'queue'
    #: The end of the dispatches it names, by `dispatch abandon` and `dispatch merge`.
    END = 'end'


class RecordUnreadable(ValueError):
    """A comment that opens as a record and does not carry one."""


@dataclass(frozen=True)
class Waiver:
    """A step past what would have refused it, on the operator's quoted word."""

    quote: str
    #: Each thing the step went past, with what it lacks.
    passed: tuple[str, ...]


def _optional(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return str(value) if value is not None else None


@dataclass(frozen=True)
class Name:
    """What tells one dispatch on an issue from another: the workspace and the branch its brief names."""

    workspace: str | None
    branch: str | None

    def __str__(self) -> str:
        named = [f'{what} {value}' for what, value in (('workspace', self.workspace), ('branch', self.branch)) if value]
        return ' on '.join(named) or 'the dispatch naming no workspace or branch'

    def as_json(self) -> dict[str, str | None]:
        return {'workspace': self.workspace, 'branch': self.branch}

    @classmethod
    def read(cls, raw: Mapping[str, Any]) -> Name:
        return cls(workspace=_optional(raw, 'workspace'), branch=_optional(raw, 'branch'))


@dataclass(frozen=True)
class Front:
    """A record's front matter: the fields reading needs, and all of it as written."""

    issue: int
    role: Role
    owned: tuple[OwnedPath, ...]
    workspace: str | None
    branch: str | None
    #: The whole front matter as the record carries it.
    fields: Mapping[str, object]

    @property
    def name(self) -> Name:
        """The name of the dispatch the record is of."""
        return Name(workspace=self.workspace, branch=self.branch)

    @classmethod
    def of(cls, front: FrontMatter) -> Front:
        return cls(
            issue=front.issue,
            role=front.role,
            owned=front.owned,
            workspace=front.workspace,
            branch=front.branch,
            fields=front.as_json(),
        )

    @classmethod
    def read(cls, raw: Mapping[str, Any]) -> Front:
        name = Name.read(raw)
        return cls(
            issue=int(raw['issue']),
            role=Role(raw['role']),
            owned=tuple(OwnedPath.parse(str(path)) for path in cast(list[object], raw['owned'])),
            workspace=name.workspace,
            branch=name.branch,
            fields=raw,
        )


@dataclass(frozen=True)
class Record:
    """One record comment."""

    event: Event
    #: The `CLAUDE_CODE_SESSION_ID` of the dispatcher that posted it.
    session: str
    #: The brief's front matter; None on an `end`.
    front: Front | None = None
    #: The brief's sha256; None on an `end`.
    brief: str | None = None
    waiver: Waiver | None = None
    #: Whether the record starts a run: posted while the issue carried no claim label.
    start: bool = False
    #: Where the dispatch's owned paths can touch another claim's or an open
    #: pull request's, as the claim found them.
    overlaps: tuple[str, ...] = ()
    #: On an `end`, the dispatches it ends; None on one that names none, which
    #: ends every dispatch live at it.
    ends: tuple[Name, ...] | None = None

    def render(self) -> str:
        data: dict[str, object] = {'version': VERSION, 'event': self.event.value, 'session': self.session}
        summary = f'`{self.event.value}` by dispatcher session `{self.session}`'
        if self.start:
            data['start'] = True
        if self.ends is not None:
            data['ends'] = [name.as_json() for name in self.ends]
            summary += ', ending ' + ('; '.join(map(str, self.ends)) or 'no dispatch')
        if self.overlaps:
            data['overlaps'] = list(self.overlaps)
        if self.front is not None:
            data |= {'front': dict(self.front.fields), 'brief': self.brief}
            summary += f', role `{self.front.role.value}`, done when: {self.front.fields.get("done")}'
        lines = [HEADING, '', summary + '.']
        if self.overlaps:
            lines += ['', 'Overlaps, settled at merge:', *(f'- {overlap}' for overlap in self.overlaps)]
        if self.waiver is not None:
            data['waiver'] = {'quote': self.waiver.quote, 'passed': list(self.waiver.passed)}
            lines += [
                '',
                "**On the operator's word**, past what would have refused it:",
                *(f'- {passed}' for passed in self.waiver.passed),
                '',
                *(f'> {line}' for line in self.waiver.quote.splitlines()),
            ]
        return '\n'.join([*lines, '', '```json', json.dumps(data, indent=2, ensure_ascii=False), '```', ''])


def parse(issue: int, comment: Comment) -> Record | None:
    """The record `comment` on `issue` carries; None for a comment that is not one."""
    body = comment.body.replace('\r\n', '\n')  # a body saved with CRLF reads as the one written
    if not comment.mine or body.split('\n', 1)[0].strip() != HEADING:
        return None
    block = _BLOCK.search(body)
    if block is None:
        raise RecordUnreadable(f'#{issue}: a record comment that does not end in a `json` block')
    try:
        data = cast(dict[str, Any], json.loads(block['json']))
        if int(data.get('version', VERSION)) > VERSION:
            raise RecordUnreadable(f'#{issue}: a record of version {data["version"]}, newer than this reader')
        waiver = cast(dict[str, Any] | None, data.get('waiver'))
        raw = cast(dict[str, Any] | None, data.get('front'))
        front = Front.read(raw) if raw is not None else None
        if front is not None and front.issue != issue:
            raise RecordUnreadable(f'#{issue}: a record whose front matter names #{front.issue}')
        event = Event(data['event'])
        if front is None and event is not Event.END:
            raise RecordUnreadable(f'#{issue}: a `{event.value}` record with no front matter')
        ends = cast(list[dict[str, Any]] | None, data.get('ends'))
        return Record(
            event=event,
            session=str(data['session']),
            front=front,
            brief=cast(str | None, data.get('brief')),
            waiver=Waiver(quote=str(waiver['quote']), passed=tuple(str(p) for p in waiver['passed']))
            if waiver
            else None,
            start=data.get('start') is True,
            overlaps=tuple(str(o) for o in cast(list[object], data.get('overlaps', []))),
            ends=tuple(Name.read(name) for name in ends) if ends is not None else None,
        )
    except RecordUnreadable:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise RecordUnreadable(f'#{issue}: a record comment whose block does not read: {exc}') from exc


@dataclass(frozen=True)
class Dispatch:
    """One brief's dispatch on an issue: the records of its name, oldest first, each a claim or a queue."""

    issue: int
    name: Name
    records: tuple[Record, ...]

    @property
    def live(self) -> Record:
        """The latest record, which says who holds the dispatch."""
        return self.records[-1]

    @property
    def front(self) -> Front:
        """The latest record's front matter."""
        front = self.live.front
        assert front is not None, 'a claim or queue record carries its front matter (`parse`)'
        return front

    @property
    def owned(self) -> tuple[OwnedPath, ...]:
        """Every path a builder or design record of the dispatch owns."""
        return tuple(
            path
            for record in self.records
            if record.front is not None and record.front.role is not Role.REVIEW
            for path in record.front.owned
        )


@dataclass(frozen=True)
class History:
    """An issue's dispatches as its records leave them, each list in the order the dispatches started."""

    issue: int
    #: The dispatches no `end` or start has ended: live while the issue carries a claim label.
    live: tuple[Dispatch, ...]
    #: The dispatches an `end` or a start ended, in the order they ended.
    ended: tuple[Dispatch, ...]


def read(issue: int, comments: Sequence[Comment]) -> History:
    """The dispatches the comments of `issue` record.

    Raises `RecordUnreadable`, naming the issue, where one of its records does not read.
    """
    standing: dict[Name, list[Record]] = {}
    ended: list[Dispatch] = []

    def end(names: Sequence[Name]) -> None:
        ended.extend(Dispatch(issue, name, tuple(standing.pop(name))) for name in names if name in standing)

    for found in (parse(issue, comment) for comment in comments):
        if found is None:
            continue
        if found.event is Event.END:
            end(list(standing) if found.ends is None else found.ends)
            continue
        if found.start:
            end(list(standing))
        assert found.front is not None, 'a claim or queue record carries its front matter (`parse`)'
        standing.setdefault(found.front.name, []).append(found)
    live = tuple(Dispatch(issue, name, tuple(records)) for name, records in standing.items())
    return History(issue=issue, live=live, ended=tuple(ended))


def dispatches(issue: int, comments: Sequence[Comment]) -> tuple[Dispatch, ...]:
    """The dispatches of `issue` no `end` or start has ended. Raises `RecordUnreadable`."""
    return read(issue, comments).live
