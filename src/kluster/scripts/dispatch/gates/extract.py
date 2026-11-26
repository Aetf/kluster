"""A change as the gates read it, and the items a gate's extractor draws from it.

The ask shows a gate the change itself (`view`). An extractor is a
function from a `Change` to `Item`s, which `harvest` records a review's tags
as cases of: it is deterministic and narrow, picking the lines its gate's
question is about and the least context that question needs. Every item
names a place at the head under review, which is where a review's tag pairs
with it. A place in a file is its path and lines, as a finding's is. A
place in the message of one of the change's commits
(`Change.commits`) is a pseudo-path that names the commit, under `COMMITS`,
and lines of the message, the subject being line 1 (`message_path`): no
path of this repository starts with `:`, so no file is read as a message.

Most questions are about the lines a change writes, and read the head
alone. A question about what the change leaves behind it, a passage
elsewhere that a removed sentence made false, reads the base side of each
hunk (`Changed.hunks`, `Change.base_text`), the tree at the head
(`Change.tree`) and the pull request's description (`Change.body`) as well.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
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


#: The most of an item's context, in characters, whatever its extractor joins: well under a batch's token cap
#: (`ask.ITEM_TOKEN_CAP`), so that a batch holds several items and no one item's call grows with what it reads.
ITEM_CONTEXT_CAP = 24_000
#: What stands where `bounded` cuts a context, with the count of characters cut.
_CUT = '\n… [{} characters of the context left out] …\n'


def bounded(context: str) -> str:
    """`context` held to `ITEM_CONTEXT_CAP`: where longer, its middle is cut out and the cut says how much went.

    Both ends stay, since an extractor puts where the item sits first and,
    for an assertion, the case around it last.
    """
    if len(context) <= ITEM_CONTEXT_CAP:
        return context
    # The count of what is cut has no more digits than the context's length.
    keep = ITEM_CONTEXT_CAP - len(_CUT.format(len(context)))
    head = keep // 2
    tail = len(context) - (keep - head)
    return f'{context[:head]}{_CUT.format(tail - head)}{context[tail:]}'


#: The directory a commit's message is presented under as a place (`message_path`).
COMMITS = ':commit'


def message_path(sha: str) -> str:
    """The pseudo-path that names the message of commit `sha` as a place."""
    return f'{COMMITS}/{sha}'


def is_message(path: str) -> bool:
    """Whether `path` names a commit's message (`message_path`)."""
    return path.startswith(f'{COMMITS}/')


def written_here(path: str) -> bool:
    """Whether the text of `path` is this repository's own writing, and so prose a gate may ask about."""
    return not path.startswith(NOT_WRITTEN_HERE)


@dataclass(frozen=True)
class Item:
    """One thing an extractor draws: a place at the head, its text, and the context the question needs.

    The context is held to `ITEM_CONTEXT_CAP` (`bounded`) as the item is
    made, so no extractor can draw more.
    """

    path: str
    line: int
    end_line: int
    text: str
    #: What the extractor gives beside the text for its gate's question, as its own docstring says; empty where
    #: none is needed.
    context: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(self, 'context', bounded(self.context))

    def overlaps(self, line: int, end_line: int) -> bool:
        """Whether any of lines `line`–`end_line` is one of this item's."""
        return self.line <= end_line and line <= self.end_line

    @property
    def place(self) -> str:
        return f'{self.path}:{self.line}' if self.line == self.end_line else f'{self.path}:{self.line}–{self.end_line}'


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


def _empty() -> Sequence[str]:
    return ()


@dataclass(frozen=True)
class Change:
    """A change from its base to its head, as an extractor reads it."""

    head: str
    files: tuple[Changed, ...]
    #: The text of a path at the head; None where the head holds no such file.
    text: Callable[[str], str | None]
    #: The text of a path at the base; None where the base holds no such file.
    #: `change` reads it from the store; a change built by hand holds no base.
    base_text: Callable[[str], str | None] = _absent
    #: Every path the head tracks. `change` reads it from the store; a change
    #: built by hand holds no tree beyond what it names.
    tree: Callable[[], Sequence[str]] = _empty
    #: The pull request's description.
    body: str = ''
    #: The commits the change holds, oldest first. `change` reads them from
    #: the store; a change built by hand holds none.
    commits: tuple[Commit, ...] = ()

    @property
    def paths(self) -> list[str]:
        return [changed.path for changed in self.files]

    def written(self, *patterns: str) -> Iterator[Item]:
        """Each line the change writes in a file one of `patterns` matches (`paths.OwnedPath`), as an item of its own.

        The building block of an extractor whose question is about written
        lines; one that needs more context builds it from `text`.
        """
        owned = [OwnedPath.parse(pattern) for pattern in patterns]
        for changed in self.files:
            if not changed.written or not any(o.owns(changed.path) for o in owned):
                continue
            lines = (self.text(changed.path) or '').split('\n')
            for first, last in changed.written:
                for number in range(first, last + 1):
                    if number <= len(lines):
                        yield Item(changed.path, number, number, lines[number - 1])


class Store(Protocol):
    """The commits of the local store, as much of them as a change is read with (`prcheck.Git`)."""

    def statuses(self, base: str, head: str) -> list[Status]: ...

    def hunks(self, base: str, head: str, path: str) -> list[Hunk]: ...

    def text(self, commit: str, path: str) -> str | None: ...

    def paths(self, commit: str) -> list[str]: ...

    def commits(self, base: str, head: str) -> list[Commit]: ...


def change(store: Store, base: str, head: str, body: str) -> Change:
    """The change from `base` to `head`, read from the local store, with `body` its pull request's description."""
    files: list[Changed] = []
    for status in store.statuses(base, head):
        hunks: Sequence[Hunk] = store.hunks(base, head, status.path)
        written = tuple(
            (hunk.head_start, hunk.head_start + hunk.head_count - 1) for hunk in hunks if hunk.head_count > 0
        )
        files.append(Changed(status.path, status.kind, written, tuple(hunks)))
    return Change(
        head=head,
        files=tuple(files),
        text=lambda path: store.text(head, path),
        base_text=lambda path: store.text(base, path),
        tree=lambda: store.paths(head),
        body=body,
        commits=tuple(store.commits(base, head)),
    )
