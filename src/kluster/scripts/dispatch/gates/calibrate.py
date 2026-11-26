"""`dispatch gates calibrate <name>`: a gate asked about every case of its set, and its record written.

The canonical section is read from the checkout the command runs in, which
is the tree the record's hash of it is held against (`calibration`). Each
case is asked as the gate asks any item, three samples to a batch, so the
record measures the verdict `dispatch gates` would give.

What the record holds is the aggregate the bar reads. Why the gate judged
each case as it did is the run's `Judgement`, which the command prints
(`Judgement.report`) and the record leaves out: every case beside its
label, the falsifier pair marked, with each sample's verdict and reason, so
a miss can be read without asking again. A run in which any sample answered
nothing measured nothing about those cases, and writes no record
(`Unanswered`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ask import Asked, judge
from .calibration import PAIR, Calibration, digest, measure, pair_held
from .cases import Case
from .gate import CASES, RECORD, Gate, GateUnreadable
from .model import Model
from .verdict import Judged, Verdict, stands, undecided

log = logging.getLogger(__name__)


def _place(case: Case) -> str:
    lines = f'{case.line}' if case.end_line == case.line else f'{case.line}-{case.end_line}'
    return f'{case.path}:{lines}'


def _stood(judged: Judged) -> str:
    """The verdict that stands on the samples, or `undecided` where unanswered samples leave it open."""
    if stands(judged.samples):
        return Verdict.FINDING.value
    return 'undecided' if undecided(judged.samples) else Verdict.PASS.value


@dataclass(frozen=True)
class Judgement:
    """A gate's verdicts on its cases, in the order of `cases.jsonl`."""

    cases: Sequence[Case]
    judged: Sequence[Judged]

    def unanswered(self) -> list[int]:
        """The place in the set of each case a sample of which answered nothing."""
        return [index for index, j in enumerate(self.judged) if any(s is None for s in j.samples)]

    def report(self) -> str:
        """Each case as judged against its label, one row to a case and one line to each of its samples.

        A case is numbered by its 0-based place in `cases.jsonl`. The first two
        are the falsifier pair, which holds only where every sample gives the
        label, so their rows say so; any other case holds where the verdict
        that stands is its label.
        """
        rows: list[str] = []
        for index, (case, judged) in enumerate(zip(self.cases, self.judged, strict=True)):
            stood = _stood(judged)
            if index < len(PAIR):
                held = pair_held(index, case, judged)
                role = f' (pair, every sample must give {PAIR[index].value})'
            else:
                held = stood == case.verdict.value
                role = ''
            rows.append(
                f'case {index}{role}: {_place(case)}: label {case.verdict.value}, stood {stood}, '
                f'{"held" if held else "NOT HELD"}'
            )
            rows.extend(
                f'  sample {n}: answered nothing' if s is None else f'  sample {n}: {s.verdict.value}: {s.why}'
                for n, s in enumerate(judged.samples, start=1)
            )
        return '\n'.join(rows)


class Unanswered(Exception):
    """A calibration some of whose samples answered nothing: it measured nothing about those cases.

    Carries the run's `judgement`, so the reasons of the samples that did
    answer are still there to be read.
    """

    def __init__(self, message: str, judgement: Judgement) -> None:
        super().__init__(message)
        self.judgement = judgement


@dataclass(frozen=True)
class Calibrated:
    """A calibration that measured every case: the record it wrote, and its verdicts on each case."""

    record: Calibration
    judgement: Judgement


def calibrate(gate: Gate, checkout: Path, model: Callable[[], Model]) -> Calibrated:
    """Measure `gate` against its cases, with its canon as `checkout` holds it, and write its record.

    Raises `Unanswered`, leaving any record already there as it was, where a
    sample answered nothing.
    """
    cases = gate.cases()
    if not cases:
        raise GateUnreadable(f'{gate.name} has no cases in {gate.directory / CASES} to measure it against')

    def text(path: str) -> str | None:
        file = checkout / path
        return file.read_text() if file.is_file() else None

    canon = gate.canon(text).text
    log.info('asking %s about %d cases, on %s', gate.name, len(cases), gate.spec.model)
    (judged,) = judge([Asked(gate.system(canon), gate.spec.model, [c.item for c in cases])], model)
    judgement = Judgement(cases, judged)
    missing = judgement.unanswered()
    if missing:
        unanswered = sum(1 for j in judged for s in j.samples if s is None)
        samples = sum(len(j.samples) for j in judged)
        raise Unanswered(
            f'{gate.name}: {unanswered} of {samples} samples answered nothing, in cases '
            f'{", ".join(map(str, missing))}. A sample that answered nothing measured nothing about its case, '
            f'so {gate.directory / RECORD} is left as it was. A sample answers nothing where its call failed, '
            'where its reply stopped short (a refusal, the token bound) or did not read, and where a reply that '
            'read skipped its item or answered it twice; the run logged a warning for each but the last',
            judgement,
        )
    record = measure(cases, judged, cases=digest((gate.directory / CASES).read_text()), hashes=gate.hashes(canon))
    _ = (gate.directory / RECORD).write_text(record.to_json())
    log.info('wrote %s: %s', gate.directory / RECORD, record.status)
    return Calibrated(record, judgement)
