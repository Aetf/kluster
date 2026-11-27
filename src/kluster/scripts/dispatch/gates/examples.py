"""A gate's examples: the past rulings its prompt carries, chosen from its history by what a run is shown.

The prompt a gate is asked with carries, after its rule, up to `BUDGET` of
its cases as examples (`Library.prompt`). Which ones is `select`, and it
turns on nothing but the cases and the paths the run is shown: every case
is ranked by how many leading directories its path shares with the path of
those it shares the most with (`shared`), the most first, then by when its
ruling was written (`cases.Case.when`), the latest first, the place itself
breaking a tie; and from that order a finding and a pass are taken in turn,
a finding first, until the budget is spent or the cases run out, the rest
of one kind taken in order once the other has none left. A commit's message,
`:commit/<sha>`, shares that one directory with every other message.

An example shows its place's lines as the local store holds them at the
case's head, a file's or a commit message's, under their numbers, and the
ruling: whether the review found the place breaks the rule or held it to
the rule, and the reason its comment gives. A case holds no text, so the
reason is read from the case's source comment through the forge
(`forge.GateForge.comment_at`) and cut from it as `harvest` binds a tag: the
line, or the list item with the paragraphs indented under it
(`tags.units`), that holds a tag of the gate with the case's verdict and
names the case's place, with its tags and its list marker taken out
(`cut`). A comment written before tags were, which tags nothing, still
gives a finding the unit that names the place closest to its own, since a
review names a place it finds against in the paragraph that says what is
wrong there: a unit naming the case's place exactly, then one naming a
place that opens on its first line, then the one naming a place within
`calibration.MARGIN` the fewest lines from it. A review often names two
defects a few lines apart, so a nearer unit is not passed over for an
earlier one, and where two units are as close, nothing says which is the
case's and it gets none. Such a comment gives a pass nothing, since a
place a review names untagged is named for a defect, another rule's where
the gate held the place to its own. A comment this account did not write,
one the forge no longer serves, and one where no unit gives the case a
reason give none, each logged, and the example shows its lines and its
ruling alone. A case whose lines the store does not hold at its head is
passed over for the next of its kind.

Which cases are drawn from is the caller's. `dispatch gates` draws from
every case of the gate, and `calibrate` measures each pull request with
examples drawn from every other pull request's cases, so no case is scored
by a prompt that holds it. A brief is asked with none (`run.prepare`).

The selection is part of what a record measures: the record holds the hash
of `selection`, the source of everything `select` runs, the functions of
`RULE` and the getters of the case's properties `ranked` orders by
(`RANKS_BY`), with the budget, so an edit to any of them stales every
record (`calibration.Hashes`). What an example says around its lines,
`FRAME`, `HEADING`, `EXAMPLE`, `RULED`, `ROW` and `REASON`, is fixed text
every call that carries examples asks, and the prompt's hash takes it
(`ask.harness`).
"""

from __future__ import annotations

import inspect
import itertools
import logging
import re
from collections import deque
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..prcheck import Commit
from . import tags
from .calibration import near
from .cases import Case
from .change import COMMITS, is_message
from .forge import PullComment
from .model import dumps
from .verdict import Verdict
from .view import lines

log = logging.getLogger(__name__)

#: The most examples a gate's prompt carries.
BUDGET = 6

#: What the prompt says above the examples.
FRAME = (
    'The examples below are places that reviews of changes already made in this repository ruled on under the '
    "rule above: each place's lines as its change left them, under their numbers, whether the review found the "
    'place breaks the rule or held it to the rule, and the reason the review gave, where it gave one. They show '
    'how the rule has been applied. They are no part of the change under review, and no finding names them.'
)
#: The examples' heading, each example's first line, its ruling by the case's verdict, and its reason.
HEADING = '## Examples'
EXAMPLE = '### Example {number}: {place}, {ruled}'
#: Each of an example's lines, under its number.
ROW = '{number:>6}   {text}'
RULED = {
    Verdict.FINDING: 'which the review found breaks the rule',
    Verdict.PASS: 'which the review held to the rule',
}
REASON = 'The review: {reason}'

#: A list item's marker at the head of a unit.
_MARKER = re.compile(r'^(?:[-*+]|\d+[.)])[ \t]+')


def directories(path: str) -> list[str]:
    """The directories `path` lies under, outermost first."""
    return path.split('/')[:-1]


def shared(path: str, seen: Collection[str]) -> int:
    """How many leading directories `path` shares with the path of `seen` it shares the most with."""
    mine = directories(path)
    most = 0
    for other in seen:
        count = 0
        for one, two in zip(mine, directories(other), strict=False):
            if one != two:
                break
            count += 1
        most = max(most, count)
    return most


def ranked(cases: Sequence[Case], seen: Collection[str]) -> list[Case]:
    """`cases` from the most leading directories shared with `seen` down, then from the latest ruling back."""
    by_place = sorted(cases, key=lambda c: c.key)
    return sorted(by_place, key=lambda c: (shared(c.path, seen), c.when), reverse=True)


def select[T](cases: Sequence[Case], seen: Collection[str], show: Callable[[Case], T | None], budget: int) -> list[T]:
    """Up to `budget` of `cases` as `show` makes them, a finding and a pass in turn from `ranked`'s order.

    A case `show` makes nothing of is passed over for the next of its kind.
    """
    order = ranked(cases, seen)
    pending = {kind: deque(c for c in order if c.verdict is kind) for kind in (Verdict.FINDING, Verdict.PASS)}
    chosen: list[T] = []
    turns = itertools.cycle((Verdict.FINDING, Verdict.PASS))
    while len(chosen) < budget and any(pending.values()):
        queue = pending[next(turns)]
        while queue:
            shown = show(queue.popleft())
            if shown is not None:
                chosen.append(shown)
                break
    return chosen


#: The functions the selection is, whose source the record's hash of it takes (`selection`).
RULE: tuple[Callable[..., object], ...] = (directories, shared, ranked, select)
#: The properties of a case `ranked` orders by, whose getters' source the hash takes too, read as `selection` runs.
RANKS_BY = ('when', 'key')


def _getter(name: str) -> Callable[..., object]:
    """The getter of `Case`'s property `name`."""
    found: object = getattr(Case, name)
    if not isinstance(found, property) or found.fget is None:
        raise TypeError(f'Case.{name} is no property with a getter, which the selection ranks by')
    return found.fget


def selection() -> str:
    """What a record's hash of the example selection takes: the source of all it runs, and the budget.

    That is each function of `RULE` and the getter of each property of
    `RANKS_BY`, so an edit to any of them, a comment or a rename included,
    stales every record.
    """
    sources = [inspect.getsource(f) for f in (*RULE, *map(_getter, RANKS_BY))]
    return '\n'.join([*sources, dumps({'budget': BUDGET})])


def _path(named: tags.Named, case: Case) -> bool:
    """Whether a place a comment names is at `case`'s path, as `harvest` binds it: the path, or its end."""
    if named.pr is not None and (named.pr, named.head) != (case.pr, case.head):
        return False
    if is_message(named.path):
        return case.path.startswith(named.path)
    return named.path == case.path or case.path.endswith(f'/{named.path}')


def _reason(unit: str) -> str | None:
    return ' '.join(_MARKER.sub('', tags.TAG.sub('', unit)).split()) or None


def _closeness(named: tags.Named, case: Case) -> tuple[int, int] | None:
    """How closely a place a unit names is `case`'s, the closest least; None where it is not near it.

    The case's own place first, then a place that opens on its first line,
    then any other within `calibration.MARGIN`, by how many lines lie between.
    """
    if not _path(named, case) or not near(named.line, named.end_line, case.line, case.end_line):
        return None
    if (named.line, named.end_line) == (case.line, case.end_line):
        return (0, 0)
    if named.line == case.line:
        return (1, 0)
    return (2, max(0, max(named.line, case.line) - min(named.end_line, case.end_line)))


def cut(body: str, gate: str, case: Case) -> str | None:
    """The reason comment `body` gives for `case` of `gate`, tags taken out; None where it gives none.

    The unit whose tag of `gate` ruled the case is its reason. Where the
    comment tags nothing, a finding's is the unit naming the place closest
    to its own (`_closeness`); where two units name places as close, it
    has none, since nothing says which is the case's.
    """
    found = list(tags.units(body))
    for unit in found:
        for tagged in tags.read(unit).tagged:
            if tagged.gate != gate or tagged.verdict is not case.verdict:
                continue
            if any(_path(n, case) and n.line == case.line for n in tagged.places):
                return _reason(unit)
    if case.verdict is not Verdict.FINDING or tags.TAG.search(body):
        return None
    ranked_units: list[tuple[tuple[int, int], str]] = []
    for unit in found:
        close = [c for n in tags.named(unit) if (c := _closeness(n, case)) is not None]
        if close:
            ranked_units.append((min(close), unit))
    if not ranked_units:
        return None
    best = min(close for close, _ in ranked_units)
    closest = [unit for close, unit in ranked_units if close == best]
    return _reason(closest[0]) if len(closest) == 1 else None


@dataclass(frozen=True)
class Example:
    """A case as the prompt shows it: its place's lines at its head, and the reason its comment gives."""

    case: Case
    lines: tuple[str, ...]
    reason: str | None

    def render(self, number: int) -> str:
        case = self.case
        head = EXAMPLE.format(number=number, place=case.place, ruled=RULED[case.verdict])
        rows = [ROW.format(number=n, text=text) for n, text in enumerate(self.lines, start=case.line)]
        why = [] if self.reason is None else [REASON.format(reason=self.reason)]
        return '\n'.join([head, *rows, *why])


class Store(Protocol):
    """The local store, as much of it as an example's lines are read from (`prcheck.Git`)."""

    def has(self, commit: str) -> bool: ...

    def text(self, commit: str, path: str) -> str | None: ...

    def commits(self, base: str, head: str) -> list[Commit]: ...


class Comments(Protocol):
    """The forge, as much of it as an example's reason is read from (`forge.GateForge`)."""

    def comment_at(self, url: str) -> PullComment | None: ...


@dataclass
class Library:
    """Where a run's examples are read from: the store for their lines and the forge for their reasons, each read once."""

    store: Store
    forge: Comments
    _comments: dict[str, PullComment | None] = field(default_factory=dict[str, PullComment | None])

    def prompt(self, gate: str, cases: Sequence[Case], seen: Collection[str], budget: int = BUDGET) -> str:
        """The examples of `gate` drawn from `cases` for a run shown `seen`, as the prompt carries them; empty for none."""
        chosen = select(cases, seen, lambda case: self.example(gate, case), budget)
        if not chosen:
            return ''
        return '\n\n'.join([HEADING, FRAME, *(e.render(n) for n, e in enumerate(chosen, start=1))])

    def example(self, gate: str, case: Case) -> Example | None:
        """`case` as an example of `gate`; None where the store does not hold its lines at its head."""
        held = self._lines(case)
        if held is None:
            log.warning('%s: the store does not hold %s at %s, so it is no example', gate, case.place, case.head[:12])
            return None
        return Example(case, held, self.reason(gate, case))

    def reason(self, gate: str, case: Case) -> str | None:
        """The reason `case`'s comment gives (`cut`); None where it gives none this account wrote."""
        if case.source not in self._comments:
            self._comments[case.source] = self.forge.comment_at(case.source)
        said = self._comments[case.source]
        if said is None:
            log.warning('%s: the forge no longer serves %s, so %s shows no reason', gate, case.source, case.place)
            return None
        if not said.mine:
            log.warning('%s: another account wrote %s, so %s shows no reason', gate, case.source, case.place)
            return None
        return cut(said.body, gate, case)

    def _lines(self, case: Case) -> tuple[str, ...] | None:
        if is_message(case.path):
            sha = case.path.removeprefix(f'{COMMITS}/')
            held = (
                [c.message for c in self.store.commits(f'{sha}^', sha) if c.sha == sha] if self.store.has(sha) else []
            )
            text = held[0] if held else None
        else:
            text = self.store.text(case.head, case.path) if self.store.has(case.head) else None
        if text is None:
            return None
        found = lines(text)
        return tuple(found[case.line - 1 : case.end_line]) if case.end_line <= len(found) else None
