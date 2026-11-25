"""The prose of a file: its paragraphs, comments and docstrings, as blocks a pattern runs over.

A block is a run of lines read as one paragraph, its whitespace collapsed,
so a phrase soft-wrapped across two lines is found whole. What a file's
prose is follows its language:

-   markdown: each paragraph outside fenced code, a heading, a list item
    and a table row each opening one of its own, and the comments of a
    shell fence, which annotate a command the reader runs;
-   Python: each run of comments on consecutive lines, and each paragraph
    of a docstring, a string that is a statement of its own;
-   a language whose comments open with `#` or `//` (`COMMENTS`), a
    template taking its language from the extension before `.j2`: each run
    of comments on consecutive lines.

A file of any other language holds no prose here.
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
    '.Containerfile': '#',
    'Containerfile': '#',
    'justfile': '#',
    '.js': '//',
    '.mjs': '//',
    '.json5': '//',
}

#: The info strings of a fence whose comments are a shell's.
_SHELL_FENCES = frozenset({'', 'sh', 'bash', 'shell', 'console', 'zsh'})
#: A fenced block's opening or closing line, and its info string.
_FENCE = re.compile(r'^[ \t]*(?P<fence>```|~~~)[ \t]*(?P<info>[\w-]*)')
#: A line that opens a paragraph of its own in markdown: a heading, a list item or a table row.
_OPENS = re.compile(r'^[ \t]*(?:#{1,6}[ \t]|[-*+][ \t]|\d+[.)][ \t]|\|)')
#: What opens a Python comment: its `#`, and the `:` that makes it an attribute's.
_COMMENT_MARK = re.compile(r'^#:?')
#: A docstring's opening quotes, with any prefix, and its closing ones.
_OPENING = re.compile(r'^[rRuU]?("""|\'\'\'|"|\')')
_CLOSING = re.compile(r'("""|\'\'\'|"|\')$')
#: The fields of a statement that hold a body of statements.
_BODIES = ('body', 'orelse', 'finalbody', 'handlers', 'cases')


@dataclass(frozen=True)
class Block:
    """A paragraph of prose: each line's number and the prose it holds, joined with single spaces."""

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
        """The block's lines as the file holds them."""
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


def _comment(line: str, marker: str) -> str | None:
    """The comment `line` holds, after `marker` at its start or after whitespace; None where it holds none."""
    found = re.search(rf'(?:^|\s){re.escape(marker)}+(?=\s|$)', line)
    return line[found.end() :].strip() if found else None


def _markdown(text: str) -> Iterator[Block]:
    paragraph: list[tuple[int, str]] = []
    comments: list[tuple[int, str]] = []
    fence: str | None = None
    shell = False
    for number, line in enumerate(text.split('\n'), start=1):
        opened = _FENCE.match(line)
        if fence is None and opened is not None:
            fence, shell = opened['fence'], opened['info'] in _SHELL_FENCES
        elif fence is not None and opened is not None and opened['fence'] == fence and not opened['info']:
            fence = None
        elif fence is not None:
            said = _comment(line, '#') if shell else None
            if said:
                comments.append((number, said))
            continue
        else:
            if line.strip():
                if paragraph and _OPENS.match(line):
                    yield Block(tuple(paragraph))
                    paragraph = []
                paragraph.append((number, line))
                continue
        if paragraph:
            yield Block(tuple(paragraph))
            paragraph = []
    if paragraph:
        yield Block(tuple(paragraph))
    yield from _runs(comments)


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


def _python(text: str) -> Iterator[Block]:
    """A Python file's comments, from its tokens, and its docstrings, from its syntax tree; none where it does not parse."""
    try:
        tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError):
        return
    yield from _runs(
        [(t.start[0], _COMMENT_MARK.sub('', t.string).strip()) for t in tokens if t.type == tokenize.COMMENT]
    )
    lines = text.split('\n')
    for node in _docstrings(tree):
        first, last = node.lineno, node.end_lineno or node.lineno
        pieces = [(n, lines[n - 1].strip()) for n in range(first, last + 1)]
        pieces[0] = (first, _OPENING.sub('', pieces[0][1]))
        pieces[-1] = (last, _CLOSING.sub('', pieces[-1][1]))
        yield from _runs([(n, piece) for n, piece in pieces if piece.strip()])


def _commented(text: str, marker: str) -> Iterator[Block]:
    pieces = [(n, said) for n, line in enumerate(text.split('\n'), start=1) if (said := _comment(line, marker))]
    yield from _runs(pieces)


def _language(path: str) -> str:
    """The suffix, or the name, that says what language `path` is in; a template's is the file's it renders."""
    pure = PurePosixPath(path.removesuffix('.j2'))
    return pure.suffix or pure.name


def blocks(path: str, text: str) -> list[Block]:
    """The prose of `text`, the file at `path`, as blocks in no particular order."""
    language = _language(path)
    if language == '.md':
        return list(_markdown(text))
    if language == '.py':
        return list(_python(text))
    marker = COMMENTS.get(language)
    return list(_commented(text, marker)) if marker else []


def readable(path: str) -> bool:
    """Whether `blocks` reads any prose from a file at `path`."""
    return _language(path) in {'.md', '.py', *COMMENTS}


#: The paths whose prose `blocks` reads, as a gate's trigger (`paths.OwnedPath`).
TRIGGER = ('**.md', '**.py', '**.j2', *(f'**{language}' for language in COMMENTS))
