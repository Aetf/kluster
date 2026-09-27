"""The prose step retries a file the checker was killed on, once.

`checks.yml`'s prose step runs `ltex-cli-plus` one markdown file at a time
under a per-file `timeout`, and tells that bound's exit 124 from the
checker's own verdicts (framework/ci.md §3). A 124 on a file is retried once,
on that file alone: the checker has been seen past the bound on a file it
checks in seconds, with nothing changed (kluster-ops#465). A retry that passes
prints its own duration as a notice, a second 124 fails the step naming the
file, and any other exit is a verdict and not retried. Each annotation is held
whole, since a search over annotations is how a recurrence is found.

The step is held by running it. Its `run` text is executed as it stands,
under the shell GitHub gives it, in a scratch repository whose tracked
markdown files are what the step checks, with a stand-in for `mise` first on
`PATH`. The stand-in answers each file from a script of exit codes, one per
invocation, and logs every invocation, so a case reads how many times a file
was checked and what the step printed. A 124 from the stand-in is the exit
`timeout` passes through from its child, which is the one the step reads.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from workflow_files import GITHUB, mapping, read_workflow, workflow_jobs

ROOT = Path(__file__).parent.parent

#: A stand-in for `mise x -- ltex-cli-plus … <file>`: the file is its last
#: argument, and its exit is the next code the script holds for that file.
STAND_IN = f"""#!{sys.executable}
import json, os, pathlib, sys
state = pathlib.Path(os.environ['STAND_IN_STATE'])
script = json.loads(state.read_text())
file = sys.argv[-1]
with open(os.environ['STAND_IN_LOG'], 'a') as log:
    log.write(file + '\\n')
code = script[file].pop(0)
state.write_text(json.dumps(script))
sys.exit(code)
"""


def prose_run() -> str:
    """The `run` text of `checks.yml`'s prose step."""
    path = GITHUB / 'workflows' / 'checks.yml'
    jobs = workflow_jobs(read_workflow(path), 'checks.yml')
    steps = jobs['checks']['steps']
    assert isinstance(steps, list)
    steps = [mapping(step, 'a checks.yml step') for step in cast('list[object]', steps)]
    (step,) = [step for step in steps if step.get('name') == 'Prose']
    run = step['run']
    assert isinstance(run, str)
    return run


def run_step(tmp_path: Path, script: dict[str, list[int]]) -> tuple[int, str, list[str]]:
    """Run the prose step over one markdown file per key of `script`.

    Returns the step's exit, what it printed, and the files the checker was
    invoked on, in order.
    """
    repo = tmp_path / 'repo'
    (repo / '.vscode').mkdir(parents=True)
    for name in ('dictionary', 'disabledRules'):
        shutil.copy(ROOT / '.vscode' / f'ltex.{name}.en-US.txt', repo / '.vscode')
    for file in script:
        (repo / file).write_text('# A document\n')
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'mise').write_text(STAND_IN)
    (bin_dir / 'mise').chmod(0o755)
    state = tmp_path / 'state.json'
    state.write_text(json.dumps(script))
    log = tmp_path / 'log'
    log.touch()

    env = {
        **os.environ,
        'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        # No base commit: the step checks every tracked markdown file.
        'BASE': '',
        'JAVA_HOME': '',
        'RUNNER_TEMP': str(tmp_path),
        'STAND_IN_STATE': str(state),
        'STAND_IN_LOG': str(log),
    }
    # GitHub's `bash` shell: `bash --noprofile --norc -eo pipefail {0}`.
    done = subprocess.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', prose_run()],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode, done.stdout + done.stderr, log.read_text().splitlines()


def test_a_file_killed_once_is_retried_and_its_pass_is_the_verdict(tmp_path: Path) -> None:
    code, output, checked = run_step(tmp_path, {'a.md': [124, 0], 'b.md': [0]})

    assert code == 0, output
    assert checked == ['a.md', 'a.md', 'b.md']
    # Whole lines: the file, the elapsed seconds and the bound are what a
    # search over annotations finds a recurrence by. The numbers are matched
    # as numbers, since the stand-in's elapsed time is wall-clock seconds.
    assert re.search(
        r'^::warning file=a\.md::ltex-cli-plus timed out on a\.md after \d+s \(bound \d+s\); retrying it once$',
        output,
        re.M,
    ), output
    assert re.search(r'^::notice file=a\.md::ltex-cli-plus passed a\.md on retry after \d+s$', output, re.M), output


def test_a_file_killed_twice_fails_the_step_naming_it(tmp_path: Path) -> None:
    code, output, checked = run_step(tmp_path, {'a.md': [124, 124, 124], 'b.md': [0]})

    assert code == 124, output
    # One retry, not a loop: the third code in the script is never read.
    assert checked == ['a.md', 'a.md', 'b.md']
    assert re.search(
        r'^::error file=a\.md::ltex-cli-plus timed out on a\.md again after \d+s \(bound \d+s\)$', output, re.M
    ), output
    assert '::notice' not in output


def test_a_verdict_on_the_retry_is_the_verdict(tmp_path: Path) -> None:
    code, output, checked = run_step(tmp_path, {'a.md': [124, 3], 'b.md': [0]})

    assert code == 3, output
    assert checked == ['a.md', 'a.md', 'b.md']
    assert '::notice' not in output
    assert '::error' not in output


@pytest.mark.parametrize('verdict', [1, 3])
def test_a_verdict_is_not_retried(tmp_path: Path, verdict: int) -> None:
    code, output, checked = run_step(tmp_path, {'a.md': [verdict, 0], 'b.md': [0]})

    assert code == verdict, output
    assert checked == ['a.md', 'b.md']
    assert '::warning' not in output
    assert '::notice' not in output


def test_every_file_passing_passes_with_no_retry(tmp_path: Path) -> None:
    code, output, checked = run_step(tmp_path, {'a.md': [0], 'b.md': [0]})

    assert code == 0, output
    assert checked == ['a.md', 'b.md']
    assert '::warning' not in output
    assert '::notice' not in output
