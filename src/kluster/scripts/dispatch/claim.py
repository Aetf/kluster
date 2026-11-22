"""`dispatch claim`, `queue` and `abandon`: a dispatch's start and end on the ledger.

`check` reads the forge and returns every precondition the dispatch fails,
each a `Refusal` naming its `Precondition`, so a refused claim reports all of
what stands in its way at once. `claim` and `queue` change the forge only when
`check` returns none. What each precondition is, and the section of
dispatch.md it comes from, is `Precondition`'s.

Each step posts its record before it changes a label. A record on an issue
with no claim label is never read, so a step that fails between the two
leaves nothing a retry trips on: the retry finds no claim label and starts a
new run.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from . import ledger, milestones, record
from .brief import Brief, Role
from .forge import Forge, Issue, Pull
from .paths import OwnedPath
from .record import Claim, Event, Front, Record, RecordUnreadable, Waiver

log = logging.getLogger(__name__)

#: The heading of the comment a queued issue's brief is (dispatch.md §1.4).
BRIEF_HEADING = '## Brief'


class Precondition(enum.StrEnum):
    """A precondition of a dispatch, by what fails it."""

    #: The issue is closed (dispatch.md §4: a dispatch is of an open task).
    ISSUE_CLOSED = 'issue-closed'
    #: The issue lacks an `area/*` or `kind/*` label or a milestone (§4).
    UNLABELED = 'unlabeled'
    #: The front matter names another issue than the one being claimed (§1.1).
    BRIEF_ISSUE = 'brief-issue'
    #: Another dispatcher holds the claim, or a claim label stands with no
    #: record to say whose it is (§2 rule 1).
    CLAIMED = 'claimed'
    #: A review of an issue this dispatcher does not hold the claim of (§3),
    #: or an adoption of an issue that carries no `in-flight` to adopt.
    NOT_HELD = 'not-held'
    #: The issue's own records do not read (§2.1).
    RECORD_UNREADABLE = 'record-unreadable'
    #: An RFC slice's issue title does not lead with the slice (rfc.md §3.4).
    RFC_TITLE = 'rfc-title'
    #: An earlier phase is not formally closed, or a milestone's title can be
    #: placed neither among the phases nor beside them (§3.1, §4; `milestones`).
    MILESTONE = 'milestone'
    #: The phase's design issue is not approved, or not named (§3.1).
    DESIGN = 'design'
    #: A serialized file is already held by another claim or an open pull
    #: request (§2 rule 3).
    SERIALIZED = 'serialized'


@dataclass(frozen=True)
class Refusal:
    precondition: Precondition
    detail: str

    def __str__(self) -> str:
        return f'{self.precondition.value}: {self.detail}'


@dataclass(frozen=True)
class Verdict:
    """What `check` found: the refusals, and the phases an operator's word let the dispatch past."""

    issue: Issue
    refusals: tuple[Refusal, ...]
    passed: tuple[milestones.NotClosed, ...]
    #: The issue's own live claim, where it has one.
    own: Claim | None
    #: Where an owned path can touch another claim's paths or an open pull
    #: request's files: recorded and printed, never refused (§2 rule 2).
    overlaps: tuple[str, ...] = ()


class WordRefused(ValueError):
    """An operator's word that is blank: it would pass a gate and leave no quote behind."""


def _word(word: str | None) -> None:
    """Refuse a given operator's word that holds nothing but whitespace."""
    if word is not None and not word.strip():
        raise WordRefused("the operator's word is blank; quote the words the operator gave")


@dataclass(frozen=True)
class Dispatch:
    """Who dispatches what: the brief, the dispatcher session, and the operator's word if it was given."""

    brief: Brief
    session: str
    #: The operator's words, quoted, that license going past a phase not formally closed.
    operator_word: str | None = None
    #: Whether the dispatch adopts an `in-flight` that stands with no record, on the operator's word (§2.1).
    adopt: bool = False

    def __post_init__(self) -> None:
        _word(self.operator_word)


def _held(issue: Issue, own: Claim | None, dispatch: Dispatch) -> Refusal | None:
    """The refusal when another dispatcher holds `issue`'s claim, or when an adoption finds nothing to adopt."""
    if dispatch.adopt:
        if ledger.CLAIMED not in issue.labels:
            return Refusal(Precondition.NOT_HELD, f'#{issue.number} carries no `{ledger.CLAIMED}` to adopt')
        if own is not None:
            return Refusal(Precondition.CLAIMED, f'#{issue.number} already carries a record of its claim')
        if dispatch.operator_word is None:
            return Refusal(Precondition.CLAIMED, f"adopting #{issue.number}'s claim takes the operator's word")
        return None
    if not ledger.claimed(issue):
        return None
    if own is None:
        return Refusal(Precondition.CLAIMED, f'#{issue.number} carries a claim label and no dispatch record')
    if own.live.session != dispatch.session:
        return Refusal(Precondition.CLAIMED, f'#{issue.number} is claimed by dispatcher session {own.live.session}')
    return None


def _gates(
    forge: Forge, issue: Issue, role: Role, operator_word: str | None
) -> tuple[list[Refusal], list[milestones.NotClosed]]:
    """The milestone gate and the design gate, for a dispatch of `issue` in `role`."""
    if issue.milestone is None or milestones.beside(issue.milestone):
        return [], []
    log.info('reading the state of the phases before %s', issue.milestone)
    every = forge.milestones()
    number = milestones.phase(issue.milestone)
    unplaced = milestones.unreadable(every)
    if number is None or unplaced:
        return [
            Refusal(Precondition.MILESTONE, f'{title!r} is neither a phase `M<n> …` nor `Parallel`')
            for title in unplaced
        ], []
    open_ = [found for m in milestones.earlier(every, number) if (found := milestones.formally_closed(forge, m))]
    refusals: list[Refusal] = []
    passed: list[milestones.NotClosed] = []
    if open_ and operator_word is None:
        refusals += [Refusal(Precondition.MILESTONE, f'an earlier phase is not formally closed: {n}') for n in open_]
    else:
        passed = open_
    if role is Role.BUILDER:
        refusals += _design(forge, issue.milestone, every)
    return refusals, passed


def _design(forge: Forge, title: str, every: Sequence[milestones.Milestone]) -> list[Refusal]:
    number = next((milestones.design_issue(m) for m in every if m.title == title), None)
    if number is None:
        return [
            Refusal(Precondition.DESIGN, f'{title} names no single design issue on a line `Design: Aetf/kluster-ops#N`')
        ]
    design = forge.issue(number)
    if design.open and milestones.APPROVED not in design.labels:
        return [
            Refusal(Precondition.DESIGN, f"{title}'s design issue #{number} is open and not `{milestones.APPROVED}`")
        ]
    return []


def _overlaps(
    owned: Sequence[OwnedPath],
    issue: int,
    claims: Collection[Claim],
    unknown: Collection[int],
    pulls: Collection[Pull],
    serialized: frozenset[str],
) -> tuple[list[Refusal], list[str]]:
    """The serialized files' refusals, and the overlaps with other claims and pull requests, which only note.

    `unknown` are the claimed issues whose paths cannot be read: a record that
    does not read, or a claim label with none. A claim of a serialized file
    refuses while any stands, since any of them may hold it. A bot's pull
    request holds no paths: it is a pin or a lock file moving, whose conflict
    the next rebase answers.
    """
    others = [c for c in claims if c.issue != issue]
    strangers = [p for p in pulls if not p.by_bot and issue not in ledger.references(p.body)]
    unread = sorted(n for n in unknown if n != issue)
    notes: list[str] = [f"#{n}'s paths are unknown" for n in unread] if owned else []
    for path in owned:
        notes += [
            f'{path} can touch what #{other.issue} owns as {theirs}'
            for other in others
            for theirs in other.owned
            if path.overlaps(theirs)
        ]
        for pull in strangers:
            touched = sorted(f for f in pull.files if path.owns(f))
            if touched:
                notes.append(f'{path} owns {", ".join(touched)}, open in #{pull.number}')
    refusals: list[Refusal] = []
    held = sorted(name for name in serialized if any(path.owns(name) for path in owned))
    refusals += [
        Refusal(Precondition.SERIALIZED, f"{name} is serialized and #{n}'s paths cannot be read")
        for name in held
        for n in unread
    ]
    for name in held:
        refusals += [
            Refusal(Precondition.SERIALIZED, f'{name} is serialized and #{other.issue} owns it')
            for other in others
            if any(theirs.owns(name) for theirs in other.owned)
        ]
        refusals += [
            Refusal(Precondition.SERIALIZED, f'{name} is serialized and open pull request #{pull.number} touches it')
            for pull in strangers
            if name in pull.files
        ]
    return refusals, notes


def check(forge: Forge, dispatch: Dispatch, number: int, serialized: frozenset[str]) -> Verdict:
    """Every precondition the dispatch of issue `number` under `dispatch` fails."""
    front = dispatch.brief.front
    log.info('reading #%d and its records', number)
    issue = forge.issue(number)
    refusals: list[Refusal] = []
    try:
        own = record.claim(number, forge.comments(number)) if ledger.claimed(issue) else None
    except RecordUnreadable as exc:
        return Verdict(issue, (Refusal(Precondition.RECORD_UNREADABLE, str(exc)),), (), None)
    if front.issue != number:
        refusals.append(Refusal(Precondition.BRIEF_ISSUE, f'the brief names #{front.issue}, not #{number}'))
    if not issue.open:
        refusals.append(Refusal(Precondition.ISSUE_CLOSED, f'#{number} is closed'))
    refusals += [
        Refusal(Precondition.UNLABELED, f'#{number} lacks {lack}')
        for lack in ledger.missing(issue.labels, issue.milestone)
    ]
    held = _held(issue, own, dispatch)
    if held is not None:
        refusals.append(held)
    if front.role is Role.REVIEW:
        if own is None:
            refusals.append(
                Refusal(Precondition.NOT_HELD, f'#{number} carries no claim of this session to review under')
            )
        return Verdict(issue=issue, refusals=tuple(refusals), passed=(), own=own)
    if front.rfc is not None and not issue.title.startswith(f'{front.rfc}:'):
        refusals.append(Refusal(Precondition.RFC_TITLE, f'the title of #{number} does not lead with `{front.rfc}:`'))
    gated, passed = _gates(forge, issue, front.role, dispatch.operator_word)
    refusals += gated
    log.info('reading every live claim and open pull request')
    read = ledger.claims(forge)
    claims = [c for c in read.live.values() if c is not None]
    unknown = [n for n, c in read.live.items() if c is None] + list(read.unreadable)
    serial, overlaps = _overlaps(front.owned, number, claims, unknown, forge.pulls(), serialized)
    refusals += serial
    return Verdict(issue=issue, refusals=tuple(refusals), passed=tuple(passed), own=own, overlaps=tuple(overlaps))


def _record(dispatch: Dispatch, event: Event, verdict: Verdict) -> str:
    passed = [*(['a claim label with no record'] if dispatch.adopt else []), *map(str, verdict.passed)]
    waiver = (
        Waiver(quote=dispatch.operator_word, passed=tuple(passed))
        if dispatch.operator_word is not None and passed
        else None
    )
    return Record(
        event=event,
        session=dispatch.session,
        front=Front.of(dispatch.brief.front),
        brief=dispatch.brief.sha256,
        waiver=waiver,
        start=dispatch.adopt or not ledger.claimed(verdict.issue),
        overlaps=verdict.overlaps,
    ).render()


class Refused(Exception):
    """The dispatch fails a precondition; nothing on the forge was changed."""

    def __init__(self, refusals: Sequence[Refusal]) -> None:
        super().__init__('\n'.join(map(str, refusals)))
        self.refusals = tuple(refusals)


def claim(forge: Forge, dispatch: Dispatch, number: int, serialized: frozenset[str]) -> Verdict:
    """Claim issue `number` for the dispatch: the record, then `in-flight`. Raises `Refused`.

    With `dispatch.adopt`, the issue's standing `in-flight` is the claim, and
    only the record is posted, quoting the operator's word.
    """
    verdict = check(forge, dispatch, number, serialized)
    if verdict.refusals:
        raise Refused(verdict.refusals)
    log.info('posting the dispatch record on #%d', number)
    forge.comment(number, _record(dispatch, Event.CLAIM, verdict))
    labels = verdict.issue.labels
    if dispatch.brief.front.role is not Role.REVIEW and ledger.CLAIMED not in labels:
        log.info('labeling #%d `%s`', number, ledger.CLAIMED)
        forge.relabel(number, add=[ledger.CLAIMED], remove=[ledger.QUEUED] if ledger.QUEUED in labels else [])
    return verdict


def queue(forge: Forge, dispatch: Dispatch, number: int, serialized: frozenset[str], brief: str) -> Verdict:
    """Queue issue `number` for a cloud session: the brief, the record, then `cloud/ready`. Raises `Refused`.

    `brief` is the brief's text, which the session reads from the comment. A
    claim this dispatcher already holds gives up its `in-flight` to the queue
    (dispatch.md §2 rule 1).
    """
    verdict = check(forge, dispatch, number, serialized)
    refusals = list(verdict.refusals)
    if dispatch.brief.front.role is Role.REVIEW:
        refusals.append(Refusal(Precondition.NOT_HELD, 'a review is not queued: the cloud queue holds builds'))
    if refusals:
        raise Refused(refusals)
    log.info('posting the brief and the dispatch record on #%d', number)
    forge.comment(number, f'{BRIEF_HEADING}\n\n{brief}')
    forge.comment(number, _record(dispatch, Event.QUEUE, verdict))
    log.info('labeling #%d `%s`', number, ledger.QUEUED)
    forge.relabel(
        number, add=[ledger.QUEUED], remove=[ledger.CLAIMED] if ledger.CLAIMED in verdict.issue.labels else []
    )
    return verdict


def abandon(forge: Forge, number: int, session: str, operator_word: str | None) -> None:
    """End this session's claim of issue `number`: the `end` record, then the claim labels off. Raises `Refused`.

    A claim of another session, or one with no record or none that reads,
    ends only on the operator's word, which the `end` record quotes.
    """
    _word(operator_word)
    issue = forge.issue(number)
    if not ledger.claimed(issue):
        raise Refused([Refusal(Precondition.NOT_HELD, f'#{number} carries no claim label')])
    try:
        own = record.claim(number, forge.comments(number))
        holder = f'dispatcher session {own.live.session}' if own is not None else 'no record'
        mine = own is not None and own.live.session == session
    except RecordUnreadable:
        holder, mine = 'a record that does not read', False
    waiver: Waiver | None = None
    if not mine:
        if operator_word is None:
            raise Refused(
                [Refusal(Precondition.CLAIMED, f"#{number}'s claim is {holder}'s; ending it takes the operator's word")]
            )
        waiver = Waiver(quote=operator_word, passed=(f'the claim of {holder}',))
    log.info('posting the end record on #%d', number)
    forge.comment(number, Record(event=Event.END, session=session, waiver=waiver).render())
    forge.relabel(number, remove=[label for label in ledger.CLAIMS if label in issue.labels])
