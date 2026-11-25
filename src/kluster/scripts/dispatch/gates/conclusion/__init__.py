"""`gate: conclusion`: does this state where something runs, when a runtime mechanism derives that?

The canon is the section of style/pulumi.md that holds "The program
declares constraints, not conclusions". The extractor narrows a change to
the passages it writes about placement:

-   in the components, the Kubernetes library and the conventions, a
    statement whose words reach placement (`_PLACEMENT_CODE`): an affinity,
    a node selector, a toleration, a requirement or a constraint, a site, a
    pool, a hold; with the header it sits in and the module-level names it
    reads, so a label or a requirement it takes from elsewhere is in view.
    A statement longer than `LONG` lines is asked about in its parts, each
    argument or entry that reaches placement;
-   there too, a docstring or comment paragraph whose words speak of where
    something runs: a node, a site, a pool (`_PLACEMENT_PROSE`);
-   in the brief of an issue in `area/cluster`, a sentence of either kind.

What the model is asked is whether the item states the answer the
scheduler or a volume binding derives, where the program should declare
the fact it is derived from.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence

from ...paths import OwnedPath
from ..extract import Change, Item, written_here
from ..gate import Canon, Spec
from ..reading import Module, Passage, brief, fields, folded, is_brief, python

#: The Python the extractor reads: where the program declares a workload and where it runs.
CODE = ('src/kluster/components/**', 'src/kluster/lib/k8s*', 'src/kluster/conventions/**')
#: The briefs of an issue in `area/cluster`, wherever that area falls among the issue's.
BRIEFS = (':brief/cluster/**', ':brief/**/cluster/**')
TRIGGER = (*CODE, *BRIEFS)

#: The most lines an item runs to: a longer statement, a pod's or a workload's
#: declaration, is asked about in its parts (`_items`).
LONG = 20

#: Words of a statement that reach where a pod runs.
_PLACEMENT_CODE = re.compile(
    r'affinity|node_?selector|toleration|node_?requirement|node_?name|topology|\bat_site\b|\bat_home\b'
    r'|on_architecture|\bSite\.|placement|_POOL\b|\bpool\b|constraints|requirements|_HOLD\b|held_to|offered_on'
    r'|kubernetes\.io/|CONTROL_PLANE|\bWORKER|HOMELAB|\bkept\(|priority_?class',
    re.I,
)
#: Words of prose that speak of where something runs: a node, a site, a pool.
_PLACEMENT_PROSE = re.compile(
    r'\b(?:runs?|running|scheduled|placed|lands?|held|holds?|pinned|pins|binds?|bound|kept|keeps?|goes)\b'
    r'[^.]{0,60}\b(?:on|at|to|onto)\b[^.]{0,40}\b(?:nodes?|worker|cloud|home|site|pool|control planes?)\b'
    r'|\b(?:one|the one|a single|every|no|which) node\b|\bnodes? (?:that )?offers?\b'
    r'|affinity|node selector|toleration|\brequirements?\b|\bconstraints?\b',
    re.I,
)


def _drawn(passage: Passage) -> bool:
    if passage.prose:
        return _PLACEMENT_PROSE.search(folded(passage.text)) is not None
    if isinstance(passage.node, ast.Import | ast.ImportFrom):
        return False
    return _PLACEMENT_CODE.search(passage.text) is not None


def _parts(node: ast.AST) -> Iterator[tuple[int, int, ast.AST]]:
    """The parts of an expression a long statement is asked about in, each with its first and last line.

    A part is a call's argument, keyword or positional, an entry of a dict,
    an element of a list, a tuple or a set, or a function's parameter: the
    smallest unit that holds one fact a declaration states. Any other node is
    looked through to the parts under it; a statement, to those of its
    header alone (`reading.fields`), its body being statements of their own.
    """
    if isinstance(node, ast.stmt):
        for child in fields(node):
            yield from _parts(child)
    elif isinstance(node, ast.arguments):
        for parameter in [*node.posonlyargs, *node.args, node.vararg, *node.kwonlyargs, node.kwarg]:
            if parameter is not None:
                yield parameter.lineno, parameter.end_lineno or parameter.lineno, parameter
    elif isinstance(node, ast.Call):
        for part in [*node.args, *node.keywords]:
            yield part.lineno, part.end_lineno or part.lineno, part
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            yield (key or value).lineno, value.end_lineno or value.lineno, value
    elif isinstance(node, ast.List | ast.Tuple | ast.Set):
        for element in node.elts:
            yield element.lineno, element.end_lineno or element.lineno, element
    else:
        for child in ast.iter_child_nodes(node):
            yield from _parts(child)


def _under(patterns: Sequence[str], path: str) -> bool:
    return any(OwnedPath.parse(pattern).owns(path) for pattern in patterns)


def extract(change: Change) -> list[Item]:
    """The passages the change writes about where something runs."""
    items: list[Item] = []
    for changed in change.files:
        text = change.text(changed.path) if changed.written and written_here(changed.path) else None
        if text is None:
            continue
        path, written = changed.path, changed.written
        if is_brief(path):
            if _under(BRIEFS, path):
                items += [
                    Item(path, s.first, s.last, s.text, s.within)
                    for s in brief(text)
                    if s.touches(written) and _drawn(s)
                ]
            continue
        module = python(text) if path.endswith('.py') and _under(CODE, path) else None
        if module is None:
            continue
        for passage in module.passages:
            if passage.touches(written) and _drawn(passage):
                items += _items(path, module, passage, written)
    return items


def _spans(
    module: Module, node: ast.AST, first: int, last: int, written: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """What a long `node`, its lines `first` to `last`, is asked about in.

    Each of its outermost parts (`_parts`) of at most `LONG` lines that the
    change writes in and that reaches placement, kept within `first` to
    `last`; a longer part, in the same way. Where `node` holds no such part,
    as a long string holds none, each written line of it that reaches
    placement.
    """
    found: list[tuple[int, int]] = []
    for start, end, part in _parts(node):
        start, end = max(start, first), min(end, last)
        touched = any(start <= b and a <= end for a, b in written)
        if start > end or not touched or not _PLACEMENT_CODE.search(module.source(start, end)):
            continue
        found += [(start, end)] if end - start < LONG else _spans(module, part, start, end, written)
    if found:
        return found
    clipped = [range(max(a, first), min(b, last) + 1) for a, b in written]
    return [(n, n) for numbers in clipped for n in numbers if _PLACEMENT_CODE.search(module.source(n, n))]


def _items(path: str, module: Module, passage: Passage, written: Sequence[tuple[int, int]]) -> list[Item]:
    """The passage as one item; a statement longer than `LONG` lines as its parts (`_parts`) that reach placement.

    A long statement is a pod's or a workload's whole declaration, or a
    function's header, of which a placement is one argument, one entry or one
    parameter (`_spans`).
    """
    if passage.prose:
        return [Item(path, passage.first, passage.last, passage.text, passage.within)]
    context = module.context(passage)
    if passage.last - passage.first < LONG or passage.node is None:
        return [Item(path, passage.first, passage.last, passage.text, context)]
    within = f'{module.source(passage.first, passage.first)}\n{context}'
    spans = _spans(module, passage.node, passage.first, passage.last, written)
    return [Item(path, first, last, module.source(first, last), within) for first, last in spans]


GATE = Spec(
    canon=Canon('docs/style/pulumi.md', 'Resources and their contents'),
    trigger=TRIGGER,
    extract=extract,
)
