"""The prose step leaves the generated SDKs' markdown alone, however it picks its files.

`checks.yml`'s prose step checks the markdown a change added or modified, or
every markdown file in the tree when there is no base to diff against or the
change touches the checker's machinery (framework/ci.md §3). Either way, what
is under `sdks/` is not this repository's prose: `pulumi install` writes it
from the generator's own text, as it writes the code basedpyright and ruff
exclude there. Held by running the step's `run` text, as `test_prose_step`
does for the retry, in a scratch repository holding one authored and one generated document,
with a stand-in for the checker that logs which files it was handed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import pytest
from workflow_files import GITHUB, mapping, read_workflow, workflow_jobs

ROOT = Path(__file__).parent.parent

#: A stand-in for `mise x -- ltex-cli-plus … <file>` that logs the file, its
#: last argument, and passes it.
STAND_IN = f"""#!{sys.executable}
import os, sys
with open(os.environ['STAND_IN_LOG'], 'a') as log:
    log.write(sys.argv[-1] + '\\n')
"""


def prose_run() -> str:
    """The `run` text of `checks.yml`'s prose step."""
    jobs = workflow_jobs(read_workflow(GITHUB / 'workflows' / 'checks.yml'), 'checks.yml')
    steps = jobs['checks']['steps']
    assert isinstance(steps, list)
    (step,) = [
        step
        for step in (mapping(step, 'a checks.yml step') for step in cast('list[object]', steps))
        if step.get('name') == 'Prose'
    ]
    run = step['run']
    assert isinstance(run, str)
    return run


#: One document this repository writes and one a generator writes, each
#: present in the commit the step reads.
AUTHORED = 'docs/notes.md'
GENERATED = 'sdks/probe/pulumi_probe/README.md'


def _commit(repo: Path, files: list[str], message: str) -> None:
    for file in files:
        (repo / file).parent.mkdir(parents=True, exist_ok=True)
        _ = (repo / file).write_text('# A document\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(
        ['git', '-c', 'user.name=probe', '-c', 'user.email=probe@example.invalid', 'commit', '-q', '-m', message],
        cwd=repo,
        check=True,
    )


def checked(tmp_path: Path, *, whole_tree: bool) -> list[str]:
    """The files the step hands the checker, over the whole tree or over the last commit's change."""
    repo = tmp_path / 'repo'
    (repo / '.vscode').mkdir(parents=True)
    for name in ('dictionary', 'disabledRules'):
        _ = (repo / '.vscode' / f'ltex.{name}.en-US.txt').write_bytes(
            (ROOT / '.vscode' / f'ltex.{name}.en-US.txt').read_bytes()
        )
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    _commit(repo, ['README.md'], 'base')
    base = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    _commit(repo, [AUTHORED, GENERATED], 'change')

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    _ = (bin_dir / 'mise').write_text(STAND_IN)
    (bin_dir / 'mise').chmod(0o755)
    log = tmp_path / 'log'
    log.touch()

    env = {
        **os.environ,
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        'BASE': '' if whole_tree else base,
        'JAVA_HOME': '',
        'RUNNER_TEMP': str(tmp_path),
        'STAND_IN_LOG': str(log),
    }
    done = subprocess.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', prose_run()],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return log.read_text().splitlines()


@pytest.mark.parametrize('whole_tree', [True, False], ids=['whole tree', 'changed files'])
def test_the_prose_step_checks_authored_markdown_and_not_a_generated_sdks(tmp_path: Path, whole_tree: bool) -> None:
    files = checked(tmp_path, whole_tree=whole_tree)

    assert AUTHORED in files
    assert GENERATED not in files


def test_the_tree_the_prose_step_skips_is_one_basedpyright_excludes() -> None:
    """The generated SDKs are one directory, named alike wherever a check steps around them."""
    pyright = cast('dict[str, object]', tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool'])['basedpyright']
    excluded = cast('dict[str, list[str]]', pyright)['exclude']

    assert "generated=(':(exclude)sdks/')" in prose_run()
    assert 'sdks' in excluded
