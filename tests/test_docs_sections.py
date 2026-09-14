"""Every section reference in the repository's prose lands on a section that exists.

A **reference** is a `§N[.N…]` in a markdown document under `docs/`, in
`README.md` or in `AGENTS.md`, outside fenced code and inline code spans -- a
`§7.4` in backticks is a quotation, not a pointer. It is **named** when it
hangs off a document's name: `<file>.md §N.N`, `rfc-NNN §N.N`, a chain after
one name (`architecture.md §3.4 and §5.2`, `[rfc.md](…) §2`, `rfc-004 §11`),
`§N of <file>.md`, or a possessive (`its §8.4`, `that document's §2`) whose
antecedent is the nearest document named earlier in the paragraph. Everything
else is **bare**. A bare reference resolves against the document it appears
in, which is the only place a bare number is legible: a reader takes `§3.6`
as *this* document's §3.6, and so does a brief written from it. A named one
resolves against the document it names.

A range or a list (`§3.1–3.2`, `§§1, 4.4`) is read member by member, each
member landing on its own; a comma followed by a date is not a list.

A number **lands** on a numbered heading (`## 5.`, `### 5.3`), an inline
`**§5.3 …**` label (the runbooks number their steps that way), a numbered
list item under a heading (`§0.4` is item 4 of the list under §0, a form the
documents and the code both use), and every parent of one of those (§7
wherever §7.1 exists).

An accepted RFC's body is frozen: it keeps the words and the numbers the
decision was made in, and a cross-reference a later renumbering overtakes is
decoded in the status header rather than edited in the body
(framework/rfc.md §5.3). So a document under `docs/rfc/` whose status word
is Accepted, Implemented or Superseded is checked in its header alone -- the
lines before the first `## ` heading -- where a dated `Updated:` line may
restate a stale number the body carries, and the body's references are not
resolved at all. A Proposed one is live text throughout, and a status word
rfc.md §3.1 does not define is reported rather than read as either.
Resolving them at the acceptance commit instead would report the same
nothing, at the price of a test that reads history; what the header-only form
leaves unreported is a body citation that was already dangling when the RFC
was accepted, and the pull request that accepted it was the place to catch
that.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent.parent

FENCE = re.compile(r'^(?:```|~~~)')
CODE_SPAN = re.compile(r'`[^`\n]*`')
HEADING = re.compile(r'^#{1,6}\s+(?:§?(\d+(?:\.\d+)*)\.?(?:\s|$))?')
INLINE_LABEL = re.compile(r'\*\*§(\d+(?:\.\d+)*)\b')
LIST_ITEM = re.compile(r'^(\d+)\.\s')
NUMBER = r'\d+(?:\.\d+)*'
#: A range or a list continues a reference (`§3.1–3.2`, `§§1, 4.4`, `§4-5`);
#: a comma followed by a date does not (`§1, 2026-08-24`).
CONTINUATION = rf'(?:\s*[–-]\s*{NUMBER}|,\s*{NUMBER}(?!\d|\.\d|[–-]\d))'
REFERENCE = re.compile(rf'§({NUMBER})((?:{CONTINUATION})*)')
MEMBER = re.compile(NUMBER)
NAME = r'(?:[\w.-]+/)*[\w-]+\.md|rfc-\d{3}'
DOCUMENT = re.compile(rf'(?<![\w/])({NAME})\b', re.IGNORECASE)
FOLLOWING = re.compile(rf'^\s*(?:of|in)\s+({NAME})\b', re.IGNORECASE)
POSSESSIVE = re.compile(r"(?:\bits|\bwhose|\bthat document's)\s*$", re.IGNORECASE)
#: What may stand between a document's name and a `§` for the reference to
#: hang off that name: punctuation, another `§N`, a link's closing, a
#: connective, a step or item number, a possessive, or a short parenthetical.
CHAIN = re.compile(
    rf'^(?:[\s,;:/()+>`§—–-]|§{NUMBER}(?:{CONTINUATION})*|\]\([^)]*\)|\*\*'
    r'|\band\b|\bor\b|\bto\b|\bthrough\b|\bthen\b|\bstep\s*\d+|\bitem\s*\d+|\brule\s*\d+'
    r"|\bits\b|\bwhose\b|\bthat document's|\([^()]{0,80}\))*$"
)
#: How far back a name reaches: a chain longer than this is a paragraph, not a citation.
WINDOW = 140
STATUS = re.compile(r'^\*\s+\*\*Status:\*\*\s+(\w+)')
#: The status words rfc.md §3.1 defines. Every word but Proposed freezes the
#: body; a word outside the set is reported rather than read as either.
LIVE = {'Proposed'}
FROZEN = {'Accepted', 'Implemented', 'Superseded'}
HEADER_BULLET = re.compile(r'^\*\s+\*\*([^*]+):\*\*')
SECTION_HEADING = re.compile(r'^## ')


@dataclass(frozen=True)
class Reference:
    """One `§` as a reader meets it: where it stands, what it says, and what it points at."""

    document: Path
    line: int
    section: str
    written: str
    name: str | None
    target: Path | None

    @property
    def text(self) -> str:
        """The reference as the reader sees it, and which member of a range or list it is."""
        member = f'§{self.section}' if self.written == f'§{self.section}' else f'§{self.section} in {self.written}'
        return f'{self.name} {member}' if self.name else member


def prose(text: str) -> str:
    """The text with fenced code blanked, lengths kept so lines and offsets survive."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if FENCE.match(line):
            fenced = not fenced
        out.append(' ' * (len(line) - 1) + '\n' if fenced or FENCE.match(line) else line)
    return ''.join(out)


def sections(text: str) -> set[str]:
    """Every number a reference into this prose can land on."""
    found: set[str] = set()
    heading: str | None = None
    for line in text.splitlines():
        if match := HEADING.match(line):
            heading = match.group(1)
            if heading:
                found.add(heading)
        elif heading and (match := LIST_ITEM.match(line)):
            found.add(f'{heading}.{match.group(1)}')
    found |= {match.group(1) for match in INLINE_LABEL.finditer(text)}
    for number in list(found):
        parts = number.split('.')
        found |= {'.'.join(parts[:depth]) for depth in range(1, len(parts))}
    return found


class Corpus:
    """The documents the sweep reads, their prose, and what each one's numbers land on."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.documents = sorted(
            path.resolve()
            for path in (*root.glob('docs/**/*.md'), root / 'README.md', root / 'AGENTS.md')
            if path.is_file()
        )
        self.prose = {path: prose(path.read_text()) for path in self.documents}
        self.sections = {path: sections(text) for path, text in self.prose.items()}
        self.by_name: dict[str, list[Path]] = {}
        for path in self.documents:
            self.by_name.setdefault(path.name.lower(), []).append(path)
            if match := re.match(r'rfc-\d{3}', path.name, re.IGNORECASE):
                self.by_name.setdefault(match.group(0).lower(), []).append(path)

    def resolve(self, name: str, source: Path) -> Path | None:
        """The document a name means from `source`: by path, then by unique basename."""
        if '/' in name:
            for base in (source.parent, self.root / 'docs', self.root):
                candidate = (base / name).resolve()
                if candidate in self.sections:
                    return candidate
            name = name.rsplit('/', 1)[1]
        local = (source.parent / name).resolve()
        if local in self.sections:
            return local
        candidates = self.by_name.get(name.lower(), [])
        return candidates[0] if len(candidates) == 1 else None

    def references(self, document: Path) -> Iterator[Reference]:
        text = self.prose[document]
        spans = [(match.start(), match.end()) for match in CODE_SPAN.finditer(text)]
        for match in REFERENCE.finditer(text):
            if any(start < match.start() < end for start, end in spans):
                continue
            before = text[max(0, match.start() - WINDOW) : match.start()]
            name = self._name_after(text[match.end() :]) or self._name_before(text, match.start(), before)
            for section in (match.group(1), *MEMBER.findall(match.group(2))):
                yield Reference(
                    document=document.relative_to(self.root),
                    line=text.count('\n', 0, match.start()) + 1,
                    section=section,
                    written=match.group(0),
                    name=name,
                    target=self.resolve(name, document) if name else document,
                )

    def _name_before(self, text: str, position: int, before: str) -> str | None:
        names = list(DOCUMENT.finditer(before))
        if names and CHAIN.match(before[names[-1].end() :]):
            return names[-1].group(1)
        if POSSESSIVE.search(before):
            paragraph = text[text.rfind('\n\n', 0, position) + 1 : position]
            if names := list(DOCUMENT.finditer(paragraph)):
                return names[-1].group(1)
        return None

    @staticmethod
    def _name_after(after: str) -> str | None:
        match = FOLLOWING.match(after)
        return match.group(1) if match else None

    def status(self, document: Path) -> tuple[int, str, int] | None:
        """An RFC's status word: its line, the word, and the line its body starts on."""
        if document.parent != (self.root / 'docs' / 'rfc').resolve():
            return None
        lines = self.prose[document].splitlines()
        first_section = next((index for index, line in enumerate(lines) if SECTION_HEADING.match(line)), len(lines))
        for index, line in enumerate(lines[:first_section]):
            if match := STATUS.match(line):
                return index + 1, match.group(1), first_section + 1
        return None

    def header_bullets(self, document: Path) -> list[str | None]:
        """Which header bullet each line belongs to, by its bold label; None outside one."""
        bullets: list[str | None] = []
        current: str | None = None
        for line in self.prose[document].splitlines():
            if match := HEADER_BULLET.match(line):
                current = match.group(1)
            elif not line.startswith(' '):
                current = None
            bullets.append(current)
        return bullets


@dataclass(frozen=True)
class Finding:
    """A reference that lands nowhere: where, what it says, and why it fails."""

    document: str
    line: int
    text: str
    why: str

    def __str__(self) -> str:
        return f'{self.document}:{self.line}: {self.text} {self.why}'


@dataclass(frozen=True)
class Sweep:
    """What one pass over a tree checked, and what it found.

    `checked` counts the references resolved -- a frozen body's are not among
    them -- and is what keeps a pass over a tree that yielded nothing from
    reading as a clean one.
    """

    checked: int
    dangling: list[Finding]


STATUS_WHY = 'is not a status word rfc.md §3.1 defines, so whether the body is frozen is unknown'


def sweep(root: Path) -> Sweep:
    """Every reference that lands nowhere, in document order, and how many were checked."""
    corpus = Corpus(root)
    checked = 0
    found: list[Finding] = []
    for document in corpus.documents:
        references = list(corpus.references(document))
        frozen_from: int | None = None
        if status := corpus.status(document):
            line, word, body_from = status
            if word in FROZEN:
                frozen_from = body_from
            elif word not in LIVE:
                found.append(Finding(str(document.relative_to(corpus.root)), line, f'Status: {word}', STATUS_WHY))
        if frozen_from is not None:
            # The body is not resolved; it is read for the citations its `Updated:` lines may restate.
            carried = {(ref.target, ref.section) for ref in references if ref.line >= frozen_from and ref.name}
            bullets = corpus.header_bullets(document)
            references = [
                ref
                for ref in references
                if ref.line < frozen_from
                and not (ref.name and bullets[ref.line - 1] == 'Updated' and (ref.target, ref.section) in carried)
            ]
        checked += len(references)
        for ref in references:
            if ref.target is None:
                why = 'names no document the sweep reads, or more than one'
            elif ref.section in corpus.sections[ref.target]:
                continue
            else:
                why = f'lands nowhere in {ref.target.relative_to(corpus.root)}'
                if not ref.name:
                    why += ' (a bare number is read against its own document)'
            found.append(Finding(str(ref.document), ref.line, ref.text, why))
    return Sweep(checked, found)


def test_every_section_reference_in_the_prose_lands() -> None:
    result = sweep(ROOT)
    assert result.checked > 0, 'the sweep read no reference at all'
    assert result.dangling == [], [str(finding) for finding in result.dangling]


# --------------------------------------------------------------------------
# What the sweep reads a reference as, on documents written for the purpose.
# --------------------------------------------------------------------------


def _tree(root: Path, documents: dict[str, str]) -> Path:
    """A root holding each document at the path given, relative to it."""
    for name, text in documents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_a_bare_reference_is_read_against_its_own_document(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            'docs/a.md': '# A\n\n## 1. One\n\nSee §1 and §2.\n',
            'docs/b.md': '# B\n\n## 2. Two\n\nSee §2.\n',
        },
    )
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/a.md:5: §2 lands nowhere in docs/a.md (a bare number is read against its own document)',
    ]


def test_a_named_reference_is_read_against_the_document_it_names(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            'docs/a.md': '# A\n\n## 3. Three\n\nSee b.md §2, b.md §3, [b](b.md) §2, §2 of b.md and nope.md §1.\n',
            'docs/b.md': '# B\n\n## 2. Two\n',
        },
    )
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/a.md:5: b.md §3 lands nowhere in docs/b.md',
        'docs/a.md:5: nope.md §1 names no document the sweep reads, or more than one',
    ]


def test_a_chain_and_a_possessive_hang_off_the_name_before_them(tmp_path: Path) -> None:
    clause = 'a clause that runs on for longer than the window a name reaches across, ' * 3
    root = _tree(
        tmp_path,
        {
            'docs/a.md': f"# A\n\nSee b.md §2 and §2.1, then its §2.2 -- {clause}that document's §2.3.\n\nAlone, its §2.4.\n",
            'docs/b.md': '# B\n\n## 2. Two\n\n### 2.1 A\n\n### 2.2 B\n\n### 2.3 C\n',
        },
    )
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/a.md:5: §2.4 lands nowhere in docs/a.md (a bare number is read against its own document)',
    ]


def test_a_numbered_list_item_under_a_heading_is_addressable(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {'docs/a.md': '# A\n\n## 0. Rules\n\n1.  First.\n2.  Second.\n\n## 1. One\n\nSee §0.2, §0.3 and §1.1.\n'},
    )
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/a.md:10: §0.3 lands nowhere in docs/a.md (a bare number is read against its own document)',
        'docs/a.md:10: §1.1 lands nowhere in docs/a.md (a bare number is read against its own document)',
    ]


def test_a_range_or_a_list_is_read_member_by_member(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            'docs/a.md': '# A\n\n## 1. One\n\nSee b.md §2–3, §2.1-2.9 and §§2, 2.1. Here, §1, 2026-08-24 is a date; §1, 9 is not.\n',
            'docs/b.md': '# B\n\n## 2. Two\n\n### 2.1 A\n',
        },
    )
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/a.md:5: b.md §3 in §2–3 lands nowhere in docs/b.md',
        'docs/a.md:5: b.md §2.9 in §2.1-2.9 lands nowhere in docs/b.md',
        'docs/a.md:5: §9 in §1, 9 lands nowhere in docs/a.md (a bare number is read against its own document)',
    ]


def test_code_is_quoted_and_not_referenced(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {'docs/a.md': '# A\n\nWas `§9`, and `b.md §9` too.\n\n```\n§9 in a fence\n```\n\nBut §9 here.\n'},
    )
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/a.md:9: §9 lands nowhere in docs/a.md (a bare number is read against its own document)',
    ]


ACCEPTED = (
    '# RFC 001: Frozen\n\n'
    '*   **Status:** {status}, 2026-09-01. The argument is §1; the mechanism is\n'
    '    rfc-002 §{status_cites}.\n'
    '*   **Created:** 2026-09-01\n'
    '*   **Updated:** 2026-09-14 -- §1 cites rfc-002 §{updated_cites}, which rfc-002 has\n'
    '    since renumbered.\n\n'
    '## 1. Context\n\nThe mechanism is rfc-002 §9. Here, §7 says why.\n'
)
CITED = '# RFC 002: Cited\n\n*   **Status:** Accepted, 2026-09-01.\n\n## 2. Two\n'


def test_an_accepted_rfc_is_checked_in_its_header_alone(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            'docs/rfc/rfc-001-frozen.md': ACCEPTED.format(status='Accepted', status_cites='2', updated_cites='9'),
            'docs/rfc/rfc-002-cited.md': CITED,
        },
    )
    # The body's `rfc-002 §9` and bare `§7` are frozen; the `Updated:` line restating §9 decodes it.
    assert sweep(root).dangling == []

    # The same body under Proposed is live text, and both land nowhere.
    frozen = root / 'docs' / 'rfc' / 'rfc-001-frozen.md'
    frozen.write_text(ACCEPTED.format(status='Proposed', status_cites='2', updated_cites='9'))
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/rfc/rfc-001-frozen.md:6: rfc-002 §9 lands nowhere in docs/rfc/rfc-002-cited.md',
        'docs/rfc/rfc-001-frozen.md:11: rfc-002 §9 lands nowhere in docs/rfc/rfc-002-cited.md',
        'docs/rfc/rfc-001-frozen.md:11: §7 lands nowhere in docs/rfc/rfc-001-frozen.md'
        ' (a bare number is read against its own document)',
    ]


def test_a_header_restates_only_the_number_its_body_carries(tmp_path: Path) -> None:
    root = _tree(tmp_path, {'docs/rfc/rfc-002-cited.md': CITED})
    frozen = root / 'docs' / 'rfc' / 'rfc-001-frozen.md'

    # An `Updated:` line restating a number the body does not cite is a dangling reference.
    frozen.write_text(ACCEPTED.format(status='Accepted', status_cites='2', updated_cites='8'))
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/rfc/rfc-001-frozen.md:6: rfc-002 §8 lands nowhere in docs/rfc/rfc-002-cited.md'
    ]

    # A stale number outside the `Updated:` line is one too, whatever the body carries.
    frozen.write_text(ACCEPTED.format(status='Implemented', status_cites='9', updated_cites='9'))
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/rfc/rfc-001-frozen.md:4: rfc-002 §9 lands nowhere in docs/rfc/rfc-002-cited.md'
    ]


def test_a_status_word_outside_the_defined_set_is_reported_and_freezes_nothing(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            'docs/rfc/rfc-001-frozen.md': ACCEPTED.format(status='Acepted', status_cites='2', updated_cites='9'),
            'docs/rfc/rfc-002-cited.md': CITED,
        },
    )
    assert [str(f) for f in sweep(root).dangling] == [
        'docs/rfc/rfc-001-frozen.md:3: Status: Acepted is not a status word rfc.md §3.1 defines,'
        ' so whether the body is frozen is unknown',
        'docs/rfc/rfc-001-frozen.md:6: rfc-002 §9 lands nowhere in docs/rfc/rfc-002-cited.md',
        'docs/rfc/rfc-001-frozen.md:11: rfc-002 §9 lands nowhere in docs/rfc/rfc-002-cited.md',
        'docs/rfc/rfc-001-frozen.md:11: §7 lands nowhere in docs/rfc/rfc-001-frozen.md'
        ' (a bare number is read against its own document)',
    ]
