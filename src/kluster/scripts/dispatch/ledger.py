"""The ledger's labels and links, as dispatch.md §2 and §4 define them, and its live claims."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from . import record
from .forge import LEDGER, Forge, Issue

#: The claim of a dispatch (dispatch.md §2 rule 1).
CLAIMED = 'in-flight'
#: The claim of an issue queued for a cloud session (dispatch.md §1.4).
QUEUED = 'cloud/ready'
#: Either claim.
CLAIMS = (CLAIMED, QUEUED)
#: An issue the monitoring files, which carries neither `area/*` nor `kind/*`.
ALERT = 'alert'
#: The label families every task carries one of (dispatch.md §4).
AREA = 'area/'
KIND = 'kind/'
#: The family whose labels are the states of a decision (dispatch.md §4.1).
DECISION = 'decision/'

#: A pull request's line naming a ledger issue it closes or refers to (dispatch.md §4).
_REFERENCE = re.compile(rf'\b(?:Closes|Refs)\s+{re.escape(LEDGER)}#(?P<number>\d+)\b', re.I)


def missing(labels: Collection[str], milestone: str | None) -> list[str]:
    """What a task lacks of an `area/*` label, a `kind/*` label and a milestone."""
    lacks = [
        f'a label from `{family}*`' for family in (AREA, KIND) if not any(label.startswith(family) for label in labels)
    ]
    return lacks if milestone is not None else [*lacks, 'a milestone']


def references(body: str) -> frozenset[int]:
    """The ledger issues a pull request's description closes or refers to."""
    return frozenset(int(number) for number in _REFERENCE.findall(body))


def claimed(issue: Issue) -> bool:
    return any(label in issue.labels for label in CLAIMS)


@dataclass(frozen=True)
class Claims:
    """The open issues that carry a claim label, by what their records say."""

    #: Each issue with its live claim; None where it has none.
    live: dict[int, record.Claim | None]
    #: Each issue whose records do not read, with why.
    unreadable: dict[int, record.RecordUnreadable]


def claims(forge: Forge) -> Claims:
    """Every open issue that carries a claim label, read."""
    numbers = sorted({issue.number for label in CLAIMS for issue in forge.issues(label=label)})
    live: dict[int, record.Claim | None] = {}
    unreadable: dict[int, record.RecordUnreadable] = {}
    for number in numbers:
        try:
            live[number] = record.claim(number, forge.comments(number))
        except record.RecordUnreadable as exc:
            unreadable[number] = exc
    return Claims(live=live, unreadable=unreadable)
