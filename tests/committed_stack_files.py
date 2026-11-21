"""The committed stack files, and the one way a case reads them: the `committed_stack_files` fixture.

A case that reads a `Pulumi.<stack>.yaml` of this checkout asks for the
fixture and reads the files through what it hands back (testing.md §2). This
module is a plugin `tests/conftest.py` loads, and its collection hook marks
`stack_files` every case that asks for that one as a parameter, its own or
that of a fixture it uses. That selection is what `checks` runs on a pull
request of the fast path's shape (ci.md §3), so the cases chosen are the ones
written the one sanctioned way, and a case that opens the files by hand is
not among them.

The project file `Pulumi.yaml` is not a stack file, and a case reads it as it
reads any other file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

#: The checkout this module sits in, the one whose stack files are committed.
CHECKOUT = Path(__file__).resolve().parents[1]

#: The fixture a reader asks for, and the marker its cases carry.
FIXTURE = 'committed_stack_files'
MARKER = 'stack_files'


@dataclass(frozen=True)
class StackFiles:
    """The stack files a checkout holds, by stack name."""

    directory: Path

    @property
    def project(self) -> str:
        """The project's name, which every configuration key of its own is prefixed with."""
        return str(yaml.safe_load((self.directory / 'Pulumi.yaml').read_text())['name'])

    def stacks(self) -> tuple[str, ...]:
        """Every stack with a file, sorted."""
        found = self.directory.glob('Pulumi.*.yaml')
        return tuple(sorted(path.name.removeprefix('Pulumi.').removesuffix('.yaml') for path in found))

    def path(self, stack: str) -> Path:
        return self.directory / f'Pulumi.{stack}.yaml'

    def text(self, stack: str) -> str:
        return self.path(stack).read_text()

    def config(self, stack: str) -> dict[str, Any]:
        """The file's `config` block as YAML parses it, empty where it has none."""
        document = cast('dict[str, Any]', yaml.safe_load(self.text(stack)))
        return cast('dict[str, Any]', document.get('config') or {})


@pytest.fixture(scope='session')
def committed_stack_files() -> StackFiles:
    """This checkout's stack files."""
    return StackFiles(CHECKOUT)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        'markers', f'{MARKER}: reads a committed stack file, through the `{FIXTURE}` fixture (testing.md §2)'
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every case whose fixtures include `FIXTURE`.

    First among the hooks, so the mark is on the case before `-m` deselects by
    it. `fixturenames` is the closure of the case's fixtures, so a case that
    reaches the fixture through another is marked too.
    """
    for item in items:
        if FIXTURE in getattr(item, 'fixturenames', ()):
            item.add_marker(MARKER)
