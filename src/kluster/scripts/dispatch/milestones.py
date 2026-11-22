"""The milestones the claim reads: their order, their closing, their design issue (dispatch.md §3.1, §4).

A **phase** is a milestone whose title opens with `M` and its number, then a
space (`M1 — bring-up`, `M0.5 — quality campaign`); the number orders the
phases. The milestone whose title opens with `Parallel` is no phase: it tracks
beside the mainline, so neither gate below holds its work. A title that is
neither (`M5a`, `m3`, `Phase 5`) cannot be ordered, and refuses every claim of
phase work rather than passing as exempt.

A phase is **formally closed** when the milestone is closed and so is its
review checkpoint, with the operator's acceptance recorded on it as
`decision/lgtm`. The checkpoint is the issue on the milestone that carries
the `checkpoint` label. A phase's work goes ahead only while every earlier
phase is formally closed.

A phase opens with its design, which its description names on a line of its
own, `Design: Aetf/kluster-ops#N`. A builder's work of the phase goes ahead
only once that issue is closed or carries `decision/lgtm`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .forge import LEDGER, Forge, Milestone

#: A phase's title: `M` and its number, at the start, then a space or the end.
_PHASE = re.compile(r'M(?P<number>\d+(?:\.\d+)?)(?:\s|$)')
#: The title of the milestone that tracks beside the phases (dispatch.md §4).
_BESIDE = re.compile(r'Parallel\b')
#: The label of a phase's review checkpoint issue.
CHECKPOINT = 'checkpoint'
#: The line of a phase's description that names its design issue.
_DESIGN = re.compile(rf'^Design: {re.escape(LEDGER)}#(?P<number>\d+)\s*$', re.M)

#: The label an operator's approval is recorded as (dispatch.md §4.1).
APPROVED = 'decision/lgtm'


def phase(title: str) -> Decimal | None:
    """The phase number of the milestone titled `title`; None for a milestone that is no phase."""
    match = _PHASE.match(title)
    return Decimal(match['number']) if match else None


def beside(title: str) -> bool:
    """Whether the milestone titled `title` tracks beside the phases, held by neither gate."""
    return _BESIDE.match(title) is not None


def unreadable(milestones: Sequence[Milestone]) -> list[str]:
    """The titles that are neither a phase's nor the one beside them."""
    return [m.title for m in milestones if phase(m.title) is None and not beside(m.title)]


def earlier(milestones: Sequence[Milestone], number: Decimal) -> list[Milestone]:
    """The phases before phase `number`, in order."""
    before = [(n, m) for m in milestones if (n := phase(m.title)) is not None and n < number]
    return [m for _, m in sorted(before, key=lambda pair: pair[0])]


@dataclass(frozen=True)
class NotClosed:
    """An earlier phase that is not formally closed, and what it lacks."""

    milestone: str
    lacks: str

    def __str__(self) -> str:
        return f'{self.milestone}: {self.lacks}'


def formally_closed(forge: Forge, milestone: Milestone) -> NotClosed | None:
    """What keeps `milestone` from being formally closed; None when it is."""
    if milestone.open:
        return NotClosed(milestone.title, 'the milestone is open')
    checkpoints = forge.issues(label=CHECKPOINT, milestone=milestone.title, closed=True)
    if not checkpoints:
        return NotClosed(milestone.title, 'it has no review checkpoint issue')
    for checkpoint in checkpoints:
        if checkpoint.open:
            return NotClosed(milestone.title, f'its review checkpoint #{checkpoint.number} is open')
        if APPROVED not in checkpoint.labels:
            return NotClosed(
                milestone.title, f"its review checkpoint #{checkpoint.number} records no operator's acceptance"
            )
    return None


def design_issue(milestone: Milestone) -> int | None:
    """The design issue `milestone`'s description names; None where it names none."""
    found = _DESIGN.findall(milestone.description)
    return int(found[0]) if len(found) == 1 else None
