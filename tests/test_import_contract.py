"""The import contract refuses the edges it names, run as the gate runs it.

`lint-imports` reads the contract in `pyproject.toml` and checks the tree it
finds on the import path, so a contract that names the wrong module, or one a
later edit weakens, keeps passing on a tree that never took the edge. These
cases hand the real contract a tree of empty packages of the same names, whose
only imports are the one each case adds, and read the verdict.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CONTRACT = ROOT / 'pyproject.toml'

#: Every package the contract names, as empty packages: a layer or a source
#: module it names and the tree lacks is an error of its own, not a verdict.
PACKAGES = (
    'kluster',
    'kluster.stacks',
    'kluster.components',
    'kluster.providers',
    'kluster.lib',
    'kluster.conventions',
    'kluster.scripts',
    'putils',
)

#: The contract a program's reach into a script breaks.
NO_SCRIPT = 'Nothing a program runs imports a script'

#: Every package a program runs from: the layers above `putils`.
PROGRAM = ('kluster.stacks', 'kluster.components', 'kluster.providers', 'kluster.lib', 'kluster.conventions')


def _tree(root: Path, edges: dict[str, str]) -> Path:
    """The packages under `root`, and a module per edge: `{importer: imported}`."""
    for package in PACKAGES:
        directory = root.joinpath(*package.split('.'))
        directory.mkdir(parents=True, exist_ok=True)
        _ = (directory / '__init__.py').write_text('')
    _ = (root / 'kluster' / 'scripts' / 'tool.py').write_text('')
    for importer, imported in edges.items():
        _ = root.joinpath(*importer.split('.')).with_suffix('.py').write_text(f'import {imported}\n')
    return root


def _lint(tree: Path) -> subprocess.CompletedProcess[str]:
    """`lint-imports` under the repository's contract, over `tree` alone."""
    return subprocess.run(
        [str(Path(sys.executable).with_name('lint-imports')), '--config', str(CONTRACT), '--no-cache'],
        capture_output=True,
        text=True,
        cwd=tree,
        env={**os.environ, 'PYTHONPATH': str(tree)},
        timeout=50,
        check=False,
    )


def test_a_tree_of_empty_packages_keeps_every_contract(tmp_path: Path) -> None:
    # The control: the verdicts below are about the edge each one adds.
    linted = _lint(_tree(tmp_path, {}))

    assert linted.returncode == 0, linted.stdout + linted.stderr
    assert f'{NO_SCRIPT} KEPT' in linted.stdout


@pytest.mark.parametrize('layer', PROGRAM)
def test_a_program_module_importing_a_script_is_refused(layer: str, tmp_path: Path) -> None:
    linted = _lint(_tree(tmp_path, {f'{layer}.reach': 'kluster.scripts.tool'}))

    assert linted.returncode != 0
    assert f'{NO_SCRIPT} BROKEN' in linted.stdout
    assert f'{layer}.reach -> kluster.scripts.tool' in linted.stdout
