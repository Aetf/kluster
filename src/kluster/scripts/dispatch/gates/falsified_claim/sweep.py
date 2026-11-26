"""The sweep `gate: falsified-claim` draws its items by: every passage at the head that a claim of the change reaches.

A claim is a sentence of a hunk, on either side of it: what the base said
before the change, and what the head says after. A passage is a block of
prose at the head (`gates.prose`). A claim reaches a passage that holds two
of its terms within `WINDOW` characters of each other, in either order,
which is the proximity pattern `a.{0,120}b` of style/README.md, "A sweep
looks for the claim, not the identifier", over whitespace-collapsed text,
run for every pair of the claim's terms.

The terms are the claim's nouns as nearly as words can say: each word of
`SHORTEST` letters or more, in lower case and with its inflection cut
(`term`), an identifier kept whole. A term weighs more the fewer files'
prose holds it (`Index.weight`), since a document's voice varies in its
common words while the repository's own nouns stay fixed: from 1 for a term
one file holds to 0 for one every file does, on a scale of the tree's own
size. A pair reaches a passage where one of its terms weighs `SEED` or more
and the two weigh `THRESHOLD` together. Each pattern the description's
`## Sweep` section names reaches a passage it matches, under `re.S | re.I`.

A hit within the lines the hunk behind it writes at the head is that hunk's
own sentence, and is no item. Every other hit is, one within another hunk of
the change included: a passage the change wrote can still be falsified by
what another part of it does. The items are the `MOST` passages the change
reaches most strongly (`Hit.score`), a description's pattern outweighing
every pair, so a change that touches a common subject draws a bounded
number of items; the extractor logs how many it left out.
"""

from __future__ import annotations

import functools
import logging
import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from ...prcheck import patterns
from ..extract import Change, Item, written_here
from ..prose import Block, blocks, readable

log = logging.getLogger(__name__)

#: A word: letters, digits and underscores, joined by dots or hyphens into one identifier.
_WORD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:[.-][A-Za-z0-9_]+)*')
#: The shortest word that is a term: shorter ones are the language's, not the repository's.
SHORTEST = 4
#: The least one term of a pair weighs for the pair to reach anything. Only a seed's passages are read,
#: so the sweep reads the passages of a claim's rarer terms and never those of the language's words.
SEED = 0.43
#: The least a pair weighs that reaches a passage.
THRESHOLD = 0.76
#: How far apart two terms may stand and still be one claim, in characters, as `.{0,120}`.
WINDOW = 120
#: The most items one change draws.
MOST = 100
#: The longest a passage is drawn whole, in lines. A longer one is narrowed to the lines its hits
#: reach where those fit in as many, and otherwise to the lines its heaviest hit reaches.
LONGEST = 24
#: How many lines either side of its hits a narrowed passage keeps.
AROUND = 2
#: The most hunks an item carries as its context, and the most characters of each side of one.
CONTEXT_HUNKS = 3
CONTEXT_CHARS = 700
#: Where a sentence ends inside a line.
_SENTENCE = re.compile(r'(?<=[.!?;:])\s+')
#: The longest a sentence runs, in characters, before a line break ends it anyway.
_SENTENCE_CHARS = 400
#: The inflections a word's term cuts, the first that fits, and what each leaves in its place.
_INFLECTIONS = (('ies', 'i'), ('ied', 'i'), ('ing', ''), ('ed', ''), ('es', ''), ('s', ''), ('y', 'i'), ('e', ''))


@functools.cache
def term(word: str) -> str:
    """A word as terms are compared: in lower case, an identifier whole and any other word with its inflection cut.

    `carried`, `carries` and `carry` are one term, and so are `rule` and `rules`.
    """
    lowered = word.lower()
    if not lowered.isalpha():
        return lowered
    for ending, left in _INFLECTIONS:
        if lowered.endswith(ending) and not lowered.endswith('ss') and len(lowered) - len(ending) >= 3:
            return lowered[: -len(ending)] + left
    return lowered


def terms(text: str) -> Iterator[tuple[str, int, int]]:
    """Each term of `text`, where it starts and where it ends."""
    for found in _WORD.finditer(text):
        if len(found[0]) >= SHORTEST:
            yield term(found[0]), found.start(), found.end()


@dataclass(frozen=True)
class Hunk:
    """One hunk of the change: its lines at the base and at the head, either side possibly empty."""

    path: str
    base: tuple[int, int] | None
    head: tuple[int, int] | None
    before: str
    after: str

    def writes(self, path: str, first: int, last: int) -> bool:
        """Whether lines `first`–`last` of `path` lie within what this hunk writes at the head."""
        return self.head is not None and path == self.path and self.head[0] <= first and last <= self.head[1]

    def render(self) -> str:
        where = f'{self.path}, lines {_span(self.head)} at the head' if self.head else self.path
        if self.base is not None:
            where += f' (lines {_span(self.base)} at the base)'

        def cut(text: str) -> str:
            return text if len(text) <= CONTEXT_CHARS else f'{text[:CONTEXT_CHARS]} …'

        return f'The change at {where}.\nBefore:\n{cut(self.before) or "(nothing)"}\nAfter:\n{cut(self.after) or "(nothing)"}'


def _span(lines: tuple[int, int] | None) -> str:
    return '' if lines is None else f'{lines[0]}' if lines[0] == lines[1] else f'{lines[0]}–{lines[1]}'


def _range(start: int, count: int) -> tuple[int, int] | None:
    """The first and last line of a side of a hunk; None for a side that holds none."""
    return (start, start + count - 1) if count > 0 else None


def _lines(text: str | None, span: tuple[int, int] | None) -> str:
    if text is None or span is None:
        return ''
    return '\n'.join(text.split('\n')[span[0] - 1 : span[1]])


def hunks(change: Change) -> list[Hunk]:
    """Every hunk of `change` in a file this repository writes (`extract.written_here`), with its text on both sides."""
    found: list[Hunk] = []
    for changed in change.files:
        if not written_here(changed.path):
            continue
        before_text, after_text = change.base_text(changed.path), change.text(changed.path)
        for hunk in changed.hunks:
            base, head = _range(hunk.base_start, hunk.base_count), _range(hunk.head_start, hunk.head_count)
            found.append(Hunk(changed.path, base, head, _lines(before_text, base), _lines(after_text, head)))
    return found


def sentences(text: str) -> Iterator[str]:
    """The sentences of a side of a hunk: ended by a stop, a blank line, or a line once one runs long."""
    run: list[str] = []
    size = 0
    for line in [*text.split('\n'), '']:
        stripped = line.strip()
        if stripped:
            run.append(stripped)
            size += len(stripped)
        if run and (not stripped or stripped[-1] in '.!?;:' or size > _SENTENCE_CHARS):
            yield from (s for s in _SENTENCE.split(' '.join(run)) if s)
            run, size = [], 0


@dataclass(frozen=True)
class Passage:
    """A block of prose at the head, with where each of its terms stands."""

    path: str
    block: Block
    at: dict[str, list[tuple[int, int]]]

    @classmethod
    def of(cls, path: str, block: Block) -> Passage:
        at: dict[str, list[tuple[int, int]]] = {}
        for t, start, end in terms(block.text):
            at.setdefault(t, []).append((start, end))
        return cls(path, block, at)

    def near(self, one: str, other: str) -> tuple[int, int] | None:
        """The span of the first place `one` and `other` stand within `WINDOW` of each other; None where none."""
        for a, b in self.at.get(one, ()):
            for c, d in self.at.get(other, ()):
                if 0 <= c - b <= WINDOW or 0 <= a - d <= WINDOW:
                    return min(a, c), max(b, d)
        return None

    def lines(self, span: tuple[int, int]) -> tuple[int, int]:
        return self.block.line_of(span[0]), self.block.line_of(max(span[0], span[1] - 1))


@dataclass
class Index:
    """The prose of the tree at the head: every passage, which passages hold each term, and how many files do."""

    passages: list[Passage] = field(default_factory=list[Passage])
    holding: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    files: dict[str, int] = field(default_factory=dict[str, int])
    read: int = 0

    @classmethod
    def of(cls, change: Change) -> Index:
        index = cls()
        for path in sorted(change.tree()):
            if not written_here(path) or not readable(path):
                continue
            text = change.text(path)
            if text is None or '\0' in text:
                continue
            index.read += 1
            held: set[str] = set()
            for block in blocks(path, text):
                passage = Passage.of(path, block)
                for t in passage.at:
                    index.holding.setdefault(t, []).append(len(index.passages))
                held |= passage.at.keys()
                index.passages.append(passage)
            for t in held:
                index.files[t] = index.files.get(t, 0) + 1
        return index

    def weight(self, t: str) -> float:
        """How much `t` says: the log of how many files' prose there is over how many hold it, over the log of the first.

        1 for a term one file holds, 0 for one every file holds or none does.
        """
        held, read = self.files.get(t, 0), self.read
        if not held or read < 2:
            return 0.0
        return math.log(read / held) / math.log(read)


@dataclass
class Hit:
    """A passage something reached: how heavily, where the heaviest reached it, and what reached it."""

    passage: Passage
    weight: float = 0.0
    heaviest: float = 0.0
    #: The lines the heaviest pair reached, and every line any hit reached, which a long passage is narrowed to.
    focus: tuple[int, int] = (0, 0)
    span: tuple[int, int] = (0, 0)
    hunks: list[Hunk] = field(default_factory=list[Hunk])
    reached: set[str] = field(default_factory=set[str])

    def add(self, lines: tuple[int, int], weight: float, reached: str, behind: Sequence[Hunk]) -> None:
        self.span = lines if not self.reached else (min(self.span[0], lines[0]), max(self.span[1], lines[1]))
        if reached not in self.reached:
            if weight > self.heaviest:
                self.heaviest, self.focus = weight, lines
            self.weight += weight
            self.reached.add(reached)
        self.hunks += [h for h in behind if h not in self.hunks]

    @property
    def score(self) -> float:
        """How strongly the change reaches the passage: its heaviest pair, and the root of what every pair weighs."""
        return self.heaviest + math.sqrt(self.weight)

    def item(self, lines: Sequence[str]) -> Item:
        block = self.passage.block
        first, last = block.first, block.last
        if last - first + 1 > LONGEST:
            around = self.span if self.span[1] - self.span[0] + 1 + 2 * AROUND <= LONGEST else self.focus
            first, last = max(first, around[0] - AROUND), min(last, around[1] + AROUND)
        reached = ', '.join(sorted(self.reached))
        context = '\n\n'.join([f'Reached by: {reached}', *(h.render() for h in self.hunks[:CONTEXT_HUNKS])])
        return Item(self.passage.path, first, last, '\n'.join(lines[first - 1 : last]), context)


def _claims(index: Index, hunk: Hunk, hits: dict[int, Hit]) -> None:
    """Record every passage a sentence of `hunk` reaches."""
    for sentence in sorted({s for side in (hunk.before, hunk.after) for s in sentences(side)}):
        held = {t for t, _, _ in terms(sentence) if t in index.files}
        seeds = sorted(t for t in held if index.weight(t) >= SEED)
        reached: dict[int, tuple[float, tuple[int, int], str]] = {}
        for seed in seeds:
            for at in index.holding.get(seed, ()):
                passage = index.passages[at]
                for other in sorted(held & passage.at.keys()):
                    if other == seed:
                        continue
                    weight = index.weight(seed) + index.weight(other)
                    if weight < THRESHOLD or weight <= reached.get(at, (0.0,))[0]:
                        continue
                    span = passage.near(seed, other)
                    if span is not None:
                        reached[at] = (weight, span, f'{min(seed, other)} … {max(seed, other)}')
        for at, (weight, span, pair) in reached.items():
            passage = index.passages[at]
            lines = passage.lines(span)
            if not hunk.writes(passage.path, *lines):
                hits.setdefault(at, Hit(passage)).add(lines, weight, pair, [hunk])


def extract(change: Change) -> list[Item]:
    """Every passage at the head outside its own hunk that a claim of `change`, or a pattern of its description, reaches."""
    index = Index.of(change)
    log.info('sweeping %d passages of prose in %d files at %s', len(index.passages), index.read, change.head[:12])
    found = hunks(change)
    hits: dict[int, Hit] = {}
    for hunk in found:
        _claims(index, hunk, hits)
    described = math.inf
    for named in patterns(change.body):
        try:
            compiled = re.compile(named.text, re.S | re.I)
        except re.error:
            continue
        behind = [h for h in found if compiled.search(' '.join(f'{h.before} {h.after}'.split()))]
        for at, passage in enumerate(index.passages):
            match = compiled.search(passage.block.text)
            if match is None:
                continue
            lines = passage.lines(match.span())
            if not any(h.writes(passage.path, *lines) for h in behind):
                hits.setdefault(at, Hit(passage)).add(lines, described, f"the description's `{named.text}`", behind)
    ranked = sorted(hits.values(), key=lambda h: (-h.score, h.passage.path, h.passage.block.first))
    if len(ranked) > MOST:
        log.info('%d passages reached; drawing the %d the heaviest claims reach', len(ranked), MOST)
    texts: dict[str, list[str]] = {}
    items: list[Item] = []
    for hit in sorted(ranked[:MOST], key=lambda h: (h.passage.path, h.passage.block.first)):
        lines = texts.setdefault(hit.passage.path, (change.text(hit.passage.path) or '').split('\n'))
        items.append(hit.item(lines))
    return items
