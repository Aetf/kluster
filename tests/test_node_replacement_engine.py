"""The engine semantics the recovery's control-plane replacement rests on, held by the pinned `pulumi` itself.

The recovery of a fleet that booted without the balancer in apid's
certificate replaces one control plane's instance in a run of its own, and
bootstraps in the next (physical/gateway-cutover.md §4 step 5). The second
run is safe only if the first leaves no predecessor behind: a replaced
instance whose original is still in the state as a pending delete keeps
running with valid cluster secrets, and stays a backend of every forward,
while the next run bootstraps etcd; it shares its replacement's hostname
whenever both booted a rendering that states one. A preview does
not show such a pending delete, and a run whose flags forbid deletes still
exits zero, so what the runbook's flags do is read off the engine.

The case runs the CLI `mise.toml` pins over a `file://` backend and a
`PULUMI_HOME` of its own (`scratch_projects`), against a program of stand-ins
that registers under the physical stack's own URNs -- project `kluster-py`,
stack `physical` -- and in its dependency shape:

-   `CloudNodes`: one instance per control plane, and one balancer backend per
    node, per forwarded port and per family, each naming its instance's id
    (`components/cloud/nodes.py`).
-   `TalosDay1`: one configuration apply per node, naming the node's private
    address and depending on every backend (`stacks/physical.py`).
-   `Gateway` and `Overlay`: a device file, and the overlay's network adopted
    by id and protected with a member on it, as the window leaves them.

The state is the one the failed first form leaves: everything but Talos day 1
applied. The run is the runbook's run-1 block as written, under a `physical`
that names the scratch stack and confirms the `up`.

The stand-in providers are a plugin of the case's own: an instance's id is
fresh on every create, as a cloud's is, so a replacement moves the backends
that name it, and every create, update and delete appends a line to a log,
which is how the case knows what the run did.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path

import process_sessions
import pytest
from scratch_projects import scratch_project

#: How long the case may take: its project's set-up and every command it runs.
#: Twice `COMMAND_TIMEOUT`, so a stalled command fails naming itself first; a
#: stop-loss, which nothing asserts on.
CASE_TIMEOUT = 240

#: How long one command may take before the case fails naming it: a stop-loss,
#: many times a command as measured, below the case bound.
COMMAND_TIMEOUT = 120

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

#: The cutover runbook, whose step 5 carries the recovery.
RUNBOOK = Path(__file__).parent.parent / 'docs' / 'physical' / 'gateway-cutover.md'

PROJECT = 'kluster-py'
STACK = 'physical'

PROVIDER = """\
import os
import pathlib
import sys
import uuid

from pulumi.provider.experimental.property_value import PropertyValue
from pulumi.provider.experimental.provider import (
    CheckResponse,
    ConfigureResponse,
    CreateResponse,
    DiffResponse,
    Provider,
    ReadResponse,
    UpdateResponse,
)
from pulumi.provider.experimental.server import main

LOG = pathlib.Path(os.environ['PROBE_LOG'])
INSTANCE = 'oci:Core/instance:Instance'


def log(line):
    with LOG.open('a') as handle:
        handle.write(line + '\\n')


def kind(urn):
    return urn.split('::')[2].split('$')[-1]


def name(urn):
    return urn.split('::')[3]


class StandIn(Provider):
    async def configure(self, request):
        return ConfigureResponse(accept_secrets=True, supports_preview=False, accept_outputs=True, accept_resources=True)

    async def check(self, request):
        return CheckResponse(inputs=request.new_inputs)

    async def diff(self, request):
        changed = [key for key, value in request.new_inputs.items() if request.old_state.get(key) != value]
        return DiffResponse(changes=bool(changed), diffs=changed)

    async def create(self, request):
        log(f'create {kind(request.urn)} {name(request.urn)}')
        props = dict(request.properties)
        if kind(request.urn) == INSTANCE:
            props['private_ip'] = PropertyValue(f'10.20.0.{len(name(request.urn))}')
            return CreateResponse(resource_id=f'{name(request.urn)}-{uuid.uuid4().hex[:8]}', properties=props)
        return CreateResponse(resource_id=name(request.urn), properties=props)

    async def read(self, request):
        log(f'read {kind(request.urn)} {request.resource_id}')
        props = request.properties or {'name': PropertyValue('adopted')}
        inputs = request.inputs or {'name': PropertyValue('adopted')}
        return ReadResponse(resource_id=request.resource_id, properties=props, inputs=inputs)

    async def update(self, request):
        log(f'update {kind(request.urn)} {name(request.urn)}')
        return UpdateResponse(properties=dict(request.news))

    async def delete(self, request):
        log(f'delete {kind(request.urn)} {name(request.urn)}')


main(sys.argv[1:], '0.0.1', StandIn())
"""

PROGRAM = """\
import os
import pathlib

import pulumi
from pulumi.dynamic import CreateResult, Resource, ResourceProvider

LOG = pathlib.Path(os.environ['PROBE_LOG'])
NODES = ('cp1', 'cp2', 'cp3')
#: Two of the ports the balancer forwards, each on both families, as
#: `components/cloud/nodes.py` names a backend: the field alone for IPv4.
FORWARDS = ('kubernetes', 'talos', 'kubernetes-ipv6', 'talos-ipv6')


def log(line):
    with LOG.open('a') as handle:
        handle.write(line + '\\n')


class DeviceFileProvider(ResourceProvider):
    def create(self, props):
        log(f'create device-file {props["path"]}')
        return CreateResult(id_=props['path'], outs=dict(props))


class DeviceFile(Resource):
    def __init__(self, name, props, opts):
        super().__init__(DeviceFileProvider(), name, props, opts)


class Gateway(pulumi.ComponentResource):
    def __init__(self, name):
        super().__init__('kluster:gateway:Gateway', name, None, None)
        DeviceFile(f'{name}-udm-boot', {'path': 'on_boot.d/udm-boot'}, pulumi.ResourceOptions(parent=self))
        self.register_outputs({})


class Overlay(pulumi.ComponentResource):
    def __init__(self, name):
        provider = pulumi.ProviderResource('zerotier', f'{name}-zerotier', {})
        super().__init__('kluster:overlay:Overlay', name, None, pulumi.ResourceOptions(providers=[provider]))
        child = pulumi.ResourceOptions(parent=self)
        network = pulumi.CustomResource(
            'zerotier:index/network:Network',
            f'{name}-network',
            {'name': 'adopted'},
            pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(import_='feedc0de00000001', protect=True)),
        )
        pulumi.CustomResource('zerotier:index/member:Member', f'{name}-member-udm', {'network_id': network.id}, child)
        self.register_outputs({})


class Instance(pulumi.CustomResource):
    private_ip: pulumi.Output[str]

    def __init__(self, name, opts):
        super().__init__('oci:Core/instance:Instance', name, {'display_name': name, 'private_ip': None}, opts)


class CloudNodes(pulumi.ComponentResource):
    def __init__(self, name):
        provider = pulumi.ProviderResource('oci', f'{name}-oci', {})
        super().__init__('kluster:cloud:CloudNodes', name, None, pulumi.ResourceOptions(providers=[provider]))
        child = pulumi.ResourceOptions(parent=self)
        self.instances = {node: Instance(f'{name}-{node}', child) for node in NODES}
        self.backends = [
            pulumi.CustomResource(
                'oci:NetworkLoadBalancer/backend:Backend',
                f'{name}-nlb-{forward}-{node}',
                {'target_id': instance.id, 'forward': forward},
                child,
            )
            for forward in FORWARDS
            for node, instance in self.instances.items()
        ]
        self.register_outputs({})


class TalosDay1(pulumi.ComponentResource):
    def __init__(self, name, nodes):
        provider = pulumi.ProviderResource('talos', f'{name}-talos', {})
        super().__init__('kluster:talos:TalosDay1', name, None, pulumi.ResourceOptions(providers=[provider]))
        for node, instance in nodes.instances.items():
            pulumi.CustomResource(
                'talos:machine/configurationApply:ConfigurationApply',
                f'{name}-{node}-config',
                {'node': instance.private_ip},
                pulumi.ResourceOptions(parent=self, depends_on=nodes.backends),
            )
        self.register_outputs({})


Gateway('kluster')
Overlay('kluster')
nodes = CloudNodes('kluster')
# The failed first form applied everything but day 1, which failed at its
# first apply and left nothing in the state.
if os.environ.get('PROBE_STAGE') != 'tonight':
    TalosDay1('kluster', nodes)
"""

#: The types the recovery's first run must write nothing to: Talos, which it
#: leaves out, the gateway's device files, and the overlay.
UNTOUCHED = ('talos:', 'device-file', 'zerotier:')

INSTANCE = 'oci:Core/instance:Instance'

#: The `physical` of the runbook's opening, for the scratch stack: every
#: command names the stack, and an `up` is confirmed, as the operator confirms
#: it once its preview has been read.
PHYSICAL = """\
physical() {
    if [ "$1" = up ]; then
        shift
        pulumi --non-interactive up --yes --skip-preview "$@" --stack physical
    else
        pulumi --non-interactive "$@" --stack physical
    fi
}
"""


def run_1_block() -> str:
    """The runbook's block that replaces `cp3`'s instance: the one `up` carrying `--replace`."""
    blocks = [
        block
        for block in re.findall(r'```sh\n(.*?)```', RUNBOOK.read_text(), re.DOTALL)
        if 'physical up' in block and '--replace' in block
    ]
    assert len(blocks) == 1, blocks
    return blocks[0]


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

    def shell(self, script: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['bash', '-c', PHYSICAL + script], cwd=self.project, env=self.env, text=True, timeout=COMMAND_TIMEOUT
        )

    def log(self) -> list[str]:
        return self.log_file.read_text().splitlines() if self.log_file.exists() else []


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def tonight(tmp_path: Path) -> Engine:
    """The stack the failed first form leaves: the fleet, the gateway and the adopted network, and no day 1."""
    log_file = tmp_path / 'engine.log'
    scratch = scratch_project(
        tmp_path, PROGRAM, name=PROJECT, providers=dict.fromkeys(('oci', 'talos', 'zerotier'), PROVIDER)
    )
    engine = Engine(project=scratch.directory, env=scratch.env | {'PROBE_LOG': str(log_file)}, log_file=log_file)
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    applied = process_sessions.run(
        ['pulumi', '--non-interactive', 'up', '--yes', '--skip-preview'],
        cwd=engine.project,
        env=engine.env | {'PROBE_STAGE': 'tonight'},
        text=True,
        timeout=COMMAND_TIMEOUT,
    )
    assert applied.returncode == 0, _ran(applied)
    return engine


def test_the_recoverys_first_run_replaces_cp3_whole_and_writes_nothing_else(tonight: Engine) -> None:
    """Run 1, as the runbook spells it, over tonight's stack.

    It has to leave no pending delete -- the original instance, and every
    backend the replacement moved, gone from the state -- and to have deleted
    the original, so that nothing named `kluster-cp3` survives into the run
    that bootstraps. And it writes nothing to Talos, the gateway or the
    overlay.
    """
    before = len(tonight.log())

    replaced = tonight.shell(run_1_block())

    assert replaced.returncode == 0, _ran(replaced)
    written = [line for line in tonight.log()[before:] if line.split(' ')[0] in {'create', 'update', 'delete'}]
    assert f'delete {INSTANCE} kluster-cp3' in written, written
    assert not [line for line in written if line.split(' ')[1].startswith(UNTOUCHED)], written
    exported = tonight.pulumi('stack', 'export')
    assert exported.returncode == 0, exported.stderr
    pending = [
        resource['urn'] for resource in json.loads(exported.stdout)['deployment']['resources'] if resource.get('delete')
    ]
    assert pending == []
