"""`dispatch claim`, `queue` and `abandon`: a dispatch's start and end on the ledger.

`check` reads the forge and returns every precondition the dispatch fails,
each a `Refusal` naming its `Precondition`, so a refused claim reports all of
what stands in its way at once. `claim` and `queue` change the forge only when
`check` returns none. What each precondition is, and the section of
dispatch.md it comes from, is `Precondition`'s.

Every brief, whatever its role, is then asked about whole by the model
gates whose trigger names a brief (`ask_about_brief`, dispatch.md §2.1),
with their canon read from the dispatcher's checkout. Their verdicts land in
the dispatcher's log, which is where the claim is run and where the brief
can still be rewritten before the agent is spawned, and not on a pull
request, which is public while a brief may name what a public page must
not. They refuse nothing, whatever status a gate's calibration earns: that
status is measured on pull requests, and says nothing of how the gate reads
a brief.

The forge is read by `check` and written after the gates answer, so the
window between the two runs as long as the model takes. Another dispatcher
that claims the same issue inside it passes the same check, and both post a
record.

Each step posts its record before it changes a label. A record on an issue
with no claim label is never read, so a step that fails between the two
leaves nothing a retry trips on: the retry finds no claim label and starts a
new run. An issue's claim is its label, and the dispatches its records hold
live under it (`record`) are each read on their own: every one of them is
the session's that holds the issue.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from functools import cache

from kluster.lib import workstation

from . import ledger, milestones, record
from .brief import Brief, Role
from .forge import Forge, Issue, Pull
from .gates.change import Change, Changed, asks_about_briefs, brief_path
from .gates.comment import Evidence, Ran
from .gates.gate import Gate, discover
from .gates.model import Model
from .gates.run import Prepared, asked, messages, prepare
from .gates.verdict import reason
from .paths import OwnedPath
from .record import Event, Front, Record, RecordUnreadable, Waiver

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
    #: Another dispatcher holds a live dispatch of the issue, or a claim label
    #: stands with no live dispatch to say whose it is (§2 rule 1).
    CLAIMED = 'claimed'
    #: A review of an issue this dispatcher holds no live dispatch of (§3), an
    #: adoption of an issue that carries no `in-flight` to adopt, or an
    #: abandonment of a dispatch no live one is named by.
    NOT_HELD = 'not-held'
    #: A brief naming neither a workspace nor a branch, which would leave its
    #: dispatch without a name to amend it or end it by alone (§2.1).
    UNNAMED = 'unnamed'
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
    """What a dispatch found: the refusals and the phases an operator's word let it past, which `check` reads, and the model gates' run over the brief, which `claim` and `queue` add once `check` passes."""

    issue: Issue
    refusals: tuple[Refusal, ...]
    passed: tuple[milestones.NotClosed, ...]
    #: The issue's own live dispatches.
    own: tuple[record.Dispatch, ...]
    #: Where an owned path can touch another claim's paths or an open pull
    #: request's files: recorded and printed, never refused (§2 rule 2).
    overlaps: tuple[str, ...] = ()
    #: The model gates' run over the brief (`ask_about_brief`); None from `check`, or with no brief file.
    brief_evidence: Evidence | None = None


class WordRefused(ValueError):
    """An operator's word that is blank: it would pass a gate and leave no quote behind."""


def _word(word: str | None) -> None:
    """Refuse a given operator's word that holds nothing but whitespace."""
    if word is not None and not word.strip():
        raise WordRefused("the operator's word is blank; quote the words the operator gave")


@dataclass(frozen=True)
class BriefFile:
    """A brief as its file holds it: the file's name, and its text, which the model gates read."""

    name: str
    text: str


@dataclass(frozen=True)
class Dispatch:
    """Who dispatches what: the brief, the dispatcher session, and the operator's word if it was given."""

    brief: Brief
    session: str
    #: The operator's words, quoted, that license going past a phase not formally closed.
    operator_word: str | None = None
    #: Whether the dispatch adopts an `in-flight` that stands with no record, on the operator's word (§2.1).
    adopt: bool = False
    #: The brief's file, which the model gates read; a dispatch made without
    #: it, as the ledger's own cases make one, is asked about by no gate.
    file: BriefFile | None = None

    def __post_init__(self) -> None:
        _word(self.operator_word)


def _held(issue: Issue, own: Sequence[record.Dispatch], dispatch: Dispatch) -> Refusal | None:
    """The refusal when another dispatcher holds a live dispatch of `issue`, or when an adoption finds nothing."""
    if dispatch.adopt:
        if ledger.CLAIMED not in issue.labels:
            return Refusal(Precondition.NOT_HELD, f'#{issue.number} carries no `{ledger.CLAIMED}` to adopt')
        if own:
            return Refusal(Precondition.CLAIMED, f'#{issue.number} already carries a record of its claim')
        if dispatch.operator_word is None:
            return Refusal(Precondition.CLAIMED, f"adopting #{issue.number}'s claim takes the operator's word")
        return None
    if not ledger.claimed(issue):
        return None
    if not own:
        return Refusal(Precondition.CLAIMED, f'#{issue.number} carries a claim label and no live dispatch record')
    others = [d for d in own if d.live.session != dispatch.session]
    if others:
        held = '; '.join(f'{d.name} by dispatcher session {d.live.session}' for d in others)
        return Refusal(Precondition.CLAIMED, f'#{issue.number} carries live dispatches of another session: {held}')
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
    claims: Collection[record.Dispatch],
    unknown: Collection[int],
    pulls: Collection[Pull],
    serialized: frozenset[str],
) -> tuple[list[Refusal], list[str]]:
    """The serialized files' refusals, and the overlaps with other claims and pull requests, which only note.

    `claims` are the live dispatches of every claimed issue, and `unknown`
    the claimed issues whose paths cannot be read: a record that does not
    read, or a claim label with no live dispatch. A claim of a serialized file
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
        own = record.dispatches(number, forge.comments(number)) if ledger.claimed(issue) else ()
    except RecordUnreadable as exc:
        return Verdict(issue, (Refusal(Precondition.RECORD_UNREADABLE, str(exc)),), (), ())
    if front.issue != number:
        refusals.append(Refusal(Precondition.BRIEF_ISSUE, f'the brief names #{front.issue}, not #{number}'))
    if front.workspace is None and front.branch is None:
        refusals.append(
            Refusal(
                Precondition.UNNAMED, 'the brief names neither a `workspace` nor a `branch`, which name its dispatch'
            )
        )
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
        if not own:
            refusals.append(
                Refusal(Precondition.NOT_HELD, f'#{number} carries no live dispatch of this session to review under')
            )
        return Verdict(issue=issue, refusals=tuple(refusals), passed=(), own=own)
    if front.rfc is not None and not issue.title.startswith(f'{front.rfc}:'):
        refusals.append(Refusal(Precondition.RFC_TITLE, f'the title of #{number} does not lead with `{front.rfc}:`'))
    gated, passed = _gates(forge, issue, front.role, dispatch.operator_word)
    refusals += gated
    log.info('reading every live dispatch and open pull request')
    read = ledger.claims(forge)
    claims = [live for found in read.live.values() for live in found]
    unknown = [n for n, found in read.live.items() if not found] + list(read.unreadable)
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


@dataclass(frozen=True)
class Asking:
    """What a brief is asked through: the gates, the tree their canonical sections are read from, and the model."""

    gates: Sequence[Gate]
    #: The text of a path in the tree the canon is read from; None where it holds no such file.
    canon: Callable[[str], str | None]
    #: Made only where a gate is shown the brief.
    model: Callable[[], Model]


def _checkout(path: str) -> str | None:
    file = workstation.repo_root() / path
    return file.read_text() if file.is_file() else None


@cache
def workstation_asking() -> Asking:
    """This checkout's gates, their canon as the checkout holds it, and the model the register's key asks."""
    return Asking(tuple(discover()), _checkout, messages)


def ask_about_brief(file: BriefFile, issue: Issue, sha256: str, asking: Asking) -> Evidence:
    """Every gate whose trigger names the brief, asked about it whole: the run, as evidence at the brief's hash.

    The brief is read as a change that writes the whole of one file, at the
    path `gates.change.brief_path` gives it from the areas of the issue. A
    gate asks about it where a pattern of its trigger names briefs and
    matches that path (`gates.change.asks_about_briefs`), and is shown it
    whole. Each gate's canonical section is read from the tree
    `asking.canon` reads, and the gate is made ready and asked as a pull
    request's gates are (`gates.run.prepare`, `gates.run.asked`).
    """
    areas = [label.removeprefix(ledger.AREA) for label in issue.labels if label.startswith(ledger.AREA)]
    path = brief_path(areas, file.name)
    written = Changed(path, 'A', ((1, file.text.count('\n') + 1),))
    change = Change(head=sha256, files=(written,), text=lambda asked: file.text if asked == path else None)
    prepared: list[Prepared] = []
    unread: list[Ran] = []
    for gate in (g for g in asking.gates if asks_about_briefs(g.spec.trigger, path)):
        ready = prepare(gate, asking.canon, change)
        if isinstance(ready, Prepared):
            prepared.append(ready)
        else:
            unread.append(ready)
    log.info('asking %s about the brief', ', '.join(p.gate.name for p in prepared) or 'no gate')
    return Evidence(sha256, tuple(asked(prepared, asking.model) + unread))


def _logged(evidence: Evidence) -> None:
    """Log every gate's run over the brief, each finding and each place left undecided, whatever its status."""
    for ran in evidence.gates:
        log.info('%s on the brief, %s: %s; %d places recorded', ran.gate, ran.status, ran.said, len(ran.entries))
        for entry in ran.open:
            why = reason(entry.samples) if entry.finding else 'no verdict: too few of its samples answered'
            log.warning('%s, %s: %s: %s', ran.gate, ran.status, entry.place, why)


def _ask(dispatch: Dispatch, verdict: Verdict, asking: Asking | None) -> Verdict:
    """`verdict` with the brief gates' run, which is logged and refuses nothing."""
    if dispatch.file is None:
        return verdict
    evidence = ask_about_brief(dispatch.file, verdict.issue, dispatch.brief.sha256, asking or workstation_asking())
    _logged(evidence)
    return replace(verdict, brief_evidence=evidence)


class Refused(Exception):
    """The dispatch fails a precondition; nothing on the forge was changed."""

    def __init__(self, refusals: Sequence[Refusal]) -> None:
        super().__init__('\n'.join(map(str, refusals)))
        self.refusals = tuple(refusals)


def claim(
    forge: Forge, dispatch: Dispatch, number: int, serialized: frozenset[str], asking: Asking | None = None
) -> Verdict:
    """Claim issue `number` for the dispatch: the record, then `in-flight`. Raises `Refused`.

    With `dispatch.adopt`, the issue's standing `in-flight` is the claim, and
    only the record is posted, quoting the operator's word. Once every
    precondition passes, the brief is asked about by the gates of `asking`,
    this checkout's where none is given (`ask_about_brief`).
    """
    verdict = check(forge, dispatch, number, serialized)
    if verdict.refusals:
        raise Refused(verdict.refusals)
    verdict = _ask(dispatch, verdict, asking)
    log.info('posting the dispatch record on #%d', number)
    forge.comment(number, _record(dispatch, Event.CLAIM, verdict))
    labels = verdict.issue.labels
    if dispatch.brief.front.role is not Role.REVIEW and ledger.CLAIMED not in labels:
        log.info('labeling #%d `%s`', number, ledger.CLAIMED)
        forge.relabel(number, add=[ledger.CLAIMED], remove=[ledger.QUEUED] if ledger.QUEUED in labels else [])
    return verdict


def queue(
    forge: Forge, dispatch: Dispatch, number: int, serialized: frozenset[str], brief: str, asking: Asking | None = None
) -> Verdict:
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
    verdict = _ask(dispatch, verdict, asking)
    log.info('posting the brief and the dispatch record on #%d', number)
    forge.comment(number, f'{BRIEF_HEADING}\n\n{brief}')
    forge.comment(number, _record(dispatch, Event.QUEUE, verdict))
    log.info('labeling #%d `%s`', number, ledger.QUEUED)
    forge.relabel(
        number, add=[ledger.QUEUED], remove=[ledger.CLAIMED] if ledger.CLAIMED in verdict.issue.labels else []
    )
    return verdict


@dataclass(frozen=True)
class Abandoned:
    """What `abandon` ended, and what it left live."""

    #: The names the `end` record gives.
    ended: tuple[record.Name, ...]
    #: The dispatches of the issue still live, under the claim labels where any is.
    standing: tuple[record.Dispatch, ...]


def abandon(
    forge: Forge, number: int, session: str, operator_word: str | None, dispatch: str | None = None
) -> Abandoned:
    """End dispatches of issue `number` with an `end` record naming them. Raises `Refused`.

    With `dispatch`, a workspace or a branch, it ends the live dispatches
    whose name holds it, and refuses where none does; without, every
    dispatch live on the issue. The claim labels come off once no dispatch
    on the issue stays live, and no workspace is touched. Ending a dispatch
    of another session, or a claim with no record or none that reads, takes
    the operator's word, which the `end` record quotes.
    """
    _word(operator_word)
    issue = forge.issue(number)
    if not ledger.claimed(issue):
        raise Refused([Refusal(Precondition.NOT_HELD, f'#{number} carries no claim label')])
    try:
        own = record.dispatches(number, forge.comments(number))
        readable = True
    except RecordUnreadable as exc:
        if dispatch is not None:
            raise Refused([Refusal(Precondition.RECORD_UNREADABLE, str(exc))]) from exc
        own, readable = (), False
    ending = own if dispatch is None else tuple(d for d in own if dispatch in (d.name.workspace, d.name.branch))
    if dispatch is not None and not ending:
        raise Refused([Refusal(Precondition.NOT_HELD, f'no live dispatch on #{number} is named {dispatch}')])
    holders = sorted({d.live.session for d in ending})
    holder = ' and '.join(f'dispatcher session {s}' for s in holders) if ending else 'no record'
    if not readable:
        holder = 'a record that does not read'
    mine = readable and holders == [session]
    waiver: Waiver | None = None
    what = 'the claim' if dispatch is None else '; '.join(str(d.name) for d in ending)
    if not mine:
        if operator_word is None:
            raise Refused(
                [
                    Refusal(
                        Precondition.CLAIMED, f"{what} on #{number} is {holder}'s; ending it takes the operator's word"
                    )
                ]
            )
        waiver = Waiver(quote=operator_word, passed=(f'{what} of {holder}',))
    log.info('posting the end record on #%d', number)
    ends = tuple(d.name for d in ending)
    forge.comment(number, Record(event=Event.END, session=session, waiver=waiver, ends=ends).render())
    standing = tuple(d for d in own if d.name not in ends)
    if not standing:
        forge.relabel(number, remove=[label for label in ledger.CLAIMS if label in issue.labels])
    return Abandoned(ended=ends, standing=standing)
