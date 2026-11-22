"""`dispatch file`: a task filed only with what dispatch.md §4 says every task carries."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from . import ledger
from .forge import Forge

log = logging.getLogger(__name__)


class FilingRefused(ValueError):
    """The issue lacks a label family or a milestone, or names a milestone that is not open."""


def file(forge: Forge, *, title: str, body: str, labels: Sequence[str], milestone: str | None) -> str:
    """File the task; its URL. Nothing is filed when anything is lacking."""
    lacks = ledger.missing(labels, milestone)
    if lacks or milestone is None:
        raise FilingRefused(f'the issue lacks {", ".join(lacks)} (dispatch.md §4)')
    log.info('reading the open milestones')
    if not any(m.open and m.title == milestone for m in forge.milestones()):
        raise FilingRefused(f'{milestone!r} is not an open milestone of the ledger')
    log.info('filing %r', title)
    return forge.create(title=title, body=body, labels=labels, milestone=milestone)
