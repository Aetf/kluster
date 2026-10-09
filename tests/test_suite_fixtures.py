"""Every async fixture scoped wider than a case names the loop it runs on.

`pyproject.toml` sets `asyncio_default_fixture_loop_scope` to `function`, so
an async fixture that states no `loop_scope` runs on its case's loop. One
whose `scope` is wider than that -- a module's `applied` run -- outlives
that loop, and pytest-asyncio refuses it when a case first
asks for it, with a `ScopeMismatch` on `_function_scoped_runner` that names
neither the fixture's module nor the missing argument. So each such fixture
states `loop_scope` beside its `scope`, and this census reads every module
under `tests/` for one that does not, whether or not any case uses it.

An **async fixture** here is what pytest-asyncio's strict mode, the one this
suite runs in, takes as one: a function decorated by a call to
`pytest_asyncio.fixture`, reached through any name the module's own imports
bind to it (`import pytest_asyncio as pa`, `from pytest_asyncio import
fixture`). A `scope` other than the literal `'function'` is wider, a callable
or a name included, since the census cannot tell what either decides. The
decorator is read from the syntax tree rather than the text, so a decorator
quoted in a docstring is not one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: The tree the census reads.
TESTS = Path(__file__).parent


def fixture_callees(tree: ast.Module) -> set[str]:
    """The dotted names by which `tree` reaches `pytest_asyncio.fixture`."""
    callees: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            callees |= {
                f'{alias.asname or alias.name}.fixture' for alias in node.names if alias.name == 'pytest_asyncio'
            }
        elif isinstance(node, ast.ImportFrom) and node.module == 'pytest_asyncio':
            callees |= {alias.asname or alias.name for alias in node.names if alias.name == 'fixture'}
    return callees


def unstated_loop_scopes(source: str) -> list[tuple[int, str]]:
    """Each async fixture in `source` scoped wider than a case with no `loop_scope`, by line and name."""
    tree = ast.parse(source)
    callees = fixture_callees(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call) and ast.unparse(decorator.func) in callees):
                continue
            keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
            scope = keywords.get('scope')
            wide = scope is not None and not (isinstance(scope, ast.Constant) and scope.value == 'function')
            if wide and 'loop_scope' not in keywords:
                found.append((decorator.lineno, node.name))
    return found


def test_every_wide_async_fixture_under_tests_states_its_loop_scope() -> None:
    modules = sorted(TESTS.rglob('*.py'))
    # Not vacuous: the census must reach modules that declare async fixtures at all.
    assert any(fixture_callees(ast.parse(module.read_text())) for module in modules)
    found = [
        f'{module.relative_to(TESTS)}:{line} {name}'
        for module in modules
        for line, name in unstated_loop_scopes(module.read_text())
    ]
    assert found == [], 'an async fixture scoped wider than a case states no loop_scope:\n' + '\n'.join(found)


@pytest.mark.parametrize(
    'source',
    [
        "import pytest_asyncio\n@pytest_asyncio.fixture(scope='module')\nasync def applied(): ...",
        "import pytest_asyncio\n@pytest_asyncio.fixture(scope='session', autouse=True)\nasync def applied(): ...",
        "import pytest_asyncio\n@pytest_asyncio.fixture(\n    scope='package',\n    name='applied_run',\n)\nasync def applied(): ...",
        "import pytest_asyncio as pa\n@pa.fixture(scope='module')\nasync def applied(): ...",
        "from pytest_asyncio import fixture\n@fixture(scope='module')\nasync def applied(): ...",
        "from pytest_asyncio import fixture as afixture\n@afixture(scope='class')\nasync def applied(): ...",
        "import pytest_asyncio\nSCOPE = 'module'\n@pytest_asyncio.fixture(scope=SCOPE)\nasync def applied(): ...",
        "import pytest_asyncio\nclass TestRun:\n    @pytest_asyncio.fixture(scope='class')\n    async def applied(self): ...",
    ],
)
def test_the_census_finds_a_wide_async_fixture_with_no_loop_scope(source: str) -> None:
    assert [name for _, name in unstated_loop_scopes(source)] == ['applied']


@pytest.mark.parametrize(
    'source',
    [
        "import pytest_asyncio\n@pytest_asyncio.fixture(scope='module', loop_scope='module')\nasync def applied(): ...",
        "import pytest_asyncio\n@pytest_asyncio.fixture(scope='function')\nasync def monitor(): ...",
        'import pytest_asyncio\n@pytest_asyncio.fixture(autouse=True)\nasync def monitor(): ...',
        'import pytest_asyncio\n@pytest_asyncio.fixture\nasync def monitor(): ...',
        "import pytest\n@pytest.fixture(scope='module')\ndef installation(): ...",
        "import pytest_asyncio\n'''@pytest_asyncio.fixture(scope='module')'''\nasync def quoted(): ...",
    ],
)
def test_the_census_passes_what_is_not_one(source: str) -> None:
    assert unstated_loop_scopes(source) == []
