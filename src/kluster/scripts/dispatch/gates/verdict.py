"""A gate's verdict on one place: three samples, and the majority that stands.

Each call is asked `SAMPLES` times, and a finding stands where `MAJORITY`
of them find it. A sample the model did not answer, for a reply that was
refused, cut short, not in the schema or never made, counts toward neither
verdict. A place whose unanswered samples could still have made a finding
stand is `undecided`: a shadow gate passes it, and a blocking gate treats
it as a finding no appeal has answered yet. A place is one a sample found
(`ask.matched`), or one of `calibrate`'s labelled items (`Judged`).
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from .extract import Item

#: How many times each call is asked.
SAMPLES = 3
#: How many of those samples a finding stands on.
MAJORITY = 2


class Verdict(enum.StrEnum):
    """What one sample says of a place."""

    FINDING = 'finding'
    PASS = 'pass'


@dataclass(frozen=True)
class Answer:
    """One sample's verdict on one place, and its one-sentence reason."""

    verdict: Verdict
    why: str


#: A sample's answer for one place; None where the sample gave none.
Sample = Answer | None


def stands(samples: Sequence[Sample]) -> bool:
    """Whether a finding stands on these samples."""
    return sum(1 for s in samples if s is not None and s.verdict is Verdict.FINDING) >= MAJORITY


def undecided(samples: Sequence[Sample]) -> bool:
    """Whether the samples that answered nothing could still have made a finding stand.

    A sample that answered nothing counts toward neither verdict, which is
    right for a model that declined a place. For a call that failed it is no
    verdict at all, and a place those samples leave short of a majority either
    way is one the gate has not judged.
    """
    found = sum(1 for s in samples if s is not None and s.verdict is Verdict.FINDING)
    missing = sum(1 for s in samples if s is None)
    return found < MAJORITY <= found + missing


def split(samples: Sequence[Sample]) -> bool:
    """Whether the samples disagree, or any of them is missing: what the disagreement rate counts."""
    return len({s.verdict if s is not None else None for s in samples}) > 1


def reason(samples: Sequence[Sample]) -> str:
    """The reason the verdict that stands gives: its first sample's."""
    wanted = Verdict.FINDING if stands(samples) else Verdict.PASS
    return next((s.why for s in samples if s is not None and s.verdict is wanted), '')


@dataclass(frozen=True)
class Judged:
    """An item and its samples, in the order they were asked."""

    item: Item
    samples: tuple[Sample, ...]

    @property
    def finding(self) -> bool:
        return stands(self.samples)


def disagreement(judged: Sequence[Judged]) -> float:
    """The share of items whose samples split; 0 for no items."""
    return sum(1 for j in judged if split(j.samples)) / len(judged) if judged else 0.0
