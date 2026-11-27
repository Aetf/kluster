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
record's hash of it is held against. Its examples there are drawn from
every other pull request's cases (`examples`), so no case is scored by a
prompt that holds it.

Each pull request is scored alone (`score`). A finding of the gate, one that
stands on its samples, recovers a review finding, a case labelled `finding`,
where both name one path and their lines lie within `calibration.MARGIN`,
one to one: each review finding is recovered once, and each gate finding
recovers one at most. A gate finding that recovers none is false where a
case labelled `pass` names its place, and is unconfirmed where no case does.
The record's score is the review findings recovered over the whole history
and the false findings per pull request the gate ran on (`Tally`,
`calibration.Calibration`).

**A candidate is recorded only where it does no worse.** Where the gate
has a record that measured another prompt, canonical text or model than
the checkout holds (`calibration.Measured`), the gate as the record holds
it, `STANDING`, is asked beside the gate as the checkout holds it,
`CANDIDATE`, in the one run: every pull request of the history, each with
the same examples. The harness, its example rows among it, and the example
selection are code, which only the code of the record's time can ask, so
both gates are asked with them as they run now. Where the record measured
those otherwise (`gate.hashes_of`), whether or not the prompt, text or
model moved too, the candidate is also held against the score the record
holds (`Tally.of`): by counts where the history is the one the record
measured, its cases' hash and its counts of review findings and pull
requests the same, which is exact; and as rates where the history has
changed since, the share recovered and the false findings per pull request
(`Tally.no_worse_in_rate_than`), which is weaker, since the two scores
come from different histories and samples of their own. A candidate that
does worse by either writes nothing and raises `Regressed`, naming both
scores. A gate with no record, or whose record measured exactly what the
checkout and the code now ask, is asked once, and its score is held to the
bar alone.

A run whose samples measured too little writes no record either, and
leaves the one already there as it was: one in which a sample of either
gate answered nothing, since that sample measured nothing about what its
call showed (`Unanswered`); and one in which a finding of either gate
stands unconfirmed, since the score cannot say whether it is false
(`Unconfirmed`), which lists each such finding as
`#<pr> <path>:<lines> at <head>`, the form a ruling names it by for
`harvest` to record it at that pull request and head (dispatch.md §3).

Why the gate judged each place as it did is the run's `Judgement`, one for
each gate asked, which the command prints (`report`) and the record leaves
out: every
case beside its label with what the gate made of it, every place a sample
found with each sample's word and reason, so a miss can be read without
asking again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..prcheck import Git, PullUnreadable
from . import view
from .ask import Asked, Judged, judge
from .calibration import Calibration, digest
from .cases import Case
from .change import change
from .comment import Entry
from .examples import Library
from .forge import GateForge
from .gate import CASES, RECORD, Gate, GateUnreadable, hashes_of
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
class Tally:
    """A gate's score over a run of its history: the review findings it recovered, and its false findings."""

    #: The pull requests of the history the gate ran on, its trigger matching a path they touch.
    pull_requests: int
    #: The review findings of the history, the cases labelled `finding`.
    findings: int
    recovered: int
    false_findings: int

    @classmethod
    def of(cls, record: Calibration) -> Tally:
        """The score a record holds."""
        return cls(record.pull_requests, record.findings, record.recovered, record.false_findings)

    @property
    def recall(self) -> Fraction:
        """The share of the review findings recovered; all of none."""
        return Fraction(self.recovered, self.findings) if self.findings else Fraction(1)

    @property
    def false_per_pull_request(self) -> Fraction:
        return Fraction(self.false_findings, self.pull_requests) if self.pull_requests else Fraction(0)

    def no_worse_than(self, other: Tally) -> bool:
        """Whether this score recovers no fewer review findings than `other` and makes no more false findings per pull request."""
        return self.recovered >= other.recovered and self.false_per_pull_request <= other.false_per_pull_request

    def no_worse_in_rate_than(self, other: Tally) -> bool:
        """Whether this score recovers no smaller a share of its review findings than `other` and makes no more false findings per pull request."""
        return self.recall >= other.recall and self.false_per_pull_request <= other.false_per_pull_request

    def __str__(self) -> str:
        return (
            f'{self.recovered} of {self.findings} review findings recovered, {self.false_findings} false findings '
            f'over {self.pull_requests} pull requests'
        )


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

    @property
    def tally(self) -> Tally:
        reviewed = self.reviewed
        return Tally(
            pull_requests=len({r.pr for r in reviewed if r.ran}),
            findings=sum(1 for r in reviewed for c in r.cases if c.verdict is Verdict.FINDING),
            recovered=sum(len(r.score.recovered) for r in reviewed),
            false_findings=sum(len(r.score.false) for r in reviewed),
        )


#: What the parts of a record's hashes are where `prompt.md` is the same and the code that asks it moved.
_CODE = {'the prompt': 'the fixed text every call carries'}

#: What the report calls the gate as the tree holds it, and the gate as its record holds it, where both ran.
CANDIDATE = 'the gate as the checkout holds it'
STANDING = 'the gate as its calibration record holds it'


class Refusal(Exception):
    """A calibration that writes no record, leaving any record already there as it was.

    Carries the run's `judgement`, so the reasons of the samples that did
    answer are still there to be read, and the judgement of the gate as its
    record holds it, `standing`, where the run measured it beside the
    candidate.
    """

    def __init__(self, message: str, judgement: Judgement, standing: Judgement | None = None) -> None:
        super().__init__(message)
        self.judgement = judgement
        self.standing = standing

    def report(self) -> str:
        return report(self.judgement, self.standing)


class Unanswered(Refusal):
    """A calibration some of whose samples answered nothing: it measured nothing about what their calls showed."""


class Unmeasured(Refusal):
    """A calibration whose every head the forge no longer serves: it measured nothing, and writes no record."""


class Unconfirmed(Refusal):
    """A calibration that made a finding no case confirms or refutes: the score cannot count it either way.

    Its `judgement` and `standing` list each such finding (`Judgement.unconfirmed`).
    """


class Regressed(Refusal):
    """A candidate that did worse over the history than the gate as its record holds it, in one run of both."""


def report(judgement: Judgement, standing: Judgement | None) -> str:
    """The run's report: the candidate's, then, where the run measured it too, the standing gate's."""
    if standing is None:
        return judgement.report()
    return '\n'.join([f'{CANDIDATE}:', judgement.report(), f'{STANDING}:', standing.report()])


@dataclass(frozen=True)
class Calibrated:
    """A calibration that measured every pull request of the history: the record it wrote, and its run."""

    record: Calibration
    judgement: Judgement
    #: The run of the gate as its record held it, where the run measured it beside the candidate.
    standing: Judgement | None = None

    def report(self) -> str:
        return report(self.judgement, self.standing)


def calibrate(gate: Gate, checkout: Path, model: Callable[[], Model], forge: GateForge, git: Git) -> Calibrated:
    """Measure `gate` against its history, with its canon as `checkout` holds it, and write its record.

    Where the gate has a record that measured another prompt, canonical
    text or model, that gate is asked too, beside it, in the same run;
    where it measured another harness or example selection, the record's
    own score stands in for it. The record is written only where the gate
    as the checkout holds it does no worse than either.

    Raises a `Refusal`, one of `Unanswered`, `Unconfirmed`, `Unmeasured`
    and `Regressed`, leaving any record already there as it was, and
    `prcheck.PullUnreadable` where the local store `git` reads does not hold
    a commit a pull request of the history needs and the forge still serves.
    """
    cases = gate.cases()
    if not cases:
        raise GateUnreadable(f'{gate.name} has no cases in {gate.directory / CASES} to measure it against')

    def text(path: str) -> str | None:
        file = checkout / path
        return file.read_text() if file.is_file() else None

    canon = gate.canon(text).text
    candidate = gate.measured(canon)
    held = gate.record()
    beside = held.measured if held is not None and held.measured != candidate else None
    # The parts of what the record measured that only the code of its time can ask: the harness, its example rows
    # among it, and the example selection.
    moved = [] if held is None else held.hashes.changed(hashes_of(held.measured))
    moved = [_CODE.get(part, part) for part in moved]
    asking = [candidate] if beside is None else [candidate, beside]
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
    library = Library(git, forge)
    ran: dict[tuple[int, str], list[Asked]] = {}
    for pr, head in history:
        changed = change(git, git.merge_base(bases[pr], head), head)
        if not gate.triggered(changed.paths):
            continue
        parts = view.parts(changed, gate.spec.trigger)
        # Every other pull request's cases, so that no case is scored by a prompt that holds it.
        others = [c for (other, _), held_cases in history.items() if other != pr for c in held_cases]
        log.info('drawing the examples of #%d', pr)
        shown = library.prompt(gate.name, others, [p.path for p in parts])
        ran[(pr, head)] = [Asked(gate.system(m.canon, m.prompt), m.model, parts, examples=shown) for m in asking]
    log.info(
        'asking %s about %d pull requests of its history, on %s; %d more it does not run on',
        gate.name,
        len(ran),
        ' and, beside it as its record holds it, '.join(m.model for m in asking),
        len(history) - len(ran),
    )
    answers = iter(judge([a for at in ran.values() for a in at], model))
    judged = {at: [next(answers) for _ in asked] for at, asked in ran.items()}
    judgements = [
        Judgement(
            [
                _reviewed(pr, head, held_cases, judged[(pr, head)][n] if (pr, head) in judged else None)
                for (pr, head), held_cases in history.items()
            ],
            dropped,
        )
        for n in range(len(asking))
    ]
    judgement = judgements[0]
    standing = judgements[1] if beside is not None else None
    unanswered = sum(j.unanswered for j in judgements)
    if unanswered:
        raise Unanswered(
            f'{gate.name}: {unanswered} samples answered nothing. A sample that answered nothing '
            f'measured nothing about what its call showed, so {gate.directory / RECORD} is left as it was. A sample '
            'answers nothing where its call failed, where its reply stopped short (a refusal, the token bound) or '
            'did not read, and where it named a path its call did not show; the run logged a warning for each',
            judgement,
            standing,
        )
    unconfirmed = {f'#{r.pr} {e.place} at {r.head}': None for j in judgements for r, e in j.unconfirmed}
    if unconfirmed:
        places = ''.join(f'\n- {place}' for place in unconfirmed)
        raise Unconfirmed(
            f'{gate.name}: {len(unconfirmed)} findings no case confirms or refutes, so '
            f'{gate.directory / RECORD} is left as it was. A comment headed `## Ruling` on any pull request of '
            'this repository rules on each, a line for each place as it stands below, with the tag of the gate '
            "and the ruling's verdict beside it (dispatch.md §3); `dispatch gates harvest` of that pull request "
            f'records it.{places}',
            judgement,
            standing,
        )
    if standing is not None and not judgement.tally.no_worse_than(standing.tally):
        raise Regressed(
            f'{gate.name}: {CANDIDATE} did worse over the history than {STANDING}, so '
            f'{gate.directory / RECORD} is left as it was. A candidate is recorded only where it recovers no '
            f'fewer review findings and makes no more false findings per pull request. {CANDIDATE}: '
            f'{judgement.tally}; {STANDING}: {standing.tally}',
            judgement,
            standing,
        )
    history_hash = digest((gate.directory / CASES).read_text())
    if held is not None and moved:
        recorded = Tally.of(held)
        tally = judgement.tally
        # The history the record measured, where its cases and the counts they come to are the same.
        same = held.cases == history_hash and (tally.findings, tally.pull_requests) == (
            recorded.findings,
            recorded.pull_requests,
        )
        if not (tally.no_worse_than(recorded) if same else tally.no_worse_in_rate_than(recorded)):
            how = 'by counts, over the same history' if same else 'as rates, the history having changed since'
            raise Regressed(
                f'{gate.name}: {CANDIDATE} did worse than the score its record holds, measured under '
                f'{" and ".join(moved)} as they were then, which only the code of that time can ask again; '
                f'compared {how}. So {gate.directory / RECORD} is left as it was. {CANDIDATE}: {tally}; '
                f'the record: {recorded}',
                judgement,
                standing,
            )
    record = _measured(judgement, cases=history_hash, gate=gate, canon=canon)
    _ = (gate.directory / RECORD).write_text(record.to_json())
    if standing is not None:
        log.info('%s: %s, beside %s: %s', CANDIDATE, judgement.tally, STANDING, standing.tally)
    log.info('wrote %s: %s', gate.directory / RECORD, record.status)
    return Calibrated(record, judgement, standing)


def _reviewed(pr: int, head: str, cases: Sequence[Case], judged: Judged | None) -> Reviewed:
    if judged is None:
        return Reviewed(pr, head, tuple(cases), ran=False)
    return Reviewed(pr, head, tuple(cases), ran=True, entries=tuple(judged.entries), unanswered=judged.unanswered)


def _measured(judgement: Judgement, *, cases: str, gate: Gate, canon: str) -> Calibration:
    """The record of `judgement`: the gate it measured, and the score over the whole history."""
    tally = judgement.tally
    return Calibration(
        hashes=gate.hashes(canon),
        measured=gate.measured(canon),
        cases=cases,
        pull_requests=tally.pull_requests,
        findings=tally.findings,
        recovered=tally.recovered,
        false_findings=tally.false_findings,
    )
