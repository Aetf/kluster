"""The prose of a file, read by its language: the passages every gate that reads prose draws from.

A passage is a run of lines a reader takes in at once, read as one
paragraph: its text is its lines' prose with every run of whitespace one
space, so a phrase soft-wrapped across two lines is found whole. What a
file's prose is follows its language:

-   markdown: each paragraph outside code, fenced or indented, and outside
    a front matter block (`literal`), where a list item opens a passage of
    its own without the items nested under it, and a heading and a table
    row are each a passage alone. A line of dashes, a table's header rule
    or a thematic break, is no prose.
-   Python: each paragraph of a docstring, a string that is a statement of
    its own (a module's, a class's, a function's or an attribute's), read
    from the syntax tree; and each paragraph of comments alone on their
    lines, read from the tokens, a comment after code being a passage of
    its own. A file that does not parse is read as a file whose comments
    open with `#`.
-   a language whose comments open with `#` or `//` (`COMMENTS`), a
    template taking its language from the name before `.j2`: each
    paragraph of the lines that open with a comment, and each comment after
    code as a passage of its own, where its marker has whitespace on both
    sides. A template's Jinja comment runs from its `{#` to the line that
    closes it, whatever the language it renders, and is read the same way;
    no other reading takes a line of it.

A paragraph of comments is a run of consecutive comment lines up to one
that holds nothing but its marker, or, in a Jinja comment, a blank line,
as a paragraph of markdown or of a docstring ends at a blank line.

A `#!` first line names an interpreter, and is no comment. A file of any
other language holds no prose here (`readable`).

A commit's message is prose too (`message`): its body is read as markdown,
its subject, its trailers and its paragraphs of references left out.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import PurePosixPath

#: What opens a comment, by a file's extension or, for a file with none, its name.
COMMENTS: dict[str, str] = {
    '.sh': '#',
    '.yml': '#',
    '.yaml': '#',
    '.toml': '#',
    '.conf': '#',
    '.service': '#',
    '.network': '#',
    '.nspawn': '#',
    '.zt': '#',
    '.Containerfile': '#',
    'Containerfile': '#',
    'Caddyfile': '#',
    'justfile': '#',
    '.gitignore': '#',
    '.js': '//',
    '.mjs': '//',
    '.json5': '//',
}

#: A template's extension, which the name before it gives the language of.
TEMPLATE = '.j2'

#: A fenced block's opening or closing line: its run of backticks or tildes, and its info string.
_FENCE = re.compile(r'^[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>.*)$')
#: A YAML front matter block's fence, on the first line of a file and on the line that closes it.
_FRONT = '---'
#: A list item's marker and the spaces after it: the item opens a passage, and its content starts past them.
_OPENER = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+')
#: A line of markdown that is a passage by itself: a heading, or a table row.
_ALONE = re.compile(r'^#{1,6}\s|^\s*\|')
#: A line of dashes alone, with any colons and pipes: a table's header rule, a thematic break, or a
#: front-matter fence.
_RULE = re.compile(r'^\s*\|?[\s:|-]*-[\s:|-]*$')
#: What opens a Python comment: its `#`, and the `:` that makes it an attribute's.
_PYTHON_MARK = re.compile(r'^#:?')
#: A docstring's opening quotes, with any prefix, and its closing ones.
_OPENING = re.compile(r'^[rRuU]?("""|\'\'\'|"|\')')
_CLOSING = re.compile(r'("""|\'\'\'|"|\')$')
#: The fields of a statement that hold a body of statements.
_BODIES = ('body', 'orelse', 'finalbody', 'handlers', 'cases')
#: A Jinja comment's opening, at the start of its line, and its closing.
_JINJA_OPENS = re.compile(r'^\s*\{#-?')
_JINJA_CLOSES = re.compile(r'-?#\}')
#: A trailer of a commit message: a token, its colon and a value (`git interpret-trailers`).
_TRAILER = re.compile(r'^[A-Za-z][\w-]*:[ \t]+\S')
#: A line of a commit message that refers to issues rather than saying anything: a keyword and what it
#: refers to (`Refs Aetf/kluster-ops#917`, `Closes …`), or a run of references a long one wraps onto.
_REFERENCE = re.compile(
    r'^[ \t]*(?:(?:refs?|close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \t]+)?(?:[\w.-]+/[\w.-]+)?#\d+\b', re.I
)


@dataclass(frozen=True)
class Block:
    """A passage of prose: each line's number and the prose it holds, joined with single spaces."""

    pieces: tuple[tuple[int, str], ...]

    @property
    def first(self) -> int:
        return self.pieces[0][0]

    @property
    def last(self) -> int:
        return self.pieces[-1][0]

    @cached_property
    def _collapsed(self) -> tuple[str, ...]:
        return tuple(' '.join(piece.split()) for _, piece in self.pieces)

    @cached_property
    def text(self) -> str:
        return ' '.join(self._collapsed)

    @cached_property
    def _starts(self) -> tuple[int, ...]:
        starts: list[int] = []
        at = 0
        for piece in self._collapsed:
            starts.append(at)
            at += len(piece) + 1
        return tuple(starts)

    def line_of(self, offset: int) -> int:
        """The line that holds character `offset` of `text`."""
        return self.pieces[bisect_right(self._starts, offset) - 1][0]

    def raw(self, lines: Sequence[str]) -> str:
        """The passage's lines as the file holds them."""
        return '\n'.join(lines[self.first - 1 : self.last])


def _runs(pieces: Sequence[tuple[int, str]]) -> Iterator[Block]:
    """`pieces` cut into blocks wherever a line number skips."""
    run: list[tuple[int, str]] = []
    for number, piece in pieces:
        if run and number != run[-1][0] + 1:
            yield Block(tuple(run))
            run = []
        run.append((number, piece))
    if run:
        yield Block(tuple(run))


def _fence(line: str) -> tuple[str, str] | None:
    """The run of backticks or tildes `line` opens with as a fence, and its info string; None where it is none.

    A backtick fence's info string holds no backtick, or the line is inline code.
    """
    found = _FENCE.match(line)
    if found is None or (found['fence'][0] == '`' and '`' in found['info']):
        return None
    return found['fence'], found['info'].strip()


def literal(lines: Sequence[str]) -> set[int]:
    """The lines of markdown `lines` that are literal text rather than prose: code, fenced or indented, and front matter.

    A fence closes on a run of its own character at least as long as the
    one that opened it, with no info string (CommonMark). A block is
    indented code where it opens after a blank line four columns or more
    past the content of the list item it sits in, or past the margin
    outside any list, and it runs to the next line that is not. Front
    matter is a `---` first line, the next `---` line, and what they hold.
    """
    code: set[int] = set()
    if lines and lines[0].strip() == _FRONT:
        closing = next((n for n, line in enumerate(lines[1:], start=2) if line.strip() == _FRONT), None)
        if closing is not None:
            code.update(range(1, closing + 1))
    fence: str | None = None
    content = 0
    indented = False
    blank = True
    for number, line in enumerate(lines, start=1):
        if number in code:
            continue
        found = _fence(line)
        if fence is None and found is not None:
            code.add(number)
            fence = found[0]
            continue
        if fence is not None:
            code.add(number)
            if found is not None and found[0][0] == fence[0] and len(found[0]) >= len(fence) and not found[1]:
                fence = None
            continue
        if not line.strip():
            blank = True
            continue
        indent = len(line) - len(line.lstrip())
        if blank or indented:
            indented = indent >= content + 4
        if indented:
            code.add(number)
        else:
            opener = _OPENER.match(line)
            if opener is not None:
                content = len(opener.group(0))
            elif blank:
                content = min(content, indent)
        blank = False
    return code


def _markdown(lines: Sequence[str]) -> Iterator[Block]:
    code = literal(lines)
    paragraph: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        alone = _ALONE.match(line) is not None
        prose = number not in code and bool(line.strip()) and _RULE.match(line) is None
        if paragraph and (not prose or alone or _OPENER.match(line) is not None):
            yield Block(tuple(paragraph))
            paragraph = []
        if not prose:
            continue
        if alone:
            yield Block(((number, line),))
        else:
            paragraph.append((number, line))
    if paragraph:
        yield Block(tuple(paragraph))


def _docstrings(tree: ast.Module) -> Iterator[ast.Expr]:
    """Each string that is a statement of its own: the docstring of a module, a class, a function or an attribute.

    Only statements are walked, through the fields that hold a body of them,
    so a string inside an expression is never one.
    """
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            yield node
        for name in _BODIES:
            stack.extend(getattr(node, name, ()))


def _python(text: str, lines: Sequence[str]) -> Iterator[Block]:
    """A Python file's comments, from its tokens, and its docstrings' paragraphs, from its syntax tree."""
    try:
        tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError):
        yield from _commented(lines, '#')
        return
    alone: list[tuple[int, str]] = []
    for token in tokens:
        number = token.start[0]
        if token.type != tokenize.COMMENT or (number == 1 and token.string.startswith('#!')):
            continue
        said = (number, _PYTHON_MARK.sub('', token.string).strip())
        if lines[number - 1][: token.start[1]].strip():
            yield Block((said,))
        else:
            alone.append(said)
    yield from _paragraphs(alone)
    for node in _docstrings(tree):
        first, last = node.lineno, node.end_lineno or node.lineno
        # The string's own columns, which the syntax tree gives in bytes: what follows it, a comment say, is not its.
        held = [lines[n - 1] for n in range(first, last + 1)]
        held[-1] = held[-1].encode()[: node.end_col_offset].decode(errors='ignore')
        held[0] = held[0].encode()[node.col_offset :].decode(errors='ignore')
        pieces = [(n, line.strip()) for n, line in zip(range(first, last + 1), held, strict=True)]
        pieces[0] = (first, _OPENING.sub('', pieces[0][1]))
        pieces[-1] = (last, _CLOSING.sub('', pieces[-1][1]))
        yield from _runs([(n, piece) for n, piece in pieces if piece.strip()])


def _commented(lines: Sequence[str], marker: str) -> Iterator[Block]:
    """Each paragraph of lines that open with `marker`, a `#!` first line aside, and each comment after code alone.

    A comment after code is one whose marker has whitespace on both sides,
    so a `#` inside a word, `${#name}` or `$#`, or a `//` inside a URL, is
    none.
    """
    escaped = re.escape(marker)
    opens = re.compile(rf'^\s*{escaped}+')
    after = re.compile(rf'\s{escaped}+(?=\s|$)')
    pieces: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        if number == 1 and line.startswith('#!'):
            continue
        found = opens.match(line)
        if found is not None:
            pieces.append((number, line[found.end() :].strip()))
            continue
        trailing = after.search(line)
        if trailing is not None and line[trailing.end() :].strip():
            yield Block(((number, line[trailing.end() :].strip()),))
    yield from _paragraphs(pieces)


def _paragraphs(pieces: Sequence[tuple[int, str]]) -> Iterator[Block]:
    """Comment lines cut into paragraphs where a line number skips or a comment holds nothing."""
    yield from _runs([(number, said) for number, said in pieces if said])


def _jinja(lines: Sequence[str]) -> Iterator[Block]:
    """Each paragraph of each Jinja comment that opens its line, from its `{#` to the line that closes it.

    A line holding only the comment's opening or closing is the paragraph's,
    and a blank line ends one.
    """
    pieces: list[tuple[int, str]] = []
    open_ = False
    for number, line in enumerate(lines, start=1):
        opened = _JINJA_OPENS.match(line) if not open_ else None
        if not open_ and opened is None:
            continue
        said = line[opened.end() :] if opened is not None else line
        closed = _JINJA_CLOSES.search(said)
        if line.strip():
            pieces.append((number, (said[: closed.start()] if closed else said).strip()))
        open_ = closed is None
        if not open_:
            yield from _runs(pieces)
            pieces = []
    yield from _runs(pieces)


def _language(path: str) -> str:
    """The suffix, or the name, that says what language `path` is in; a template's is the file's it renders."""
    pure = PurePosixPath(path.removesuffix(TEMPLATE))
    return pure.suffix or pure.name


def blocks(path: str, text: str) -> list[Block]:
    """The prose of `text`, the file at `path`, as passages in no particular order."""
    lines = text.split('\n')
    language = _language(path)
    jinja = list(_jinja(lines)) if path.endswith(TEMPLATE) else []
    if language == '.md':
        found = list(_markdown(lines))
    elif language == '.py':
        found = list(_python(text, lines))
    elif language in COMMENTS:
        found = list(_commented(lines, COMMENTS[language]))
    else:
        found = []
    # A line inside a Jinja comment is that comment's, whatever the language reads it as.
    held = {n for block in jinja for n, _ in block.pieces}
    return jinja + [kept for block in found for kept in _runs([p for p in block.pieces if p[0] not in held])]


def message(text: str) -> list[Block]:
    """The prose of a commit message: each paragraph of its body, read as markdown, at the message's own lines.

    The subject is the message's first paragraph, as git reads it; the
    trailers its last, where every line of it is one; and a paragraph of
    references one whose every line refers to issues (`Refs …`,
    `Closes …`), wherever it sits. None of them is a passage.
    """
    lines = text.split('\n')
    subject = next((n for n, line in enumerate(lines) if not line.strip()), len(lines))
    found = [
        b
        for b in _markdown([''] * subject + lines[subject:])
        if not all(_REFERENCE.match(piece) for _, piece in b.pieces)
    ]
    last = max((n for n, line in enumerate(lines, start=1) if line.strip()), default=0)
    trailers = found[-1] if found and found[-1].last == last and found[-1].first > subject else None
    if trailers is not None and all(_TRAILER.match(piece) for _, piece in trailers.pieces):
        found.pop()
    return found


def readable(path: str) -> bool:
    """Whether `blocks` reads any prose from a file at `path`."""
    return path.endswith(TEMPLATE) or _language(path) in {'.md', '.py', *COMMENTS}


#: The paths whose prose `blocks` reads, as a gate's trigger (`paths.OwnedPath`).
TRIGGER = ('**.md', '**.py', f'**{TEMPLATE}', *(f'**{language}' for language in COMMENTS))
