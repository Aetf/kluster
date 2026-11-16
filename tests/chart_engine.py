"""A chart of a case's own, installed through `helm_chart` by the pinned `pulumi`, with no cluster.

What the engine makes of a chart -- the transforms it applies to what the
chart renders, the dependencies it registers the rendered objects with -- shows
only in a run against the engine, never under the mock monitor
(framework/testing.md §8). `installed` is that run: the CLI `mise.toml` pins,
over a `file://` backend and a `PULUMI_HOME` of the case's own, runs `up` on a
fresh stack of a program that installs the case's chart through `helm_chart`,
served from a Helm repository the case runs on `127.0.0.1`, and reads the
stack's state back. The provider is in its `renderYamlToDirectory` mode, which
writes each object as a manifest instead of sending it to a cluster, so an
`up` runs with no cluster at all.

Like `tests/test_extension_provider.py`, a case fetches the Kubernetes
provider plugin, at the version the locked `pulumi-kubernetes` registers,
which the engine installs into the case's own home: a chart is that
provider's component, so no stand-in would exercise the engine's handling of
it.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import shutil
import site
import subprocess as sp
import tarfile
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http import server as http_server
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
from scratch_projects import scratch_project

#: What a module running this harness is marked with: it skips where the
#: pinned CLI or `uv` is not on `PATH`, saying so.
pulumi_and_uv_on_path = pytest.mark.skipif(
    shutil.which('pulumi') is None or shutil.which('uv') is None, reason='the pinned pulumi CLI or uv is not on PATH'
)

#: How long a case of this harness may take: its project's set-up, the
#: plugin's download and every command it runs. On four cores, idle and then
#: with four times as many busy processes as cores, the case of
#: `tests/test_admission_wait_engine.py` took 3 s and 6 s, and the case of
#: `tests/test_install_order_engine.py`, whose chart renders a few more
#: objects, 3 s and 5 s. Twice `COMMAND_TIMEOUT`, so a stalled command fails
#: naming itself before this fires; a stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, many times the slowest whole case measured under that load, and
#: below `CASE_TIMEOUT`.
COMMAND_TIMEOUT = 120

#: A Helm repository's index naming the one packaged chart, by a URL relative
#: to the repository's own.
INDEX = """\
apiVersion: v1
entries:
  probe:
    - apiVersion: v2
      name: probe
      version: 0.1.0
      digest: {digest}
      urls: [probe-0.1.0.tgz]
      created: "2026-01-01T00:00:00Z"
generated: "2026-01-01T00:00:00Z"
"""

#: The program: the chart installed through `helm_chart` from the case's
#: repository, on a provider that renders to a directory.
PROGRAM = """\
import os

import pulumi
import pulumi_kubernetes as k8s

from kluster.lib.k8s import helm_chart
from kluster.lib.versions import ChartPin

provider = k8s.Provider(
    'render',
    render_yaml_to_directory=os.environ['PROBE_RENDERED'],
)
helm_chart(
    'probe',
    pin=ChartPin(
        name='probe', repository=os.environ['PROBE_REPOSITORY'], version='0.1.0', digest=None, definitions=False
    ),
    namespace='probe',
    opts=pulumi.ResourceOptions(providers=[provider]),
)
"""

STACK = 'probe'


def packaged(chart: Mapping[str, str]) -> bytes:
    """`chart`, files by their path under its directory, as Helm packages it: a gzipped tar under a directory named for it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for path, content in chart.items():
            data = content.encode()
            member = tarfile.TarInfo(f'probe/{path}')
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


@contextmanager
def served(directory: Path, chart: Mapping[str, str]) -> Generator[str]:
    """A Helm repository of the case's own, serving `chart` from `127.0.0.1` at the URL it yields."""
    directory.mkdir()
    archive = packaged(chart)
    _ = (directory / 'probe-0.1.0.tgz').write_bytes(archive)
    _ = (directory / 'index.yaml').write_text(INDEX.format(digest=hashlib.sha256(archive).hexdigest()))

    class Serving(http_server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class's own name
            pass

    # The standard library's server on its own thread, the way its own
    # documentation runs one beside a client.
    server = http_server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Serving, directory=str(directory)))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        serving.join()


def _pulumi(*args: str, cwd: Path, env: dict[str, str]) -> sp.CompletedProcess[str]:
    """One `pulumi` command, its plugins in its POSIX session and gone with it however it ends."""
    return process_sessions.run(
        ['pulumi', '--non-interactive', *args], cwd=cwd, env=env, text=True, timeout=COMMAND_TIMEOUT
    )


@dataclass(frozen=True)
class Installed:
    """What an `up` of `PROGRAM` left: the stack's resources as `pulumi stack export` reads them, and the manifests."""

    resources: list[dict[str, Any]]
    #: The directory the provider wrote each object's manifest under.
    rendered: Path


@contextmanager
def installed(tmp_path: Path, chart: Mapping[str, str]) -> Generator[Installed]:
    """`chart` installed through `helm_chart` by an `up` of a fresh stack, with no cluster.

    The case's `PULUMI_HOME`, which holds the provider plugin, goes when the
    block ends rather than being left for retention: unpacked, the plugin is
    hundreds of megabytes.
    """
    scratch = scratch_project(tmp_path, PROGRAM)
    # The program imports this repository's code, which the test run reaches
    # through editable installs, and an editable install's own `.pth` is read
    # only in a site directory: so each of the run's is made one for the
    # program, rather than merely put on its path.
    (site_packages,) = (tmp_path / 'venv').glob('lib/python*/site-packages')
    _ = (site_packages / 'test_run_sites.pth').write_text(
        ''.join(f'import site; site.addsitedir({directory!r})\n' for directory in site.getsitepackages())
    )
    rendered = tmp_path / 'rendered'
    helm = tmp_path / 'helm'
    try:
        with served(tmp_path / 'repository', chart) as repository:
            env = scratch.env | {
                'PROBE_REPOSITORY': repository,
                'PROBE_RENDERED': str(rendered),
                # Helm's repository cache and configuration are the case's own.
                'HELM_CACHE_HOME': str(helm / 'cache'),
                'HELM_CONFIG_HOME': str(helm / 'config'),
                'HELM_DATA_HOME': str(helm / 'data'),
                # The repository is on the loopback, and a proxy the test run's
                # environment names would be asked for it instead.
                'no_proxy': '127.0.0.1',
                'NO_PROXY': '127.0.0.1',
            }
            # No kubeconfig: `KUBECONFIG` names a file that does not exist, so
            # the provider asks no cluster for its version and renders without
            # one, and neither the test run's kubeconfig nor the home's is read.
            env = {key: value for key, value in env.items() if not key.startswith('KUBE')} | {
                'KUBECONFIG': str(tmp_path / 'no-kubeconfig')
            }

            # The backend in hand is the case's own, and holds nothing yet
            # (framework/testing.md §5.1).
            listed = _pulumi('stack', 'ls', '--all', '--json', cwd=scratch.directory, env=env)
            assert listed.returncode == 0, listed.stderr
            assert json.loads(listed.stdout) == []
            initialized = _pulumi('stack', 'init', STACK, cwd=scratch.directory, env=env)
            assert initialized.returncode == 0, initialized.stderr
            applied = _pulumi('up', '--yes', '--skip-preview', cwd=scratch.directory, env=env)
            assert applied.returncode == 0, applied.stdout + applied.stderr
            exported = _pulumi('stack', 'export', cwd=scratch.directory, env=env)
            assert exported.returncode == 0, exported.stderr
        resources = cast('list[dict[str, Any]]', json.loads(exported.stdout)['deployment']['resources'])
        yield Installed(resources=resources, rendered=rendered)
    finally:
        shutil.rmtree(tmp_path / 'pulumi-home', ignore_errors=True)
