"""`kluster.lib.state_backend.table` as a program of its own: what it imports, and the settings file it reads.

What it does against a table is held in `tests/test_state_roles.py`, beside
the box it needs.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from kluster.lib import bundle
from kluster.lib.state_backend import state, table


def test_the_program_imports_nothing_but_the_standard_library() -> None:
    """An interpreter with a Postgres client runs the file with no code of this repository beside it.

    A relative import is refused whatever it names: it reaches a sibling
    module, which the program does not have, even where the name is one the
    standard library also uses.
    """
    tree = ast.parse(Path(table.__file__).read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
    relative = [node.lineno for node in imports if isinstance(node, ast.ImportFrom) and node.level > 0]
    imported = {
        name.split('.')[0]
        for node in imports
        for name in ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''])
    }

    assert relative == []
    assert imported <= set(sys.stdlib_module_names) | {'__future__'}


def test_the_settings_file_carries_what_a_workstation_passes(tmp_path: Path) -> None:
    written = state.table_settings()
    path = tmp_path / 'settings.json'
    _ = path.write_text(written.dumps())

    assert table.Settings.load(path) == written


def test_a_settings_file_missing_a_field_is_refused_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / 'settings.json'
    _ = path.write_text('{"database": "pulumi_state"}')

    with pytest.raises(table.StateError, match=re.escape(str(path))):
        _ = table.Settings.load(path)


def test_a_bundle_is_read_as_its_connection_string_and_the_files_beside_it(tmp_path: Path) -> None:
    """The layout the settings file carries names the files `bundle` names, for the variables it names them by."""
    configured = state.table_settings()
    _ = (tmp_path / configured.url_file).write_text(
        'postgres://upkeep@192.0.2.1:5432/pulumi_state?sslmode=verify-full\n'
    )

    client = configured.client(tmp_path)

    assert client.url == 'postgres://upkeep@192.0.2.1:5432/pulumi_state?sslmode=verify-full'
    assert client.env == bundle.ssl_env(tmp_path)
