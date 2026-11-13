"""A chart's webhook configurations reach the provider awaiting their CA bundles, held by the pinned `pulumi` itself.

`kluster.lib.k8s.helm_chart` binds one resource transform on the chart, and
what makes that reach the objects the chart renders is the engine: it keeps a
remote component's transforms and applies them to every registration whose
parent chain passes through it. The mock monitor applies no transform, so only
a run against the engine shows a rendered object carrying what the transform
set (framework/testing.md §8).

So the case runs the CLI `mise.toml` pins over a `file://` backend and a
`PULUMI_HOME` of its own, against a program that installs a chart of the
case's own through `helm_chart`, served from a Helm repository the case runs
on `127.0.0.1`. The provider is in its `renderYamlToDirectory` mode, which
writes each object as a manifest instead of sending it to a cluster, so an
`up` runs with no cluster at all. The chart renders a validating and a
mutating webhook configuration and a ConfigMap; what the case reads is the
inputs each object was created with, from the stack's state, and the
manifest the provider wrote for each webhook configuration.

Like `tests/test_extension_provider.py`, the case fetches the Kubernetes
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
from collections.abc import Iterator
from http import server as http_server
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
from scratch_projects import scratch_project

pytestmark = pytest.mark.skipif(
    shutil.which('pulumi') is None or shutil.which('uv') is None, reason='the pinned pulumi CLI or uv is not on PATH'
)

#: How long the case may take: its project's set-up, the plugin's download and
#: every command it runs. On four cores the case took 3 s idle and 6 s with
#: four times as many busy processes as cores. Twice `COMMAND_TIMEOUT`, so a
#: stalled command fails naming itself before this fires; a stop-loss, which
#: nothing asserts on.
CASE_TIMEOUT = 240

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, many times the slowest whole case measured under that load, and
#: below `CASE_TIMEOUT`.
COMMAND_TIMEOUT = 120

#: The chart, as files under its directory. Two webhook configurations, each
#: with an entry calling a Service and carrying no CA bundle; the validating
#: one has a second entry that carries one. And one object of another kind.
CHART = {
    'Chart.yaml': """\
apiVersion: v2
name: probe
version: 0.1.0
""",
    'templates/webhooks.yaml': """\
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: probe-validating
webhooks:
  - name: pending.probe.example.com
    clientConfig:
      service: {name: probe-webhook, namespace: probe, path: /validate}
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
  - name: injected.probe.example.com
    clientConfig:
      service: {name: probe-webhook, namespace: probe, path: /validate}
      caBundle: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg==
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
---
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: probe-mutating
webhooks:
  - name: pending.probe.example.com
    clientConfig:
      service: {name: probe-webhook, namespace: probe, path: /mutate}
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
""",
    'templates/configmap.yaml': """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-settings
data:
  level: info
""",
}

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
WAIT_FOR = 'pulumi.com/waitFor'


def packaged() -> bytes:
    """The chart as Helm packages it: a gzipped tar of its files under a directory named for it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for path, content in CHART.items():
            data = content.encode()
            member = tarfile.TarInfo(f'probe/{path}')
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[str]:
    """A Helm repository of the case's own, serving the chart from `127.0.0.1` at the URL it yields."""
    served = tmp_path / 'repository'
    served.mkdir()
    archive = packaged()
    _ = (served / 'probe-0.1.0.tgz').write_bytes(archive)
    _ = (served / 'index.yaml').write_text(INDEX.format(digest=hashlib.sha256(archive).hexdigest()))

    class Serving(http_server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the base class's own name
            pass

    # The standard library's server on its own thread, the way its own
    # documentation runs one beside a client.
    server = http_server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Serving, directory=str(served)))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        serving.join()


@pytest.fixture(autouse=True)
def home_removed(tmp_path: Path) -> Iterator[None]:
    """The case's `PULUMI_HOME`, which holds the plugin, gone with the case rather than left for retention."""
    yield
    # The plugin is hundreds of megabytes unpacked.
    shutil.rmtree(tmp_path / 'pulumi-home', ignore_errors=True)


def _pulumi(*args: str, cwd: Path, env: dict[str, str]) -> sp.CompletedProcess[str]:
    """One `pulumi` command, its plugins in its POSIX session and gone with it however it ends."""
    return process_sessions.run(
        ['pulumi', '--non-interactive', *args], cwd=cwd, env=env, text=True, timeout=COMMAND_TIMEOUT
    )


@pytest.mark.timeout(CASE_TIMEOUT)
def test_a_charts_webhook_configurations_reach_the_provider_awaiting_their_bundles(
    tmp_path: Path, repository: str
) -> None:
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
    # No kubeconfig: `KUBECONFIG` names a file that does not exist, so the
    # provider asks no cluster for its version and renders without one, and
    # neither the test run's kubeconfig nor the home's is read.
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
    created = {
        str(resource['inputs']['kind']): cast('dict[str, Any]', resource['inputs'])
        for resource in resources
        if resource['type'].startswith('kubernetes:') and 'kind' in resource.get('inputs', {})
    }
    assert sorted(created) == ['ConfigMap', 'MutatingWebhookConfiguration', 'ValidatingWebhookConfiguration']

    def awaited(kind: str) -> list[str]:
        return json.loads(created[kind]['metadata']['annotations'][WAIT_FOR])

    # The validating configuration's second entry carries its bundle, so only
    # the first is awaited.
    assert awaited('ValidatingWebhookConfiguration') == ['jsonpath={.webhooks[0].clientConfig.caBundle}']
    assert awaited('MutatingWebhookConfiguration') == ['jsonpath={.webhooks[0].clientConfig.caBundle}']
    assert WAIT_FOR not in created['ConfigMap']['metadata'].get('annotations', {})

    # And the manifest the provider wrote for each carries it.
    manifests = {path.name: path.read_text() for path in rendered.rglob('*.yaml')}
    for kind in ('validatingwebhookconfiguration', 'mutatingwebhookconfiguration'):
        (manifest,) = [text for name, text in manifests.items() if kind in name.lower()]
        assert WAIT_FOR in manifest, manifest
