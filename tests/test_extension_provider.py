"""An extension package's resource lands on the explicit provider it is handed, held by the pinned `pulumi` itself.

The kubernetes provider's CRD extension (kluster-ops#160) generates classes
whose type tokens are `crds:<group>/<version>:<Kind>` while the provider that
serves them is `kubernetes`. Each stack here builds its providers and turns
the default ones off (`pulumi:disable-default-providers`, style/pulumi.md
"Every provider is explicit"), so a `crds:` resource that the SDK does not
send to the explicit `kubernetes` provider it was given asks the engine for
the default one and fails the run with `denydefaultprovider not registered`.
Whether it is sent there is the SDK's lookup of the extension's base
provider (pulumi/pulumi#24702), which only a registration against the engine
exercises: the mock monitor answers `RegisterPackage` without the extension
ever reaching a provider.

So each case runs the CLI `mise.toml` pins over a `file://` backend and a
`PULUMI_HOME` of the module's own, against a program declaring one `crds:`
resource from an extension generated for one throwaway CRD, with the
explicit provider handed over in one spelling per case. A `preview` is the
whole run: the provider's kubeconfig names a closed port, so the provider
previews without a cluster, and the step the engine planned names the
provider it was given.

Unlike the other engine suites, this one fetches: the extension is the
kubernetes provider's, so `pulumi package gen-sdk` and the engine both need
its plugin, at the version the locked `pulumi-kubernetes` registers. It is
downloaded once per run, into the module's home.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess as sp
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which('pulumi') is None or shutil.which('uv') is None, reason='the pinned pulumi CLI or uv is not on PATH'
)

#: One CRD with one served version and one field: the smallest manifest the
#: extension generates a resource class from.
CRD = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.probe.example.com
spec:
  group: probe.example.com
  names: {kind: Widget, listKind: WidgetList, plural: widgets, singular: widget}
  scope: Namespaced
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                size: {type: integer}
"""

#: The token every generated class of the extension carries: the extension's
#: name, not the provider's.
TOKEN = 'crds:probe.example.com/v1:Widget'

#: A kubeconfig whose server is a closed port: the provider configures with it
#: and previews without a cluster, and nothing is ever contacted.
KUBECONFIG = {
    'apiVersion': 'v1',
    'kind': 'Config',
    'clusters': [{'name': 'nowhere', 'cluster': {'server': 'https://127.0.0.1:1'}}],
    'users': [{'name': 'nobody', 'user': {'token': 'none'}}],
    'contexts': [{'name': 'nowhere', 'context': {'cluster': 'nowhere', 'user': 'nobody'}}],
    'current-context': 'nowhere',
}

#: The program, with `{options}` the resource options handing the provider
#: over and `{parent}` whatever has to exist for them to name.
PROGRAM = """\
import json

import pulumi
import pulumi_kubernetes as k8s
from pulumi_crds.probe.v1 import Widget


class Holder(pulumi.ComponentResource):
    def __init__(self, name, opts=None):
        super().__init__('probe:index:Holder', name, None, opts)


provider = k8s.Provider('explicit', kubeconfig=json.dumps({kubeconfig}))
{parent}
Widget('widget', metadata={{'name': 'w'}}, spec={{'size': 1}}, opts={options})
"""

#: Every way a program hands a resource its provider, by the resource options
#: that do it and the parent they need: the option itself, the map and the
#: list, and a parent component that carries the provider -- the spelling the
#: stacks use, where a component is handed its providers and its children
#: inherit them.
SPELLINGS = {
    'provider': ('pulumi.ResourceOptions(provider=provider)', ''),
    'providers-map': ("pulumi.ResourceOptions(providers={'kubernetes': provider})", ''),
    'providers-list': ('pulumi.ResourceOptions(providers=[provider])', ''),
    'parent': (
        'pulumi.ResourceOptions(parent=holder)',
        "holder = Holder('holder', opts=pulumi.ResourceOptions(providers=[provider]))",
    ),
}

PROJECT = """\
name: probe
runtime:
  name: python
  options:
    toolchain: uv
    virtualenv: {venv}
"""

#: The stack's configuration: the kubernetes default provider turned off, as
#: every stack here turns off the default provider of each package it builds
#: providers for.
STACK_CONFIG = """\
config:
  pulumi:disable-default-providers:
    - kubernetes
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

#: How long one `pulumi` command may take before the case fails naming it: a
#: hang guard, far above what any of these takes.
COMMAND_TIMEOUT = 300

STACK = 'probe'
PASSPHRASE = 'a-passphrase-for-a-scratch-stack-that-holds-nothing'


def _scrubbed(environ: Mapping[str, str]) -> dict[str, str]:
    """The test run's environment, without anything that could steer `pulumi` at another backend."""
    return {key: value for key, value in environ.items() if not key.startswith(('PULUMI_', 'PG', 'KUBE'))}


def _pulumi(*args: str, cwd: Path, env: Mapping[str, str]) -> sp.CompletedProcess[str]:
    return sp.run(
        ['pulumi', '--non-interactive', *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
        check=False,
    )


@dataclass
class Extension:
    """The generated extension SDK, and the home holding the plugin it was generated with."""

    sdk: Path
    home: Path


@pytest.fixture(scope='module')
def extension(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Extension]:
    root = tmp_path_factory.mktemp('extension')
    manifest = root / 'crd.yaml'
    _ = manifest.write_text(CRD)
    home = root / 'pulumi-home'
    env = _scrubbed(os.environ) | {'PULUMI_HOME': str(home), 'PULUMI_SKIP_UPDATE_CHECK': 'true'}
    generated = _pulumi(
        'package',
        'gen-sdk',
        'kubernetes',
        '--version',
        version('pulumi-kubernetes'),
        '--extension',
        f'name=crds crd-manifest={manifest}',
        '--language',
        'python',
        '--out',
        str(root / 'sdk'),
        cwd=root,
        env=env,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    yield Extension(sdk=root / 'sdk' / 'python', home=home)
    # The plugin is hundreds of megabytes unpacked: gone with the module, not
    # left for the temporary directory's retention.
    shutil.rmtree(root, ignore_errors=True)


def preview(extension: Extension, tmp_path: Path, spelling: str) -> sp.CompletedProcess[str]:
    """`pulumi preview --json` of the program handing the provider over in `spelling`, on a fresh stack."""
    options, parent = SPELLINGS[spelling]
    project = tmp_path / 'project'
    project.mkdir()
    venv = tmp_path / 'venv'
    _ = sp.run(['uv', 'venv', '-q', '--python', sys.executable, str(venv)], check=True, timeout=120)
    (site_packages,) = venv.glob('lib/python*/site-packages')
    # The generated SDK first: the repository's own `pulumi_crds` is the
    # `crd2pulumi` package of the same name. Then the test run's site
    # directories, in its order, so the program runs on the SDK this run
    # imports.
    reached = [str(extension.sdk), *(entry for entry in sys.path if entry.endswith('site-packages'))]
    _ = (site_packages / 'test_run.pth').write_text('\n'.join(reached) + '\n')
    _ = (project / '__main__.py').write_text(
        PROGRAM.format(kubeconfig=repr(KUBECONFIG), options=options, parent=parent)
    )
    _ = (project / 'Pulumi.yaml').write_text(PROJECT.format(venv=venv))
    _ = (project / f'Pulumi.{STACK}.yaml').write_text(STACK_CONFIG)
    _ = (project / 'pyproject.toml').write_text(
        PYPROJECT.format(major=sys.version_info.major, minor=sys.version_info.minor)
    )
    _ = sp.run(['uv', 'lock', '-q', '--offline'], cwd=project, check=True, timeout=120)
    (tmp_path / 'state').mkdir()
    env = _scrubbed(os.environ) | {
        'PULUMI_BACKEND_URL': f'file://{tmp_path / "state"}',
        'PULUMI_HOME': str(extension.home),
        'PULUMI_CONFIG_PASSPHRASE': PASSPHRASE,
        'PULUMI_SKIP_UPDATE_CHECK': 'true',
        'UV_OFFLINE': '1',
    }
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = _pulumi('stack', 'ls', '--all', '--json', cwd=project, env=env)
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = _pulumi('stack', 'init', STACK, cwd=project, env=env)
    assert initialized.returncode == 0, initialized.stderr
    return _pulumi('preview', '--json', cwd=project, env=env)


@pytest.mark.timeout(240)
@pytest.mark.parametrize('spelling', sorted(SPELLINGS))
def test_an_extension_resource_lands_on_the_explicit_provider_it_is_handed(
    extension: Extension, tmp_path: Path, spelling: str
) -> None:
    result = preview(extension, tmp_path, spelling)
    assert result.returncode == 0, result.stdout + result.stderr
    steps = cast('list[dict[str, Any]]', json.loads(result.stdout)['steps'])
    (widget,) = [step for step in steps if step['newState']['type'] == TOKEN]
    provider = str(widget['newState']['provider'])
    assert provider.split('::')[-2] == 'explicit', provider
    assert provider.split('::')[-3] == 'pulumi:providers:kubernetes', provider
