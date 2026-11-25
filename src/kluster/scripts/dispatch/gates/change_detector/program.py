"""The program's side of an assertion: the definition of each program name a case reads, as the head holds it.

Whether an assertion's literal is a mirror turns on where the value it is
held against comes from: a constant typed at its own declaration, or a value
a rule computes (docs/style/testing.md). A test module's `from kluster…
import NAME` says neither, so the item gives the definition beside it. The
program is every module under `src/`.

The reach is one level, the way the test module's own bindings are given:
the names the case reads that the module imports from the program, and for
an imported module, the attributes the case reads off it. A name a module
binds by an absolute import from another is followed once, which is how a
package's `__init__.py` re-exports what a submodule defines. Nothing the
definition itself reads is followed. A definition is given as the test
module's own statements are (`module`): an assignment whole, a function or a
class by its signature and docstring.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .module import Binding, bindings, definition_end, first_line

#: Where the program's modules are, relative to the repository root.
SOURCE = 'src'

Text = Callable[[str], str | None]


@dataclass(frozen=True)
class Definition:
    """One statement of a program module, and where it is."""

    path: str
    first: int
    last: int
    text: str


@dataclass(frozen=True)
class _Module:
    path: str
    lines: list[str]
    bound: dict[str, Binding]


def imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Each name a test module binds by an absolute `from … import`, and the module and the name it imports.

    Whether the module is the program's is read off the head when a case
    reads the name (`definitions`).
    """
    found: dict[str, tuple[str, str]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0:
            for alias in statement.names:
                found[alias.asname or alias.name] = (statement.module or '', alias.name)
    return found


class Program:
    """The program as one head holds it: each path read, and each module parsed, once for every item drawn from it."""

    def __init__(self, text: Text) -> None:
        self._text = functools.cache(text)
        self._modules: dict[str, _Module | None] = {}

    def definitions(
        self, imported: dict[str, tuple[str, str]], names: set[str], attributes: dict[str, set[str]]
    ) -> Iterator[Definition]:
        """The definitions of what a case reads: each imported name in `names`, and each attribute it reads off an imported module.

        `attributes` maps a name to the attributes the case reads off it. An
        import the case reads nothing of is not looked up.
        """
        for local, (module, name) in sorted(imported.items()):
            if local not in names and local not in attributes:
                continue
            submodule = self._module(f'{module}.{name}')
            if submodule is not None:
                for attribute in sorted(attributes.get(local, ())):
                    yield from self._defined(submodule, attribute)
            else:
                held = self._module(module)
                if held is not None:
                    yield from self._defined(held, name)

    def _module(self, name: str) -> _Module | None:
        """The program module called `name` at the head; None where the head holds no such module."""
        if name not in self._modules:
            self._modules[name] = self._read(name)
        return self._modules[name]

    def _read(self, name: str) -> _Module | None:
        stem = f'{SOURCE}/{name.replace(".", "/")}'
        for path in (f'{stem}.py', f'{stem}/__init__.py'):
            source = self._text(path)
            if source is not None:
                return _Module(path, source.split('\n'), bindings(ast.parse(source)))
        return None

    def _defined(self, module: _Module, name: str, *, follow: bool = True) -> Iterator[Definition]:
        """The statement `module` defines `name` by, following one import of it from another program module."""
        statement = module.bound.get(name)
        if isinstance(statement, ast.ImportFrom):
            original = next(a.name for a in statement.names if (a.asname or a.name) == name)
            held = self._module(statement.module or '') if follow and statement.level == 0 else None
            if held is not None:
                yield from self._defined(held, original, follow=False)
            return
        if statement is None:
            return
        first = first_line(statement)
        last = definition_end(statement)
        yield Definition(module.path, first, last, '\n'.join(module.lines[first - 1 : last]))
