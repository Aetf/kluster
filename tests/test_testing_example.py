"""The example test `docs/framework/testing.md` §2.1 offers passes as written.

The example is the first suite a reader copies, so one that fails as written
teaches the failure. It is read out of the document on every run rather than
kept here as a copy: a copy is a second example, and the one in the document
is the one that has to work.

The example is run the way the document says to run it -- a `test_*.py` file
under `pytest`, under the configuration `pyproject.toml` carries -- in a
process of its own, so its fixtures and its runtime mocks are its own and not
this run's. It is written outside the checkout, so `tests/` is put on the
path for it: the document places the file in `tests/`, which is what makes
`mock_monitor` importable there. The child inherits this process's
environment, which `tests/conftest.py` has already stripped of credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import fences

ROOT = Path(__file__).parent.parent
TESTING = ROOT / 'docs' / 'framework' / 'testing.md'

#: The heading of the section that holds the example.
HEADING = '### 2.1 '


def the_example() -> str:
    """The one Python block in §2.1; anything other than one is a failure.

    The section runs to the next heading outside a fence: a `# ` comment in
    the example's own code sits at the start of a line too.
    """
    blocks: dict[int, list[str]] = {}
    in_section = False
    for line in fences.lines(TESTING.read_text()):
        if line.fence is None and line.text.startswith('#'):
            in_section = line.text.startswith(HEADING)
        elif in_section and line.fence is not None and line.fence.info == 'python':
            blocks.setdefault(line.fence.opened_at, []).append(line.text)
    name = TESTING.relative_to(ROOT)
    assert len(blocks) == 1, f'{name} §2.1 holds {len(blocks)} Python blocks; this test runs exactly one, the example'
    (example,) = blocks.values()
    return '\n'.join(example) + '\n'


def test_the_example_is_read_out_of_the_document() -> None:
    """The extraction reads the example itself, not whatever block sits nearby."""
    assert 'def test_' in the_example()


def test_the_example_passes_as_written(tmp_path: Path) -> None:
    (tmp_path / 'test_network.py').write_text(the_example())
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'tests')}

    run = subprocess.run(
        [
            sys.executable,
            '-m',
            'pytest',
            '-c',
            str(ROOT / 'pyproject.toml'),
            '-p',
            'no:cacheprovider',
            str(tmp_path / 'test_network.py'),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        # A stop-loss below the per-case bound, so a hung example fails here
        # as `TimeoutExpired` naming its seconds rather than as this case's
        # own timeout, whose stack would be this process's wait.
        timeout=45,
    )

    # Zero is a run that collected at least one case and failed none; a file
    # that collected nothing exits 5.
    assert run.returncode == 0, run.stdout + run.stderr
