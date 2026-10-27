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
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import cast

import process_sessions
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


#: Each git call's bound, and the step's. Summed over a case they stay below
#: its 60 s bound, so whichever hangs is named by its own timeout.
GIT_TIMEOUT = 5
STEP_TIMEOUT = 20


def _environment(home: Path) -> dict[str, str]:
    """The scratch repository's environment: the caller's `PATH` and none of their git.

    No `GIT_*` variable of the caller's reaches git -- an inherited `GIT_DIR`
    would point it at another repository -- and neither does their global or
    system configuration, where a signing helper or an excludes file would
    decide what these cases see.
    """
    return {
        'PATH': os.environ['PATH'],
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
    }


def _git(repo: Path, *args: str) -> str:
    done = process_sessions.run(
        ['git', *args],
        cwd=repo,
        env=_environment(repo.parent),
        check=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    return done.stdout


def _commit(repo: Path, files: list[str], message: str) -> None:
    for file in files:
        (repo / file).parent.mkdir(parents=True, exist_ok=True)
        _ = (repo / file).write_text('# A document\n')
    _ = _git(repo, 'add', '.')
    _ = _git(repo, '-c', 'user.name=probe', '-c', 'user.email=probe@example.invalid', 'commit', '-q', '-m', message)


def checked(tmp_path: Path, *, whole_tree: bool) -> list[str]:
    """The files the step hands the checker, over the whole tree or over the last commit's change."""
    repo = tmp_path / 'repo'
    (repo / '.vscode').mkdir(parents=True)
    for name in ('dictionary', 'disabledRules'):
        _ = (repo / '.vscode' / f'ltex.{name}.en-US.txt').write_bytes(
            (ROOT / '.vscode' / f'ltex.{name}.en-US.txt').read_bytes()
        )
    _ = _git(repo, 'init', '-q')
    _commit(repo, ['README.md'], 'base')
    base = _git(repo, 'rev-parse', 'HEAD').strip()
    _commit(repo, [AUTHORED, GENERATED], 'change')

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    _ = (bin_dir / 'mise').write_text(STAND_IN)
    (bin_dir / 'mise').chmod(0o755)
    log = tmp_path / 'log'
    log.touch()

    env = {
        **_environment(tmp_path),
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        'BASE': '' if whole_tree else base,
        'JAVA_HOME': '',
        'RUNNER_TEMP': str(tmp_path),
        'STAND_IN_LOG': str(log),
    }
    done = process_sessions.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', prose_run()],
        cwd=repo,
        env=env,
        text=True,
        timeout=STEP_TIMEOUT,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return log.read_text().splitlines()


@pytest.mark.parametrize('whole_tree', [True, False], ids=['whole tree', 'changed files'])
def test_the_prose_step_checks_authored_markdown_and_not_a_generated_sdks(tmp_path: Path, whole_tree: bool) -> None:
    files = checked(tmp_path, whole_tree=whole_tree)

    assert AUTHORED in files
    assert GENERATED not in files


def test_the_tree_the_prose_step_skips_is_one_basedpyright_excludes(tmp_path: Path) -> None:
    """The generated SDKs are one directory, named alike wherever a check steps around them."""
    pyright = cast('dict[str, object]', tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool'])['basedpyright']
    excluded = cast('dict[str, list[str]]', pyright)['exclude']

    # The tree the step skips, as running it shows: the generated file's top
    # directory.
    assert GENERATED not in checked(tmp_path, whole_tree=True)
    assert PurePosixPath(GENERATED).parts[0] in excluded
