"""The program as a head holds it: the definition of a program name an item reads, beside the item.

Whether a passage restates what the program decides turns on what the
program says there: a constant typed at its own declaration, a value a rule
computes (docs/style/testing.md), the census a list in prose names again
(docs/style/README.md). A `from kluster… import NAME` in a test, or a
dotted name in a docstring's code span, says neither, so the item gives the
definition beside it. The program is every module under `src/`.

Two readers ask for definitions:

-   a test module's case, by the names it reads off its absolute imports
    (`definitions`): a name it imports from the program, an attribute it
    reads off an imported program module, and a member it reads off either
    of those where that is an object of a class the program defines;
-   a prose passage, by the dotted names it writes in code spans
    (`spanned`): each resolved through its own module's imports, or as
    an absolute module path, or as a binding of its own module.

The reach is one name and the member read off it. A name a module binds
by an absolute import from another is followed once, which is how a
package's `__init__.py` re-exports what a submodule defines. Nothing a
definition itself reads is followed. A definition is given as a test
module's own statements are (`module`): an assignment whole, a function or
a class by its signature and docstring.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass

from .module import Binding, bindings, definition_end, first_line

#: Where the program's modules are, relative to the repository root.
SOURCE = 'src'

Text = Callable[[str], str | None]
#: A dotted read off a name, the name first: `(name,)`, `(name, attribute)`, `(name, attribute, member)`.
Read = tuple[str, ...]


@dataclass(frozen=True)
class Definition:
    """One statement of a program module, and where it is."""

    path: str
    first: int
    last: int
    text: str

    @property
    def place(self) -> str:
        return f'{self.path}:{self.first}' if self.first == self.last else f'{self.path}:{self.first}–{self.last}'


@dataclass(frozen=True)
class Source:
    """A module as the program reader reads it: its path, its lines and its top-level bindings."""

    path: str
    lines: list[str]
    bound: dict[str, Binding]

    @classmethod
    def parse(cls, path: str, text: str) -> Source:
        return cls(path, text.split('\n'), bindings(ast.parse(text)))


def imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Each name a module binds by an absolute `from … import`, and the module and the name it imports.

    Whether the module is the program's is read off the head when a reader
    asks for the name (`definitions`).
    """
    found: dict[str, tuple[str, str]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0:
            for alias in statement.names:
                found[alias.asname or alias.name] = (statement.module or '', alias.name)
    return found


def reads(scope: ast.AST) -> set[Read]:
    """Each name `scope` reads, and each attribute, and member of it, read off a name."""
    found: set[Read] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found.add((node.id,))
        elif isinstance(node, ast.Attribute):
            match node.value:
                case ast.Name(id=name):
                    found.add((name, node.attr))
                case ast.Attribute(value=ast.Name(id=name), attr=attribute):
                    found.add((name, attribute, node.attr))
                case _:
                    pass
    return found


def _given(module: Source, statement: Binding) -> Definition:
    first = first_line(statement)
    last = definition_end(statement)
    return Definition(module.path, first, last, '\n'.join(module.lines[first - 1 : last]))


class Program:
    """The program as one head holds it: each path read, and each module parsed, once for every item drawn from it."""

    def __init__(self, text: Text) -> None:
        self._text = functools.cache(text)
        self._modules: dict[str, Source | None] = {}

    def definitions(self, imported: Mapping[str, tuple[str, str]], read: Collection[Read]) -> Iterator[Definition]:
        """The definitions of what a case reads (`reads`) off the names `imported` binds (`imports`).

        An imported name the case reads is given; an imported program
        module, the attribute the case reads off it. A member read off
        either is given too, where what it is read off is an object of a
        class the program defines. An import the case reads nothing of is not
        looked up.
        """
        for local, (module, name) in sorted(imported.items()):
            off = sorted(r[1:] for r in read if r[0] == local)
            if not off:
                continue
            submodule = self._module(f'{module}.{name}')
            if submodule is not None:
                for attribute in sorted({rest[0] for rest in off if rest}):
                    members = {rest[1] for rest in off if len(rest) == 2 and rest[0] == attribute}
                    yield from self._given(submodule, attribute, members)
            else:
                held = self._module(module)
                if held is not None:
                    yield from self._given(held, name, {rest[0] for rest in off if rest})

    def spanned(self, own: Source, imported: Mapping[str, tuple[str, str]], dotted: str) -> Iterator[Definition]:
        """The definition a code span's `dotted` name names, read from `own`, the module the span is written in.

        Its first name is resolved through `own`'s absolute imports
        (`imported`) where it is one of them, as a module path where it is
        a program module, and as a binding of `own` otherwise, which a
        plain `import` of a module outside the program is not. Of what
        follows, the longest run that names a program module is that module,
        and the rest a binding of it and a member of that.
        """
        first, *rest = dotted.split('.')
        if first in imported:
            module, name = imported[first]
            qualified = [*module.split('.'), name, *rest]
        elif self._module(first) is not None:
            qualified = [first, *rest]
        else:
            if not isinstance(own.bound.get(first), ast.Import):
                yield from self._given(own, first, set(rest[:1]) if len(rest) == 1 else set())
            return
        for cut in range(len(qualified) - 1, 0, -1):
            held = self._module('.'.join(qualified[:cut]))
            tail = qualified[cut:]
            if held is not None:
                if len(tail) <= 2:
                    yield from self._given(held, tail[0], set(tail[1:]))
                return

    def _module(self, name: str) -> Source | None:
        """The program module called `name` at the head; None where the head holds no such module."""
        if name not in self._modules:
            self._modules[name] = self._read(name)
        return self._modules[name]

    def _read(self, name: str) -> Source | None:
        stem = f'{SOURCE}/{name.replace(".", "/")}'
        for path in (f'{stem}.py', f'{stem}/__init__.py'):
            source = self._text(path)
            if source is not None:
                return Source.parse(path, source)
        return None

    def _binding(self, module: Source, name: str, *, follow: bool = True) -> tuple[Source, Binding] | None:
        """The statement `module` binds `name` by, and the module it is in, following one import of it."""
        statement = module.bound.get(name)
        if isinstance(statement, ast.ImportFrom):
            original = next(a.name for a in statement.names if (a.asname or a.name) == name)
            held = self._module(statement.module or '') if follow and statement.level == 0 else None
            return self._binding(held, original, follow=False) if held is not None else None
        if statement is None:
            return None
        return module, statement

    def _given(self, module: Source, name: str, members: Collection[str]) -> Iterator[Definition]:
        """The definition of `name` in `module`, then each of `members` of the class it is an object of."""
        bound = self._binding(module, name)
        if bound is None:
            return
        yield _given(*bound)
        held = self._class(*bound) if members else None
        if held is None:
            return
        where, cls = held
        for member in sorted(members):
            statement = _member(cls, member)
            if statement is not None:
                yield _given(where, statement)

    def _class(self, module: Source, statement: Binding) -> tuple[Source, ast.ClassDef] | None:
        """The class `statement` binds, or the program class whose call it assigns; None for anything else."""
        if isinstance(statement, ast.ClassDef):
            return module, statement
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        match value:
            case ast.Call(func=ast.Name(id=callee)):
                bound = self._binding(module, callee)
                if bound is not None and isinstance(bound[1], ast.ClassDef):
                    return bound[0], bound[1]
                return None
            case _:
                return None


def _member(cls: ast.ClassDef, name: str) -> Binding | None:
    """The statement of `cls`'s body that defines its member `name`: a method, a property, a field."""
    for statement in cls.body:
        match statement:
            case ast.FunctionDef(name=bound) | ast.AsyncFunctionDef(name=bound) if bound == name:
                return statement
            case ast.AnnAssign(target=ast.Name(id=bound)) if bound == name:
                return statement
            case ast.Assign(targets=targets) if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                return statement
            case _:
                pass
    return None
