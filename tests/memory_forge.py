"""A ledger and a code repository held in memory: the forge `dispatch` reads and writes, for the cases.

Its own named module because the cases of `dispatch` and of `dispatch-hook`
both run against it, and a test module is not imported by another
(`tests/test_suite_imports.py`). A record `dispatch claim` writes into it is
read back through the same calls `dispatch` makes of GitHub. `heads` holds
what `gh pr view` reports of a pull request's head, which only the hooks
read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from kluster.scripts.dispatch.forge import Comment, Issue, Milestone, Pull
from kluster.scripts.dispatch.hooks import PullHead


@dataclass
class _Issue:
    number: int
    title: str
    milestone: str | None
    labels: set[str] = field(default_factory=set[str])
    open: bool = True
    comments: list[Comment] = field(default_factory=list[Comment])


@dataclass
class MemoryForge:
    """A ledger and a code repository held in memory."""

    issues_: dict[int, _Issue] = field(default_factory=dict[int, _Issue])
    milestones_: dict[str, Milestone] = field(default_factory=dict[str, Milestone])
    pulls_: list[Pull] = field(default_factory=list[Pull])
    #: Each open pull request's head, by number, as `gh pr view` reports it.
    heads: dict[int, PullHead] = field(default_factory=dict[int, PullHead])

    def add(self, number: int, milestone: str | None, *labels: str, title: str = 'a task', open_: bool = True) -> None:
        self.issues_[number] = _Issue(number, title, milestone, set(labels), open_)

    def issue(self, number: int) -> Issue:
        i = self.issues_[number]
        return Issue(i.number, i.title, i.open, frozenset(i.labels), i.milestone)

    def issues(self, *, label: str | None = None, milestone: str | None = None, closed: bool = False) -> list[Issue]:
        return [
            self.issue(i.number)
            for i in self.issues_.values()
            if (closed or i.open) and (label is None or label in i.labels) and (milestone in (None, i.milestone))
        ]

    def milestones(self) -> list[Milestone]:
        return list(self.milestones_.values())

    def comments(self, number: int) -> list[Comment]:
        return list(self.issues_[number].comments)

    def comment(self, number: int, body: str) -> None:
        self.issues_[number].comments.append(Comment(body, mine=True))

    def relabel(self, number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
        labels = self.issues_[number].labels
        assert not set(remove) - labels, 'gh refuses to remove a label the issue does not carry'
        labels.difference_update(remove)
        labels.update(add)

    def create(self, *, title: str, body: str, labels: Sequence[str], milestone: str) -> str:
        number = max(self.issues_, default=0) + 1
        self.add(number, milestone, *labels, title=title)
        return f'https://github.com/Aetf/kluster-ops/issues/{number}'

    def pulls(self) -> list[Pull]:
        return list(self.pulls_)

    def pull(self, number: int) -> PullHead:
        return self.heads[number]
