"""What a gate is shown of a change: each file it writes, whole, with the change's diff inline, and its commits' messages.

A file is shown as its text at the head, every line under its number there,
the lines the change writes marked `+` and the lines it removes shown as `-`
rows, unnumbered, where they stood: the file whole and the diff of it in one
reading, the way a reviewer reads a change with its context expanded. A
commit's message is shown the same way at the place that names it
(`change.message_path`), the subject being line 1, every line of it
written by the change.

A gate is shown each such file whose path its trigger matches (`parts`):
every file the change adds, modifies or retypes whose text is this
repository's own writing (`change.written_here`) and is text at all, and
the message of each of the change's commits whose place the trigger matches.
A file the change deletes holds no line at the head for a finding to name,
and is not shown.

A part too long for one call is cut into runs of its rows (`Part.runs`),
each run whole lines with the removed rows that stand among them, so every
run reads as the file's own lines with the hunks they hold.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from ..paths import OwnedPath
from .change import Change, Changed, message_path, written_here

#: How a row marks a line the change writes, one it leaves as it was, and one it removes.
WRITTEN, KEPT, REMOVED = '+', ' ', '-'

#: How a part names the file's place in the change, by the change's status of it.
KINDS = {'A': 'added', 'M': 'modified', 'T': 'retyped'}
MESSAGE = 'commit message'


@dataclass(frozen=True)
class Row:
    """One row of a part: a line at the head under its number, or a removed line, with no number."""

    number: int | None
    mark: str
    text: str

    def render(self) -> str:
        number = '' if self.number is None else str(self.number)
        return f'{number:>6} {self.mark} {self.text}'


@dataclass(frozen=True)
class Part:
    """A file, or a run of its lines, as one call shows it."""

    path: str
    kind: str
    #: How many lines the file holds at the head.
    total: int
    rows: tuple[Row, ...]

    @property
    def first(self) -> int:
        return next(r.number for r in self.rows if r.number is not None)

    @property
    def last(self) -> int:
        return next(r.number for r in reversed(self.rows) if r.number is not None)

    def shows(self, line: int) -> bool:
        return self.first <= line <= self.last

    def render(self) -> str:
        whole = '' if (self.first, self.last) == (1, self.total) else f', lines {self.first}–{self.last}'
        head = f'## {self.path} ({self.kind}, {self.total} lines{whole})'
        return '\n'.join([head, *(r.render() for r in self.rows)])

    def size(self) -> int:
        """The part's size in tokens, over-estimated: three bytes of UTF-8 to a token."""
        return len(self.render().encode()) // 3 + 1

    def runs(self, cap: int) -> list[Part]:
        """The part cut into runs of at most `cap` estimated tokens each; the part alone where it fits.

        A run is cut only ahead of the removed rows that stand before a line,
        so they stay with it, and may cut through a hunk, whose lines then
        go to the runs either side. A line longer than `cap` alone is a run
        of its own. Each run is a call's, and each call's samples are matched
        alone (`ask.matched`), so a finding about lines on both sides of a cut
        can stand in both runs, or in neither where each run holds one
        sample's finding of it. That reaches only a part over `cap` alone,
        which at `ask.CALL_CAP` is a file of about 180 KB.
        """
        if self.size() <= cap:
            return [self]
        # The longest heading a run of this part can carry.
        heading = len(f'## {self.path} ({self.kind}, {self.total} lines, lines {self.total}–{self.total})') // 3 + 1
        # Each line with the removed rows that stand before it; removed rows after the last line join it.
        units: list[list[Row]] = [[]]
        for row in self.rows:
            units[-1].append(row)
            if row.number is not None:
                units.append([])
        if not units[-1]:
            _ = units.pop()
        elif len(units) > 1:
            units[-2] += units.pop()
        out: list[list[Row]] = []
        size = heading
        for unit in units:
            cost = sum((len(row.render().encode()) + 1) // 3 + 1 for row in unit)
            if not out or size + cost > cap:
                out.append([])
                size = heading
            out[-1] += unit
            size += cost
        return [Part(self.path, self.kind, self.total, tuple(rows)) for rows in out]


def lines(text: str) -> list[str]:
    """`text`'s lines as git numbers them: split on newlines alone, a last newline closing the last line."""
    split = text.split('\n')
    return split[:-1] if split and split[-1] == '' else split


def _rows(changed: Changed, head: Sequence[str], base: Sequence[str]) -> tuple[Row, ...]:
    """The file's lines at the head, each marked by whether the change writes it, with the removed lines among them."""
    written = {n for first, last in changed.written for n in range(first, last + 1)}
    removed: dict[int, list[str]] = {}
    for hunk in changed.hunks:
        if hunk.base_count:
            # A hunk that writes lines removes ahead of its first; one that only removes, after the line it follows.
            before = hunk.head_start if hunk.head_count else hunk.head_start + 1
            removed.setdefault(before, []).extend(base[hunk.base_start - 1 : hunk.base_start - 1 + hunk.base_count])
    rows: list[Row] = []
    for number, text in enumerate(head, start=1):
        rows += [Row(None, REMOVED, gone) for gone in removed.get(number, [])]
        rows.append(Row(number, WRITTEN if number in written else KEPT, text))
    rows += [Row(None, REMOVED, gone) for gone in removed.get(len(head) + 1, [])]
    return tuple(rows)


def parts(change: Change, trigger: Collection[str]) -> list[Part]:
    """Each file of `change`, and each of its commits' messages, that `trigger` matches, as the gate is shown it."""
    owned = [OwnedPath.parse(pattern) for pattern in trigger]

    def matched(path: str) -> bool:
        return any(o.owns(path) for o in owned)

    found: list[Part] = []
    for changed in change.files:
        if changed.kind not in KINDS or not written_here(changed.path) or not matched(changed.path):
            continue
        text = change.text(changed.path)
        if text is None or '\0' in text or not lines(text):
            continue
        head = lines(text)
        base = lines(change.base_text(changed.path) or '')
        found.append(Part(changed.path, KINDS[changed.kind], len(head), _rows(changed, head, base)))
    for commit in change.commits:
        place = message_path(commit.sha)
        message = lines(commit.message)
        if matched(place) and message:
            rows = tuple(Row(n, WRITTEN, text) for n, text in enumerate(message, start=1))
            found.append(Part(place, MESSAGE, len(message), rows))
    return found
