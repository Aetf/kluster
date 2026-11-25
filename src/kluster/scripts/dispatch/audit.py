"""`dispatch audit`: the ledger's anomalies, listed for the dispatcher to act on.

It changes nothing on the forge. What it lists is `Anomaly`'s; each finding
names the issue, pull request or workspace it is about.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from . import ledger, record
from .brief import Role
from .forge import Forge

log = logging.getLogger(__name__)

#: The workspace every repository has, which no dispatch creates.
PRIMARY_WORKSPACE = 'default'


class Anomaly(enum.StrEnum):
    """A kind of finding."""

    #: An open issue, not an alert, lacking an `area/*` or `kind/*` label or a
    #: milestone (dispatch.md §4).
    UNLABELED = 'unlabeled'
    #: An issue carrying a claim label and no live dispatch, whose holder nothing names.
    CLAIM_WITHOUT_RECORD = 'claim-without-record'
    #: An issue whose record comments do not read.
    RECORD_UNREADABLE = 'record-unreadable'
    #: A claimed build with no open pull request that closes or refers to its
    #: issue: a dispatch still building, or one that died (dispatch.md §4.2).
    CLAIM_WITHOUT_PULL = 'claim-without-pull'
    #: A live dispatch made on the operator's word, past a gate.
    OPERATOR_WORD = 'operator-word'
    #: An open pull request, not a bot's, that names no ledger issue (dispatch.md §4).
    PULL_WITHOUT_ISSUE = 'pull-without-issue'
    #: An issue in two states of a decision at once (dispatch.md §4.1).
    TWO_DECISIONS = 'two-decisions'
    #: A `jj` workspace no live dispatch names (dispatch.md §1.2).
    WORKSPACE_WITHOUT_RECORD = 'workspace-without-record'


@dataclass(frozen=True)
class Finding:
    anomaly: Anomaly
    #: The issue, pull request or workspace: `#N`, `pull request #N`, `workspace <name>`.
    about: str
    detail: str

    def __str__(self) -> str:
        return f'{self.anomaly.value}: {self.about} {self.detail}'


def audit(forge: Forge, workspaces: Sequence[str]) -> list[Finding]:
    """Every anomaly on the ledger, its open pull requests, and the `jj` workspaces named."""
    findings: list[Finding] = []
    log.info('reading the open issues')
    for issue in forge.issues():
        if ledger.ALERT not in issue.labels:
            findings += [
                Finding(Anomaly.UNLABELED, f'#{issue.number}', f'lacks {lack}')
                for lack in ledger.missing(issue.labels, issue.milestone)
            ]
        decisions = sorted(label for label in issue.labels if label.startswith(ledger.DECISION))
        if len(decisions) > 1:
            findings.append(Finding(Anomaly.TWO_DECISIONS, f'#{issue.number}', f'carries {", ".join(decisions)}'))
    log.info('reading the open pull requests')
    pulls = forge.pulls()
    referenced = frozenset(number for pull in pulls for number in ledger.references(pull.body))
    findings += [
        Finding(
            Anomaly.PULL_WITHOUT_ISSUE,
            f'pull request #{pull.number}',
            'names no ledger issue on a `Closes` or `Refs` line',
        )
        for pull in pulls
        if not pull.by_bot and not ledger.references(pull.body)
    ]
    log.info('reading the records of every claimed issue')
    named: set[str] = set()
    claimed = {issue.number: issue for label in ledger.CLAIMS for issue in forge.issues(label=label)}
    for number, issue in sorted(claimed.items()):
        try:
            live = record.dispatches(number, forge.comments(number))
        except record.RecordUnreadable as exc:
            findings.append(Finding(Anomaly.RECORD_UNREADABLE, f'#{number}', str(exc)))
            continue
        if not live:
            labels = ', '.join(f'`{label}`' for label in ledger.CLAIMS if label in issue.labels)
            findings.append(Finding(Anomaly.CLAIM_WITHOUT_RECORD, f'#{number}', f'carries {labels}'))
            continue
        named |= {d.name.workspace for d in live if d.name.workspace is not None}
        building = ledger.CLAIMED in issue.labels and any(
            r.front is not None and r.front.role is Role.BUILDER for d in live for r in d.records
        )
        if building and number not in referenced:
            findings.append(Finding(Anomaly.CLAIM_WITHOUT_PULL, f'#{number}', 'is claimed for a build'))
        findings += [
            Finding(Anomaly.OPERATOR_WORD, f'#{number}', f'past {"; ".join(r.waiver.passed)}: {r.waiver.quote!r}')
            for d in live
            for r in d.records
            if r.waiver is not None
        ]
    findings += [
        Finding(Anomaly.WORKSPACE_WITHOUT_RECORD, f'workspace {name}', 'is named by no live dispatch')
        for name in workspaces
        if name != PRIMARY_WORKSPACE and name not in named
    ]
    return findings
