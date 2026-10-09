"""No module under `src/kluster/providers/` says anything through the standard `logging` module.

The modules there are run by the provider host, each operation in the
process a dynamic provider runs in, where nothing configures `logging`: its
`info` lines are dropped, and a fetch, an upload or a wait that says what it
is doing through them says it to nobody. What reaches the operator is
`pulumi.log`, the engine's diagnostics, which `pulumi` draws on the stack's
row while the run goes, at a terminal and under a pipe alike, and lists at the
run's end. So a module there that imports `logging` is the defect this
census refuses, by its import, before any line it would write is lost.

An import is read from the syntax tree rather than the text, so an import
quoted in a docstring is not one, and an import inside a function is:
`import logging`, under any name, `import logging.handlers`, and any
`from logging … import`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: The tree the census reads.
PROVIDERS = Path(__file__).parent.parent / 'src' / 'kluster' / 'providers'


def logging_imports(source: str) -> list[int]:
    """The line of each import of the standard `logging` module in `source`."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or '']
        else:
            continue
        if any(name == 'logging' or name.startswith('logging.') for name in names):
            found.append(node.lineno)
    return found


def test_no_module_under_providers_imports_logging() -> None:
    modules = sorted(PROVIDERS.rglob('*.py'))
    # Not vacuous: the census reads the modules that speak through `pulumi.log`.
    assert any('pulumi.log.' in module.read_text() for module in modules)
    found = [
        f'{module.relative_to(PROVIDERS)}:{line}' for module in modules for line in logging_imports(module.read_text())
    ]
    assert found == [], (
        'a provider module imports logging, whose lines no display shows; say it through pulumi.log:\n'
        + '\n'.join(found)
    )


@pytest.mark.parametrize(
    'source',
    [
        'import logging\nlog = logging.getLogger(__name__)',
        'import logging as stdlib_logging',
        'import os, logging',
        'import logging.handlers',
        'from logging import getLogger',
        'from logging.handlers import QueueHandler',
        'def create(self, props):\n    import logging\n    logging.info("creating")',
    ],
)
def test_the_census_finds_an_import_of_logging(source: str) -> None:
    assert logging_imports(source) != []


@pytest.mark.parametrize(
    'source',
    [
        'import pulumi\npulumi.log.info("fetching")',
        'from pulumi import log',
        'import logging_extras',
        'from . import logging',
        '"""import logging"""',
    ],
)
def test_the_census_passes_what_is_not_one(source: str) -> None:
    assert logging_imports(source) == []
