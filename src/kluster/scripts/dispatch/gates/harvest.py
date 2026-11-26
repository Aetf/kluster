"""`dispatch gates harvest <pr>...`: the places the reviews and rulings of pull requests tag, recorded in each gate's history.

A review rules on a gate's family of findings with a tag beside the place
it names (dispatch.md §3):

-   `[gate:<name>]` on a finding: the place breaks the rule `<name>` asks
    about, so the case is a finding the gate should make. A rejected appeal
    is tagged this way.
-   `[gate:<name> clean]` on a place the review holds to the rule in so many
    words: the case is a pass, and a gate finding there is false. An
    accepted appeal is tagged this way.
-   either with `operator` last, `[gate:<name> operator]` or
    `[gate:<name> clean operator]`: the ruling is the operator's own, which
    the dispatcher writes only when it relays the operator's word, and which
    no other ruling at its place replaces (`cases.record`).

Only a comment this account wrote is read (anyone else's comment is data,
dispatch.md §1, and a tag planted in one would teach a gate what its author
liked), and only a review, whose first line is a review's heading as
`dispatch merge` reads one (`merge.REVIEW_HEADING`, dispatch.md §3), or a
ruling, whose first line is `## Ruling` alone. A tag binds to the places its unit names (`units`): a list item with
the paragraphs indented under it, or any other line. So the tag goes on the
line, or in the list item, that names the place, and the place of a rule it
cites stays out of it. A tag in a unit that names no place binds to nothing,
and is reported; so is a unit in which one gate's tags disagree, and text
that opens like a tag, `[gate` in any case, and does not read as one. A
place is named one of two ways:

-   **at the comment's own pull request**, by a path, `path:line` or
    `path:first–last`, the path holding a letter. The head is the first
    commit a review names by twelve characters or more that the local store
    holds, outside the places it names at another head, and the change is
    that head against its merge base with the pull request's base, as
    `dispatch gates` reads it. A ruling names no head of its own, so a
    place it names this way binds to nothing, and is reported.
-   **at a pull request and head it names**, in the form `calibrate`'s
    refusal prints a finding no case confirms, `#<pr> <path>:<lines> at
    <head>`, the head in full and the path any run of characters but spaces.
    The case is that pull request's at that head, wherever the comment is,
    so a finding of a calibration is ruled on in the calibration pull
    request. The head is a commit of that pull request (`_Reader.belongs`),
    or the place is reported and makes no case.

Either way the place binds by a path the gate is shown at that head
(`view.parts`), or the one such path that ends with it. A place in a
commit's message, `:commit/<sha>:<line>` (`change.message_path`), binds to
the one commit of the change whose SHA opens with the one it names, by
seven characters or more, where the gate is shown that message. A place the
gate is not shown at that head, or a line its file does not hold there, is
reported and makes no case, since a gate is never asked about it and so
could not be measured on it.

The case is the place and the verdict, with the comment as its source and
the time the comment was written (`cases`). A comment that rules one place
two ways rules it neither way, and says so. Each case is recorded at its
place (`cases.record`), where the ruling written later stands, the
operator's above any: a ruling that replaces another, or that an operator's
keeps out, is reported with both, and harvesting rulings the history
already reflects writes nothing and reports no ruling.
Where a review's pull request carries the findings comment of the review's
head, each case at that head is paired with the gate's entries there, the
places its samples found, and a place of a file the gate was shown where no
finding of its run stands near it is one it passed; a disagreement is
reported, and so is each finding the gate made that the review left
untagged, which a tag then makes a case. Where it carries none, that is
reported too: nothing was paired. Every report is a `Note`, by its `Kind`.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from ..merge import REVIEW_HEADING
from ..prcheck import Git, PullRequest
from . import comment, view
from .cases import Case, record
from .change import COMMITS, Change, change, is_message, message_path
from .comment import Evidence
from .forge import GateForge, PullComment
from .gate import CASES, Gate
from .run import findings_comment
from .verdict import Verdict

log = logging.getLogger(__name__)

#: A ruling's first line. A review's is `merge.REVIEW_HEADING`.
RULING = re.compile(r'\A## Ruling[ \t]*$', re.M)
#: A commit a review names, by twelve characters or more.
SHA = re.compile(r'(?<![0-9a-f])[0-9a-f]{12,40}(?![0-9a-f])')
#: A tag: the gate, whether it holds the place clean, and whether the ruling is the operator's.
TAG = re.compile(r'\[gate:(?P<gate>[a-z0-9-]+)(?P<clean> clean)?(?P<operator> operator)?\]')
#: Where a tag opens, in any case: `[gate` and then a colon or a space. Each is a tag or is reported as one that
#: does not read; a link to `gateway.md` or a subscript `Sequence[Gate]` opens none.
TAG_OPENING = re.compile(r'\[gate(?=[ \t]*:|[ \t])', re.I)
#: A place at the comment's own head: a path, which holds a letter, or a commit's message by its SHA; a line; and
#: a last line after a dash.
PLACE = re.compile(
    rf'(?<![\w/.-])(?P<path>{COMMITS}/[0-9a-f]{{7,40}}|(?=[\w./-]*[A-Za-z])[\w.-][\w./-]*)'
    r':(?P<line>\d+)(?:[ \t]*[–-][ \t]*(?P<end>\d+))?'
)
#: A place at a pull request and head it names, as `calibrate` prints an unconfirmed finding: the path is any
#: run of characters but spaces, bounded by the pull request ahead of it and the head after it.
AT = re.compile(
    r'#(?P<pr>\d+)[ \t]+`?(?P<path>\S+?):(?P<line>\d+)(?:[–-](?P<end>\d+))?`?'
    r'[ \t]+at[ \t]+(?P<head>[0-9a-f]{40})(?![0-9a-f])'
)
#: Where a list item opens, at any depth, or a heading.
_OPENS = re.compile(r'^[ \t]*(?:[-*+]|\d+[.)])[ \t]|^#{1,6}[ \t]')


class HarvestGit(Git):
    """The local store, with a commit found by its prefix."""

    def resolve(self, prefix: str) -> str | None:
        """The full SHA of the commit `prefix` names; None where the store holds no one such commit."""
        done = self._run('rev-parse', '--verify', '--quiet', f'{prefix}^{{commit}}', ok=(0, 1, 128))
        return done.stdout.decode().strip() if done.returncode == 0 else None


class Kind(enum.StrEnum):
    """What a note of a harvest reports."""

    #: The pull request holds no comment this account wrote that harvest reads.
    UNREAD = 'unread'
    #: A review that names no commit the local store holds, or a ruling's place that names no head.
    NO_HEAD = 'no head'
    #: A review whose head carries no findings comment to pair it against.
    UNPAIRED = 'unpaired'
    #: A tag naming no gate there is.
    NO_GATE = 'no gate'
    #: Text that opens like a tag and does not read as one.
    MALFORMED = 'malformed'
    #: A unit, or a comment's places, at which one gate's tags give two rulings.
    AMBIGUOUS = 'ambiguous'
    #: A place named at a head that is no commit of the pull request it names.
    FOREIGN = 'foreign'
    #: A tag in a unit that names no place (`units`).
    NO_PLACE = 'no place'
    #: A place the gate is not shown at that head, or a line its file does not hold there.
    NOT_SHOWN = 'not shown'
    #: A place the gate's samples left undecided.
    UNDECIDED = 'undecided'
    #: A place the gate judged otherwise than the ruling.
    DISAGREES = 'disagrees'
    #: A finding of the gate the review tagged neither way.
    UNTAGGED = 'untagged'
    #: A ruling that replaced the case held at its place.
    REPLACED = 'replaced'
    #: A ruling written after the operator's case at its place, which keeps it out.
    KEPT = 'kept'


@dataclass(frozen=True)
class Note:
    """One thing a harvest found and reports: what kind, the comment or pull request it is about, at what place."""

    kind: Kind
    #: The comment's URL, or `#<pr>`.
    source: str
    text: str
    #: The place, `path:lines`, where the note is about one.
    place: str = ''

    def __str__(self) -> str:
        return f'{self.source}: {self.text}'


@dataclass
class Harvested:
    """What a harvest appended to each gate's history, and what it found that it did not append."""

    appended: dict[str, list[Case]] = field(default_factory=dict[str, list[Case]])
    notes: list[Note] = field(default_factory=list[Note])


@dataclass(frozen=True)
class Named:
    """A place as a comment names it: a path, its first line and its last, and the pull request and head it gives."""

    path: str
    line: int
    end_line: int
    pr: int | None = None
    head: str | None = None


@dataclass(frozen=True)
class Tagged:
    """One tag of a comment, with every place its unit names (`units`)."""

    gate: str
    verdict: Verdict
    operator: bool
    places: tuple[Named, ...]


def units(text: str) -> Iterator[str]:
    """What a tag in `text` binds within: a list item with its indented paragraphs, or any other line alone.

    An item nested under another is a unit of its own, and ends its parent's.
    """
    block: list[str] = []
    item = False
    gap = False

    def done() -> Iterator[str]:
        if item:
            yield ' '.join(block)
        else:
            yield from block

    for line in text.split('\n'):
        if not line.strip():
            gap = bool(block)
            continue
        opens = _OPENS.match(line) is not None
        continues = not gap or (item and line[:1] in (' ', '\t'))
        if block and (opens or not continues):
            yield from done()
            block = []
        if not block:
            item = opens and not line.lstrip().startswith('#')
        block.append(line.strip())
        gap = False
    if block:
        yield from done()


def _named(match: re.Match[str], *, at: bool) -> Named:
    line = int(match['line'])
    end = int(match['end'] or line)
    if at:
        return Named(match['path'].strip('`'), line, end, int(match['pr']), match['head'])
    return Named(match['path'], line, end)


@dataclass(frozen=True)
class Read:
    """A comment's tags, with the places each binds, and each unit whose tags of one gate disagree."""

    tagged: list[Tagged]
    #: What makes a unit ambiguous, and its text, which binds none of its tags.
    ambiguous: list[tuple[str, str]]
    #: Each text that opens like a tag and is none.
    malformed: list[str]


def tags(text: str) -> Read:
    """Every tag of `text`, with the places its unit names (`units`); a tag whose unit names none has none.

    A unit in which one gate's tags give two rulings, two verdicts or one the
    operator's and one not, binds none of them, and so does one that names
    several places with the tags of several gates beside them: nothing says
    which ruling is which place's. One place takes every gate's tag beside it.
    """
    tagged: list[Tagged] = []
    ambiguous: list[tuple[str, str]] = []
    for unit in units(text):
        at = list(AT.finditer(unit))
        spans = [m.span() for m in at]
        plain = [m for m in PLACE.finditer(unit) if not any(a <= m.start() < b for a, b in spans)]
        places = tuple(sorted([_named(m, at=True) for m in at] + [_named(m, at=False) for m in plain], key=_order))
        rulings: dict[str, set[tuple[Verdict, bool]]] = {}
        for tag in TAG.finditer(unit):
            ruled = (Verdict.PASS if tag['clean'] else Verdict.FINDING, tag['operator'] is not None)
            rulings.setdefault(tag['gate'], set()).add(ruled)
        if len(places) > 1 and len(rulings) > 1:
            ambiguous.append((f'tags of {", ".join(sorted(rulings))} beside its places', unit))
            continue
        for gate, ruled in rulings.items():
            if len(ruled) > 1:
                ambiguous.append((f'[gate:{gate}] tags that disagree', unit))
                continue
            ((verdict, operator),) = ruled
            tagged.append(Tagged(gate, verdict, operator, places))
    malformed = [_opening(text, m.start()) for m in TAG_OPENING.finditer(text) if TAG.match(text, m.start()) is None]
    return Read(tagged, ambiguous, malformed)


def _opening(text: str, start: int) -> str:
    """The text a tag opening at `start` runs to: its closing bracket, or the end of its line."""
    end = min(i for i in (text.find(']', start), text.find('\n', start), len(text)) if i >= 0)
    return text[start : end + 1 if end < len(text) and text[end] == ']' else end]


def _order(named: Named) -> tuple[str, int, int]:
    return (named.path, named.line, named.end_line)


def _path(named: str, paths: Sequence[str]) -> str | None:
    if named in paths:
        return named
    ending = [p for p in paths if p.endswith(f'/{named}')]
    return ending[0] if len(ending) == 1 else None


def _message(named: str, changed: Change) -> str | None:
    """The place of the message of the one commit of `changed` whose SHA opens with the one `named` gives."""
    prefix = named.removeprefix(f'{COMMITS}/')
    found = [message_path(c.sha) for c in changed.commits if c.sha.startswith(prefix)]
    return found[0] if len(found) == 1 else None


def _pair(tagged: Case, gate: str, evidence: Evidence | None, source: str) -> Note | None:
    """The disagreement between a review's case and the gate's verdict at the same head; None where none.

    The gate found the place where a finding of its run stands near it
    (`cases.Case.near`), which is how `calibrate` matches them, and otherwise
    passed it, in a record of `comment.VERSION` 2 or later where the gate
    asked; in an earlier one, whose entries are the items it asked, a place
    no entry is near was not asked. A place the gate's samples left undecided
    is no verdict to disagree with, and is reported as one.
    """
    ran = next((r for r in evidence.gates if r.gate == gate), None) if evidence else None
    if ran is None or evidence is None:
        return None
    entries = [e for e in ran.entries if tagged.near(e.path, e.line, e.end_line)]
    if not entries and not (evidence.version >= 2 and ran.asked):
        return None
    if any(e.finding for e in entries):
        said = Verdict.FINDING
    elif any(e.undecided for e in entries):
        why = f'{gate} gave no verdict at {tagged.place}, which the review tags {tagged.verdict}'
        return Note(Kind.UNDECIDED, source, why, tagged.place)
    else:
        said = Verdict.PASS
    if said is tagged.verdict:
        return None
    why = f'{gate} disagrees with the review at {tagged.place}: the gate said {said}, the review {tagged.verdict}'
    return Note(Kind.DISAGREES, source, why, tagged.place)


@dataclass
class _Reader:
    """What a harvest reads its comments against: the forge, the store, the gates, and each change it has read."""

    forge: GateForge
    git: HarvestGit
    gates: Sequence[Gate]
    _changes: dict[tuple[int, str], Change] = field(default_factory=dict[tuple[int, str], Change])
    _shown: dict[tuple[str, str], dict[str, view.Part]] = field(
        default_factory=dict[tuple[str, str], dict[str, view.Part]]
    )
    _heads: dict[int, set[str]] = field(default_factory=dict[int, set[str]])

    def change(self, pr: int, head: str, pull: PullRequest | None = None) -> Change:
        """The change of pull request `pr` at `head`, read once."""
        if (pr, head) not in self._changes:
            base = (pull or self.forge.pull_request(pr)).base
            self._changes[(pr, head)] = change(self.git, self.git.merge_base(base, head), head)
        return self._changes[(pr, head)]

    def belongs(self, gate: Gate, pr: int, head: str) -> bool:
        """Whether `head` is a commit of pull request `pr`.

        It is where the gate's history already holds a case of `pr` at
        `head`, the run a calibration measured, which is where every finding
        it asks a ruling on lies; and otherwise where the forge says so: a
        commit the pull request holds now, or the head a comment this account
        wrote on it was written against, the head a review read or the head a
        findings comment ran at. A commit a review cites in its prose is none.
        """
        if any((c.pr, c.head) == (pr, head) for c in gate.cases()):
            return True
        if pr not in self._heads:
            held = set(self.forge.pull_commits(pr))
            for said in (c for c in self.forge.pull_comments(pr) if c.mine):
                ran = comment.head_of(said.body)
                own = self.own(said.body) if ran is None and _read(said.body) is False else None
                held |= {sha for sha in (ran, own) if sha is not None}
            self._heads[pr] = held
        return head in self._heads[pr]

    def own(self, body: str) -> str | None:
        """A review's own head: the first commit it names that the local store holds, outside its `at` places."""
        return next((full for full in map(self.git.resolve, _own_shas(body)) if full is not None), None)

    def shown(self, gate: Gate, changed: Change) -> dict[str, view.Part]:
        """What `gate` is shown of `changed`, by path, read once."""
        if (gate.name, changed.head) not in self._shown:
            self._shown[(gate.name, changed.head)] = {p.path: p for p in view.parts(changed, gate.spec.trigger)}
        return self._shown[(gate.name, changed.head)]


def _read(body: str) -> bool | None:
    """Whether a comment harvest reads is a ruling, rather than a review; None for a comment it does not read."""
    opening = body.lstrip()
    if RULING.match(opening):
        return True
    return False if REVIEW_HEADING.match(opening) else None


def _own_shas(body: str) -> list[str]:
    """The commits a comment names by twelve characters or more, outside the places it names at another head."""
    outside = AT.sub(' ', body)
    return SHA.findall(outside)


def _comment(
    said: PullComment, ruling: bool, pull: PullRequest, reader: _Reader, held: Sequence[PullComment]
) -> tuple[dict[str, list[Case]], list[Note]]:
    """The cases one comment rules, by gate, and its notes."""
    notes: list[Note] = []
    url = said.url
    own = None if ruling else reader.own(said.body)
    if not ruling and own is None:
        notes.append(Note(Kind.NO_HEAD, url, 'names no commit the local store holds; fetch the head it read'))
    found = findings_comment(held, own) if own is not None else None
    evidence = comment.parse(found.body) if found else None
    if own is not None and found is None:
        notes.append(Note(Kind.UNPAIRED, url, f'no findings comment of {own[:12]} to pair the review against'))
    by_name = {g.name: g for g in reader.gates}
    drawn: dict[str, list[Case]] = {}
    read = tags(said.body)
    notes += [Note(Kind.MALFORMED, url, f'{text} opens like a tag and does not read as one') for text in read.malformed]
    notes += [Note(Kind.AMBIGUOUS, url, f'one unit holds {why}, so none binds: {unit}') for why, unit in read.ambiguous]
    for tagged in read.tagged:
        gate = by_name.get(tagged.gate)
        if gate is None:
            notes.append(Note(Kind.NO_GATE, url, f'[gate:{tagged.gate}] names no gate'))
            continue
        if not tagged.places:
            notes.append(
                Note(
                    Kind.NO_PLACE,
                    url,
                    f'a [gate:{tagged.gate}] tag sits in a unit that names no path:line; '
                    'put it on the line, or in the list item, that names the place',
                )
            )
            continue
        for named in tagged.places:
            if named.pr is not None and named.head is not None:
                if reader.git.resolve(named.head) is None:
                    why = f'#{named.pr} {named.path}:{named.line} names {named.head}, which the local store lacks'
                    notes.append(Note(Kind.NO_HEAD, url, f'{why}; fetch it', named.path))
                    continue
                if not reader.belongs(gate, named.pr, named.head):
                    why = f'#{named.pr} {named.path}:{named.line} names {named.head[:12]}, no commit of #{named.pr}'
                    notes.append(Note(Kind.FOREIGN, url, why, named.path))
                    continue
                at: tuple[int, str] = (named.pr, named.head)
                changed = reader.change(named.pr, named.head, pull if named.pr == pull.number else None)
            elif own is not None:
                at = (pull.number, own)
                changed = reader.change(pull.number, own, pull)
            else:
                # A review that names no head is reported once, above.
                if ruling:
                    why = (
                        f'{named.path}:{named.line} is at no head: a ruling names the pull request and head of a place'
                    )
                    notes.append(Note(Kind.NO_HEAD, url, why, named.path))
                continue
            parts = reader.shown(gate, changed)
            path = _message(named.path, changed) if is_message(named.path) else _path(named.path, list(parts))
            part = parts.get(path) if path is not None else None
            if part is None:
                why = (
                    f'{gate.name} is not shown {named.path} at {at[1][:12]}, or several paths end with it, '
                    'so it makes no case; not appended'
                )
                notes.append(Note(Kind.NOT_SHOWN, url, why, named.path))
                continue
            if not part.shows(named.line):
                why = (
                    f'{part.path} holds lines 1–{part.last} at {at[1][:12]}, not {named.line}, '
                    'so it makes no case; not appended'
                )
                notes.append(Note(Kind.NOT_SHOWN, url, why, part.path))
                continue
            case = Case(
                pr=at[0],
                head=at[1],
                path=part.path,
                line=named.line,
                end_line=min(max(named.end_line, named.line), part.last),
                verdict=tagged.verdict,
                source=url,
                written=said.created,
                operator=tagged.operator,
            )
            drawn.setdefault(gate.name, []).append(case)
            if at == (pull.number, own):
                disagreement = _pair(case, gate.name, evidence, url)
                if disagreement is not None:
                    notes.append(disagreement)
    for name, ruled in list(drawn.items()):
        verdicts: dict[tuple[int, str, str, int, int], set[tuple[Verdict, bool]]] = {}
        for case in ruled:
            verdicts.setdefault(case.key, set()).add((case.verdict, case.operator))
        split = {key for key, given in verdicts.items() if len(given) > 1}
        notes += [
            Note(
                Kind.AMBIGUOUS,
                url,
                f'its [gate:{name}] tags rule #{key[0]} {path}:{key[3]} two ways, so none binds',
                path,
            )
            for key in sorted(split)
            for path in [key[2]]
        ]
        drawn[name] = [c for c in ruled if c.key not in split]
    for ran in evidence.gates if evidence else ():
        here = [c for c in drawn.get(ran.gate, []) if (c.pr, c.head) == (pull.number, own)]
        notes += [
            Note(
                Kind.UNTAGGED,
                url,
                f'{ran.gate} found {e.place} and the review tags it neither way; '
                f'[gate:{ran.gate}] or [gate:{ran.gate} clean] there makes it a case',
                e.place,
            )
            for e in ran.findings
            if not any(c.near(e.path, e.line, e.end_line) for c in here)
        ]
    return drawn, notes


def _recorded(gate: Gate, new: Sequence[Case], harvested: Harvested) -> None:
    done = record(gate.directory / CASES, new)
    harvested.appended.setdefault(gate.name, []).extend(done.appended)
    for old, case in done.replaced:
        why = (
            f'{gate.name} at {old.place} of #{old.pr}: {case.verdict} from {case.source} replaces '
            f'{old.verdict} from {old.source}'
        )
        harvested.notes.append(Note(Kind.REPLACED, case.source, why, old.place))
    for held, case in done.kept:
        why = (
            f"{gate.name} at {held.place} of #{held.pr}: the operator's {held.verdict} from {held.source} "
            f'stands, and {case.verdict} from {case.source}, written after it, is not recorded'
        )
        harvested.notes.append(Note(Kind.KEPT, case.source, why, held.place))


def harvest(forge: GateForge, git: HarvestGit, numbers: Sequence[int], gates: Sequence[Gate]) -> Harvested:
    """Record every case the reviews and rulings on pull requests `numbers` tag in its gate's history."""
    harvested = Harvested()
    by_name = {g.name: g for g in gates}
    reader = _Reader(forge, git, gates)
    for number in numbers:
        log.info('reading the reviews and rulings of #%d', number)
        pull = forge.pull_request(number)
        comments = forge.pull_comments(number)
        read = [(c, ruling) for c in comments if c.mine and (ruling := _read(c.body)) is not None]
        if not read:
            harvested.notes.append(Note(Kind.UNREAD, f'#{number}', 'has no review or ruling this account wrote'))
        for said, ruling in read:
            drawn, notes = _comment(said, ruling, pull, reader, comments)
            harvested.notes += notes
            for name, new in drawn.items():
                _recorded(by_name[name], new, harvested)
    return harvested
