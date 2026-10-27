"""The engine semantics a device directory's `delete_before_replace` rests on, held by the pinned `pulumi` itself.

A `DeviceDirectory`'s create adopts a directory that is already there, and
its delete refuses one that holds anything (`providers.device_files`). A
replacement the engine decides without asking the provider -- a targeted
`--replace`, a taint -- takes its order from the resource's own option, so the
class declares `delete_before_replace`, and what that buys is the engine's to
answer: the replacement of a full directory is refused at its delete, before
anything is made, the original stays in state, and the next run plans nothing.
No mock can show it, so each case runs the CLI `mise.toml` pins over a
`file://` backend and a `PULUMI_HOME` of the case's own, against a program of
stand-ins shaped as the device resources are, on a directory of the case's
own:

-   `directory`: made with `mkdir -p`, replaced on `path`, removed only while
    it is empty and otherwise refused, and deleted before it is replaced.
-   `file`: a file inside it, which `depends_on` it.

Every provider method that acts appends a line to a log, so the log is the
order the engine ran them in. The project around the program is
`scratch_projects`', and every command leads a POSIX session of its own that
ends with it (`process_sessions`), so a `pulumi` stopped at its bound takes its
plugins with it.
"""

from __future__ import annotations

import json
import shutil
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
from scratch_projects import scratch_project

#: How long one case may take: its project's set-up and every command it runs
#: against the engine. On 24 cores at a load average of 9 the case took 5 s,
#: and it runs the same handful of commands as `tests/test_homelab_engine.py`,
#: whose bounds these are. Twice `COMMAND_TIMEOUT`, so a stalled command fails
#: naming itself before this fires; a stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, many times the whole case as measured, and below the case bound.
COMMAND_TIMEOUT = 120

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

PROGRAM = """\
import os
import pathlib

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider

LOG = pathlib.Path(os.environ['PROBE_LOG'])
DEVICE = pathlib.Path(os.environ['PROBE_DEVICE'])


def log(line):
    with LOG.open('a') as handle:
        handle.write(line + '\\n')


class Directory(ResourceProvider):
    \"\"\"`mkdir -p`, and a delete that refuses a directory holding anything, as a device directory's are.\"\"\"

    def create(self, props):
        (DEVICE / props['path']).mkdir(parents=True, exist_ok=True)
        log(f'create directory {props["path"]}')
        return CreateResult(id_=props['path'], outs=dict(props))

    def diff(self, _id, olds, news):
        replaces = ['path'] if olds.get('path') != news.get('path') else []
        return DiffResult(changes=bool(replaces), replaces=replaces)

    def delete(self, _id, props):
        try:
            (DEVICE / props['path']).rmdir()
        except OSError:
            log(f'refuse directory {props["path"]}')
            raise
        log(f'delete directory {props["path"]}')


class File(ResourceProvider):
    def create(self, props):
        (DEVICE / props['path']).write_text(props['content'])
        log(f'create file {props["path"]}')
        return CreateResult(id_=props['path'], outs=dict(props))

    def delete(self, _id, props):
        (DEVICE / props['path']).unlink(missing_ok=True)
        log(f'delete file {props["path"]}')


class Stand(Resource):
    def __init__(self, provider, name, props, opts):
        super().__init__(provider, name, props, opts)


directory = Stand(
    Directory(),
    'directory',
    {'path': 'machines'},
    pulumi.ResourceOptions(delete_before_replace=True),
)
Stand(
    File(),
    'file',
    {'path': 'machines/state', 'content': 'what the machine keeps'},
    pulumi.ResourceOptions(depends_on=[directory]),
)
"""

STACK = 'probe'

#: What the file stand-in writes, and what the case reads back to find it untouched.
CONTENT = 'what the machine keeps'


@dataclass
class Engine:
    """One scratch project and its backend, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]
    log_file: Path
    device: Path

    def pulumi(self, *args: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=self.project, env=self.env, text=True, timeout=COMMAND_TIMEOUT
        )

    def up(self, *args: str) -> sp.CompletedProcess[str]:
        return self.pulumi('up', '--yes', '--skip-preview', *args)

    def log(self) -> list[str]:
        """What ran since the last read, in order, and nothing before it."""
        lines = self.log_file.read_text().splitlines() if self.log_file.exists() else []
        self.log_file.unlink(missing_ok=True)
        return lines

    def resources(self) -> list[dict[str, Any]]:
        exported = self.pulumi('stack', 'export')
        assert exported.returncode == 0, exported.stderr
        deployment = cast('dict[str, Any]', json.loads(exported.stdout))['deployment']
        return cast('list[dict[str, Any]]', deployment.get('resources') or [])

    def urn(self, name: str) -> str:
        (urn,) = [str(resource['urn']) for resource in self.resources() if str(resource['urn']).endswith(f'::{name}')]
        return urn


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def launched(tmp_path: Path) -> Engine:
    """A fresh stack after its first `up`, the directory holding the file, with the log read empty."""
    log_file = tmp_path / 'engine.log'
    device = tmp_path / 'device'
    device.mkdir()
    scratch = scratch_project(tmp_path, PROGRAM)
    engine = Engine(
        project=scratch.directory,
        env=scratch.env | {'PROBE_LOG': str(log_file), 'PROBE_DEVICE': str(device)},
        log_file=log_file,
        device=device,
    )
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = engine.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    first = engine.up()
    assert first.returncode == 0, _ran(first)
    assert engine.log() == ['create directory machines', 'create file machines/state']
    return engine


def test_a_full_directory_replaced_delete_first_is_refused_before_anything_is_made(launched: Engine) -> None:
    """The original stays, alone under its name, and the next run has nothing to do.

    Created first instead, the create would adopt the full directory and the
    original's refused delete would stay in state beside the replacement, to
    be retried, and refused, on every later run. The file inside is no
    dependent of the directory's identity -- it `depends_on` the directory and
    nothing more -- so a delete-first replacement of the directory leaves it
    alone.
    """
    result = launched.up('--replace', launched.urn('directory'))

    assert result.returncode != 0, _ran(result)
    assert launched.log() == ['refuse directory machines']
    assert (launched.device / 'machines' / 'state').read_text() == CONTENT
    resources = launched.resources()
    assert len([resource for resource in resources if str(resource['urn']).endswith('::directory')]) == 1
    assert not any(resource.get('delete') for resource in resources), 'nothing waits in state to be deleted'

    again = launched.pulumi('preview', '--expect-no-changes')

    assert again.returncode == 0, _ran(again)
    assert launched.log() == []
