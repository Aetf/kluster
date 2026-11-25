"""The forge, as the model gates need more of it: a pull request's comments by id, posted and edited.

`prcheck.PullForge` reads a pull request whole, its comments among it, but
not by an id a comment can be edited by. A findings comment is edited in
place on a run at the same head, and an appeal edits it again, so the gates
read the comments through the issue-comments endpoint, which carries each
one's id and URL, and write them through it too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol, cast

from ..forge import CODE
from ..prcheck import PullForge, PullGh


@dataclass(frozen=True)
class PullComment:
    """A comment on a pull request of the code repository."""

    id: int
    body: str
    #: Whether the account `gh` runs as wrote it.
    mine: bool
    url: str


class GateForge(PullForge, Protocol):
    """The forge, with a pull request's comments by id."""

    def pull_comments(self, number: int) -> list[PullComment]:
        """A pull request's comments, oldest first."""
        ...

    def post(self, number: int, body: str) -> None: ...

    def edit(self, comment: int, body: str) -> None: ...


@dataclass(frozen=True)
class GateGh(PullGh):
    """`GateForge` through the `gh` on `PATH`."""

    @cached_property
    def _login(self) -> str:
        return str(self._json(['api', 'user'])['login'])

    def pull_comments(self, number: int) -> list[PullComment]:
        pages = cast(
            list[list[dict[str, Any]]],
            self._json(['api', '--paginate', '--slurp', f'repos/{CODE}/issues/{number}/comments?per_page=100']),
        )
        return [
            PullComment(
                id=int(raw['id']),
                body=str(raw['body']),
                mine=str(cast(dict[str, Any], raw['user'])['login']) == self._login,
                url=str(raw['html_url']),
            )
            for page in pages
            for raw in page
        ]

    def post(self, number: int, body: str) -> None:
        payload = json.dumps({'body': body})
        _ = self._run(['api', '-X', 'POST', f'repos/{CODE}/issues/{number}/comments', '--input', '-'], stdin=payload)

    def edit(self, comment: int, body: str) -> None:
        payload = json.dumps({'body': body})
        _ = self._run(['api', '-X', 'PATCH', f'repos/{CODE}/issues/comments/{comment}', '--input', '-'], stdin=payload)
