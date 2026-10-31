"""The engine semantics the gateway cutover's targeted apply rests on, held by the pinned `pulumi` itself.

Step 3 of the cutover window (docs/physical/gateway-cutover.md §4) is the
`physical` stack's first apply, restricted by `--target`. The overlay network
declares `import_`, and a targeted run imports such a resource whether or not
it is targeted, and only then holds it to every resource it depends on, its
parent among them (framework/pulumi.md §1.4). So the
window's targets name what that adoption depends on as well, and what the
window rests on is that its `up`, with exactly the runbook's targets, leaves a
checkpoint the next command can load and writes nothing to the network or to
a member. No mock can show either, so the case runs the CLI `mise.toml` pins
over a `file://` backend and a `PULUMI_HOME` of its own, against a program of
stand-ins that registers under the physical stack's own URNs -- project
`kluster-py`, stack `physical` -- so the runbook's targets are read from the
runbook and passed as they are:

-   `Gateway`: a component with stand-in device files two levels deep, as
    `components/gateway/` declares its children.
-   `Overlay`: the overlay's dependency shape (`components/overlay/`). A
    `zerotier` provider built as the component's sibling, one generated
    identity per CI member, the network adopted by id and protected, with its
    flow rules rendered over the identities' node addresses, and one member
    per identity.

The overlay's resources are served by a stand-in `zerotier` provider plugin of
the case's own, because the engine imports one of them: an import's `Read`
carries neither inputs nor state, so a dynamic provider has nothing to find
itself in (framework/testing.md §8). The plugin's `read` answers the import
the way Central would, and every method of it that acts appends a line to a
log, which is how the case knows what reached "Central". The project around
the program is `scratch_projects`', and every command leads a POSIX session of
its own that ends with it (`process_sessions`), so a `pulumi` stopped at its
bound takes its plugins with it.
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

#: How long one case may take: its project's set-up and every command it runs
#: against the engine. On 24 cores at a load average of 10 the case took 7 s.
#: Twice `COMMAND_TIMEOUT`, so a stalled command fails naming itself before
#: this fires; a stop-loss, which nothing asserts on.
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

#: The cutover runbook, whose step 3 is the command this case runs.
RUNBOOK = Path(__file__).parent.parent / 'docs' / 'physical' / 'gateway-cutover.md'

#: The project and stack the runbook's URNs name.
PROJECT = 'kluster-py'
STACK = 'physical'

PROVIDER = """\
import hashlib
import os
import pathlib
import sys

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
NETWORK = 'zerotier:index/network:Network'
IDENTITY = 'zerotier:index/identity:Identity'
#: What Central holds for the network before the stack's first run with no targets.
CENTRAL = {'name': 'adopted', 'flow_rules': 'accept;'}


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
        if kind(request.urn) == IDENTITY:
            # Minted locally, as `zerotier_identity` mints a key pair.
            node = hashlib.sha256(name(request.urn).encode()).hexdigest()[:10]
            props['identity_id'] = PropertyValue(node)
            props['private_key'] = PropertyValue(f'{node}:secret-half', is_secret=True)
            return CreateResponse(resource_id=node, properties=props)
        return CreateResponse(resource_id=name(request.urn), properties=props)

    async def read(self, request):
        log(f'read {kind(request.urn)} {request.resource_id}')
        if kind(request.urn) == NETWORK:
            inputs = {key: PropertyValue(value) for key, value in CENTRAL.items()}
            props = dict(inputs) | {'network_id': PropertyValue(request.resource_id)}
            return ReadResponse(resource_id=request.resource_id, properties=props, inputs=inputs)
        return ReadResponse(resource_id=request.resource_id, properties=request.properties, inputs=request.inputs)

    async def update(self, request):
        log(f'update {kind(request.urn)} {name(request.urn)}')
        network_id = request.olds.get('network_id', PropertyValue(None))
        return UpdateResponse(properties=dict(request.news) | {'network_id': network_id})

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
ROSTER = ('ci-physical', 'ci-dns')
NETWORK_ID = 'feedc0de00000001'


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


class Container(pulumi.ComponentResource):
    def __init__(self, name, opts):
        super().__init__('kluster:gateway:Container', name, None, opts)
        DeviceFile(f'{name}-nspawn', {'path': f'machines/{name}.nspawn'}, pulumi.ResourceOptions(parent=self))
        self.register_outputs({})


class Gateway(pulumi.ComponentResource):
    def __init__(self, name):
        super().__init__('kluster:gateway:Gateway', name, None, None)
        DeviceFile(f'{name}-udm-boot', {'path': 'on_boot.d/udm-boot'}, pulumi.ResourceOptions(parent=self))
        Container(f'{name}-caddy', pulumi.ResourceOptions(parent=self))
        self.register_outputs({})


class Identity(pulumi.CustomResource):
    identity_id: pulumi.Output[str]
    private_key: pulumi.Output[str]

    def __init__(self, name, opts):
        super().__init__('zerotier:index/identity:Identity', name, {'identity_id': None, 'private_key': None}, opts)


class Network(pulumi.CustomResource):
    network_id: pulumi.Output[str]

    def __init__(self, name, flow_rules, opts):
        props = {'name': 'kluster', 'flow_rules': flow_rules, 'network_id': None}
        super().__init__('zerotier:index/network:Network', name, props, opts)


class Member(pulumi.CustomResource):
    def __init__(self, name, network_id, member_id, opts):
        props = {'network_id': network_id, 'member_id': member_id}
        super().__init__('zerotier:index/member:Member', name, props, opts)


class Overlay(pulumi.ComponentResource):
    def __init__(self, name):
        provider = pulumi.ProviderResource('zerotier', f'{name}-zerotier', {})
        super().__init__('kluster:overlay:Overlay', name, None, pulumi.ResourceOptions(providers=[provider]))
        child = pulumi.ResourceOptions(parent=self)
        self.identities = {member: Identity(f'{name}-identity-{member}', child) for member in ROSTER}
        rules = pulumi.Output.all(*(identity.identity_id for identity in self.identities.values())).apply(
            lambda ids: ' '.join(f'drop not chr ztsrc {node};' for node in ids)
        )
        self.network = Network(
            f'{name}-network',
            rules,
            pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(import_=NETWORK_ID, protect=True)),
        )
        for member, identity in self.identities.items():
            Member(f'{name}-member-{member}', self.network.network_id, identity.identity_id, child)
        self.register_outputs({})


Gateway('kluster')
overlay = Overlay('kluster')
for member, identity in overlay.identities.items():
    pulumi.export(f'ci_identity_{member}', pulumi.Output.secret(identity.private_key))
"""

#: The types whose create, update or delete would be a write to Central the
#: window does not make: the network, which only step 5 converges, and a
#: member, which is an admission.
UNWRITTEN = ('zerotier:index/network:Network', 'zerotier:index/member:Member')


def step_3_targets() -> list[str]:
    """The `-t` arguments of the runbook's one targeted `physical up`, the window's step 3."""
    blocks = [
        block
        for block in (found.lstrip() for found in re.findall(r'```sh\n(.*?)```', RUNBOOK.read_text(), re.DOTALL))
        if block.startswith('physical up') and ' -t ' in block
    ]
    assert len(blocks) == 1, blocks
    return re.findall(r"-t '([^']*)'", blocks[0])


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

    def log(self) -> list[str]:
        return self.log_file.read_text().splitlines() if self.log_file.exists() else []


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest.fixture
def fresh(tmp_path: Path) -> Engine:
    """The program's stack, initialized and never applied, as `physical` is before the window."""
    log_file = tmp_path / 'engine.log'
    scratch = scratch_project(tmp_path, PROGRAM, name=PROJECT, providers={'zerotier': PROVIDER})
    engine = Engine(project=scratch.directory, env=scratch.env | {'PROBE_LOG': str(log_file)}, log_file=log_file)
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = engine.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    return engine


def test_the_windows_targeted_up_leaves_a_stack_the_next_command_loads_and_writes_no_network_or_member(
    fresh: Engine,
) -> None:
    """Step 3's `up`, with the runbook's targets, over a stack with no state.

    The network is outside the targets and is imported all the same, and a
    target set that misses its parent or an identity its flow rules are
    rendered over fails this `up` after the import has run: the checkpoint is
    written with the network beside a parent or a dependency the run never
    created, and every later command refuses to load it. So the `up` has to pass, nothing may
    have created, updated or deleted the network or a member, and a preview
    with the same targets afterwards has to load the checkpoint and pass.
    """
    targets = [argument for target in step_3_targets() for argument in ('--target', target)]

    applied = fresh.pulumi('up', '--yes', '--skip-preview', *targets)

    assert applied.returncode == 0, _ran(applied)
    written = [line for line in fresh.log() if line.split(' ')[0] in {'create', 'update', 'delete'}]
    assert not [line for line in written if line.split(' ')[1] in UNWRITTEN], written

    again = fresh.pulumi('preview', *targets)

    assert again.returncode == 0, _ran(again)
