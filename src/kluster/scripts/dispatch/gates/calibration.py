"""Whether a gate's verdict blocks: its calibration record, and the bar the record is held to.

`dispatch gates calibrate <name>` asks the gate about every case of its
labelled set and writes `calibration.json`: the hashes of what it measured,
which are the prompt (`prompt.md` with the harness's own fixed text,
`ask.harness`), the canonical text and the model id, the hash of the set,
and what the set scored. A gate blocks only while those scores meet the
bar and all three of those hashes still match the gate as it is run;
anything else is `shadow`, posted and advisory, with the reason said
(`standing`). A pytest case holds every record's hashes against its gate
in the tree, so an edit to a prompt, a model or a canonical section turns
the gate red until it is measured again (dispatch.md §3).

The bar is this repository's, not the canon's, and moving it re-derives
every status at once.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .cases import Case
from .verdict import Judged, Verdict, disagreement

#: The least share of the set's positives a blocking gate finds.
RECALL = 0.8
#: The most false positives a blocking gate gives per negative: one in twenty.
FALSE_POSITIVES = 1 / 20
#: The fewest positives, and the fewest negatives, a set measures a blocking gate on.
MINIMUM = 8


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
    positives: int
    negatives: int
    #: The share of positives found; None for a set with none.
    recall: float | None
    #: The share of negatives found; None for a set with none.
    false_positives: float | None
    disagreement: float
    #: Whether the falsifier pair held in every sample.
    pair: bool

    @property
    def status(self) -> Status:
        """The status these measurements earn at the bar as it stands; the record holds no other."""
        return Status.SHADOW if self.shortfalls() else Status.BLOCKING

    def shortfalls(self) -> list[str]:
        """Each way the measurement falls short of the bar, in words; empty where it meets it."""
        short: list[str] = []
        if not self.pair:
            short.append('its first two cases, the one it must catch and the one it must pass, did not hold')
        if self.positives < MINIMUM or self.negatives < MINIMUM:
            short.append(
                f'the set holds {self.positives} positives and {self.negatives} negatives, '
                f'where the bar measures on {MINIMUM} of each'
            )
        if self.recall is not None and self.recall < RECALL:
            short.append(f'recall {self.recall:.2f}, under {RECALL}')
        if self.false_positives is not None and self.false_positives > FALSE_POSITIVES:
            short.append(f'false positives {self.false_positives:.2f} per negative, over {FALSE_POSITIVES}')
        return short

    def to_json(self) -> str:
        """The record as written: its measurements, and for a reader the status they earn, which `load` ignores."""
        return json.dumps({**asdict(self), 'status': self.status.value}, indent=2, sort_keys=True) + '\n'


def load(path: Path) -> Calibration:
    """The record at `path`. Raises `RecordUnreadable`."""
    try:
        raw = cast(dict[str, Any], json.loads(path.read_text()))
        hashes = cast(dict[str, Any], raw['hashes'])

        def share(key: str) -> float | None:
            value: object = raw[key]
            return None if value is None else float(cast(float, value))

        return Calibration(
            hashes=Hashes(prompt=str(hashes['prompt']), canon=str(hashes['canon']), model=str(hashes['model'])),
            cases=str(raw['cases']),
            positives=int(raw['positives']),
            negatives=int(raw['negatives']),
            recall=share('recall'),
            false_positives=share('false_positives'),
            disagreement=float(raw['disagreement']),
            pair=bool(raw['pair']),
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
    return Status.BLOCKING, (f'calibrated on {record.positives} positives and {record.negatives} negatives, at the bar')


def measure(set_: Sequence[Case], judged: Sequence[Judged], *, cases: str, hashes: Hashes) -> Calibration:
    """The record of `judged`, the gate's verdicts on `set_` in the same order."""
    if len(set_) != len(judged):
        raise ValueError(f'{len(judged)} verdicts for {len(set_)} cases')
    pairs = list(zip(set_, judged, strict=True))
    positives = [j for c, j in pairs if c.verdict is Verdict.FINDING]
    negatives = [j for c, j in pairs if c.verdict is Verdict.PASS]
    recall = sum(1 for j in positives if j.finding) / len(positives) if positives else None
    false_positives = sum(1 for j in negatives if j.finding) / len(negatives) if negatives else None
    pair = (
        len(pairs) >= 2
        and pairs[0][0].verdict is Verdict.FINDING
        and pairs[1][0].verdict is Verdict.PASS
        and all(s is not None and s.verdict is Verdict.FINDING for s in pairs[0][1].samples)
        and all(s is not None and s.verdict is Verdict.PASS for s in pairs[1][1].samples)
    )
    return Calibration(
        hashes=hashes,
        cases=cases,
        positives=len(positives),
        negatives=len(negatives),
        recall=recall,
        false_positives=false_positives,
        disagreement=disagreement(judged),
        pair=pair,
    )
