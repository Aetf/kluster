"""The `change-detector` gate's extractor: each assertion a change writes in a test, with what decides its question.

Whether an assertion is a change detector turns on where each side of it
comes from: a literal typed in the case, a constant the program types at its
declaration, a value the program computes, a file no import reaches. So an
item is one assertion, an `assert` statement or a `pytest.raises(match=…)`,
any of whose lines the change writes or rewrites, and its context is what a
reader needs to trace its sides and nothing more, in this order:

-   the test module's top-level statements binding the names the case reads
    or takes as a fixture (`module`);
-   the program's definitions of the names among them it imports from the
    program, and of the attributes the case reads off an imported program
    module (`program`);
-   the case the assertion sits in, whole: the outermost function around
    it. An assertion at module level has none.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from ...paths import OwnedPath
from ..extract import Change, Item
from . import program
from .module import Binding, Function, bindings, definition_end, first_line

#: The files whose assertions the gate reads.
TESTS = 'tests/**.py'


@dataclass(frozen=True)
class _Check:
    """An assertion, and the case it sits in; None at module level."""

    node: ast.Assert | ast.Call
    case: Function | None


@dataclass(frozen=True)
class _Module:
    """A test module at the head, as its items' contexts read it."""

    path: str
    lines: Sequence[str]
    bound: dict[str, Binding]
    imported: dict[str, tuple[str, str]]


def assertions(change: Change) -> list[Item]:
    """Each assertion `change` writes or rewrites in a test module, as an item with its case and what it reads."""
    tests = OwnedPath.parse(TESTS)
    code = program.Program(change.text)
    found: list[Item] = []
    for changed in change.files:
        if not changed.written or not tests.owns(changed.path):
            continue
        source = change.text(changed.path)
        if source is None:
            continue
        tree = ast.parse(source)
        module = _Module(changed.path, source.split('\n'), bindings(tree), program.imports(tree))
        found.extend(_items(code, module, tree, changed.written))
    return found


def _items(
    code: program.Program, module: _Module, tree: ast.Module, written: Sequence[tuple[int, int]]
) -> Iterator[Item]:
    for check in sorted(_checks(tree), key=lambda c: (c.node.lineno, c.node.col_offset)):
        first, last = check.node.lineno, check.node.end_lineno or check.node.lineno
        if not any(first <= end and start <= last for start, end in written):
            continue
        yield Item(module.path, first, last, _span(module.lines, first, last), _context(code, module, check))


def _checks(tree: ast.Module) -> Iterator[_Check]:
    """Every assertion of the module, with the outermost function around it."""
    for statement in tree.body:
        case = statement if isinstance(statement, Function) else None
        for inner in ast.walk(statement):
            if isinstance(inner, ast.Assert) or (isinstance(inner, ast.Call) and _raises_matching(inner)):
                yield _Check(inner, case)


def _raises_matching(call: ast.Call) -> bool:
    """Whether `call` is `pytest.raises` given a `match`."""
    match call.func:
        case ast.Attribute(value=ast.Name(id='pytest'), attr='raises'):
            return any(keyword.arg == 'match' for keyword in call.keywords)
        case _:
            return False


def _context(code: program.Program, module: _Module, check: _Check) -> str:
    """The test module's statements the case reads, the program's definitions of them, then the case."""
    case = check.case
    scope: ast.AST = case if case is not None else check.node
    names = {n.id for n in ast.walk(scope) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    if case is not None:
        arguments = case.args
        names |= {a.arg for a in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    attributes: dict[str, set[str]] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attributes.setdefault(node.value.id, set()).add(node.attr)
    read = {id(s): s for name, s in module.bound.items() if name in names and s is not case}
    blocks = [
        _block(module.path, first_line(s), definition_end(s), module.lines)
        for s in sorted(read.values(), key=lambda s: s.lineno)
    ]
    defined = code.definitions(module.imported, names, attributes)
    blocks += [f'{_heading(d.path, d.first, d.last)}\n{d.text}' for d in sorted(set(defined), key=_where)]
    if case is not None:
        first, last = first_line(case), case.end_lineno or case.lineno
        blocks.append(f'{_heading(module.path, first, last)}, the enclosing case\n{_span(module.lines, first, last)}')
    return '\n\n'.join(blocks)


def _where(definition: program.Definition) -> tuple[str, int]:
    return definition.path, definition.first


def _block(path: str, first: int, last: int, lines: Sequence[str]) -> str:
    return f'{_heading(path, first, last)}\n{_span(lines, first, last)}'


def _heading(path: str, first: int, last: int) -> str:
    return f'# {Item(path, first, last, "").place}'


def _span(lines: Sequence[str], first: int, last: int) -> str:
    return '\n'.join(lines[first - 1 : last])
