"""The forge, as the model gates need more of it: a pull request's comments by id, posted and edited, and its commits.

`prcheck.PullForge` reads a pull request whole, its comments among it, but
not by an id a comment can be edited by. A findings comment is edited in
place on a run at the same head, and an appeal edits it again, so the gates
read the comments through the issue-comments endpoint, which carries each
one's id and URL, and write them through it too.
A calibration asks whether the forge still holds a head its history names
(`GateForge.serves`), since a head it no longer holds drops its cases.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol, cast

from ..forge import CODE, ForgeError
from ..prcheck import PullForge, PullGh


@dataclass(frozen=True)
class PullComment:
    """A comment on a pull request of the code repository."""

    id: int
    body: str
    #: Whether the account `gh` runs as wrote it.
    mine: bool
    url: str
    #: When it was written, as the forge gives it: an ISO 8601 time in UTC, which sorts as it reads.
    created: str = ''


class GateForge(PullForge, Protocol):
    """The forge, with a pull request's comments by id."""

    def pull_comments(self, number: int) -> list[PullComment]:
        """A pull request's comments, oldest first."""
        ...

    def post(self, number: int, body: str) -> None: ...

    def edit(self, comment: int, body: str) -> None: ...

    def serves(self, commit: str) -> bool:
        """Whether the code repository on the forge still holds `commit`."""
        ...

    def pull_commits(self, number: int) -> list[str]:
        """The SHAs of the commits a pull request holds now."""
        ...


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
                created=str(raw['created_at']),
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

    def serves(self, commit: str) -> bool:
        try:
            _ = self._run(['api', f'repos/{CODE}/commits/{commit}', '--jq', '.sha'])
        except ForgeError as exc:
            # The commits endpoint answers 422 for an id it holds no commit of, a full SHA among them; 404 is
            # the repository itself not found, which a token without access to it reads too, and raises.
            if '(HTTP 422)' in str(exc):
                return False
            raise
        return True

    def pull_commits(self, number: int) -> list[str]:
        pages = cast(
            list[list[dict[str, Any]]],
            self._json(['api', '--paginate', '--slurp', f'repos/{CODE}/pulls/{number}/commits?per_page=100']),
        )
        return [str(raw['sha']) for page in pages for raw in page]
