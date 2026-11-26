"""`dispatch gates calibrate <name>`: a gate asked about every pull request of its history, scored, and its record written.

The history is the gate's cases (`cases`), taken a pull request and a head
at a time. Each is the change from that head's merge base with the base the
forge records for the pull request (`prcheck.PullRequest.base`), the tip of
`main` as the forge last saw it there, to the head, as `dispatch gates`
reads a pull request's change. It is read from the local store, which
holds both commits once the dispatcher has fetched. A head the forge no
longer serves drops its cases, whether or not the store still holds it, so
that what a record measured can be measured again from the forge; the
report names them (`Judgement.dropped`). A commit the forge serves and the
store does not hold refuses the run, naming it, since a fetch brings it in;
so does a history every head of which is dropped (`Unmeasured`), which
measured nothing. The gate runs on a pull request where
its trigger matches a path the change touches, and is asked there as
`dispatch gates` asks it (`view.parts`, `ask.judge`), with its canonical
section read from the checkout the command runs in, which is the tree the
record's hash of it is held against.

Each pull request is scored alone (`score`). A finding of the gate, one that
stands on its samples, recovers a review finding, a case labelled `finding`,
where both name one path and their lines lie within `calibration.MARGIN`,
one to one: each review finding is recovered once, and each gate finding
recovers one at most. A gate finding that recovers none is false where a
case labelled `pass` names its place, and is unconfirmed where no case does.
The record's score is the review findings recovered over the whole history
and the false findings per pull request the gate ran on
(`calibration.Calibration`).

Two kinds of run measured too little to write a record, and each leaves the
record already there as it was: one in which a sample answered nothing,
since that sample measured nothing about what its call showed
(`Unanswered`); and one in which a gate finding stands unconfirmed, since
the score cannot say whether it is false (`Unconfirmed`), which lists each
such finding as `#<pr> <path>:<lines> at <head>`, the form a ruling names
it by for `harvest` to record it at that pull request and head
(dispatch.md §3).

Why the gate judged each place as it did is the run's `Judgement`, which
the command prints (`Judgement.report`) and the record leaves out: every
case beside its label with what the gate made of it, every place a sample
found with each sample's word and reason, so a miss can be read without
asking again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..prcheck import Git, PullUnreadable
from . import view
from .ask import Asked, Judged, judge
from .calibration import Calibration, digest
from .cases import Case
from .change import change
from .comment import Entry
from .forge import GateForge
from .gate import CASES, RECORD, Gate, GateUnreadable
from .model import Model
from .verdict import Verdict

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Score:
    """What one pull request's gate findings made of its cases."""

    #: Each review finding the gate recovered, with the gate finding that recovered it.
    recovered: tuple[tuple[Case, Entry], ...] = ()
    #: Each gate finding a case labelled `pass` confirms is false, with that case.
    false: tuple[tuple[Entry, Case], ...] = ()
    #: Each gate finding that recovered no review finding and that no case names the place of.
    unconfirmed: tuple[Entry, ...] = ()


def _augment(
    finding: int, found: Sequence[Entry], review: Sequence[Case], held: dict[int, int], seen: set[int]
) -> bool:
    """Whether gate finding `finding` takes a review finding, moving another gate finding off one where it can."""
    entry = found[finding]
    for index, case in enumerate(review):
        if index in seen or not case.near(entry.path, entry.line, entry.end_line):
            continue
        seen.add(index)
        if index not in held or _augment(held[index], found, review, held, seen):
            held[index] = finding
            return True
    return False


def score(cases: Sequence[Case], found: Sequence[Entry]) -> Score:
    """The score of one pull request at one head: its `cases`, and `found`, the gate's findings there.

    Gate findings are matched to review findings one to one, as many as can
    be: a finding near two review findings takes the one no other finding
    can, so the count recovered does not turn on the order the findings come
    in.
    """
    review = [c for c in cases if c.verdict is Verdict.FINDING]
    clean = [c for c in cases if c.verdict is Verdict.PASS]
    held: dict[int, int] = {}
    for finding in range(len(found)):
        _ = _augment(finding, found, review, held, set())
    taken = set(held.values())
    false: list[tuple[Entry, Case]] = []
    unconfirmed: list[Entry] = []
    for index, entry in enumerate(found):
        if index in taken:
            continue
        confirming = next((c for c in clean if c.near(entry.path, entry.line, entry.end_line)), None)
        if confirming is None:
            unconfirmed.append(entry)
        else:
            false.append((entry, confirming))
    recovered = tuple((review[index], found[finding]) for index, finding in sorted(held.items()))
    return Score(recovered, tuple(false), tuple(unconfirmed))


@dataclass(frozen=True)
class Reviewed:
    """One pull request of the history at one head: its cases, and the gate's run on its change."""

    pr: int
    head: str
    cases: tuple[Case, ...]
    #: Whether the gate's trigger matches a path the change touches, so that it ran.
    ran: bool
    #: Every place a sample found, or the samples left undecided (`ask.matched`); none where it did not run.
    entries: tuple[Entry, ...] = ()
    #: The samples that answered nothing.
    unanswered: int = 0

    @property
    def score(self) -> Score:
        return score(self.cases, [e for e in self.entries if e.finding])

    def report(self) -> list[str]:
        """The pull request's rows: each case against its label, then each place a sample found, with its samples."""
        scored = self.score
        by = {c.key: e for c, e in scored.recovered}
        against = {c.key: e for e, c in scored.false}
        ran = '' if self.ran else ': its trigger matches no path the change touches, so the gate did not run'
        rows = [f'#{self.pr} at {self.head[:12]}{ran}']
        for case in self.cases:
            if case.verdict is Verdict.FINDING:
                gate = by.get(case.key)
                said = 'MISSED' if gate is None else f'recovered by {gate.place}'
            else:
                gate = against.get(case.key)
                said = 'held' if gate is None else f'FALSE FINDING at {gate.place}'
            rows.append(f'  case {case.place}: label {case.verdict.value}, {said}')
        unconfirmed = {(e.path, e.line, e.end_line) for e in scored.unconfirmed}
        for entry in self.entries:
            if entry.finding:
                stood = 'UNCONFIRMED finding' if (entry.path, entry.line, entry.end_line) in unconfirmed else 'finding'
            else:
                stood = 'undecided' if entry.undecided else 'no finding'
            rows.append(f'  place {entry.place}: {stood}')
            rows.extend(
                f'    sample {n}: answered nothing' if s is None else f'    sample {n}: {s.verdict.value}: {s.why}'
                for n, s in enumerate(entry.samples, start=1)
                if s is None or s.why
            )
        if self.unanswered:
            rows.append(f'  {self.unanswered} samples answered nothing')
        return rows


@dataclass(frozen=True)
class Judgement:
    """A gate's run over its history, a pull request and a head at a time, in the order of its cases."""

    reviewed: Sequence[Reviewed]
    #: Each pull request and head of the history the forge no longer serves, with its cases, left out.
    dropped: Sequence[tuple[int, str, tuple[Case, ...]]] = ()

    def report(self) -> str:
        rows = [row for r in self.reviewed for row in r.report()]
        rows += [
            f'#{pr} at {head[:12]}: the forge no longer serves this head, so its {len(cases)} cases are left out'
            for pr, head, cases in self.dropped
        ]
        return '\n'.join(rows)

    @property
    def unanswered(self) -> int:
        return sum(r.unanswered for r in self.reviewed)

    @property
    def unconfirmed(self) -> list[tuple[Reviewed, Entry]]:
        return [(r, e) for r in self.reviewed for e in r.score.unconfirmed]


class Unanswered(Exception):
    """A calibration some of whose samples answered nothing: it measured nothing about what their calls showed.

    Carries the run's `judgement`, so the reasons of the samples that did
    answer are still there to be read.
    """

    def __init__(self, message: str, judgement: Judgement) -> None:
        super().__init__(message)
        self.judgement = judgement


class Unmeasured(Exception):
    """A calibration whose every head the forge no longer serves: it measured nothing, and writes no record."""

    def __init__(self, message: str, judgement: Judgement) -> None:
        super().__init__(message)
        self.judgement = judgement


class Unconfirmed(Exception):
    """A calibration that made a finding no case confirms or refutes: the score cannot count it either way.

    Carries the run's `judgement`, whose `unconfirmed` lists each such finding.
    """

    def __init__(self, message: str, judgement: Judgement) -> None:
        super().__init__(message)
        self.judgement = judgement


@dataclass(frozen=True)
class Calibrated:
    """A calibration that measured every pull request of the history: the record it wrote, and its run."""

    record: Calibration
    judgement: Judgement


def calibrate(gate: Gate, checkout: Path, model: Callable[[], Model], forge: GateForge, git: Git) -> Calibrated:
    """Measure `gate` against its history, with its canon as `checkout` holds it, and write its record.

    Raises `Unanswered`, `Unconfirmed` or `Unmeasured`, leaving any record
    already there as it was, and `prcheck.PullUnreadable` where the local store `git` reads
    does not hold a commit a pull request of the history needs and the forge
    still serves.
    """
    cases = gate.cases()
    if not cases:
        raise GateUnreadable(f'{gate.name} has no cases in {gate.directory / CASES} to measure it against')

    def text(path: str) -> str | None:
        file = checkout / path
        return file.read_text() if file.is_file() else None

    canon = gate.canon(text).text
    system = gate.system(canon)
    # The history's pull requests at their heads, in the order each one's first case comes in.
    history: dict[tuple[int, str], list[Case]] = {}
    for case in cases:
        history.setdefault((case.pr, case.head), []).append(case)
    dropped: list[tuple[int, str, tuple[Case, ...]]] = []
    for pr, head in list(history):
        if not forge.serves(head):
            log.warning('#%d: the forge no longer serves %s, so its cases are left out', pr, head)
            dropped.append((pr, head, tuple(history.pop((pr, head)))))
    if not history:
        raise Unmeasured(
            f'{gate.name}: the forge serves no head of its history, so the run measured nothing and '
            f'{gate.directory / RECORD} is left as it was',
            Judgement([], dropped),
        )
    bases: dict[int, str] = {}
    missing: list[str] = []
    for pr, head in history:
        if pr not in bases:
            log.info('reading the base of #%d', pr)
            bases[pr] = forge.pull_request(pr).base
        missing += [f'#{pr}: {sha}' for sha in dict.fromkeys((bases[pr], head)) if not git.has(sha)]
    if missing:
        raise PullUnreadable(
            f'the local store holds no commit of {", ".join(missing)}; fetch them, and run the calibration again'
        )
    ran: dict[tuple[int, str], Asked] = {}
    for pr, head in history:
        changed = change(git, git.merge_base(bases[pr], head), head)
        if gate.triggered(changed.paths):
            ran[(pr, head)] = Asked(system, gate.spec.model, view.parts(changed, gate.spec.trigger))
    log.info(
        'asking %s about %d pull requests of its history, on %s; %d more it does not run on',
        gate.name,
        len(ran),
        gate.spec.model,
        len(history) - len(ran),
    )
    judged = dict(zip(ran, judge(list(ran.values()), model), strict=True))
    judgement = Judgement(
        [_reviewed(pr, head, held, judged.get((pr, head))) for (pr, head), held in history.items()], dropped
    )
    if judgement.unanswered:
        raise Unanswered(
            f'{gate.name}: {judgement.unanswered} samples answered nothing. A sample that answered nothing '
            f'measured nothing about what its call showed, so {gate.directory / RECORD} is left as it was. A sample '
            'answers nothing where its call failed, where its reply stopped short (a refusal, the token bound) or '
            'did not read, and where it named a path its call did not show; the run logged a warning for each',
            judgement,
        )
    if judgement.unconfirmed:
        places = ''.join(f'\n- #{r.pr} {e.place} at {r.head}' for r, e in judgement.unconfirmed)
        raise Unconfirmed(
            f'{gate.name}: {len(judgement.unconfirmed)} findings no case confirms or refutes, so '
            f'{gate.directory / RECORD} is left as it was. A comment headed `## Ruling` on any pull request of '
            'this repository rules on each, a line for each place as it stands below, with the tag of the gate '
            "and the ruling's verdict beside it (dispatch.md §3); `dispatch gates harvest` of that pull request "
            f'records it.{places}',
            judgement,
        )
    record = _measured(judgement, cases=digest((gate.directory / CASES).read_text()), gate=gate, canon=canon)
    _ = (gate.directory / RECORD).write_text(record.to_json())
    log.info('wrote %s: %s', gate.directory / RECORD, record.status)
    return Calibrated(record, judgement)


def _reviewed(pr: int, head: str, cases: Sequence[Case], judged: Judged | None) -> Reviewed:
    if judged is None:
        return Reviewed(pr, head, tuple(cases), ran=False)
    return Reviewed(pr, head, tuple(cases), ran=True, entries=tuple(judged.entries), unanswered=judged.unanswered)


def _measured(judgement: Judgement, *, cases: str, gate: Gate, canon: str) -> Calibration:
    """The record of `judgement`: the score over the whole history."""
    reviewed = judgement.reviewed
    return Calibration(
        hashes=gate.hashes(canon),
        cases=cases,
        pull_requests=len({r.pr for r in reviewed if r.ran}),
        findings=sum(1 for r in reviewed for c in r.cases if c.verdict is Verdict.FINDING),
        recovered=sum(len(r.score.recovered) for r in reviewed),
        false_findings=sum(len(r.score.false) for r in reviewed),
    )
