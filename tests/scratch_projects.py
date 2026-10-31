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

**The CLI's own directories are the case's** (`cli_directories`): its
`PULUMI_HOME`, and the `TMPDIR` every process under it makes its temporary
files in -- a plugin's tarball, a provider's kubeconfig, `uv`'s locks -- so
pytest's clean-up of the case's directory takes them, a run killed
mid-download included. Every suite that runs the pinned CLI takes both from
there.

**A stand-in the engine imports is a provider plugin of the case's own**
(`providers`), a Python program in a directory under the case's, run on the
project's environment and served from the project's `plugins.providers`
path, so the engine fetches nothing for it (§8).
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
#: language host asks for unless it runs under `uv` (the `toolchain` option, or
#: a `uv.lock` above the project), and then it asks for the lock beside the
#: project (rfc-006 slice 0, X7).
PROJECT = """\
name: {name}
runtime:
  name: python
  options:
    toolchain: uv
    virtualenv: {venv}
"""
#: The project's `plugins.providers`, followed by one `PROVIDER` entry per
#: provider plugin the case serves.
PLUGINS = """\
plugins:
  providers:
"""
PROVIDER = """\
    - name: {name}
      path: {path}
"""
#: A provider plugin's own project file: the program in its directory, run by
#: the Python language host on the environment the project runs on. The host
#: runs a plugin and installs nothing for it, so it asks for neither `pip` nor
#: a lock.
PLUGIN = """\
runtime:
  name: python
  options:
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


#: The names the CLI, its plugins and `uv` give the temporary files a run can
#: leave behind in `TMPDIR`: a plugin's tarball, an unpacked package, a
#: provider's kubeconfig, a project environment's lock.
LEFT_BEHIND = ('pulumi-', 'kubeconfig', 'uv-')


def cli_directories(root: Path, *, home: Path | None = None) -> dict[str, str]:
    """The `PULUMI_HOME` and `TMPDIR` of a run of the pinned CLI, both under `root` unless `home` names the home.

    `TMPDIR` is what Go's temporary files and `uv`'s locks follow, in `pulumi`
    and in every plugin and language host it starts, so whatever a run leaves
    behind lands under `root`. It is created here: a temporary file in a
    directory that does not exist fails.
    """
    temporary = root / 'tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    return {'PULUMI_HOME': str(home or root / 'pulumi-home'), 'TMPDIR': str(temporary)}


def scrubbed(environ: Mapping[str, str]) -> dict[str, str]:
    """The test run's environment, without anything that could steer `pulumi` at another backend."""
    return {key: value for key, value in environ.items() if not key.startswith(('PULUMI_', 'PG'))}


def scratch_project(
    root: Path, program: str, *, name: str = 'probe', providers: Mapping[str, str] | None = None
) -> ScratchProject:
    """`program` as the `__main__.py` of a project under `root`, with its backend and home under `root` too.

    `name` is the project's, which every URN the program registers carries.
    `providers` maps a provider plugin's name to the program that serves it:
    each is written to a directory of its own under `root` and named in the
    project's `plugins.providers`, so a resource of that package is served by
    it rather than by a plugin the engine would install.
    """
    project = root / 'project'
    project.mkdir()
    venv = root / 'venv'
    _ = process_sessions.run(
        ['uv', 'venv', '-q', '--python', sys.executable, str(venv)], timeout=SETUP_TIMEOUT, check=True
    )
    (site_packages,) = venv.glob('lib/python*/site-packages')
    _ = (site_packages / 'test_run.pth').write_text('\n'.join(site.getsitepackages()) + '\n')
    _ = (project / '__main__.py').write_text(program)
    declared = PROJECT.format(name=name, venv=venv)
    if providers:
        declared += PLUGINS
        for plugin, served in providers.items():
            directory = root / 'plugins' / plugin
            directory.mkdir(parents=True)
            _ = (directory / '__main__.py').write_text(served)
            _ = (directory / 'PulumiPlugin.yaml').write_text(PLUGIN.format(venv=venv))
            declared += PROVIDER.format(name=plugin, path=directory)
    _ = (project / 'Pulumi.yaml').write_text(declared)
    _ = (project / 'pyproject.toml').write_text(
        PYPROJECT.format(major=sys.version_info.major, minor=sys.version_info.minor)
    )
    _ = process_sessions.run(['uv', 'lock', '-q', '--offline'], cwd=project, timeout=SETUP_TIMEOUT, check=True)
    (root / 'state').mkdir()
    env = (
        scrubbed(os.environ)
        | cli_directories(root)
        | {
            'PULUMI_BACKEND_URL': f'file://{root / "state"}',
            'PULUMI_CONFIG_PASSPHRASE': PASSPHRASE,
            'PULUMI_SKIP_UPDATE_CHECK': 'true',
            'UV_OFFLINE': '1',
        }
    )
    return ScratchProject(directory=project, env=env)
