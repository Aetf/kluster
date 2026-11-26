"""A file read as passages: the units a gate's extractor draws its items from.

A passage is a run of lines one narrow question can be asked about, with
the least context that question needs:

-   in Python, a statement, where a compound statement is its header alone
    and its body is statements of their own; a paragraph of a docstring,
    where a `:param` line opens one too; or a run of lines holding nothing
    but a comment. A passage's `within` is the header of the function or
    class it sits in, and a statement's `enclosing` is what lies between
    that header and it: the function's or the class's docstring, and the
    header of each compound statement it is nested in, with the clause
    that opens the body it is in (`else:`, `except …:`, `case …:`).
-   in markdown, a paragraph outside code and front matter, which are what
    the prose reader says they are (`prose.literal`), where a list joins
    the paragraph that leads into it with a colon, and a list's items,
    loose or tight, are one passage. A run of code is a code passage.
-   in a brief, a sentence of its prose below the front matter, with its
    paragraph as context.

A brief is read as a change too (`brief_path`): `dispatch claim` presents it
as the one file of a change that writes it whole, at a path under
`BRIEFS` that names the areas of its issue. A gate asks about a brief only
where its trigger names briefs, a pattern rooted at `BRIEFS` that matches
the path (`asks_about_briefs`), so a trigger that takes every path of a
pull request, `**`, takes no brief. Its extractor draws from a brief by the
path's form (`is_brief`). No path of this repository starts with `:`, so no
path a pull request touches is read as a brief.

An extractor selects, among a file's passages, the ones its own question is
about. What a Python passage's question needs beside it is its context
(`Module.context`): for a statement, where it sits, the comment run ending
on the line above it (a binding's `#:` documentation), and the module-level
names it reads; for a docstring or comment, where it sits and the
program's definition of each dotted name it writes in a code span
(`program.Program.spanned`). Each part is capped on its own at
`CONTEXT_CAP`, so a long one cannot crowd out the rest, and the whole is
held to `extract.ITEM_CONTEXT_CAP` as the item is made, however many names
the passage spans.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from typing import cast

from ..paths import OwnedPath
from .module import bindings
from .program import Program, Source, imports
from .prose import literal

#: The directory a brief is presented under as a change (`brief_path`).
BRIEFS = ':brief'

#: The most of each part of a passage's context an item carries, in characters.
CONTEXT_CAP = 1500

#: A line that opens a list item, in markdown and in a docstring.
_ITEM = re.compile(r'^\s*(?:[-*+•]|\d+[.)])\s+\S')
#: A docstring line that opens a field of its own.
_FIELD = re.compile(r'^\s*:(?:param|raises?|returns?|yields?|type|rtype)\b')
#: A brief's front matter: a `---` line opening the file, and the next one.
_FRONT_MATTER = re.compile(r'\A---\n.*?\n---\n', re.S)
#: Where one sentence ends and the next begins.
_SENTENCE = re.compile(r'(?<=[.!?])["\')\]*_`]*\s+(?=[A-Z0-9*`(\[])')
#: A code span holding a dotted name and nothing else.
_SPANNED = re.compile(r'`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`')
#: The keyword opening a compound statement's clause that has no node of its own.
_CLAUSE = re.compile(r'\s*(?:else|finally)\b')


@dataclass(frozen=True)
class Passage:
    """A run of lines, `first` to `last` at the head, with its text and its context."""

    first: int
    last: int
    text: str
    #: Whether the passage is prose: a docstring, a comment, markdown, a brief.
    prose: bool
    #: The header of the function or class the passage sits in; a brief sentence's paragraph.
    within: str = ''
    #: A code passage's statement.
    node: ast.stmt | None = None
    #: A statement's: the docstring of the function or class it sits in, then each compound statement's header around it.
    enclosing: tuple[str, ...] = ()

    def touches(self, written: Collection[tuple[int, int]]) -> bool:
        """Whether a line of the passage is one of `written`'s runs."""
        return any(self.first <= last and first <= self.last for first, last in written)


def brief_path(areas: Collection[str], name: str) -> str:
    """Where a brief of an issue in `areas`, its `area/*` labels without the prefix, is presented."""
    return '/'.join([BRIEFS, *sorted(areas), name])


def is_brief(path: str) -> bool:
    """Whether `path` is a brief's (`brief_path`)."""
    return path.startswith(f'{BRIEFS}/')


def asks_about_briefs(trigger: Collection[str], path: str) -> bool:
    """Whether a gate whose trigger is `trigger` asks about the brief at `path`: a pattern of it names briefs and matches."""
    return any(is_brief(pattern) and OwnedPath.parse(pattern).owns(path) for pattern in trigger)


def folded(text: str) -> str:
    """`text` with every run of whitespace one space: a sentence wrapped across lines reads as one."""
    return ' '.join(text.split())


def capped(text: str) -> str:
    return text if len(text) <= CONTEXT_CAP else f'{text[:CONTEXT_CAP]}…'


def _blocks(lines: Sequence[str], start: int) -> list[tuple[int, int]]:
    """The paragraphs of `lines`, whose first is line `start`, as first and last line: a list joins its lead."""
    groups: list[tuple[int, int]] = []
    first: int | None = None
    for index, line in enumerate(lines):
        number = start + index
        if not line.strip():
            if first is not None:
                groups.append((first, number - 1))
                first = None
            continue
        if first is not None and _FIELD.match(line):
            groups.append((first, number - 1))
            first = None
        if first is None:
            first = number
    if first is not None:
        groups.append((first, start + len(lines) - 1))
    merged: list[tuple[int, int]] = []
    for group in groups:
        head = lines[group[0] - start]
        if merged:
            before = lines[merged[-1][1] - start].rstrip()
            listed = _ITEM.match(lines[merged[-1][0] - start]) is not None or before.endswith(':')
            if (_ITEM.match(head) or head[:1] in ' \t') and listed:
                merged[-1] = (merged[-1][0], group[1])
                continue
        merged.append(group)
    return merged


def _span(lines: Sequence[str], first: int, last: int) -> str:
    return '\n'.join(lines[first - 1 : last])


def markdown(text: str, start: int = 1) -> list[Passage]:
    """The paragraphs of markdown `text`, whose first line is line `start`; each run of code is a code passage."""
    lines = text.split('\n')
    held = literal(lines)
    passages: list[Passage] = []
    prose: list[str] = []
    prose_start = start
    code_from: int | None = None

    def flush() -> None:
        for first, last in _blocks(prose, prose_start):
            passages.append(Passage(first, last, _span(lines, first - start + 1, last - start + 1), prose=True))
        prose.clear()

    def close(last: int) -> None:
        if code_from is not None:
            code = _span(lines, code_from - start + 1, last - start + 1)
            passages.append(Passage(code_from, last, code, prose=False))

    for index, line in enumerate(lines):
        number = start + index
        if index + 1 in held:
            if code_from is None:
                flush()
                code_from = number
            continue
        close(number - 1)
        code_from = None
        if not prose:
            prose_start = number
        prose.append(line)
    close(start + len(lines) - 1)
    flush()
    return sorted(passages, key=lambda p: p.first)


def brief(text: str) -> list[Passage]:
    """The sentences of a brief's prose below its front matter, each with its paragraph as context."""
    matter = _FRONT_MATTER.match(text)
    skipped = text[: matter.end()].count('\n') if matter else 0
    body = text[matter.end() :] if matter else text
    sentences: list[Passage] = []
    for paragraph in markdown(body, start=skipped + 1):
        if not paragraph.prose:
            continue
        offset = 0
        for sentence in _SENTENCE.split(paragraph.text):
            at = paragraph.text.index(sentence, offset)
            offset = at + len(sentence)
            first = paragraph.first + paragraph.text.count('\n', 0, at)
            last = first + sentence.count('\n')
            sentences.append(Passage(first, last, sentence.strip(), prose=True, within=capped(paragraph.text)))
    return sentences


@dataclass(frozen=True)
class Module:
    """A Python file read as passages, with its syntax tree."""

    tree: ast.Module
    lines: tuple[str, ...]
    passages: tuple[Passage, ...]

    def source(self, first: int, last: int) -> str:
        return _span(self.lines, first, last)

    def definitions(self, node: ast.stmt) -> str:
        """The module-level assignment or import of each name `node` reads, in the file's order."""
        read = {n.id for n in header(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        found: list[str] = []
        for statement in self.tree.body:
            if statement is node:
                continue
            if isinstance(statement, ast.Import | ast.ImportFrom):
                bound = {(a.asname or a.name).split('.')[0] for a in statement.names}
            elif isinstance(statement, ast.Assign | ast.AnnAssign):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                bound = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            else:
                continue
            if bound & read:
                end = statement.end_lineno or statement.lineno
                found.append(self.source(statement.lineno, min(end, statement.lineno + 2)))
        return '\n'.join(found)

    def comment(self, passage: Passage) -> str:
        """The run of comment lines ending on the line above `passage`: a binding's `#:` documentation."""
        first = passage.first
        while first > 1 and self.lines[first - 2].lstrip().startswith('#'):
            first -= 1
        return self.source(first, passage.first - 1) if first < passage.first else ''

    def spanned(self, passage: Passage, program: Program, path: str) -> list[str]:
        """The program's definition of each dotted name a prose passage writes in a code span, headed by its place.

        `path` is this module's, where a name the module binds itself is
        defined; the definition the passage sits in is not given again.
        """
        own = Source(path, list(self.lines), bindings(self.tree))
        imported = imports(self.tree)
        found: list[str] = []
        for name in dict.fromkeys(_SPANNED.findall(passage.text)):
            for definition in program.spanned(own, imported, name):
                around = definition.path == path and definition.first <= passage.first <= definition.last
                block = f'# {definition.place}\n{definition.text}'
                if not around and block not in found:
                    found.append(block)
        return found

    def context(self, passage: Passage, program: Program | None = None, path: str = '') -> str:
        """What a passage's question needs beside it, each part capped on its own.

        A statement's: where it sits, what encloses it, the comment above it,
        and the module-level names it reads. A docstring's or a comment's:
        where it sits, and, given the `program` and this module's `path`, the
        definitions of what it names in code spans.
        """
        parts = [passage.within, *passage.enclosing]
        if passage.node is not None:
            parts += [self.comment(passage), self.definitions(passage.node)]
        elif program is not None:
            parts += self.spanned(passage, program, path)
        return '\n'.join(capped(part) for part in parts if part)


def _bodies(statement: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists a compound statement holds: its bodies, and each handler's or case's."""
    bodies: list[list[ast.stmt]] = []
    for field in ('body', 'orelse', 'finalbody'):
        value: object = getattr(statement, field, None)
        if isinstance(value, list) and value:
            bodies.append(cast(list[ast.stmt], value))
    for field in ('handlers', 'cases'):
        value = getattr(statement, field, None)
        if isinstance(value, list):
            bodies += [cast(ast.ExceptHandler | ast.match_case, member).body for member in cast(list[object], value)]
    return bodies


def _header_end(lines: Sequence[str], start: int, before: int) -> int:
    """The last line of a header that opens at line `start`, its body or next clause opening after line `before`.

    Comment and blank lines between the two are the body's, not the header's.
    """
    last = before
    while last > start and (not lines[last - 1].strip() or lines[last - 1].lstrip().startswith('#')):
        last -= 1
    return max(start, last)


def _headed(lines: Sequence[str], statement: ast.stmt, bodies: Sequence[Sequence[ast.stmt]]) -> int:
    """The last line of a compound statement's own header: before its first body, or a `match`'s first `case`."""
    if isinstance(statement, ast.Match):
        return _header_end(lines, statement.lineno, statement.cases[0].pattern.lineno - 1)
    return _header_end(lines, statement.lineno, min(b[0].lineno for b in bodies) - 1)


def _opening(lines: Sequence[str], statement: ast.stmt, body: Sequence[ast.stmt], header: str) -> str:
    """What opens `body` of the compound `statement` whose header is `header`: the header, and the clause of a body after the first.

    A handler's and a case's clause is its own node. An `else:` or a
    `finally:` has none, so it is the line at the statement's own indent
    nearest above the body's first statement, or that statement's own line,
    where that line opens with its keyword; an `elif` is a statement of its
    own, the `if` nested in the body, so the header alone opens it.
    """
    if body is getattr(statement, 'body', None):
        return header
    clauses = cast(
        list[ast.ExceptHandler | ast.match_case],
        [*getattr(statement, 'handlers', ()), *getattr(statement, 'cases', ())],
    )
    for clause in clauses:
        if body is clause.body:
            node = clause.pattern if isinstance(clause, ast.match_case) else clause
            return f'{header}\n{_span(lines, node.lineno, _header_end(lines, node.lineno, body[0].lineno - 1))}'
    for number in range(body[0].lineno, statement.lineno, -1):
        line = lines[number - 1]
        if not line.strip() or line.lstrip().startswith('#') or len(line) - len(line.lstrip()) != statement.col_offset:
            continue
        return f'{header}\n{line}' if _CLAUSE.match(line) else header
    return header


def _docstring(body: Sequence[ast.stmt]) -> ast.Expr | None:
    """The docstring opening `body`, a module's, a class's or a function's; None where it opens with none."""
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first
    return None


def python(text: str) -> Module | None:
    """`text` as passages; None where it does not parse, which no gate then draws from."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = tuple(text.split('\n'))
    passages: list[Passage] = []

    def statement_passage(statement: ast.stmt, first: int, last: int, within: str, enclosing: tuple[str, ...]) -> None:
        passages.append(Passage(first, last, _span(lines, first, last), False, within, statement, enclosing))

    def walk(body: Sequence[ast.stmt], within: str, documented: bool, enclosing: tuple[str, ...]) -> None:
        docstring = _docstring(body) if documented else None
        for statement in body:
            if statement is docstring:
                end = statement.end_lineno or statement.lineno
                for first, last in _blocks(lines[statement.lineno - 1 : end], statement.lineno):
                    passages.append(Passage(first, last, _span(lines, first, last), prose=True, within=within))
                continue
            bodies = _bodies(statement)
            if not bodies:
                end = statement.end_lineno or statement.lineno
                statement_passage(statement, statement.lineno, end, within, enclosing)
                continue
            decorated = isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            decorators = statement.decorator_list if decorated else []
            first = min([statement.lineno, *(d.lineno for d in decorators)])
            last = _headed(lines, statement, bodies)
            statement_passage(statement, first, last, within, enclosing)
            if decorated:
                inner = _span(lines, first, last)
                opened = _docstring(statement.body)
                held = (_span(lines, opened.lineno, opened.end_lineno or opened.lineno),) if opened else ()
                for nested in bodies:
                    walk(nested, inner, True, held)
                continue
            header = _span(lines, statement.lineno, last)
            for nested in bodies:
                walk(nested, within, False, (*enclosing, _opening(lines, statement, nested, header)))

    walk(tree.body, '', True, ())
    passages += _comments(text, lines)
    return Module(tree, lines, tuple(sorted(passages, key=lambda p: (p.first, p.last))))


def _comments(text: str, lines: Sequence[str]) -> list[Passage]:
    """Each run of lines holding nothing but a comment."""
    numbers: list[int] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT and not lines[token.start[0] - 1][: token.start[1]].strip():
                numbers.append(token.start[0])
    except (tokenize.TokenError, IndentationError):
        return []
    runs: list[tuple[int, int]] = []
    for number in numbers:
        if runs and runs[-1][1] == number - 1:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return [Passage(first, last, _span(lines, first, last), prose=True) for first, last in runs]


#: The fields of a compound statement that hold its body rather than its header.
_BODY_FIELDS = frozenset({'body', 'orelse', 'finalbody', 'handlers', 'cases'})


def fields(statement: ast.stmt) -> Iterator[ast.AST]:
    """The nodes directly under `statement` outside its body: all of a simple statement's, a compound one's header's."""
    for field, value in ast.iter_fields(statement):
        if field in _BODY_FIELDS:
            continue
        children = cast(list[object], value) if isinstance(value, list) else [value]
        yield from (child for child in children if isinstance(child, ast.AST))


def header(statement: ast.stmt) -> Iterator[ast.AST]:
    """Every node of `statement` outside its body: all of a simple statement, a compound one's header."""
    yield statement
    for child in fields(statement):
        yield from ast.walk(child)


def strings(statement: ast.stmt) -> Iterator[str]:
    """The text of every string literal in `statement`'s header, an f-string's literal parts among them."""
    for child in header(statement):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value
