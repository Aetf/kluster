"""`dispatch gates harvest <pr>...`: labelled cases from the reviews of pull requests, appended to each gate's set.

A review rules on a gate's family of findings with a tag beside the place
it names (dispatch.md §3):

-   `[gate:<name>]` on a finding: the place breaks the rule `<name>` asks
    about, so the case is a positive. A rejected appeal is tagged this way.
-   `[gate:<name> clean]` on a place the review holds to the rule in so many
    words: the case is a negative. An accepted appeal is tagged this way.

Only a review this account wrote is read, a comment whose first line is a
heading opening `Review`: anyone else's comment is data (dispatch.md §1),
and a tag planted in one would teach a gate what its author liked. The
review's head is the first commit it names by twelve characters or more
that the local store holds. A tag binds to every place its paragraph names,
a list item and the indented paragraphs under it being one paragraph, by a
path the change touches, or the one changed path that ends with it; failing
both, by a path the head tracks, or the one tracked path that ends with it,
since a passage a change made false can sit in a file the change never
touched. A place in a commit's message, `:commit/<sha>:<line>`
(`extract.message_path`), binds to the one commit of the change whose SHA
opens with the one it names, by seven characters or more. So the reviewer
puts the tag in the paragraph that names the place,
and keeps out of it the place of a rule it cites. A tag in a paragraph that names no
place binds to nothing, and is reported.

The case is the item the gate's own extractor draws at that place at that
head, since a case holds an item's text (`cases`). A place the extractor
draws no item at is reported and not appended. An extractor that raises at
that head draws nothing there (`run.drawn`), and that is reported too, the
other pull requests of the harvest going on. Where the pull request carries
the findings comment of the same head, each case is paired with the gate's
verdict there, which is a pass at a place of a file the gate was shown
(`view.parts`) where no entry of its run lies; a disagreement is reported,
and so is each finding the gate made that the review left untagged, which a
tag then makes a case. Where it carries none, that is reported too: nothing
was paired.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field

from ..prcheck import Git, PullRequest
from . import comment, run, view
from .cases import Case
from .cases import append as append_cases
from .comment import Evidence
from .extract import COMMITS, Change, Item, change, is_message, message_path
from .forge import GateForge, PullComment
from .gate import CASES, Gate
from .run import ExtractorFailed, findings_comment
from .verdict import Verdict

log = logging.getLogger(__name__)

#: A review's first line: a heading whose text opens with `Review`.
REVIEW = re.compile(r'\A#{1,6}[ \t]+Review\b')
#: A commit a review names, by twelve characters or more.
SHA = re.compile(r'(?<![0-9a-f])[0-9a-f]{12,40}(?![0-9a-f])')
#: A review's tag: the gate, and whether it holds the place clean.
TAG = re.compile(r'\[gate:(?P<gate>[a-z0-9-]+)(?P<clean> clean)?\]')
#: A place a review names: a path with an extension, or a commit's message by its SHA; a line; and a last
#: line after a dash.
PLACE = re.compile(
    rf'(?<![\w/.-])(?P<path>{COMMITS}/[0-9a-f]{{7,40}}|[\w.-][\w./-]*\.[A-Za-z0-9]+)'
    r':(?P<line>\d+)(?:[ \t]*[–-][ \t]*(?P<end>\d+))?'
)
#: Where a paragraph or a top-level list item opens.
_OPENS = re.compile(r'^ {0,1}(?:[-*+]|\d+[.)])[ \t]|^#{1,6}[ \t]')


class HarvestGit(Git):
    """The local store, with a commit found by its prefix."""

    def resolve(self, prefix: str) -> str | None:
        """The full SHA of the commit `prefix` names; None where the store holds no one such commit."""
        done = self._run('rev-parse', '--verify', '--quiet', f'{prefix}^{{commit}}', ok=(0, 1, 128))
        return done.stdout.decode().strip() if done.returncode == 0 else None


@dataclass
class Harvested:
    """What a harvest appended to each gate's set, and what it found that it did not append."""

    appended: dict[str, list[Case]] = field(default_factory=dict[str, list[Case]])
    notes: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Tagged:
    """One tag of a review, with every place its paragraph names."""

    gate: str
    verdict: Verdict
    #: Each place, as the review names it: a path, its first line and its last.
    places: tuple[tuple[str, int, int], ...]


def blocks(text: str) -> Iterator[str]:
    """`text`'s paragraphs, a list item with its indented paragraphs as one, each with its lines joined."""
    block: list[str] = []
    item = False
    gap = False
    for line in text.split('\n'):
        if not line.strip():
            gap = bool(block)
            continue
        opens = _OPENS.match(line) is not None
        continues = not gap or (item and line[:1] in (' ', '\t'))
        if block and (opens or not continues):
            yield ' '.join(block)
            block = []
        if not block:
            item = opens and not line.lstrip().startswith('#')
        block.append(line.strip())
        gap = False
    if block:
        yield ' '.join(block)


def tags(review: str) -> list[Tagged]:
    """Every tag of `review`, with the places its paragraph names; a tag whose paragraph names none has none."""
    found: list[Tagged] = []
    for block in blocks(review):
        places = tuple((p['path'], int(p['line']), int(p['end'] or p['line'])) for p in PLACE.finditer(block))
        found += [
            Tagged(tag['gate'], Verdict.PASS if tag['clean'] else Verdict.FINDING, places)
            for tag in TAG.finditer(block)
        ]
    return found


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


def _pair(tagged: Case, gate: str, evidence: Evidence | None, shown: Collection[str]) -> str | None:
    """The disagreement between a review's case and the gate's verdict at the same head; None where none.

    A place of a path in `shown` that no entry of a run that asked overlaps
    is one the gate passed, in a record of `comment.VERSION` 2 or later; in
    an earlier one, whose entries are the items it asked, it was not asked.
    A place the gate's samples left undecided is no verdict to disagree with,
    and is reported as one.
    """
    ran = next((r for r in evidence.gates if r.gate == gate), None) if evidence else None
    if ran is None:
        return None
    entry = next((e for e in ran.entries if e.path == tagged.path and e.overlaps(tagged.line, tagged.end_line)), None)
    passes = evidence is not None and evidence.version >= 2 and ran.asked and tagged.path in shown
    if entry is None and not passes:
        return None
    if entry is not None and entry.undecided:
        return f'{gate} gave no verdict at {tagged.item.place}, which the review tags {tagged.verdict}'
    said = Verdict.FINDING if entry is not None and entry.finding else Verdict.PASS
    if said is tagged.verdict:
        return None
    return f'{gate} disagrees with the review at {tagged.item.place}: the gate said {said}, the review {tagged.verdict}'


def _review(
    review: PullComment,
    pull: PullRequest,
    git: HarvestGit,
    gates: Sequence[Gate],
    held: Sequence[PullComment],
) -> tuple[dict[str, list[Case]], list[str]]:
    number = pull.number
    notes: list[str] = []
    head = next((full for sha in SHA.findall(review.body) if (full := git.resolve(sha)) is not None), None)
    if head is None:
        return {}, [f'{review.url}: names no commit the local store holds; fetch the head it read']
    changed: Change = change(git, git.merge_base(pull.base, head), head, pull.body)
    by_name = {g.name: g for g in gates}
    found = findings_comment(held, head)
    evidence = comment.parse(found.body) if found else None
    drawn: dict[str, list[Case]] = {}
    # Each gate's items at the head, drawn once for the review however many places it tags: or why it drew none.
    extracted: dict[str, list[Item] | ExtractorFailed] = {}
    # Each gate's paths the run at the head was shown, read once for the review.
    shown: dict[str, set[str]] = {}
    if found is None:
        notes.append(f'{review.url}: no findings comment of {head[:12]} to pair the review against')
    for tagged in tags(review.body):
        gate = by_name.get(tagged.gate)
        if gate is None:
            notes.append(f'{review.url}: [gate:{tagged.gate}] names no gate')
            continue
        if not tagged.places:
            notes.append(
                f'{review.url}: a [gate:{tagged.gate}] tag sits in a paragraph that names no path:line; '
                'put it in the paragraph that names the place'
            )
            continue
        for named, line, end_line in tagged.places:
            if is_message(named):
                path = _message(named, changed)
                missing = f'{named} names no commit of the change, or several'
            else:
                path = _path(named, changed.paths) or _path(named, changed.tree())
                missing = f'{named} is no path the tree at {head[:12]} holds, or names several'
            if path is None:
                notes.append(f'{review.url}: {missing}')
                continue
            if gate.name not in extracted:
                try:
                    extracted[gate.name] = run.drawn(gate, changed)
                except ExtractorFailed as exc:
                    extracted[gate.name] = exc
                    notes.append(f'{review.url}: {gate.name} drew nothing at {head[:12]}: {exc}; not appended')
            drawn_here = extracted[gate.name]
            if isinstance(drawn_here, ExtractorFailed):
                continue
            items = [i for i in drawn_here if i.path == path and i.overlaps(line, end_line)]
            if not items:
                notes.append(
                    f'{review.url}: {gate.name} draws no item at {path}:{line}, so it makes no case; not appended'
                )
                continue
            for item in items:
                case = Case(
                    pr=number,
                    head=head,
                    path=item.path,
                    line=item.line,
                    end_line=item.end_line,
                    verdict=tagged.verdict,
                    source=review.url,
                    text=item.text,
                    context=item.context,
                )
                drawn.setdefault(gate.name, []).append(case)
                if gate.name not in shown:
                    shown[gate.name] = {part.path for part in view.parts(changed, gate.spec.trigger)}
                disagreement = _pair(case, gate.name, evidence, shown[gate.name])
                if disagreement is not None:
                    notes.append(f'{review.url}: {disagreement}')
    for ran in evidence.gates if evidence else ():
        tagged_here = drawn.get(ran.gate, [])
        notes += [
            f'{review.url}: {ran.gate} found {e.place} and the review tags it neither way; '
            f'[gate:{ran.gate}] or [gate:{ran.gate} clean] there makes it a case'
            for e in ran.findings
            if not any(c.path == e.path and e.overlaps(c.line, c.end_line) for c in tagged_here)
        ]
    return drawn, notes


def harvest(forge: GateForge, git: HarvestGit, numbers: Sequence[int], gates: Sequence[Gate]) -> Harvested:
    """Append every case the reviews of pull requests `numbers` tag to its gate's set."""
    harvested = Harvested()
    by_name = {g.name: g for g in gates}
    for number in numbers:
        log.info('reading the reviews of #%d', number)
        pull = forge.pull_request(number)
        comments = forge.pull_comments(number)
        reviews = [c for c in comments if c.mine and REVIEW.match(c.body.lstrip())]
        if not reviews:
            harvested.notes.append(f'#{number} has no review this account wrote')
        for review in reviews:
            drawn, notes = _review(review, pull, git, gates, comments)
            harvested.notes += notes
            for name, new in drawn.items():
                added = append_cases(by_name[name].directory / CASES, new)
                harvested.appended.setdefault(name, []).extend(added)
    return harvested
