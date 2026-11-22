"""The forge, as the ledger's operations need it, and the `gh` that reaches it.

Everything the subcommands read or write on GitHub goes through `Forge`, so
the cases run against a forge of their own and the real one is reached only
from `Gh`. `Gh` names the repository on every call: `--repo` where the
subcommand takes it, the path where it is `gh api`. It holds no credential;
`gh` authenticates as whatever account the workstation logged it in as.
"""

from __future__ import annotations

import json
import subprocess as sp
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

#: The ledger: the private repository whose issues are the work (dispatch.md §4).
LEDGER = 'Aetf/kluster-ops'
#: The repository whose pull requests do the work.
CODE = 'Aetf/kluster'

#: How long any one `gh` invocation may take. Each is a few API calls, so a
#: slow one is a broken one rather than a big one.
TIMEOUT = 120

#: The most issues or pull requests one listing returns. Far above what the
#: ledger holds open; a listing that reaches it is refused rather than read
#: short.
LISTING_LIMIT = 2000


class ForgeError(RuntimeError):
    """A call to the forge failed, or answered in a shape this module does not read."""


@dataclass(frozen=True)
class Issue:
    """An issue of the ledger, as much of it as the preconditions read."""

    number: int
    title: str
    open: bool
    labels: frozenset[str]
    #: The milestone's title, or None for an issue on none.
    milestone: str | None


@dataclass(frozen=True)
class Milestone:
    """A milestone of the ledger."""

    title: str
    open: bool
    description: str


@dataclass(frozen=True)
class Comment:
    """A comment on a ledger issue."""

    body: str
    #: Whether the account `gh` runs as wrote it.
    mine: bool


@dataclass(frozen=True)
class Pull:
    """An open pull request of the code repository."""

    number: int
    body: str
    #: Every path the pull request changes.
    files: frozenset[str]
    #: Whether a bot account opened it, which no session can be (dispatch.md §1).
    by_bot: bool


class Forge(Protocol):
    """The operations the subcommands make. Issue numbers are the ledger's."""

    def issue(self, number: int) -> Issue: ...

    def issues(self, *, label: str | None = None, milestone: str | None = None, closed: bool = False) -> list[Issue]:
        """The open issues, narrowed to a label or a milestone; with `closed`, the closed ones too."""
        ...

    def milestones(self) -> list[Milestone]:
        """Every milestone, open and closed."""
        ...

    def comments(self, number: int) -> list[Comment]:
        """An issue's comments, oldest first."""
        ...

    def comment(self, number: int, body: str) -> None: ...

    def relabel(self, number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None: ...

    def create(self, *, title: str, body: str, labels: Sequence[str], milestone: str) -> str:
        """File an issue; its URL."""
        ...

    def pulls(self) -> list[Pull]:
        """The code repository's open pull requests."""
        ...


def _issue(raw: dict[str, Any]) -> Issue:
    milestone = cast(dict[str, Any] | None, raw.get('milestone'))
    return Issue(
        number=int(raw['number']),
        title=str(raw['title']),
        open=str(raw['state']).upper() == 'OPEN',
        labels=frozenset(str(label['name']) for label in cast(list[dict[str, Any]], raw['labels'])),
        milestone=str(milestone['title']) if milestone else None,
    )


#: The fields `_issue` reads, as `--json` takes them.
_ISSUE_FIELDS = 'number,title,state,labels,milestone'


@dataclass(frozen=True)
class Gh:
    """`Forge` through the `gh` on `PATH`, which `mise x` makes the pinned one."""

    def _run(self, args: Sequence[str], *, stdin: str | None = None) -> str:
        try:
            completed = sp.run(['gh', *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT, check=False)
        except FileNotFoundError as exc:
            raise ForgeError('`gh` is not on PATH; run under `mise x`, which puts the pinned one there') from exc
        except sp.TimeoutExpired as exc:
            raise ForgeError(f'`gh {" ".join(args)}` did not finish within {TIMEOUT}s') from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise ForgeError(f'`gh {" ".join(args)}` failed: {detail[-1] if detail else completed.returncode}')
        return completed.stdout

    def _json(self, args: Sequence[str]) -> Any:
        out = self._run(args)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise ForgeError(f'`gh {" ".join(args)}` printed no JSON: {exc}') from exc

    def _listing(self, args: Sequence[str]) -> list[dict[str, Any]]:
        listed = cast(list[dict[str, Any]], self._json([*args, '--limit', str(LISTING_LIMIT)]))
        if len(listed) >= LISTING_LIMIT:
            raise ForgeError(f'`gh {" ".join(args)}` reached its limit of {LISTING_LIMIT}; the listing may be short')
        return listed

    def issue(self, number: int) -> Issue:
        return _issue(self._json(['issue', 'view', str(number), '--repo', LEDGER, '--json', _ISSUE_FIELDS]))

    def issues(self, *, label: str | None = None, milestone: str | None = None, closed: bool = False) -> list[Issue]:
        args = ['issue', 'list', '--repo', LEDGER, '--state', 'all' if closed else 'open', '--json', _ISSUE_FIELDS]
        if label is not None:
            args += ['--label', label]
        if milestone is not None:
            args += ['--milestone', milestone]
        return [_issue(raw) for raw in self._listing(args)]

    def milestones(self) -> list[Milestone]:
        pages = cast(
            list[list[dict[str, Any]]],
            self._json(['api', '--paginate', '--slurp', f'repos/{LEDGER}/milestones?state=all&per_page=100']),
        )
        return [
            Milestone(title=str(raw['title']), open=raw['state'] == 'open', description=str(raw['description'] or ''))
            for page in pages
            for raw in page
        ]

    def comments(self, number: int) -> list[Comment]:
        raw = cast(dict[str, Any], self._json(['issue', 'view', str(number), '--repo', LEDGER, '--json', 'comments']))
        return [
            Comment(body=str(comment['body']), mine=comment['viewerDidAuthor'] is True)
            for comment in cast(list[dict[str, Any]], raw['comments'])
        ]

    def comment(self, number: int, body: str) -> None:
        _ = self._run(['issue', 'comment', str(number), '--repo', LEDGER, '--body-file', '-'], stdin=body)

    def relabel(self, number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
        args = ['issue', 'edit', str(number), '--repo', LEDGER]
        for label in add:
            args += ['--add-label', label]
        for label in remove:
            args += ['--remove-label', label]
        _ = self._run(args)

    def create(self, *, title: str, body: str, labels: Sequence[str], milestone: str) -> str:
        args = ['issue', 'create', '--repo', LEDGER, '--title', title, '--body-file', '-', '--milestone', milestone]
        for label in labels:
            args += ['--label', label]
        return self._run(args, stdin=body).strip()

    def pulls(self) -> list[Pull]:
        listed = self._listing(
            ['pr', 'list', '--repo', CODE, '--state', 'open', '--json', 'number,body,files,changedFiles,author']
        )
        return [
            Pull(
                number=int(raw['number']),
                body=str(raw['body']),
                files=self._files(raw),
                by_bot=bool(cast(dict[str, Any], raw['author']).get('is_bot')),
            )
            for raw in listed
        ]

    def _files(self, raw: dict[str, Any]) -> frozenset[str]:
        """Every path a listed pull request changes.

        The listing carries a pull request's first hundred files only, so one
        that changes more is read again through the REST endpoint, which pages
        through all of them.
        """
        files = frozenset(str(f['path']) for f in cast(list[dict[str, Any]], raw['files']))
        if len(files) >= int(raw['changedFiles']):
            return files
        pages = cast(
            list[list[dict[str, Any]]],
            self._json(['api', '--paginate', '--slurp', f'repos/{CODE}/pulls/{raw["number"]}/files?per_page=100']),
        )
        return frozenset(str(f['filename']) for page in pages for f in page)
