"""No module under `tests/` imports a test module.

A **test module** is a file pytest collects: a `test_*.py` or a `*_test.py`,
its default `python_files`. What two of them share by import -- a helper, a
fixture's body, a fake, a table -- lives in a module named for what it holds,
which pytest does not collect, and both import that. The rule and why it
holds are framework/testing.md §2; this is the census that keeps it, over
every module under `tests/`, `tests/live/` included, helper modules as much
as test modules.

An import is read from the syntax tree rather than the text, so an import
quoted in a docstring or a comment is not one, and an import inside a
function is. A `from` import is read as the module joined to each name it
imports, and a module is a test module when any part of that dotted name is:
`tests.test_age`, `from tests import test_age` and `from . import test_age`
count as much as `test_age`. That reading also flags a `test_` function
imported by name from any module, which pytest would collect a second time
in the importer. What the census does not read is an import made at run
time -- `importlib.import_module`, `__import__`, a `pytest_plugins` entry --
none of which any module under `tests/` aims at a test module.

No two modules under `tests/` share a name, `conftest.py` aside: both
directories put their modules at the top level of `sys.path`, where one of
two same-named helpers would shadow the other.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

#: The tree the census reads.
TESTS = Path(__file__).parent


def is_test_module(name: str) -> bool:
    """Whether any part of a dotted module name is one pytest collects by default."""
    return any(part.startswith('test_') or part.endswith('_test') for part in name.split('.'))


def imported_test_modules(source: str) -> list[tuple[int, str]]:
    """Each test module `source` imports, by its line and the name the import gives it."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # The alias names the module in `from . import test_x` and `from tests import test_x`.
            names = [f'{node.module}.{alias.name}' if node.module else alias.name for alias in node.names]
        else:
            continue
        found += [(node.lineno, name) for name in names if is_test_module(name)]
    return found


def test_no_module_under_tests_imports_a_test_module() -> None:
    modules = sorted(TESTS.rglob('*.py'))
    # Not vacuous: an empty tree would pass the census with nothing read.
    assert len(modules) > 0
    found = [
        f'{module.relative_to(TESTS)}:{line} imports {name}'
        for module in modules
        for line, name in imported_test_modules(module.read_text())
    ]
    assert found == [], 'a test module is imported; move what it shares into a named module:\n' + '\n'.join(found)


@pytest.mark.parametrize(
    'source',
    [
        'from test_physical_stack import Installation',
        'import test_age',
        'import os, test_age',
        'import test_age as age',
        'from tests.test_age import listed',
        'from . import test_age',
        'from tests import test_age',
        'from tests.live import test_oci_seed_drill',
        'import gateway_test',
        'def helper():\n    from test_age import listed\n',
    ],
)
def test_an_import_of_a_test_module_is_flagged(source: str) -> None:
    assert len(imported_test_modules(source)) == 1


@pytest.mark.parametrize(
    'source',
    [
        'from physical_installation import Installation',
        'from tests import mock_monitor',
        'import pytest',
        'import testing',
        'from pytest_asyncio import fixture',
        'from kluster.scripts.credentials import age',
        '"""from test_age import listed"""',
        '# import test_age',
    ],
)
def test_an_import_of_anything_else_is_not(source: str) -> None:
    assert imported_test_modules(source) == []


def test_no_two_modules_under_tests_share_a_name() -> None:
    names = Counter(module.name for module in TESTS.rglob('*.py') if module.name != 'conftest.py')
    # Not vacuous: the census above reads the same tree and asserts it is not empty.
    assert [name for name, count in names.items() if count > 1] == []
