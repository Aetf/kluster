"""What a gate's extractor reads, and what it hands the model: a change, and the items drawn from it.

An extractor is a function from a `Change` to `Item`s. It is deterministic
and narrow: it picks the lines its gate's question is about and the least
context that question needs, so a gate never reads the whole repository and
a large change makes more items, not a longer prompt. Every item names a
place at the head under review, which is where its finding is posted and
where a review's tag pairs with it (`harvest`).

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
from ..prcheck import Hunk, Status

#: Where the text is held here but written elsewhere, so that no passage of it is this repository's
#: prose: generated (the SDKs, the CRD package, mise's lockfiles), vendored, or recorded as test data.
#: Today's instances.
NOT_WRITTEN_HERE = ('sdks/', 'packages/crds/', 'mise.lock', 'mise.ci.lock', 'tests/vendor/', 'tests/data/')


def written_here(path: str) -> bool:
    """Whether the text of `path` is this repository's own writing, and so prose a gate may ask about."""
    return not path.startswith(NOT_WRITTEN_HERE)


@dataclass(frozen=True)
class Item:
    """One thing a gate asks about: a place at the head, its text, and the context the question needs."""

    path: str
    line: int
    end_line: int
    text: str
    #: The enclosing case or paragraph, and the module-level names the text reads; empty where none is needed.
    context: str = ''

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
    )
