"""Whether a gate's verdict blocks: its calibration record, and the bar the record is held to.

`dispatch gates calibrate <name>` asks the gate about every pull request of
its history, as `dispatch gates` asks it, and writes `calibration.json`: the
hashes of what it measured, which are the prompt (`prompt.md` with the
harness's own fixed text, `gate.Gate.hashes`), the canonical text and the
model id, the hash of the history, and what the history scored. The score
is the review findings the gate recovered over the whole history, and the
false findings it made per pull request it ran on (`calibrate`). A gate
blocks only while that score meets the bar and all three of those hashes
still match the gate as it is run; anything else is `shadow`, posted and
advisory, with the reason said (`standing`). A case added to the history
stales nothing: it moves the next measurement. A run in which any sample
answered nothing, or that made a finding no case confirms, writes no record
and leaves the one already there (`calibrate.Unanswered`,
`calibrate.Unconfirmed`). The record holds no sample's reason; the command
prints each one. A pytest case holds every record's hashes against its gate
in the tree, so an edit to a prompt, a model or a canonical section turns
the gate red until it is measured again (dispatch.md §3).

The bar is this repository's, not the canon's, and moving it re-derives
every status at once.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

#: The least share of the history's review findings a blocking gate recovers: four in five.
RECALL = 4 / 5
#: The most false findings a blocking gate makes per pull request it ran on: one in five.
FALSE_FINDINGS = 1 / 5
#: The fewest review findings, and the fewest pull requests the gate ran on, a history measures a blocking
#: gate on.
FINDINGS = 8
PULL_REQUESTS = 20
#: How many lines apart two findings at one path may lie and still name one place: a model and a reviewer, or
#: two samples, point at one defect a line or two apart (`ask.matched`).
MARGIN = 2


def near(first: int, last: int, other_first: int, other_last: int) -> bool:
    """Whether lines `first`–`last` and `other_first`–`other_last` of one path name one place: within `MARGIN`."""
    return max(first, other_first) - min(last, other_last) <= MARGIN


class Status(enum.StrEnum):
    """Whether a gate's findings block: `blocking` sends a fix cycle; `shadow` is posted and advisory."""

    BLOCKING = 'blocking'
    SHADOW = 'shadow'


class RecordUnreadable(ValueError):
    """A calibration record that is not one."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Hashes:
    """What a calibration measured, by the sha256 of each part."""

    prompt: str
    canon: str
    model: str

    @classmethod
    def of(cls, *, prompt: str, canon: str, model: str) -> Hashes:
        return cls(prompt=digest(prompt), canon=digest(canon), model=digest(model))

    def changed(self, now: Hashes) -> list[str]:
        """The parts that differ between this measurement and `now`, in words."""
        parts = (('the prompt', self.prompt, now.prompt), ('the canonical text', self.canon, now.canon))
        parts += (('the model', self.model, now.model),)
        return [name for name, then, current in parts if then != current]


@dataclass(frozen=True)
class Calibration:
    """A gate's calibration record."""

    hashes: Hashes
    #: The sha256 of the `cases.jsonl` it measured.
    cases: str
    #: The pull requests of the history the gate ran on, its trigger matching a path they touch.
    pull_requests: int
    #: The review findings of the history, the cases labelled `finding`.
    findings: int
    #: The review findings a finding of the gate recovered, one to one.
    recovered: int
    #: The gate's findings that a case labelled `pass` confirms are false.
    false_findings: int

    @property
    def recall(self) -> float | None:
        """The share of the review findings recovered; None for a history with none."""
        return self.recovered / self.findings if self.findings else None

    @property
    def false_per_pull_request(self) -> float | None:
        """The false findings per pull request the gate ran on; None where it ran on none."""
        return self.false_findings / self.pull_requests if self.pull_requests else None

    @property
    def status(self) -> Status:
        """The status these measurements earn at the bar as it stands; the record holds no other."""
        return Status.SHADOW if self.shortfalls() else Status.BLOCKING

    def shortfalls(self) -> list[str]:
        """Each way the measurement falls short of the bar, in words; empty where it meets it."""
        short: list[str] = []
        if self.findings < FINDINGS or self.pull_requests < PULL_REQUESTS:
            short.append(
                f'the history holds {self.findings} review findings over {self.pull_requests} pull requests the '
                f'gate ran on, where the bar measures on {FINDINGS} findings and {PULL_REQUESTS} pull requests'
            )
        if self.recall is not None and self.recall < RECALL:
            short.append(f'it recovered {self.recovered} of {self.findings} review findings, under {RECALL:.0%}')
        if self.false_per_pull_request is not None and self.false_per_pull_request > FALSE_FINDINGS:
            short.append(
                f'{self.false_findings} false findings over {self.pull_requests} pull requests, '
                f'over {FALSE_FINDINGS:g} per pull request'
            )
        return short

    def to_json(self) -> str:
        """The record as written: its measurements, and for a reader the score and status they earn, which `load` ignores."""
        derived = {
            'recall': self.recall,
            'false_per_pull_request': self.false_per_pull_request,
            'status': self.status.value,
        }
        return json.dumps({**asdict(self), **derived}, indent=2, sort_keys=True) + '\n'


def load(path: Path) -> Calibration:
    """The record at `path`. Raises `RecordUnreadable`."""
    try:
        raw = cast(dict[str, Any], json.loads(path.read_text()))
        hashes = cast(dict[str, Any], raw['hashes'])
        return Calibration(
            hashes=Hashes(prompt=str(hashes['prompt']), canon=str(hashes['canon']), model=str(hashes['model'])),
            cases=str(raw['cases']),
            pull_requests=int(raw['pull_requests']),
            findings=int(raw['findings']),
            recovered=int(raw['recovered']),
            false_findings=int(raw['false_findings']),
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise RecordUnreadable(f'{path} is no calibration record: {exc!r}') from exc


def standing(record: Calibration | None, now: Hashes) -> tuple[Status, str]:
    """The status a gate runs with, and why.

    Derived from the record's measurements against the bar as it stands, so a
    moved bar applies at once. Only a record that measured the gate as it is
    counts at all.
    """
    if record is None:
        return Status.SHADOW, 'no calibration record: the gate has never been measured'
    changed = record.hashes.changed(now)
    if changed:
        return Status.SHADOW, (
            f'{" and ".join(changed)} changed since the calibration record was measured, '
            'so the record no longer measures this gate; `dispatch gates calibrate` measures it again'
        )
    shortfalls = record.shortfalls()
    if shortfalls:
        return Status.SHADOW, f'calibrated below the bar: {"; ".join(shortfalls)}'
    return (
        Status.BLOCKING,
        f'calibrated on {record.findings} review findings over {record.pull_requests} pull requests, at the bar',
    )
