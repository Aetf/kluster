"""A change as the gates read it, and the places in it that are no file of this repository.

A change is its head, the paths it touches with the lines it writes and the
hunks of each, the text of a path at either side, and its commits. Every
place a gate finds, and every place a review tags, names a path and lines at
the head. Two kinds of place are pseudo-paths under a directory no path of
this repository has, since none starts with `:`:

-   the message of one of the change's commits (`Change.commits`), under
    `COMMITS`, the subject being line 1 (`message_path`);
-   a brief, under `BRIEFS`, which `dispatch claim` presents as the one file
    of a change that writes it whole, at a path that names the areas of its
    issue (`brief_path`). A gate is shown a brief only where a pattern of its
    trigger is rooted at `BRIEFS` and matches the path (`asks_about_briefs`),
    so a trigger that takes every path of a pull request, `**`, takes no
    brief.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Protocol

from ..paths import OwnedPath
from ..prcheck import Commit, Hunk, Status

#: Where the text is held here but written elsewhere, so that no passage of it is this repository's
#: prose: generated (the SDKs, the CRD package, the lockfiles of mise and uv), vendored, or recorded as test data.
#: Today's instances.
NOT_WRITTEN_HERE = (
    'sdks/',
    'packages/crds/',
    'mise.lock',
    'mise.ci.lock',
    'uv.lock',
    'tests/vendor/',
    'tests/data/',
)

#: The directory a commit's message is presented under as a place (`message_path`).
COMMITS = ':commit'

#: The directory a brief is presented under as a change (`brief_path`).
BRIEFS = ':brief'


def message_path(sha: str) -> str:
    """The pseudo-path that names the message of commit `sha` as a place."""
    return f'{COMMITS}/{sha}'


def is_message(path: str) -> bool:
    """Whether `path` names a commit's message (`message_path`)."""
    return path.startswith(f'{COMMITS}/')


def brief_path(areas: Collection[str], name: str) -> str:
    """Where a brief of an issue in `areas`, its `area/*` labels without the prefix, is presented."""
    return '/'.join([BRIEFS, *sorted(areas), name])


def is_brief(path: str) -> bool:
    """Whether `path` is a brief's (`brief_path`)."""
    return path.startswith(f'{BRIEFS}/')


def asks_about_briefs(trigger: Collection[str], path: str) -> bool:
    """Whether a gate whose trigger is `trigger` asks about the brief at `path`: a pattern of it names briefs and matches."""
    return any(is_brief(pattern) and OwnedPath.parse(pattern).owns(path) for pattern in trigger)


def written_here(path: str) -> bool:
    """Whether the text of `path` is this repository's own writing, and so prose a gate may ask about."""
    return not path.startswith(NOT_WRITTEN_HERE)


@dataclass(frozen=True)
class Changed:
    """One path a change touches, with the line ranges at the head that it adds or rewrites."""

    path: str
    #: `A`dded, `M`odified, `D`eleted, or its `T`ype changed.
    kind: str
    #: Each run of lines the change writes at the head, first and last line.
    written: tuple[tuple[int, int], ...]
    #: Each hunk of the file's diff, its lines at the base and at the head (`git diff -U0`), in order.
    hunks: tuple[Hunk, ...] = ()


def _absent(_path: str) -> str | None:
    return None


@dataclass(frozen=True)
class Change:
    """A change from its base to its head."""

    head: str
    files: tuple[Changed, ...]
    #: The text of a path at the head; None where the head holds no such file.
    text: Callable[[str], str | None]
    #: The text of a path at the base; None where the base holds no such file.
    #: `change` reads it from the store; a change built by hand holds no base.
    base_text: Callable[[str], str | None] = _absent
    #: The commits the change holds, oldest first. `change` reads them from
    #: the store; a change built by hand holds none.
    commits: tuple[Commit, ...] = ()

    @property
    def paths(self) -> list[str]:
        return [changed.path for changed in self.files]


class Store(Protocol):
    """The commits of the local store, as much of them as a change is read with (`prcheck.Git`)."""

    def statuses(self, base: str, head: str) -> list[Status]: ...

    def hunks(self, base: str, head: str, path: str) -> list[Hunk]: ...

    def text(self, commit: str, path: str) -> str | None: ...

    def commits(self, base: str, head: str) -> list[Commit]: ...


def change(store: Store, base: str, head: str) -> Change:
    """The change from `base` to `head`, read from the local store."""
    files: list[Changed] = []
    for status in store.statuses(base, head):
        hunks = store.hunks(base, head, status.path)
        written = tuple(
            (hunk.head_start, hunk.head_start + hunk.head_count - 1) for hunk in hunks if hunk.head_count > 0
        )
        files.append(Changed(status.path, status.kind, written, tuple(hunks)))
    return Change(
        head=head,
        files=tuple(files),
        text=lambda path: store.text(head, path),
        base_text=lambda path: store.text(base, path),
        commits=tuple(store.commits(base, head)),
    )
