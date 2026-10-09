"""A scratch Pulumi project that the pinned CLI runs over a backend and a home of the case's own.

Engine tests (framework/testing.md §8) run a program of stand-ins against the
`pulumi` that `mise.toml` pins. What every one of them needs around its
program is the same, and lives here: a Python project under the `uv`
toolchain, in a virtual environment of the case's own whose `.pth` file
reaches the test run's packages, so the locked SDK is the one that runs and
nothing is fetched; a `file://` backend and a `PULUMI_HOME` under the case's
temporary directory; and an environment with every `PULUMI_` and `PG`
variable of the test run's own removed, so nothing steers a command at
another backend (§5.1).
"""

from __future__ import annotations

import os
import site
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import process_sessions

#: The project around a program. A `uv` environment has no `pip`, which the
#: language host asks for unless the toolchain is `uv`, and then it asks for
#: the lock beside the project (rfc-006 slice 0, X7).
PROJECT = """\
name: probe
runtime:
  name: python
  options:
    toolchain: uv
    virtualenv: {venv}
"""
PYPROJECT = """\
[project]
name = "probe"
version = "0"
requires-python = ">={major}.{minor}"
dependencies = []

[tool.uv]
package = false
"""

PASSPHRASE = 'a-passphrase-for-a-scratch-stack-that-holds-nothing'

#: How long building the project's environment, or locking it, may take
#: before the case fails naming the `uv` command: a stop-loss, below the case
#: bound of every module that builds one.
SETUP_TIMEOUT = 120


@dataclass(frozen=True)
class ScratchProject:
    """A project directory holding a program, and the environment every command against it runs with."""

    directory: Path
    env: dict[str, str]


def scrubbed(environ: Mapping[str, str]) -> dict[str, str]:
    """The test run's environment, without anything that could steer `pulumi` at another backend."""
    return {key: value for key, value in environ.items() if not key.startswith(('PULUMI_', 'PG'))}


def scratch_project(root: Path, program: str) -> ScratchProject:
    """`program` as the `__main__.py` of a project under `root`, with its backend and home under `root` too."""
    project = root / 'project'
    project.mkdir()
    venv = root / 'venv'
    _ = process_sessions.run(
        ['uv', 'venv', '-q', '--python', sys.executable, str(venv)], timeout=SETUP_TIMEOUT, check=True
    )
    (site_packages,) = venv.glob('lib/python*/site-packages')
    _ = (site_packages / 'test_run.pth').write_text('\n'.join(site.getsitepackages()) + '\n')
    _ = (project / '__main__.py').write_text(program)
    _ = (project / 'Pulumi.yaml').write_text(PROJECT.format(venv=venv))
    _ = (project / 'pyproject.toml').write_text(
        PYPROJECT.format(major=sys.version_info.major, minor=sys.version_info.minor)
    )
    _ = process_sessions.run(['uv', 'lock', '-q', '--offline'], cwd=project, timeout=SETUP_TIMEOUT, check=True)
    (root / 'state').mkdir()
    env = scrubbed(os.environ) | {
        'PULUMI_BACKEND_URL': f'file://{root / "state"}',
        'PULUMI_HOME': str(root / 'pulumi-home'),
        'PULUMI_CONFIG_PASSPHRASE': PASSPHRASE,
        'PULUMI_SKIP_UPDATE_CHECK': 'true',
        'UV_OFFLINE': '1',
    }
    return ScratchProject(directory=project, env=env)
