"""`gate: enumeration`: does this name a set's members where a definition would cover them?

The canon is style/README.md, "A membership is a definition, not a list".
The extractor narrows a change to the passages that can enumerate a set,
among those the change writes:

-   in Python under `src/` and `tests/`, a collection display of names, ids
    or paths (`_listing`) that a statement defines or acts on, rather than
    one handed to an ordinary call as its arguments; a run of sibling calls
    of one function, each naming a member by a literal, which is a
    registration census (`keep('authelia', …)`); and a statement or prose
    passage whose words name members as a series (`_SERIES`). Each is
    given its context as the reader gives it (`reading.Module.context`):
    a statement, what it sits in and the definitions it reads, where a
    definition of its set is often stated; a docstring or a comment, the
    program's definitions of what it names in code spans, which is where
    the set it lists may be stated;
-   in the style rules, a paragraph whose words do;
-   in a brief, a sentence telling the agent to add an entry somewhere.

What the model is asked is whether the item names members where a
definition would take the next one, so that the next member needs an edit
here; a definition, today's instances after one, and a census nothing can
derive pass.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence

from ...paths import OwnedPath
from ..extract import Change, Item, written_here
from ..gate import Canon, Spec
from ..program import Program
from ..reading import Module, Passage, brief, folded, header, is_brief, markdown, python, strings

#: The Python the extractor reads, the style rules, and every brief.
CODE = ('src/**', 'tests/**')
RULES = 'docs/style/**'
TRIGGER = (*CODE, RULES, ':brief/**')

#: Words that name members as a series: three or more joined by commas and a
#: conjunction; two or more with a word that closes the set; a list; or
#: members given as today's instances.
_SERIES = re.compile(
    r'[^,;:.()]{1,80}(?:,\s+[^,;:.()]{1,80}){1,},?\s+(?:and|or|nor)\s+\S'
    r'|\b(?:alone|only|nothing else|no other|neither|either|both)\b[^.]{0,120}\b(?:or|and|nor)\b'
    r'|\b(?:or|and|nor)\b[^.]{0,120}\b(?:alone|only|nothing else|no other)\b'
    r"|\btoday(?:'s| that is|:)",
    re.I,
)
#: Two or more lines of a passage opening a list item.
_LIST = re.compile(r'^\s*(?:[-*+•]|\d+[.)])\s+\S', re.M)
#: A brief's sentence telling the agent to add an entry somewhere.
_ENTRY = re.compile(
    r'\b(?:add|adds|adding|append|appends|register|registers|enter|list|name)\b[^.]{0,80}?'
    r'\b(?:entry|entries|row|rows|member|members|case|cases|item|items|list|census|table|set)\b',
    re.I,
)

#: The calls a collection handed as an argument is still a membership in.
_MEMBERSHIP_CALLS = frozenset({'frozenset', 'set', 'tuple', 'list', 'sorted', 'dict', 'parametrize'})


def _series(text: str) -> bool:
    return len(_LIST.findall(text)) >= 2 or _SERIES.search(folded(text)) is not None


def _atom(node: ast.expr) -> bool:
    """A member named outright: a literal, a name, a dotted or subscripted name, or a tuple of them."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Attribute):
        return _atom(node.value)
    if isinstance(node, ast.Subscript):
        return _atom(node.value) and _atom(node.slice)
    if isinstance(node, ast.Tuple):
        return all(_atom(e) for e in node.elts)
    return isinstance(node, ast.Name)


def _members(display: ast.expr) -> list[ast.expr] | None:
    """The members a collection display names, where it is one; None for any other expression."""
    if isinstance(display, ast.List | ast.Tuple | ast.Set):
        return list(display.elts)
    if isinstance(display, ast.Dict):
        return [k for k in display.keys if k is not None]
    return None


def _named_set(members: Sequence[ast.expr]) -> bool:
    """Whether `members` are two or more members, each named outright."""
    return len(members) >= 2 and all(_atom(m) for m in members)


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''


def _unpacks(assignment: ast.Assign | ast.AnnAssign) -> bool:
    """Whether `assignment` binds several names at once, its value being their values rather than a set."""
    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
    return any(isinstance(t, ast.Tuple | ast.List) for t in targets)


def _listing(statement: ast.stmt) -> bool:
    """Whether `statement` defines or acts on a set it names member by member.

    A display is a membership where a statement binds it, iterates it or
    tests against it, or where it is handed to a call that makes a set of
    it or parametrizes a case; one handed to any other call is that call's
    arguments, the words of a command among them.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for parent in header(statement):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in header(statement):
        members = _members(node) if isinstance(node, ast.expr) else None
        if members is None or not _named_set(members) or isinstance(getattr(node, 'ctx', None), ast.Store):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.For | ast.comprehension) and node is not parent.iter:
            continue
        if isinstance(parent, ast.List | ast.Tuple | ast.Set | ast.Dict) and _members(parent) is not None:
            continue
        if isinstance(parent, ast.Call | ast.keyword):
            call = parents.get(parent) if isinstance(parent, ast.keyword) else parent
            if isinstance(call, ast.Call) and _callee(call) in _MEMBERSHIP_CALLS:
                return True
            continue
        if isinstance(parent, ast.Assign | ast.AnnAssign | ast.For | ast.comprehension | ast.Compare):
            if isinstance(parent, ast.Assign | ast.AnnAssign) and (node is not parent.value or _unpacks(parent)):
                continue
            return True
        if isinstance(parent, ast.arguments):
            return True
    return False


#: What holds a run of registrations as its body, where the module does not.
type _Owner = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _registrations(module: Module, written: Sequence[tuple[int, int]]) -> Iterator[tuple[Item, set[int]]]:
    """Each run of sibling calls of one function, each naming a member by a literal, that the change writes in.

    The item runs from the header of the function the calls are the body of,
    where they are, to the last call; its text is each call's function and
    first argument. Also given: the lines of the calls, which no other item
    of the extractor's then covers.
    """
    bodies: list[tuple[Sequence[ast.stmt], _Owner | None]] = [(module.tree.body, None)]
    for node in ast.walk(module.tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bodies.append((node.body, node))
    for body, owner in bodies:
        calls: dict[str, list[ast.Call]] = {}
        for statement in body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                first = call.args[0] if call.args else None
                if isinstance(call.func, ast.Name) and isinstance(first, ast.Constant):
                    calls.setdefault(call.func.id, []).append(call)
        for name, run in calls.items():
            if len(run) < 2:
                continue
            spans = [(c.lineno, c.end_lineno or c.lineno) for c in run]
            if not any(a <= last and first <= b for a, b in spans for first, last in written):
                continue
            start = owner.lineno if owner is not None else spans[0][0]
            header = module.source(owner.lineno, owner.body[0].lineno - 1) if owner is not None else ''
            members = [f'{name}({ast.unparse(c.args[0])}, …)' for c in run]
            text = '\n'.join(([header] if header else []) + members)
            covered = {n for a, b in spans for n in range(a, b + 1)}
            yield Item('', start, spans[-1][1], text), covered


def _python(code: Program, path: str, text: str, written: Sequence[tuple[int, int]]) -> list[Item]:
    module = python(text)
    if module is None:
        return []
    items: list[Item] = []
    covered: set[int] = set()
    for item, lines in _registrations(module, written):
        items.append(Item(path, item.line, item.end_line, item.text))
        covered |= lines
    for passage in module.passages:
        if not passage.touches(written) or any(n in covered for n in range(passage.first, passage.last + 1)):
            continue
        if _drawn(passage):
            items.append(Item(path, passage.first, passage.last, passage.text, module.context(passage, code, path)))
    return sorted(items, key=lambda i: (i.line, i.end_line))


def _drawn(passage: Passage) -> bool:
    if passage.prose:
        return _series(passage.text)
    node = passage.node
    if node is None:
        return False
    if _listing(node):
        return True
    return any(_series(text) for text in strings(node) if len(text.split()) > 3)


def _prose(path: str, passages: Sequence[Passage], written: Sequence[tuple[int, int]]) -> list[Item]:
    return [
        Item(path, p.first, p.last, p.text, p.within)
        for p in passages
        if p.prose and p.touches(written) and _series(p.text)
    ]


def _under(patterns: Sequence[str], path: str) -> bool:
    return any(OwnedPath.parse(pattern).owns(path) for pattern in patterns)


def extract(change: Change) -> list[Item]:
    """The passages the change writes that can name a set's members."""
    items: list[Item] = []
    code = Program(change.text)
    for changed in change.files:
        text = change.text(changed.path) if changed.written and written_here(changed.path) else None
        if text is None:
            continue
        path, written = changed.path, changed.written
        if is_brief(path):
            items += [
                Item(path, s.first, s.last, s.text, s.within)
                for s in brief(text)
                if s.touches(written) and _ENTRY.search(folded(s.text))
            ]
        elif path.endswith('.py') and _under(CODE, path):
            items += _python(code, path, text, written)
        elif path.endswith('.md') and _under((RULES,), path):
            items += _prose(path, markdown(text), written)
    return items


GATE = Spec(
    canon=Canon('docs/style/README.md', 'A membership is a definition, not a list'),
    trigger=TRIGGER,
    extract=extract,
)
