"""The cluster zone's membership, owned from the network's end, as the pinned `pulumi` plans it.

`SiteFirewall` states the cluster network's zone from the network,
`firewall_zone_id`, and the zone declares no `networks`. The controller holds
the association from either end, so a refresh reads the network's id into the
zone's `networks`. The pinned provider plans an unset `networks` as an empty
list (terraform-provider-unifi v1.1.0,
`internal/provider/firewall/resource_firewall_zone.go`: `Optional`,
`Computed`, `Default: ut.DefaultEmptyList`), and its client sends the list on
every write of the zone (go-unifi v1.9.3, `unifi/firewall_zone.generated.go`:
`network_ids` without `omitempty`). So against a refreshed state the zone
plans an update that would empty it, unless its `ignore_changes` takes the
list out of the diff. The engine copies an ignored path from the old inputs,
which hold no `networks`; what answers is the bridge, which copies each
ignored path from the prior state before it plans, in its diff and in its
update alike (pulumi-terraform-bridge 20da3e0a, which `terraform-provider`
v1.5.0 is built from: `pkg/pf/tfbridge/provider_diff.go` and
`provider_update.go`, through `unstable/propertyvalue/ignore_changes.go`).

No mock plans a step, so each case runs the CLI `mise.toml` pins over a
`file://` backend and a `PULUMI_HOME` of the case's own, against a program
that declares the real `SiteFirewall`. The `unifi` package is served by a
stand-in `terraform-provider` plugin of the case's own, answering the SDK's
parameterization, that plans as the bridge and the provider above do. It
keeps what the controller holds in a file, each network's zone, which the
zone's read reports as its `networks`, and every write it is asked for
appends a line to a log. The runs target the component, the zone and the
network, since the rest of the census dials a controller this case does not
stand in for. The project around the program is `scratch_projects`', and
every command leads a POSIX session of its own that ends with it
(`process_sessions`).
"""

from __future__ import annotations

import json
import shutil
import site
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
from scratch_projects import scratch_project

from kluster.components.gateway.unifi import API_KEY

#: How long one case may take: its project's set-up and every command it runs
#: against the engine. A stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, below the case bound.
COMMAND_TIMEOUT = 120

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

STACK = 'probe'
ZONE = 'unifi:index/firewallZone:FirewallZone'
NETWORK = 'unifi:index/network:Network'

#: The runs' targets: the component, its zone and its network. The provider
#: instance they are declared with is created with them.
TARGETS = ('**SiteFirewall::kluster', '**FirewallZone::kluster-zone', '**Network::kluster-network')

PROVIDER = """\
import json
import os
import pathlib
import sys

from pulumi.provider.experimental.property_value import PropertyValue
from pulumi.provider.experimental.provider import (
    CheckResponse,
    ConfigureResponse,
    CreateResponse,
    DiffResponse,
    InvokeResponse,
    ParameterizeResponse,
    Provider,
    ReadResponse,
    UpdateResponse,
)
from pulumi.provider.experimental.server import main

LOG = pathlib.Path(os.environ['PROBE_LOG'])
#: What the controller holds: each network's zone, by the network's id.
CONTROLLER = pathlib.Path(os.environ['PROBE_CONTROLLER'])
ZONE = 'unifi:index/firewallZone:FirewallZone'
NETWORK = 'unifi:index/network:Network'


def log(line):
    with LOG.open('a') as handle:
        handle.write(line + '\\n')


def kind(urn):
    return urn.split('::')[2].split('$')[-1]


def name(urn):
    return urn.split('::')[3]


def zones():
    return json.loads(CONTROLLER.read_text()) if CONTROLLER.exists() else {}


def planned(urn, inputs):
    # A zone's unset `networks` is planned as the provider's default.
    if kind(urn) == ZONE and inputs.get('networks', PropertyValue(None)).value is None:
        return dict(inputs) | {'networks': PropertyValue([])}
    return dict(inputs)


class StandIn(Provider):
    async def parameterize(self, request):
        return ParameterizeResponse(name='unifi', version='1.1.0')

    async def configure(self, request):
        return ConfigureResponse(accept_secrets=True, supports_preview=True, accept_outputs=True, accept_resources=True)

    async def invoke(self, request):
        zone = request.args['name'].value
        return InvokeResponse(return_value={'id': PropertyValue(f'zone-{zone}'), 'name': PropertyValue(zone)})

    async def diff_config(self, request):
        changed = [key for key, value in request.new_inputs.items() if request.old_state.get(key) != value]
        return DiffResponse(changes=bool(changed), diffs=changed)

    async def check(self, request):
        return CheckResponse(inputs=request.new_inputs)

    async def diff(self, request):
        # The bridge's copy of each ignored path, from the prior state.
        inputs = dict(request.new_inputs)
        for path in request.ignore_changes:
            if path in request.old_state:
                inputs[path] = request.old_state[path]
            else:
                inputs.pop(path, None)
        plan = planned(request.urn, inputs)
        changed = [key for key, value in plan.items() if request.old_state.get(key) != value]
        return DiffResponse(changes=bool(changed), diffs=changed)

    async def create(self, request):
        props = planned(request.urn, request.properties)
        resource_id = f'{name(request.urn)}-id'
        if not request.preview:
            log(f'create {kind(request.urn)} {name(request.urn)}')
            if kind(request.urn) == NETWORK:
                CONTROLLER.write_text(json.dumps(zones() | {resource_id: props['firewallZoneId'].value}))
        return CreateResponse(resource_id=resource_id, properties=props)

    async def read(self, request):
        # The controller reports a zone's members whichever end stated them.
        props = dict(request.properties)
        if kind(request.urn) == ZONE:
            members = [network for network, zone in zones().items() if zone == request.resource_id]
            props['networks'] = PropertyValue([PropertyValue(member) for member in members])
        return ReadResponse(resource_id=request.resource_id, properties=props, inputs=request.inputs)

    async def update(self, request):
        # The same copy, ahead of the write the update sends.
        inputs = dict(request.news)
        for path in request.ignore_changes:
            if path in request.olds:
                inputs[path] = request.olds[path]
        props = planned(request.urn, inputs)
        if not request.preview:
            log(f'update {kind(request.urn)} {name(request.urn)} networks={props.get("networks")}')
        return UpdateResponse(properties=props)

    async def delete(self, request):
        log(f'delete {kind(request.urn)} {name(request.urn)}')


main(sys.argv[1:], '1.5.0', StandIn())
"""

PROGRAM = """\
from kluster.components.gateway.unifi import SiteFirewall

SiteFirewall('kluster', api_url='https://gateway.invalid', site='default', worker_gua=None, static_hosts={})
"""


@dataclass
class Engine:
    """One scratch project and its backend, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]
    log_file: Path

    def pulumi(self, *args: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=self.project, env=self.env, text=True, timeout=COMMAND_TIMEOUT
        )

    def targeted(self, command: str, *args: str) -> sp.CompletedProcess[str]:
        return self.pulumi(command, *(argument for target in TARGETS for argument in ('--target', target)), *args)

    def log(self) -> list[str]:
        return self.log_file.read_text().splitlines() if self.log_file.exists() else []

    def state(self, name: str) -> dict[str, Any]:
        """What state holds for the resource of this name: its `id`, its `outputs`."""
        exported = self.pulumi('stack', 'export')
        assert exported.returncode == 0, exported.stderr
        resources = cast('list[dict[str, Any]]', json.loads(exported.stdout)['deployment']['resources'])
        (found,) = [resource for resource in resources if str(resource['urn']).endswith(f'::{name}')]
        return found


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def launched(tmp_path: Path) -> Engine:
    """A stack after its first `up`: the zone created empty, then the network naming it."""
    scratch = scratch_project(tmp_path, PROGRAM, providers={'terraform-provider': PROVIDER})
    # The program imports this repository's code and the bridged SDK it
    # declares with, which the test run reaches through editable installs, and
    # an editable install's own `.pth` is read only in a site directory: so
    # each of the run's is made one for the program.
    (site_packages,) = (tmp_path / 'venv').glob('lib/python*/site-packages')
    _ = (site_packages / 'test_run_sites.pth').write_text(
        ''.join(f'import site; site.addsitedir({directory!r})\n' for directory in site.getsitepackages())
    )
    engine = Engine(
        project=scratch.directory,
        env=scratch.env | {'PROBE_LOG': str(tmp_path / 'log'), 'PROBE_CONTROLLER': str(tmp_path / 'controller.json')},
        log_file=tmp_path / 'log',
    )
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = engine.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    configured = engine.pulumi('config', 'set', '--secret', API_KEY, 'unifi-api-key')
    assert configured.returncode == 0, configured.stderr
    first = engine.targeted('up', '--yes', '--skip-preview')
    assert first.returncode == 0, _ran(first)
    assert engine.log() == [f'create {ZONE} kluster-zone', f'create {NETWORK} kluster-network']
    assert engine.state('kluster-zone')['outputs']['networks'] == []
    return engine


def test_a_refreshed_zone_plans_nothing_for_the_membership_its_network_states(launched: Engine) -> None:
    """The refresh reads the network into the zone, and the preview after it plans no update."""
    refreshed = launched.targeted('refresh', '--yes', '--skip-preview')
    assert refreshed.returncode == 0, _ran(refreshed)
    network = launched.state('kluster-network')['id']
    assert launched.state('kluster-zone')['outputs']['networks'] == [network]

    previewed = launched.targeted('preview', '--refresh', '--expect-no-changes', '--diff')

    assert previewed.returncode == 0, _ran(previewed)


def test_a_refreshed_run_writes_nothing_to_the_zone(launched: Engine) -> None:
    """`up --refresh`, the recovery form, leaves the zone and its membership as the controller holds them."""
    written = len(launched.log())

    result = launched.targeted('up', '--yes', '--skip-preview', '--refresh')

    assert result.returncode == 0, _ran(result)
    assert launched.log()[written:] == []
