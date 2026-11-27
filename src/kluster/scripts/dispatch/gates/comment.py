"""The one pull-request comment a run of the model gates leaves: its findings, and the evidence behind them.

The comment opens `## Gate findings (head <sha>)`, and a run at the same
head edits it in place, while a run at a new head posts another (dispatch.md
§3). Under the findings it carries the run's whole record as a fenced `json`
block: every gate that ran, with its status and why, its model and the link
to its canonical section; and every place any of its samples found, and
every stretch its samples left undecided (`ask.matched`), by its place,
with one letter for each sample's word there and any appeal of it. A place
no sample found is a pass, and holds no entry. That record is what
`harvest` pairs a review of the same head against, so a gate's finding the
reviewer did not raise and a reviewer's finding the gate passed both
surface as calibration cases.

**The comment fits in one GitHub comment, whatever the run.** GitHub
refuses a body over `LIMIT` characters, and a refused comment would leave
the run's calls paid for and nothing recorded. So the record is compact,
its entries grouped by path, and the samples' reasons are what gives way:
`render` keeps the reasons of every entry whose samples are not a unanimous
pass, then of standing findings alone, then none, and last drops the list
of findings and undecided places above the record, which holds every one
of them. The verdicts always stay, since they are what a review pairs
against. Where a run's entries would not fit even as verdicts alone
(`fits`), its gates' answers are dropped and each is recorded as having
given no verdict.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from .calibration import Status, near
from .verdict import Answer, Sample, Verdict, reason, stands, undecided

#: The record's version, which a reader newer than a record reads and an older one refuses. From version 2
#: an entry is a place a sample found or the samples left undecided, and a place on the lines the change writes
#: of a file the gate was shown that no entry holds is one it passed. A version 1 record is a run of the item ask, which holds an entry for
#: every item asked, passes included, so a place no entry holds there was never asked about
#: (`Evidence.version`, which `harvest` reads).
VERSION = 2

#: The longest body GitHub takes for a comment, in characters.
LIMIT = 65_536

#: A sample's verdict as the record spells it, and a sample that answered nothing.
LETTERS = {Verdict.FINDING: 'f', Verdict.PASS: 'p'}
NONE = '-'

#: The comment's first line.
HEADING = '## Gate findings (head {head})'
_HEADING = re.compile(r'\A## Gate findings \(head (?P<head>[0-9a-f]{40})\)[ \t]*$', re.M)
#: The record: the last fenced `json` block of the comment.
_RECORD = re.compile(r'^```json\n(?P<json>.*?)\n```[ \t]*$', re.M | re.S)


#: An entry by its gate and its place: the gate's name, the path, the first and the last line.
Place = tuple[str, str, int, int]


class EvidenceUnreadable(ValueError):
    """A findings comment whose record does not read."""


class EvidenceTooLong(ValueError):
    """A run whose record would not fit in one comment even as verdicts alone."""


@dataclass(frozen=True)
class Entry:
    """One place of a gate's run, with each sample's word on it and any appeal of it."""

    path: str
    line: int
    end_line: int
    samples: tuple[Sample, ...]
    #: The builder's reason, where its report appeals the finding.
    appeal: str | None = None

    @property
    def finding(self) -> bool:
        return stands(self.samples)

    @property
    def undecided(self) -> bool:
        """Whether the samples that answered nothing leave the place unjudged (`verdict.undecided`)."""
        return undecided(self.samples)

    @property
    def place(self) -> str:
        return f'{self.path}:{self.line}' if self.line == self.end_line else f'{self.path}:{self.line}–{self.end_line}'

    def overlaps(self, line: int, end_line: int) -> bool:
        return self.line <= end_line and line <= self.end_line


@dataclass(frozen=True)
class Ran:
    """One gate's part in a run."""

    gate: str
    model: str
    status: Status
    #: Why the gate runs with that status.
    why: str
    #: The link to its canonical section at the head; empty where the section did not read.
    rule: str
    entries: tuple[Entry, ...]
    #: Whether the gate's answers are recorded; False where its canon did not read, no model could be asked, or
    #: what it answered would not fit in one comment.
    asked: bool = True
    #: Whether the gate asked and its answers were dropped, for they would not fit in one comment.
    dropped: bool = False

    @property
    def findings(self) -> list[Entry]:
        return [e for e in self.entries if e.finding]

    @property
    def open(self) -> list[Entry]:
        """Each entry a blocking status holds against the change: a finding, or a place left undecided."""
        return [e for e in self.entries if e.finding or e.undecided]

    @property
    def silent(self) -> bool:
        """Whether no sample of the gate answered: every entry is a place no sample's answer reached."""
        return bool(self.entries) and all(s is None for e in self.entries for s in e.samples)

    @property
    def unheard(self) -> bool:
        """Whether the gate gave no verdict: it asked nothing, its answers were dropped, or nothing was answered."""
        return not self.asked or self.silent

    @property
    def said(self) -> str:
        """Why the gate runs as it does, and, where it gave no verdict, which way it gave none."""
        if self.dropped:
            return f'gave no verdict, its answers dropped: {self.why}'
        if not self.asked:
            return f'asked nothing: {self.why}'
        if self.silent:
            return f'{self.why}. No sample answered any of them'
        return self.why


@dataclass(frozen=True)
class Evidence:
    """A run of the model gates at one head."""

    head: str
    gates: tuple[Ran, ...]
    #: The version of the record the run is read from, or is written as (`VERSION`).
    version: int = VERSION

    @property
    def unanswered(self) -> list[tuple[Ran, Entry]]:
        """Each entry of a blocking gate, a finding or one left undecided, that no appeal answers."""
        return [(ran, e) for ran in self.gates if ran.status is Status.BLOCKING for e in ran.open if e.appeal is None]

    def appealed(self, held: Evidence | None) -> Evidence:
        """This run with the appeals `held`, a run at the same head, records.

        A run's places move a line or two between runs, so an appeal of a
        finding goes to the nearest finding of the same gate and path that is
        near it (`calibration.near`), ties to the earlier line. An appeal of a
        place left undecided goes to an undecided place of the same gate over
        the same lines, which the same head cuts the same way. Each appeal goes
        to one entry at most, so a place the run split in two is answered once
        and the other half is held again, and a finding is never answered by
        an appeal of a call that failed.
        """
        if held is None or held.head != self.head:
            return self
        appeals = sorted(
            ((ran.gate, e) for ran in held.gates for e in ran.entries if e.appeal is not None),
            key=lambda held: (held[0], held[1].path, held[1].line, held[1].end_line),
        )
        given: dict[tuple[str, int], str] = {}
        for gate, appealed in appeals:
            ran = next((r for r in self.gates if r.gate == gate), None)
            if ran is None:
                continue

            def fits(e: Entry, appealed: Entry = appealed) -> bool:
                if e.path != appealed.path:
                    return False
                if appealed.finding:
                    return e.finding and near(appealed.line, appealed.end_line, e.line, e.end_line)
                return not e.finding and e.undecided and (e.line, e.end_line) == (appealed.line, appealed.end_line)

            open_ = [
                (n, e) for n, e in enumerate(ran.entries) if (gate, n) not in given and e.appeal is None and fits(e)
            ]
            nearest = min(open_, key=lambda ne: (abs(ne[1].line - appealed.line), ne[1].line), default=None)
            if nearest is not None and appealed.appeal is not None:
                given[(gate, nearest[0])] = appealed.appeal
        return replace(
            self,
            gates=tuple(
                replace(
                    ran,
                    entries=tuple(
                        replace(e, appeal=given.get((ran.gate, n), e.appeal)) for n, e in enumerate(ran.entries)
                    ),
                )
                for ran in self.gates
            ),
        )

    def with_appeals(self, reasons: Mapping[Place, str]) -> Evidence:
        """This run with each appeal of `reasons` on the entry at its gate and place."""
        return replace(
            self,
            gates=tuple(
                replace(
                    ran,
                    entries=tuple(
                        replace(e, appeal=reasons.get((ran.gate, e.path, e.line, e.end_line), e.appeal))
                        for e in ran.entries
                    ),
                )
                for ran in self.gates
            ),
        )


def one_line(text: str) -> str:
    """`text` as a code span on one line: no newline breaks the list it sits in, and no `@name` in it notifies."""
    folded = ' '.join(text.split())
    run = max((len(m) for m in re.findall(r'`+', folded)), default=0)
    fence = '`' * (run + 1)
    return f'{fence} {folded} {fence}' if run or folded.startswith('`') or folded.endswith('`') else f'`{folded}`'


def _letters(samples: Sequence[Sample]) -> str:
    return ''.join(LETTERS[s.verdict] if s is not None else NONE for s in samples)


#: Which entries' reasons a rendering keeps, from the most to the least.
_KEEP_SPLIT, _KEEP_FINDINGS, _KEEP_NONE = 'split', 'findings', 'none'


def _kept(e: Entry, keep: str) -> bool:
    if keep == _KEEP_SPLIT:
        return any(s is None or s.verdict is not Verdict.PASS for s in e.samples)
    return keep == _KEEP_FINDINGS and e.finding


def _record(evidence: Evidence, keep: str) -> str:
    def entry(e: Entry) -> list[object]:
        row: list[object] = [e.line, e.end_line, _letters(e.samples)]
        reasons = [s.why if s is not None else '' for s in e.samples] if _kept(e, keep) else None
        if reasons is not None or e.appeal is not None:
            row.append(reasons)
        if e.appeal is not None:
            row.append(e.appeal)
        return row

    def ran(r: Ran) -> dict[str, object]:
        paths: dict[str, list[list[object]]] = {}
        for e in r.entries:
            paths.setdefault(e.path, []).append(entry(e))
        return {
            'gate': r.gate,
            'model': r.model,
            'status': r.status.value,
            'why': r.why,
            'rule': r.rule,
            'asked': r.asked,
            'dropped': r.dropped,
            'paths': paths,
        }

    record = {'version': evidence.version, 'head': evidence.head, 'gates': [ran(r) for r in evidence.gates]}
    return json.dumps(record, ensure_ascii=False, separators=(',', ':'))


def _gate_line(ran: Ran) -> str:
    return f'- **{ran.gate}** on `{ran.model}`, {ran.status}: {ran.said}. {len(ran.entries)} places recorded.'


def _body(evidence: Evidence, keep: str, listed: bool) -> str:
    listing = [(ran, e) for ran in evidence.gates for e in ran.open]
    findings = sum(1 for _, e in listing if e.finding)
    blocking = sum(1 for ran, _ in listing if ran.status is Status.BLOCKING)
    lines = [
        HEADING.format(head=evidence.head),
        '',
        f'{findings} findings and {len(listing) - findings} places left undecided, {blocking} of them under a '
        'blocking gate. A shadow gate is advisory: it has not been measured at the bar a blocking gate meets '
        '(docs/framework/dispatch.md §3).',
        '',
    ]
    if listed:
        for ran, e in listing:
            rule = f' ([rule]({ran.rule}))' if ran.rule else ''
            said = one_line(reason(e.samples)) if e.finding else 'no verdict: too few of its samples answered'
            lines.append(f'- `{e.place}` **{ran.gate}**, {ran.status}: {said}{rule}')
            lines.append(f'  Samples: {_letters(e.samples)}.')
            if e.appeal is not None:
                lines.append(f'  Appealed: {one_line(e.appeal)}')
    elif listing:
        lines.append('The findings are too many to list here; the record below holds every one of them.')
    if listing:
        lines.append('')
    lines += ['Gates that ran:', '', *(_gate_line(ran) for ran in evidence.gates)]
    lines += ['', '<details><summary>The run, as data</summary>', '', '```json', _record(evidence, keep), '```', '']
    lines.append('</details>')
    return '\n'.join(lines) + '\n'


#: The renderings `render` tries, from the fullest to the most compact.
_RENDERINGS = ((_KEEP_SPLIT, True), (_KEEP_FINDINGS, True), (_KEEP_NONE, True), (_KEEP_NONE, False))


def render(evidence: Evidence) -> str:
    """The comment's body: the fullest rendering within `LIMIT`. Raises `EvidenceTooLong`."""
    for keep, listed in _RENDERINGS:
        body = _body(evidence, keep, listed)
        if len(body) <= LIMIT:
            return body
    raise EvidenceTooLong(
        f'{sum(len(r.entries) for r in evidence.gates)} entries do not fit in one comment of {LIMIT} characters '
        'even as verdicts alone'
    )


def fits(evidence: Evidence) -> bool:
    """Whether `evidence` fits in one comment as verdicts alone, with no list of findings above it."""
    return len(_body(evidence, *_RENDERINGS[-1])) <= LIMIT


def head_of(body: str) -> str | None:
    """The head a findings comment is of; None for any other comment."""
    found = _HEADING.match(body.lstrip())
    return found['head'] if found else None


def _samples(letters: str, reasons: list[object] | None) -> tuple[Sample, ...]:
    verdicts = {letter: verdict for verdict, letter in LETTERS.items()}
    whys = [str(r) for r in reasons] if reasons is not None else [''] * len(letters)
    if len(whys) != len(letters):
        raise ValueError(f'{len(letters)} verdicts and {len(whys)} reasons')
    return tuple(
        None if letter == NONE else Answer(verdicts[letter], why) for letter, why in zip(letters, whys, strict=True)
    )


def _entry(path: str, raw: object) -> Entry:
    row = cast(list[object], raw)
    reasons = cast(list[object] | None, row[3]) if len(row) > 3 else None
    appeal = row[4] if len(row) > 4 else None
    return Entry(
        path=path,
        line=int(cast(int, row[0])),
        end_line=int(cast(int, row[1])),
        samples=_samples(str(row[2]), reasons),
        appeal=None if appeal is None else str(appeal),
    )


def parse(body: str) -> Evidence | None:
    """The run a findings comment records; None for any other comment. Raises `EvidenceUnreadable`."""
    head = head_of(body)
    if head is None:
        return None
    blocks = list(_RECORD.finditer(body))
    if not blocks:
        raise EvidenceUnreadable(f'the findings comment of {head} holds no record')
    try:
        raw = cast(dict[str, Any], json.loads(blocks[-1]['json']))
        version, recorded = int(raw['version']), str(raw['head'])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise EvidenceUnreadable(f'the record of {head} does not read: {exc!r}') from exc
    if version > VERSION:
        raise EvidenceUnreadable(f'the record of {head} is version {version}, newer than this reader')
    if recorded != head:
        raise EvidenceUnreadable(f'the record under the heading of {head} is of {recorded}')
    try:
        return Evidence(
            head=head,
            version=version,
            gates=tuple(
                Ran(
                    gate=str(ran['gate']),
                    model=str(ran['model']),
                    status=Status(ran['status']),
                    why=str(ran['why']),
                    rule=str(ran['rule']),
                    entries=tuple(
                        _entry(path, row)
                        for path, rows in cast(dict[str, list[object]], ran['paths']).items()
                        for row in rows
                    ),
                    asked=bool(ran['asked']),
                    dropped=bool(ran.get('dropped', False)),
                )
                for ran in cast(list[dict[str, Any]], raw['gates'])
            ),
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise EvidenceUnreadable(f'the record of {head} does not read: {exc!r}') from exc
