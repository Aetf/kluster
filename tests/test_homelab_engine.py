"""The engine semantics the homelab worker's seed and its rebuild rest on, held by the pinned `pulumi` itself.

The seed is first-boot input: a new rendering of the worker's configuration
plans nothing on it or on the domain that attaches it, and a replaced disk
replaces the seed from the current rendering, and the domain with both
(`components.homelab`). Every name is stated on the host, so whatever is
replaced is deleted before it is created again. No mock can show what the
engine does with `ignore_changes`, `replace_with` and `delete_before_replace`
together, so each
case runs the CLI `mise.toml` pins over a `file://` backend and a
`PULUMI_HOME` of the case's own, against a program of stand-ins shaped as the
libvirt resources are:

-   `disk`, the worker's disk: protected, deleted before it is replaced, and
    replaced on `gen`.
-   `seed`: replaced on `source`, which it ignores, replaced with `disk`, and
    deleted before it is replaced.
-   `domain`: replaced on its `disks`, the ids of the two above, and deleted
    before it is replaced.

Every provider method appends a line to a log, so the log is the order the
engine ran them in. The project around the program is `scratch_projects`',
and every command leads a POSIX session of its own that ends with it
(`process_sessions`), so a `pulumi` stopped at its bound takes its plugins
with it.

The rebuild's cases run a second program, the worker's image and its disk
(`REBUILD_PROGRAM`). The image is the real `FactoryImage`, fetching from a
server of the case's own into a cache under the case's directory; the server
appends `fetch image` to the same log when it is asked for the artifact. The
disk is a stand-in that opens its `source` at its create and nowhere before,
as the pinned libvirt provider does: its volume schema gives `source` no
validation and the resource no `CustomizeDiff`, and its create opens the file
to probe for a QCOW2 header before it asks libvirt for the volume
(terraform-provider-libvirt v0.8.1, which `pulumi-libvirt` 0.5.4 bridges,
`libvirt/resource_libvirt_volume.go` and `libvirt/volume_image.go`). Like the
worker's disk, it is deleted before it is replaced and ignores changes to
`source`.
"""

from __future__ import annotations

import json
import lzma
import re
import shutil
import subprocess as sp
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http import server as http_server
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
from scratch_projects import scratch_project

import kluster

#: How long one case may take: its project's set-up and every command it runs
#: against the engine. On four cores the slowest case here took 5 s idle and
#: 9 s with four times as many busy processes as cores. Twice
#: `COMMAND_TIMEOUT`, so a stalled command fails naming itself before this
#: fires; a stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, several times the slowest whole case measured under load, and
#: below the case bound.
COMMAND_TIMEOUT = 120

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

PROGRAM = """\
import json
import os
import pathlib
import uuid

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider

LOG = pathlib.Path(os.environ['PROBE_LOG'])


def log(line):
    with LOG.open('a') as handle:
        handle.write(line + '\\n')


class Replaced(ResourceProvider):
    \"\"\"A resource every input of which replaces it, as every field of a libvirt volume does.\"\"\"

    def __init__(self, kind, key):
        self.kind = kind
        self.key = key

    def create(self, props):
        log(f'create {self.kind} {self.key}={props[self.key]}')
        return CreateResult(id_=f'{self.kind}-{uuid.uuid4().hex[:8]}', outs=dict(props))

    def diff(self, _id, olds, news):
        replaces = [self.key] if olds.get(self.key) != news.get(self.key) else []
        return DiffResult(changes=bool(replaces), replaces=replaces)

    def delete(self, id_, _props):
        log(f'delete {self.kind}')


class Stand(Resource):
    def __init__(self, name, key, value, opts):
        super().__init__(Replaced(name, key), name, {key: value}, opts)


settings = json.loads(pathlib.Path('settings.json').read_text())
disk = Stand(
    'disk',
    'gen',
    settings['gen'],
    pulumi.ResourceOptions(protect=True, delete_before_replace=True),
)
seed = Stand(
    'seed',
    'source',
    settings['rendering'],
    pulumi.ResourceOptions(
        ignore_changes=['source'],
        replace_with=[disk],
        delete_before_replace=True,
    ),
)
Stand(
    'domain',
    'disks',
    pulumi.Output.all(disk.id, seed.id).apply(lambda ids: ','.join(ids)),
    pulumi.ResourceOptions(delete_before_replace=True),
)
"""

STACK = 'probe'


@dataclass
class Engine:
    """One scratch project and its backend, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]
    log_file: Path

    def settings(self, *, rendering: str = 'r1', gen: str = 'g1') -> None:
        _ = (self.project / 'settings.json').write_text(json.dumps({'rendering': rendering, 'gen': gen}))

    def pulumi(self, *args: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=self.project, env=self.env, text=True, timeout=COMMAND_TIMEOUT
        )

    def up(self, *args: str) -> sp.CompletedProcess[str]:
        return self.pulumi('up', '--yes', '--skip-preview', *args)

    def urn(self, name: str) -> str:
        return str(self.resources()[name]['urn'])

    def log(self) -> list[str]:
        """What ran since the last read, in order, and nothing before it."""
        lines = self.log_file.read_text().splitlines() if self.log_file.exists() else []
        self.log_file.unlink(missing_ok=True)
        return lines

    def resources(self) -> dict[str, dict[str, Any]]:
        exported = self.pulumi('stack', 'export')
        assert exported.returncode == 0, exported.stderr
        deployment = cast('dict[str, Any]', json.loads(exported.stdout))['deployment']
        found = cast('list[dict[str, Any]]', deployment.get('resources') or [])
        return {str(resource['urn']).rsplit('::', 1)[1]: resource for resource in found}


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def launched(tmp_path: Path) -> Engine:
    """A fresh stack after its first `up`, with the log read empty."""
    log_file = tmp_path / 'engine.log'
    scratch = scratch_project(tmp_path, PROGRAM)
    engine = Engine(project=scratch.directory, env=scratch.env | {'PROBE_LOG': str(log_file)}, log_file=log_file)
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = engine.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    engine.settings()
    first = engine.up()
    assert first.returncode == 0, _ran(first)
    _ = engine.log()
    return engine


def test_a_new_rendering_runs_nothing_on_the_seed_or_the_domain(launched: Engine) -> None:
    launched.settings(rendering='r2')

    result = launched.up()

    assert result.returncode == 0, _ran(result)
    assert launched.log() == []


def test_a_rebuilt_disk_replaces_the_domain_and_the_seed_from_the_current_rendering(launched: Engine) -> None:
    disk = launched.urn('disk')
    launched.settings(rendering='r2')

    # The disk is protected in state and in the program, and the engine
    # refuses the replacement until the state's protection is lifted.
    refused = launched.up('--replace', disk)
    assert refused.returncode != 0
    assert 'protect' in _ran(refused)
    assert launched.log() == []
    unprotected = launched.pulumi('state', 'unprotect', '--yes', disk)
    assert unprotected.returncode == 0, _ran(unprotected)

    result = launched.up('--replace', disk)

    assert result.returncode == 0, _ran(result)
    log = [re.sub(r'(disk|seed)-[0-9a-f]{8}', r'\1-<id>', line) for line in launched.log()]
    # Every name is stated on the host, so nothing is created while an object
    # of its name still exists: all three go first, the disk last, and the
    # seed comes back from the rendering current at this run.
    deleted, created = log[:3], log[3:]
    assert sorted(deleted) == ['delete disk', 'delete domain', 'delete seed']
    assert deleted[-1] == 'delete disk'
    assert created == ['create disk gen=g1', 'create seed source=r2', 'create domain disks=disk-<id>,seed-<id>']
    # The program's `protect` protects the new disk in the same run.
    assert launched.resources()['disk'].get('protect') is True


@pytest.mark.parametrize(
    ('name', 'created'), [('seed', 'create seed source=r1'), ('domain', 'create domain disks=')], ids=['seed', 'domain']
)
def test_a_resource_replaced_alone_is_deleted_before_it_is_created_again(
    launched: Engine, name: str, created: str
) -> None:
    """The seed or the domain alone, as a `--replace` of it or a field read back differently replaces it.

    Both names are stated on the host. The provider adopts an existing volume
    of the seed's name, so a create before the delete would upload into the
    live seed and then delete it from under the new domain; libvirt refuses a
    second domain of one name. The disk's option says nothing here, since the
    disk is not replaced, so each resource's own option is what orders it.
    """
    result = launched.up('--replace', launched.urn(name))

    assert result.returncode == 0, _ran(result)
    log = [line for line in launched.log() if line.split()[1] == name]
    assert len(log) == 2, log
    assert log[0] == f'delete {name}'
    assert log[1].startswith(created), log


REBUILD_PROGRAM = """\
import os
import pathlib

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider

from kluster.providers.talos_factory import FactoryImage

LOG = pathlib.Path(os.environ['PROBE_LOG'])


def log(line):
    with LOG.open('a') as handle:
        handle.write(line + '\\n')


class Disk(ResourceProvider):
    \"\"\"A volume created from a file, which it opens at its create and nowhere before.\"\"\"

    def create(self, props):
        content = pathlib.Path(props['source']).read_bytes()
        log(f'create disk from {content.decode()}')
        return CreateResult(id_='disk', outs=dict(props))

    def diff(self, _id, olds, news):
        return DiffResult(changes=False)

    def delete(self, _id, _props):
        log('delete disk')


class Volume(Resource):
    def __init__(self, name, props, opts):
        super().__init__(Disk(), name, props, opts)


image = FactoryImage('image', url=os.environ['PROBE_URL'], path=os.environ['PROBE_IMAGE'], room=0)
Volume(
    'disk',
    {'source': image.path},
    pulumi.ResourceOptions(ignore_changes=['source'], delete_before_replace=True),
)
"""

#: What the case's factory serves, compressed as the Image Factory serves
#: `nocloud`, and what the disk is created from once it is decompressed.
ARTIFACT = b'talos'


@dataclass
class Rebuild:
    """The worker's image and disk after their first `up`, with the log read empty."""

    engine: Engine
    #: The file the image caches the artifact in, on the machine running the program.
    cached: Path


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[str]:
    """A factory of the case's own, serving the artifact at the URL it yields and logging every request."""
    log_file = tmp_path / 'engine.log'
    compressed = lzma.compress(ARTIFACT)

    class Serving(http_server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with log_file.open('a') as handle:
                _ = handle.write('fetch image\n')
            self.send_response(200)
            self.send_header('Content-Length', str(len(compressed)))
            self.end_headers()
            _ = self.wfile.write(compressed)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class's own name
            pass

    # The standard library's server on its own thread, the way its own
    # documentation runs one beside a client.
    server = http_server.ThreadingHTTPServer(('127.0.0.1', 0), Serving)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/nocloud-amd64.raw.xz'
    finally:
        server.shutdown()
        server.server_close()
        serving.join()


@pytest.fixture
def rebuild(tmp_path: Path, factory: str) -> Rebuild:
    log_file = tmp_path / 'engine.log'
    cached = tmp_path / 'cache' / 'talos.raw'
    scratch = scratch_project(tmp_path, REBUILD_PROGRAM)
    # The test run's packages reach the program through the paths of its
    # site directories, and an editable install's own `.pth` there is not
    # read through a path, so the provider's tree is named for the program.
    (site_packages,) = (tmp_path / 'venv').glob('lib/python*/site-packages')
    _ = (site_packages / 'kluster.pth').write_text(f'{Path(kluster.__file__).parent.parent}\n')
    engine = Engine(
        project=scratch.directory,
        env=scratch.env
        | {
            'PROBE_LOG': str(log_file),
            'PROBE_URL': factory,
            'PROBE_IMAGE': str(cached),
            # The factory is on the loopback, and a proxy the test run's
            # environment names would not reach it. Both spellings, since
            # `requests` reads the lowercase one first.
            'no_proxy': '127.0.0.1',
            'NO_PROXY': '127.0.0.1',
        },
        log_file=log_file,
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
    assert engine.log() == ['fetch image', f'create disk from {ARTIFACT.decode()}']
    return Rebuild(engine=engine, cached=cached)


def test_a_rebuild_naming_the_disk_alone_deletes_it_before_a_missing_image_fails_its_create(rebuild: Rebuild) -> None:
    """Why the rebuild names the image too (operations.md §3.2).

    The image's resource has nothing to do in a run that replaces only the
    disk, so nothing brings back a cached file that is gone, and the disk's
    create is the first thing to open it: after the delete.
    """
    rebuild.cached.unlink()

    result = rebuild.engine.up('--replace', rebuild.engine.urn('disk'))

    assert result.returncode != 0
    assert rebuild.engine.log() == ['delete disk']
    # What state keeps is the deleted original, awaiting a replacement that
    # was never made.
    assert rebuild.engine.resources()['disk'].get('pendingReplacement') is True


@pytest.mark.parametrize('cache', ['cleaned', 'kept'])
def test_a_rebuild_naming_the_image_too_has_the_image_in_place_before_it_deletes_the_disk(
    rebuild: Rebuild, cache: str
) -> None:
    """The rebuild's `up`, replacing the image and the disk (operations.md §3.2).

    The image's replacement is created before the disk's delete-first
    replacement begins: it fetches the artifact where the cache has lost it
    and reuses it where it has not. The image's original is deleted last, and
    finds the replacement's write at the path, so the file stands.
    """
    if cache == 'cleaned':
        rebuild.cached.unlink()
    engine = rebuild.engine

    result = engine.up('--replace', engine.urn('image'), '--replace', engine.urn('disk'))

    assert result.returncode == 0, _ran(result)
    fetched = ['fetch image'] if cache == 'cleaned' else []
    assert engine.log() == [*fetched, 'delete disk', f'create disk from {ARTIFACT.decode()}']
    assert rebuild.cached.read_bytes() == ARTIFACT
