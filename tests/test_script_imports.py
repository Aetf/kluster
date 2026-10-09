"""Every third-party module a console script imports is one the package installs.

`[project.scripts]` installs every script with the package, so a script has
to run wherever the package is installed, the dev group absent: a wheel, or
`uv sync --no-dev`. What it imports from outside the standard library and this
repository's own packages therefore comes from `[project] dependencies`,
never from the dev group, where `uv run`'s default install would hide the
gap until an install without it.

The census reads every module under `src/kluster/scripts/` from its syntax
tree: an import inside a function is one, and an import under
`if TYPE_CHECKING:` is not, since nothing runs it. A module is third-party
when it is neither the standard library's nor a package under `src/`; it is
installed when a distribution that provides it, as the installed metadata
says, is named in `[project] dependencies`.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / 'src' / 'kluster' / 'scripts'

#: This repository's own top-level packages, which the package itself installs.
FIRST_PARTY = frozenset(path.name for path in (ROOT / 'src').iterdir() if path.is_dir())


def runtime_imports(source: str) -> list[tuple[int, str]]:
    """Each top-level module `source` imports when it runs, by line: none under `if TYPE_CHECKING:`, none relative."""
    tree = ast.parse(source)
    unrun = {
        id(node)
        for branch in ast.walk(tree)
        if isinstance(branch, ast.If) and ast.unparse(branch.test) in ('TYPE_CHECKING', 'typing.TYPE_CHECKING')
        for statement in branch.body
        for node in ast.walk(statement)
    }
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in unrun:
            continue
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name.split('.')[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split('.')[0]))
    return found


def third_party(module: str) -> bool:
    return module not in sys.stdlib_module_names and module not in FIRST_PARTY and module != '__future__'


def _normalized(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def declared() -> set[str]:
    """The distributions `[project] dependencies` names, normalized."""
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    return {
        _normalized(re.split(r'[\s<>=!~\[;]', requirement, maxsplit=1)[0]) for requirement in project['dependencies']
    }


def test_every_third_party_module_a_script_imports_is_a_project_dependency() -> None:
    providers = importlib.metadata.packages_distributions()
    names = declared()
    modules = sorted(SCRIPTS.rglob('*.py'))
    imported = [
        (module, line, top)
        for module in modules
        for line, top in runtime_imports(module.read_text())
        if third_party(top)
    ]
    # Not vacuous: the scripts import third-party modules, and the census reads them.
    assert imported
    missing = [
        f'{module.relative_to(ROOT)}:{line} imports {top}, from {providers.get(top) or "no installed distribution"}'
        for module, line, top in imported
        if not {_normalized(distribution) for distribution in providers.get(top, [])} & names
    ]
    assert missing == [], 'a script imports what only the dev group installs:\n' + '\n'.join(missing)


@pytest.mark.parametrize(
    ('source', 'found'),
    [
        ('import tqdm', ['tqdm']),
        ('from tqdm import tqdm', ['tqdm']),
        ('import ruamel.yaml', ['ruamel']),
        ('def main():\n    import requests', ['requests']),
        ('import os, cryptography', ['os', 'cryptography']),
        ('from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import tqdm', ['typing']),
        ('from . import sources', []),
        ('"""import tqdm"""', []),
    ],
)
def test_the_census_reads_what_a_module_imports_when_it_runs(source: str, found: list[str]) -> None:
    assert [top for _, top in runtime_imports(source)] == found


def test_the_census_tells_third_party_from_the_rest() -> None:
    assert [module for module in ('os', '__future__', 'kluster', 'putils', 'tqdm') if third_party(module)] == ['tqdm']
