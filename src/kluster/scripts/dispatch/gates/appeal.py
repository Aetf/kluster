"""`dispatch gates appeal <pr> <report>`: a builder's appeals of gate findings, recorded beside them.

A builder answers a blocking finding by fixing it, which a run at the new
head judges, or by appealing it in its report, one line each:

    Gate appeal: <gate> <path>:<line>: <reason>

The appeals are written into the findings comment of the pull request's
head, each on the finding whose place holds that line, so the finding reads
as answered and the reviewer who rules on it finds the reason beside it
(dispatch.md §3). An item a blocking gate left undecided is appealed the
same way. An appeal that names neither a finding standing at the head nor
an undecided item is refused, every such appeal at once, and nothing is
written.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from . import comment
from .comment import Evidence, Place
from .forge import GateForge
from .run import findings_comment

log = logging.getLogger(__name__)

#: One appeal line of a report.
APPEAL = re.compile(
    r'^[ \t]*Gate appeal:[ \t]*(?P<gate>[a-z0-9-]+)[ \t]+(?P<path>[^\s:]+):(?P<line>\d+):[ \t]*(?P<reason>\S.*?)[ \t]*$',
    re.M,
)


class AppealRefused(ValueError):
    """Appeals that name neither a finding standing at the head nor an item left undecided."""

    def __init__(self, refusals: Sequence[str]) -> None:
        super().__init__('; '.join(refusals))
        self.refusals = list(refusals)


@dataclass(frozen=True)
class Appeal:
    gate: str
    path: str
    line: int
    reason: str

    def __str__(self) -> str:
        return f'Gate appeal: {self.gate} {self.path}:{self.line}'


def appeals(report: str) -> list[Appeal]:
    """Every appeal line of `report`, in order."""
    return [Appeal(m['gate'], m['path'], int(m['line']), m['reason']) for m in APPEAL.finditer(report)]


def apply(evidence: Evidence, made: Sequence[Appeal]) -> Evidence:
    """`evidence` with each of `made` on the finding it names. Raises `AppealRefused`."""
    refusals: list[str] = []
    reasons: dict[Place, str] = {}
    for appeal in made:
        ran = next((r for r in evidence.gates if r.gate == appeal.gate), None)
        if ran is None:
            refusals.append(f'{appeal}: no gate {appeal.gate} ran at {evidence.head[:12]}')
            continue
        entry = next((e for e in ran.open if e.path == appeal.path and e.overlaps(appeal.line, appeal.line)), None)
        if entry is None:
            refusals.append(
                f'{appeal}: {appeal.gate} has no finding standing there, nor an item left undecided, '
                f'at {evidence.head[:12]}'
            )
            continue
        reasons[(ran.gate, entry.path, entry.line, entry.end_line)] = appeal.reason
    if refusals:
        raise AppealRefused(refusals)
    return evidence.with_appeals(reasons)


def record(forge: GateForge, number: int, report: str) -> Evidence:
    """Write every appeal of `report` into the findings comment of pull request `number`'s head. Raises `AppealRefused`."""
    made = appeals(report)
    if not made:
        raise AppealRefused(['the report holds no `Gate appeal: <gate> <path>:<line>: <reason>` line'])
    pull = forge.pull_request(number)
    held = findings_comment(forge.pull_comments(number), pull.head)
    evidence = comment.parse(held.body) if held is not None else None
    if held is None or evidence is None:
        raise AppealRefused([f'#{number} has no findings comment of its head {pull.head[:12]}: no gate ran there'])
    appealed = apply(evidence, made)
    log.info('#%d: recording %d appeals in the findings comment of %s', number, len(made), pull.head[:12])
    forge.edit(held.id, comment.render(appealed))
    return appealed
