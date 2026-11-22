"""`dispatch gates`, its deterministic half: a pull request's content held to its dispatch (dispatch.md §3).

Each check returns findings, sentences naming the path, line or text each is
about, so a run reports every one at once:

-   **Owned paths** (`owned`): every path the change touches is one the
    dispatch owns, or a new file the description names (§1.1 items 2 and
    5). A file owned only in a section of it is held hunk by hunk to that
    section's lines at the base.
-   **Host names** (`hosts`): every host name and address the description
    names occurs in the tree at the head, in a file's text or in a path, or
    in a path at the base, which a change that deletes a file names
    (§1.1 item 6). A host name here is a dotted name whose last label is
    letters; a name of one label reads like any other word, and reading
    for one stays the reviewer's.
-   **The sweep** (`sweep`): each pattern the description's `## Sweep`
    names is run again over the tree at the head, as §1.1 gives its form.
    A pattern that finds nothing there is evidence only beside a control
    it fires on, and every control the section gives must fire
    (style/README.md, "Comments and docs").

The pull request is read from the forge, its dispatch from the ledger, and
its commits from the local store through `git` plumbing that reads commits
and never touches a working copy. The store holds them once the dispatcher
has fetched, and a run refuses rather than fetching for itself.
"""

from __future__ import annotations

import enum
import ipaddress
import logging
import re
import subprocess as sp
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from . import ledger, prbody, record
from .brief import Role
from .forge import CODE, Comment, Forge, ForgeError, Gh, Issue
from .paths import OwnedPath
from .record import Claim, Event, Front

log = logging.getLogger(__name__)

#: How long one `git` call may take. Each reads the local store.
GIT_TIMEOUT = 120

#: The tree the sweep leaves out: generated code, whose text is its
#: generator's (AGENTS.md, "Writing the prose").
GENERATED = 'sdks/'


class PullUnreadable(ValueError):
    """The pull request belongs to no live dispatch, or its commits are not in the local store."""


@dataclass(frozen=True)
class PullRequest:
    """A pull request of the code repository, as the gates and the merge read it."""

    number: int
    title: str
    body: str
    #: `OPEN`, `MERGED` or `CLOSED`.
    state: str
    branch: str
    #: The full SHA of its head on the forge.
    head: str
    #: The full SHA of the tip of the branch it merges into, as the forge last saw it.
    base: str
    comments: tuple[Comment, ...]
    #: Whether the forge can merge it, as `gh pr view` reports `mergeStateStatus`:
    #: `BEHIND`, `BLOCKED`, `CLEAN`, `DIRTY`, `UNKNOWN` while it is worked out, and others.
    merge_state: str = 'UNKNOWN'

    @property
    def url(self) -> str:
        return f'https://github.com/{CODE}/pull/{self.number}'


class PullForge(Forge, Protocol):
    """The forge, with the pull request read whole."""

    def pull_request(self, number: int) -> PullRequest: ...


@dataclass(frozen=True)
class PullGh(Gh):
    """`PullForge` through the `gh` on `PATH`."""

    def pull_request(self, number: int) -> PullRequest:
        fields = 'number,title,body,state,headRefName,headRefOid,baseRefOid,comments,mergeStateStatus'
        raw = cast(dict[str, Any], self._json(['pr', 'view', str(number), '--repo', CODE, '--json', fields]))
        return PullRequest(
            number=int(raw['number']),
            title=str(raw['title']),
            body=str(raw['body']),
            state=str(raw['state']).upper(),
            branch=str(raw['headRefName']),
            head=str(raw['headRefOid']),
            base=str(raw['baseRefOid']),
            comments=tuple(
                Comment(body=str(c['body']), mine=c['viewerDidAuthor'] is True)
                for c in cast(list[dict[str, Any]], raw['comments'])
            ),
            merge_state=str(raw['mergeStateStatus']),
        )


# -- the dispatch a pull request belongs to -----------------------------------


@dataclass(frozen=True)
class Dispatched:
    """The dispatch whose builder record names a pull request's branch: its live claim, or the run its `end` ended."""

    issue: Issue
    #: The run's records, its `end` aside.
    claim: Claim
    #: The builder record's front matter.
    front: Front
    #: Whether an `end` record has ended the run.
    ended: bool = False

    @property
    def closes(self) -> bool | None:
        closes = self.front.fields.get('closes')
        return closes if isinstance(closes, bool) else None


def ended_run(number: int, comments: Sequence[Comment]) -> Claim | None:
    """The run the last `end` record on issue `number` ended, its `end` aside; None where no `end` stands last.

    A run read this way ends at its `end`, where a live claim (`record.claim`)
    starts after it; a record after that `end` starts another run, which is
    the issue's live one, so none is returned.
    """
    records = [r for r in (record.parse(number, comment) for comment in comments) if r is not None]
    if not records or records[-1].event is not Event.END:
        return None
    before = records[:-1]
    cuts = [index for index, r in enumerate(before) if r.event is Event.END or r.start]
    run = before[cuts[-1] :] if cuts else before
    if run and run[0].event is Event.END:
        run = run[1:]
    return Claim(issue=number, records=tuple(run)) if run else None


def _builder(run: Claim | None, branch: str) -> Front | None:
    """The front matter of the latest builder record of `run` that names `branch`."""
    fronts = [
        r.front
        for r in (run.records if run is not None else ())
        if r.front is not None and r.front.role is Role.BUILDER and r.front.fields.get('branch') == branch
    ]
    return fronts[-1] if fronts else None


def dispatched(forge: Forge, pull: PullRequest, *, ended: bool = False) -> Dispatched:
    """The live dispatch of `pull`; with `ended`, the run an `end` ended where none is live. Raises `PullUnreadable`.

    The issues read are every open one carrying a claim label and every one
    the description names, which finds a claimed issue its own pull
    request's keyword has already closed, and one whose labels a close-out
    already took off.
    """
    log.info("reading the ledger for the dispatch of #%d's branch %s", pull.number, pull.branch)
    numbers = sorted(
        {issue.number for label in ledger.CLAIMS for issue in forge.issues(label=label)} | ledger.references(pull.body)
    )
    live: list[Dispatched] = []
    over: list[Dispatched] = []
    unread: list[str] = []
    for number in numbers:
        issue = forge.issue(number)
        if not ledger.claimed(issue) and not ended:
            continue
        try:
            comments = forge.comments(number)
            claim = record.claim(number, comments) if ledger.claimed(issue) else None
            run = ended_run(number, comments) if ended else None
        except record.RecordUnreadable as exc:
            unread.append(str(exc))
            continue
        front = _builder(claim, pull.branch)
        if claim is not None and front is not None:
            live.append(Dispatched(issue=issue, claim=claim, front=front))
        front = _builder(run, pull.branch)
        if run is not None and front is not None:
            over.append(Dispatched(issue=issue, claim=run, front=front, ended=True))
    found = live or over
    if len(found) > 1:
        issues = ', '.join(f'#{d.issue.number}' for d in found)
        raise PullUnreadable(f'the records of {issues} all name branch {pull.branch}')
    if not found:
        unreadable = f' (and these records do not read: {"; ".join(unread)})' if unread else ''
        which = 'no live builder record, nor a run an `end` ended,' if ended else 'no live builder record'
        raise PullUnreadable(f'#{pull.number} is on branch {pull.branch}, which {which} names{unreadable}')
    return found[0]


# -- the local store ----------------------------------------------------------


@dataclass(frozen=True)
class Status:
    """A path a change touches, and how: `A`dded, `M`odified, `D`eleted, or its `T`ype changed."""

    kind: str
    path: str


@dataclass(frozen=True)
class Hunk:
    """One hunk of a file's diff, by its lines at the base and at the head.

    Where `base_count` is 0 the hunk only inserts, after base line `base_start`.
    """

    base_start: int
    base_count: int
    head_start: int
    head_count: int

    def within(self, first: int, last: int) -> bool:
        """Whether every base line the hunk changes, or the place it inserts at, lies in lines `first`–`last`."""
        if self.base_count == 0:
            return first <= self.base_start <= last
        return first <= self.base_start and self.base_start + self.base_count - 1 <= last

    def __str__(self) -> str:
        if self.head_count == 0:
            return f'(a removal at the base, lines {self.base_start}–{self.base_start + self.base_count - 1})'
        return f'{self.head_start}–{self.head_start + self.head_count - 1}'


#: A hunk's header in a diff with no context: the base's start and count, then the head's.
_HUNK = re.compile(r'^@@ -(?P<bs>\d+)(?:,(?P<bc>\d+))? \+(?P<hs>\d+)(?:,(?P<hc>\d+))? @@', re.M)


@dataclass(frozen=True)
class Git:
    """The commits of the local store, through the `git` on `PATH`, run in the checkout at `root`."""

    root: Path

    def _run(self, *args: str, stdin: bytes | None = None, ok: Sequence[int] = (0,)) -> sp.CompletedProcess[bytes]:
        try:
            done = sp.run(
                ['git', '--no-pager', *args], cwd=self.root, input=stdin, capture_output=True, timeout=GIT_TIMEOUT
            )
        except (OSError, sp.TimeoutExpired) as exc:
            raise ForgeError(f'`git {" ".join(args[:3])} …` did not run: {exc}') from exc
        if done.returncode not in ok:
            detail = done.stderr.decode(errors='replace').strip() or str(done.returncode)
            raise ForgeError(f'`git {" ".join(args[:3])} …` failed: {detail}')
        return done

    def has(self, commit: str) -> bool:
        return self._run('cat-file', '-e', f'{commit}^{{commit}}', ok=(0, 1, 128)).returncode == 0

    def merge_base(self, one: str, other: str) -> str:
        return self._run('merge-base', one, other).stdout.decode().strip()

    def statuses(self, base: str, head: str) -> list[Status]:
        out = self._run('diff', '--name-status', '-z', '--no-renames', base, head).stdout.decode()
        tokens = out.split('\0')
        return [Status(kind=kind[:1], path=path) for kind, path in zip(tokens[0::2], tokens[1::2], strict=False)]

    def hunks(self, base: str, head: str, path: str) -> list[Hunk]:
        out = self._run('diff', '-U0', '--no-color', '--no-ext-diff', '--no-renames', base, head, '--', path)
        return [
            Hunk(
                base_start=int(m['bs']),
                base_count=int(m['bc'] if m['bc'] is not None else 1),
                head_start=int(m['hs']),
                head_count=int(m['hc'] if m['hc'] is not None else 1),
            )
            for m in _HUNK.finditer(out.stdout.decode(errors='replace'))
        ]

    def text(self, commit: str, path: str) -> str | None:
        """The text of `path` at `commit`; None where it is not there."""
        done = self._run('cat-file', 'blob', f'{commit}:{path}', ok=(0, 128))
        return done.stdout.decode(errors='replace') if done.returncode == 0 else None

    def paths(self, commit: str) -> list[str]:
        return [p for p in self._run('ls-tree', '-r', '-z', '--name-only', commit).stdout.decode().split('\0') if p]

    def contains(self, commit: str, needle: str) -> bool:
        """Whether a tracked file's text at `commit` holds `needle`, in any case."""
        return self._run('grep', '-q', '-F', '-i', '-I', '-e', needle, commit, '--', ok=(0, 1)).returncode == 0

    def texts(self, commit: str) -> Iterator[tuple[str, str]]:
        """Each tracked file at `commit` that is UTF-8 text, with its text."""
        listed = self._run('ls-tree', '-r', '-z', commit).stdout.decode().split('\0')
        blobs = [
            (meta.split()[2], path)
            for meta, _, path in (entry.partition('\t') for entry in listed if entry)
            if meta.split()[1] == 'blob'
        ]
        out = self._run('cat-file', '--batch', stdin=''.join(f'{oid}\n' for oid, _ in blobs).encode()).stdout
        offset = 0
        for _, path in blobs:
            header_end = out.index(b'\n', offset)
            size = int(out[offset:header_end].split()[2])
            content = out[header_end + 1 : header_end + 1 + size]
            offset = header_end + 1 + size + 1
            try:
                yield path, content.decode()
            except UnicodeDecodeError:
                continue


# -- owned paths --------------------------------------------------------------

#: A markdown heading: its level and its text.
_HEADING = re.compile(r'^(?P<level>#{1,6})[ \t]+(?P<name>.*?)[ \t]*#*[ \t]*$')
#: A fenced block's opening or closing line.
_FENCE = re.compile(r'^[ \t]*(?:```|~~~)')
#: A section named by its number, `§3.5`.
_NUMBERED = re.compile(r'§(?P<number>\d+(?:\.\d+)*)')


def section_lines(text: str, section: str) -> tuple[int, int] | str:
    """The first and last line of `section` in the markdown `text`; why not, where it has no one section.

    A section is named by its number, `§3.5`, which a heading's text opens
    with, or by a heading's whole text. It runs from its heading to the line
    before the next heading of the same or a higher level.
    """
    headings: list[tuple[int, int, str]] = []
    fenced = False
    lines = text.split('\n')
    for index, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        heading = None if fenced else _HEADING.fullmatch(line)
        if heading is not None:
            headings.append((index, len(heading['level']), heading['name']))
    numbered = _NUMBERED.fullmatch(section)
    if numbered is not None:
        opening = re.compile(rf'{re.escape(numbered["number"])}\.?(?:\s|$)')
        matches = [h for h in headings if opening.match(h[2])]
    else:
        matches = [h for h in headings if h[2] == section.strip()]
    if len(matches) != 1:
        return f'{"no" if not matches else "more than one"} heading of the base names {section}'
    start, level, _ = matches[0]
    end = next((line - 1 for line, other, _ in headings if line > start and other <= level), len(lines))
    return start, end


def owned(
    statuses: Sequence[Status],
    owned_paths: Sequence[OwnedPath],
    body: str,
    hunks: Callable[[str], Sequence[Hunk]],
    base_text: Callable[[str], str | None],
) -> tuple[list[str], list[str]]:
    """The findings on paths the change touches outside what the dispatch owns; and notes on disclosed new files."""
    findings: list[str] = []
    notes: list[str] = []
    for status in statuses:
        path = status.path
        if any(o.section is None and o.owns(path) for o in owned_paths):
            continue
        sections = [o.section for o in owned_paths if o.section is not None and o.path == path]
        if not sections:
            if status.kind == 'A' and path in body:
                notes.append(f'{path} is a new file outside the owned paths, which the description names')
            elif status.kind == 'A':
                findings.append(
                    f'{path} is a new file outside the owned paths, and the description does not name it '
                    '(dispatch.md §1.1 item 5)'
                )
            else:
                findings.append(f'{path} is outside the owned paths')
            continue
        named = ', '.join(sections)
        text = base_text(path)
        if status.kind != 'M' or text is None:
            findings.append(f'{path} is owned only in {named}, and the change does not edit it in place')
            continue
        ranges: list[tuple[int, int]] = []
        for section in sections:
            found = section_lines(text, section)
            if isinstance(found, str):
                findings.append(f'{path}#{section}: {found}')
            else:
                ranges.append(found)
        owned_lines = ', '.join(f'{first}–{last}' for first, last in ranges) or 'nothing'
        findings += [
            f'{path}:{hunk} lies outside {named}, lines {owned_lines} at the base'
            for hunk in hunks(path)
            if not any(hunk.within(first, last) for first, last in ranges)
        ]
    return findings, notes


# -- host names ---------------------------------------------------------------

#: A dotted name: labels of letters, digits and hyphens, the last of letters alone.
_DOTTED = re.compile(r'(?<![\w.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[a-z]{2,63}(?![\w-])')
#: Something shaped like an IPv4 address, which `ipaddress` then confirms.
_IPV4 = re.compile(r'(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w]|\.\d)')
#: Something shaped like an IPv6 address, which `ipaddress` then confirms.
_IPV6 = re.compile(r'(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])')


def _address(candidate: str) -> bool:
    try:
        _ = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def host_names(body: str) -> list[str]:
    """The host names and addresses `body` names, each once, in the order it first names them."""
    found = [m[0] for m in _DOTTED.finditer(body)]
    found += [m[0] for pattern in (_IPV4, _IPV6) for m in pattern.finditer(body) if _address(m[0])]
    return list(dict.fromkeys(found))


def hosts(body: str, paths: Sequence[str], contains: Callable[[str], bool]) -> list[str]:
    """The findings on host names and addresses in `body` that the tree holds nowhere.

    `paths` are the tree's paths at the head and at the base; `contains`
    answers whether a file's text at the head holds a string, in any case.
    """
    lowered = [path.lower() for path in paths]
    return [
        f'{name} is named in the description and occurs nowhere in the tree at the head (dispatch.md §1.1 item 6)'
        for name in host_names(body)
        if not any(name.lower() in path for path in lowered) and not contains(name)
    ]


# -- the sweep ----------------------------------------------------------------

#: A list item at the top level of a section: its marker, then its text.
_ITEM = re.compile(r'^ {0,1}[-*+][ \t]+')
#: A code span opening an item's text: its run of backticks, its content, the same run.
_OPENING_SPAN = re.compile(r'\A(?P<ticks>`+)(?P<span>.+?)(?P=ticks)(?!`)')
#: The control an item names: `Control:`, then the string in straight or curly quotes.
_ITEM_CONTROL = re.compile(r'\bcontrol:\s*(?:"(?P<straight>[^"]*)"|“(?P<curly>[^”]*)”)', re.I)


@dataclass(frozen=True)
class Pattern:
    """A pattern the sweep names, and the control it names for it."""

    text: str
    control: str | None


def _items(section: str) -> Iterator[str]:
    """Each top-level list item of `section`, its lines joined and its whitespace collapsed."""
    item: list[str] | None = None
    for line in section.split('\n'):
        if _ITEM.match(line):
            if item is not None:
                yield ' '.join(' '.join(item).split())
            item = [_ITEM.sub('', line, count=1)]
        elif item is not None and (line.startswith((' ', '\t')) or not line.strip()):
            item.append(line)
        elif item is not None:
            yield ' '.join(' '.join(item).split())
            item = None
    if item is not None:
        yield ' '.join(' '.join(item).split())


def patterns(body: str) -> list[Pattern]:
    """Each pattern the `## Sweep` section names: a top-level list item that opens with it in a code span."""
    section = prbody.sections(body).get(prbody.Section.SWEEP.value, '')
    found: list[Pattern] = []
    for item in _items(section):
        span = _OPENING_SPAN.match(item)
        if span is None:
            continue
        text = span['span']
        if text.startswith(' ') and text.endswith(' ') and text.strip():
            text = text[1:-1]
        control = _ITEM_CONTROL.search(item[span.end() :])
        found.append(Pattern(text, control['straight'] or control['curly'] if control else None))
    return found


@dataclass(frozen=True)
class Swept:
    """One pattern run again over the tree."""

    pattern: Pattern
    #: How many files it matched in; None where it does not compile.
    files: int | None
    #: Whether its control fires it; None where it names none.
    fires: bool | None


def sweep(body: str, texts: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """The findings and the notes of the sweep run again over `texts`, by path.

    Every pass is over text whose whitespace is collapsed, under `re.S | re.I`.
    """
    named = patterns(body)
    if not named:
        return [
            f'`## {prbody.Section.SWEEP}` names no pattern as a list item that opens with it in a code span '
            '(dispatch.md §1.1)'
        ], []
    folded = {path: ' '.join(text.split()) for path, text in texts.items()}
    findings: list[str] = []
    runs: list[Swept] = []
    for pattern in named:
        try:
            compiled = re.compile(pattern.text, re.S | re.I)
        except re.error as exc:
            findings.append(f'`{pattern.text}` does not compile: {exc}')
            runs.append(Swept(pattern, None, None))
            continue
        files = sum(1 for text in folded.values() if compiled.search(text))
        fires = None if pattern.control is None else compiled.search(' '.join(pattern.control.split())) is not None
        runs.append(Swept(pattern, files, fires))
        if fires is False:
            findings.append(f'`{pattern.text}` does not fire on its control "{pattern.control}"')
        elif files == 0 and fires is None:
            findings.append(
                f'`{pattern.text}` finds nothing at the head and names no control it fires on, so its silence '
                'is no evidence (style/README.md, "Comments and docs")'
            )
    notes = [f'the sweep ran {len(named)} patterns again over {len(folded)} files at the head']
    notes += [
        f'`{run.pattern.text}`: {run.files} file{"" if run.files == 1 else "s"}'
        + ('' if run.fires is None else ', and its control fires' if run.fires else ', and its control does not fire')
        for run in runs
        if run.files is not None
    ]
    if not folded:
        findings.append('the sweep read no files at the head, so it found nothing about the repository')
    return findings, notes


# -- the run ------------------------------------------------------------------


class Check(enum.StrEnum):
    """A check `gates` runs, by what its findings are about."""

    OWNED = 'owned-paths'
    HOSTS = 'host-names'
    SWEEP = 'sweep'


@dataclass(frozen=True)
class Report:
    """What `gates` found: each check's findings, and the notes that are no finding."""

    findings: tuple[tuple[Check, str], ...]
    notes: tuple[tuple[Check, str], ...]


def gates(forge: PullForge, git: Git, number: int) -> Report:
    """The deterministic checks of pull request `number`. Raises `PullUnreadable`."""
    log.info('reading #%d', number)
    pull = forge.pull_request(number)
    dispatch = dispatched(forge, pull)
    missing = [sha for sha in (pull.head, pull.base) if not git.has(sha)]
    if missing:
        raise PullUnreadable(
            f'the local store holds no commit {", ".join(missing)} of #{number}: fetch first, which §2 rule 5 '
            "follows with the primary's rebase"
        )
    base = git.merge_base(pull.base, pull.head)
    log.info('reading the change of #%d against %s', number, base[:12])
    owned_findings, owned_notes = owned(
        git.statuses(base, pull.head),
        dispatch.front.owned,
        pull.body,
        lambda path: git.hunks(base, pull.head, path),
        lambda path: git.text(base, path),
    )
    log.info('reading the tree at %s', pull.head[:12])
    paths = [*git.paths(pull.head), *git.paths(base)]
    host_findings = hosts(pull.body, paths, lambda name: git.contains(pull.head, name))
    texts = {path: text for path, text in git.texts(pull.head) if not path.startswith(GENERATED)}
    sweep_findings, sweep_notes = sweep(pull.body, texts)
    return Report(
        findings=tuple(
            [(Check.OWNED, f) for f in owned_findings]
            + [(Check.HOSTS, f) for f in host_findings]
            + [(Check.SWEEP, f) for f in sweep_findings]
        ),
        notes=tuple([(Check.OWNED, n) for n in owned_notes] + [(Check.SWEEP, n) for n in sweep_notes]),
    )
