"""The forge, as the model gates need more of it: a pull request's comments by id, posted and edited, and its commits.

`prcheck.PullForge` reads a pull request whole, its comments among it, but
not by an id a comment can be edited by. A findings comment is edited in
place on a run at the same head, and an appeal edits it again, so the gates
read the comments through the issue-comments endpoint, which carries each
one's id and URL, and write them through it too.
A calibration asks whether the forge still holds a head its history names
(`GateForge.serves`), since a head it no longer holds drops its cases. An
example a gate's prompt carries reads the reason its case's comment gives
(`GateForge.comment_at`), by the URL the case holds, which may be a comment on
a pull request, a review comment, or an issue's body or comment in another
repository, an operator's ruling in a private one among them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol, cast

from ..forge import CODE, ForgeError
from ..prcheck import PullForge, PullGh

#: A comment's URL on the forge: the repository, the issue or pull request, and the comment's id where it is a
#: comment and not the issue's own body, an issue or pull-request comment's or a review comment's.
_COMMENT_URL = re.compile(
    r'\Ahttps://github\.com/(?P<repository>[\w.-]+/[\w.-]+)/(?:pull|issues)/(?P<number>\d+)'
    r'(?:#(?:issuecomment-(?P<comment>\d+)|discussion_r(?P<review>\d+)))?\Z'
)
#: The statuses the API answers a comment it holds no longer with. A 403 is a rate limit or a permission, which a
#: run is not to read as a comment gone, and raises.
_GONE = ('(HTTP 404)', '(HTTP 410)')


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

    def comment_at(self, url: str) -> PullComment | None:
        """The comment at `url`, of any repository; None where the URL names none or the forge no longer serves it."""
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

    def comment_at(self, url: str) -> PullComment | None:
        found = _COMMENT_URL.match(url)
        if found is None:
            return None
        repository = found['repository']
        if found['comment'] is not None:
            endpoint = f'repos/{repository}/issues/comments/{found["comment"]}'
        elif found['review'] is not None:
            endpoint = f'repos/{repository}/pulls/comments/{found["review"]}'
        else:
            endpoint = f'repos/{repository}/issues/{found["number"]}'
        try:
            raw = cast(dict[str, Any], self._json(['api', endpoint]))
        except ForgeError as exc:
            if any(status in str(exc) for status in _GONE):
                return None
            raise
        return PullComment(
            id=int(raw['id']),
            body=str(raw.get('body') or ''),
            mine=str(cast(dict[str, Any], raw['user'])['login']) == self._login,
            url=url,
            created=str(raw['created_at']),
        )
