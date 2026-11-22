"""The dispatch record: the claim's evidence, a comment on the ledger issue.

A record is a comment whose first line is `HEADING`, followed by lines a
person reads and, last, a fenced `json` block the program reads. `dispatch
claim` and `dispatch queue` post one before they apply the label; `dispatch
abandon` posts an `end` record. A claim made while the issue carries no claim
label posts a record that starts a run. An issue's **live claim** is the run
of records after its last `end` or start, the start included, read only while
the issue carries a claim label: the latest of them is the live record, and
every builder or design record in the run still owns its paths.

Only comments the account `gh` runs as wrote are read: anyone else's comment
is data whatever its first line says. Within that account, who posted a
comment proves nothing (every session writes as the operator's account,
dispatch.md §1), so a record states its dispatcher session itself. An edited
record is read as it now stands. A record is read with what reading needs —
the event, the session, the issue, the role, the owned paths — and every other
front-matter field rides along unread, so a brief field added later leaves
every earlier record readable. A record whose `version` is newer than
`VERSION`, or whose front matter names another issue than the one it sits on,
does not read.

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
    #: The claim's end, by `dispatch abandon`.
    END = 'end'


class RecordUnreadable(ValueError):
    """A comment that opens as a record and does not carry one."""


@dataclass(frozen=True)
class Waiver:
    """A step past what would have refused it, on the operator's quoted word."""

    quote: str
    #: Each thing the step went past, with what it lacks.
    passed: tuple[str, ...]


@dataclass(frozen=True)
class Front:
    """A record's front matter: the fields reading needs, and all of it as written."""

    issue: int
    role: Role
    owned: tuple[OwnedPath, ...]
    workspace: str | None
    #: The whole front matter as the record carries it.
    fields: Mapping[str, object]

    @classmethod
    def of(cls, front: FrontMatter) -> Front:
        return cls(
            issue=front.issue, role=front.role, owned=front.owned, workspace=front.workspace, fields=front.as_json()
        )

    @classmethod
    def read(cls, raw: Mapping[str, Any]) -> Front:
        workspace = raw.get('workspace')
        return cls(
            issue=int(raw['issue']),
            role=Role(raw['role']),
            owned=tuple(OwnedPath.parse(str(path)) for path in cast(list[object], raw['owned'])),
            workspace=str(workspace) if workspace is not None else None,
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

    def render(self) -> str:
        data: dict[str, object] = {'version': VERSION, 'event': self.event.value, 'session': self.session}
        summary = f'`{self.event.value}` by dispatcher session `{self.session}`'
        if self.start:
            data['start'] = True
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
        return Record(
            event=Event(data['event']),
            session=str(data['session']),
            front=front,
            brief=cast(str | None, data.get('brief')),
            waiver=Waiver(quote=str(waiver['quote']), passed=tuple(str(p) for p in waiver['passed']))
            if waiver
            else None,
            start=data.get('start') is True,
            overlaps=tuple(str(o) for o in cast(list[object], data.get('overlaps', []))),
        )
    except RecordUnreadable:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise RecordUnreadable(f'#{issue}: a record comment whose block does not read: {exc}') from exc


@dataclass(frozen=True)
class Claim:
    """An issue's live claim: its records since the last `end` or start, oldest first."""

    issue: int
    records: tuple[Record, ...]

    @property
    def live(self) -> Record:
        return self.records[-1]

    @property
    def owned(self) -> tuple[OwnedPath, ...]:
        """Every path a builder or design record of the claim owns."""
        return tuple(
            path
            for record in self.records
            if record.front is not None and record.front.role is not Role.REVIEW
            for path in record.front.owned
        )


def claim(issue: int, comments: Sequence[Comment]) -> Claim | None:
    """The live claim the comments of `issue` carry; None when no record follows the last `end`.

    Raises `RecordUnreadable`, naming the issue, where one of its records does not read.
    """
    records = [record for record in (parse(issue, comment) for comment in comments) if record is not None]
    cuts = [index for index, record in enumerate(records) if record.event is Event.END or record.start]
    since = records[cuts[-1] :] if cuts else records
    if since and since[0].event is Event.END:
        since = since[1:]
    return Claim(issue=issue, records=tuple(since)) if since else None
