"""The engine semantics a lost cloud node's recovery and the instances' protection rest on, held by the pinned `pulumi`.

Each node's instance is protected (`components/cloud/nodes.py`,
declarative/physical.md §1): the program neither replaces nor deletes it, and
an instance leaves the state only through a refresh that reads it gone. What
that rests on is three things the engine does, and no mock shows:

-   a replacement of a protected instance is refused before anything is
    created, whether the provider plans it or `--target-replace` asks for it;
-   the lost node's targeted `refresh --run-program`, at the count
    operations.md §3.1 gates it on, drops the instance that reads back
    without an ID, protected as it is, and on the dedicated VIP's node the
    secondary private IP and the attachment with it, and keeps the reserved
    IP and every backend;
-   the `up` that follows writes that node and nothing else: the instance's
    create, a replacement of each of the node's backends, and on the
    dedicated VIP's node the private IP's and the attachment's creates and
    one update of the reserved IP.

The cases run the CLI `mise.toml` pins over a `file://` backend and a
`PULUMI_HOME` of their own (`scratch_projects`), against a program of
stand-ins that registers under the physical stack's own URNs -- project
`kluster-py`, stack `physical` -- in the fleet's dependency shape: a balancer
with a backend set per forward; `CloudNodes`, one instance per node, a backend
per node and per forward naming the instance's id, and on `cp1` the secondary
private IP on its VNIC and the reserved IP naming that; and a `NodeVolume`
whose attachment names `cp1`'s instance. Every stand-in declares the options
the real fleet registers under its type and name (`fleet_registrations`), so
none is restated here.

The stand-in provider is a plugin of the case's own: every create hands out a
fresh id, as a cloud does, so a re-created instance moves what names it, and
every create, update and delete appends a line to a log. A replacement is
planned for a changed placement of an instance or volume, a changed
`target_id` of a backend and a changed `instance_id` of an attachment, inputs
the bridged provider does not update in place; every other change is an
update. Its read is the cloud after a termination: what the case names in
`PROBE_GONE` reads back without an ID, and a reserved IP whose private IP is
among them reads back assigned to nothing. Backends read back as they are,
because OCI keeps the backends of a terminated instance.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from fleet_registrations import fleet_options
from scratch_projects import scratch_project

from kluster import conventions

#: How long one case may take: its project's set-up and every command it runs.
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

#: The runbook whose lost-node block the recovery runs.
RUNBOOK = Path(__file__).parent.parent / 'docs' / 'operations.md'

PROJECT = 'kluster-py'
STACK = 'physical'

PROVIDER = """\
import json
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
#: What the cloud no longer holds, by id.
GONE = set(json.loads(os.environ.get('PROBE_GONE', '[]')))
#: The inputs whose change the cloud cannot apply in place, by type.
REPLACING = {
    'oci:Core/instance:Instance': {'availability_domain'},
    'oci:Core/volume:Volume': {'availability_domain'},
    'oci:Core/volumeAttachment:VolumeAttachment': {'instance_id'},
    'oci:NetworkLoadBalancer/backend:Backend': {'target_id'},
}


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
        replaces = [key for key in changed if key in REPLACING.get(kind(request.urn), set())]
        return DiffResponse(changes=bool(changed), diffs=changed, replaces=replaces)

    async def create(self, request):
        log(f'create {kind(request.urn)} {name(request.urn)}')
        return CreateResponse(resource_id=f'{name(request.urn)}-{uuid.uuid4().hex[:8]}', properties=dict(request.properties))

    async def read(self, request):
        if request.resource_id in GONE:
            return ReadResponse(resource_id=None)
        props = dict(request.properties)
        inputs = dict(request.inputs)
        assigned = props.get('private_ip_id')
        if assigned is not None and assigned.value in GONE:
            props['private_ip_id'] = inputs['private_ip_id'] = PropertyValue('')
        return ReadResponse(resource_id=request.resource_id, properties=props, inputs=inputs)

    async def update(self, request):
        log(f'update {kind(request.urn)} {name(request.urn)}')
        return UpdateResponse(properties=dict(request.news))

    async def delete(self, request):
        log(f'delete {kind(request.urn)} {name(request.urn)}')


main(sys.argv[1:], '0.0.1', StandIn())
"""

PROGRAM = """\
import json
import os

import pulumi

#: Each stand-in's options, as the real fleet registers them under its type
#: and name (`fleet_registrations`).
OPTIONS = json.loads(os.environ['PROBE_OPTIONS'])
NODES = ('cp1', 'cp2', 'cp3')
VIP_NODE = 'cp1'
VOLUMES = {'hath-cache': 'cp1'}
#: Two of the ports the balancer forwards, each on both families, as
#: `components/cloud/nodes.py` names a backend set: the field alone for IPv4.
FORWARDS = ('kubernetes', 'talos', 'kubernetes-ipv6', 'talos-ipv6')
#: Where each node is placed: one AD apiece, unless the case moves one.
PLACEMENTS = {'cp1': 'AD-1', 'cp2': 'AD-2', 'cp3': 'AD-3'} | json.loads(os.environ.get('PROBE_PLACEMENTS', '{}'))


def declared(cls, typ, name, props, parent):
    stated = pulumi.ResourceOptions(parent=parent, **OPTIONS[f'{typ}::{name}'])
    return cls(typ, name, props, stated)


class Placed(pulumi.CustomResource):
    availability_domain: pulumi.Output[str]


class Instance(Placed):
    pass


class NodeLoadBalancer(pulumi.ComponentResource):
    def __init__(self, name, provider):
        super().__init__('kluster:cloud:NodeLoadBalancer', name, None, pulumi.ResourceOptions(providers=[provider]))
        typ = 'oci:NetworkLoadBalancer/backendSet:BackendSet'
        self.backend_sets = {
            forward: declared(pulumi.CustomResource, typ, f'{name}-nlb-{forward}', {'name': forward}, self)
            for forward in FORWARDS
        }
        self.register_outputs({})


class CloudNodes(pulumi.ComponentResource):
    def __init__(self, name, provider, balancer):
        super().__init__('kluster:cloud:CloudNodes', name, None, pulumi.ResourceOptions(providers=[provider]))
        self.instances = {
            node: declared(
                Instance,
                'oci:Core/instance:Instance',
                f'{name}-{node}',
                {'display_name': f'{name}-{node}', 'availability_domain': PLACEMENTS[node]},
                self,
            )
            for node in NODES
        }
        private = declared(
            pulumi.CustomResource,
            'oci:Core/privateIp:PrivateIp',
            f'{name}-vip1-private',
            {'vnic_id': self.instances[VIP_NODE].id},
            self,
        )
        declared(pulumi.CustomResource, 'oci:Core/publicIp:PublicIp', f'{name}-vip1', {'private_ip_id': private.id}, self)
        for forward in FORWARDS:
            for node, instance in self.instances.items():
                declared(
                    pulumi.CustomResource,
                    'oci:NetworkLoadBalancer/backend:Backend',
                    f'{name}-nlb-{forward}-{node}',
                    {'backend_set_name': balancer.backend_sets[forward].id, 'target_id': instance.id},
                    self,
                )
        self.register_outputs({})


class NodeVolume(pulumi.ComponentResource):
    def __init__(self, name, provider, instance):
        super().__init__('kluster:cloud:NodeVolume', name, None, pulumi.ResourceOptions(providers=[provider]))
        volume = declared(
            Placed,
            'oci:Core/volume:Volume',
            f'{name}-volume',
            {'availability_domain': instance.availability_domain},
            self,
        )
        declared(
            pulumi.CustomResource,
            'oci:Core/volumeAttachment:VolumeAttachment',
            f'{name}-attachment',
            {'instance_id': instance.id, 'volume_id': volume.id},
            self,
        )
        self.register_outputs({})


oci = pulumi.ProviderResource('oci', 'kluster-oci', {})
balancer = NodeLoadBalancer('kluster', oci)
nodes = CloudNodes('kluster', oci, balancer)
for volume, node in VOLUMES.items():
    NodeVolume(f'kluster-{volume}', oci, nodes.instances[node])
"""

INSTANCE = 'oci:Core/instance:Instance'
BACKEND = 'oci:NetworkLoadBalancer/backend:Backend'
PRIVATE_IP = 'oci:Core/privateIp:PrivateIp'
RESERVED_IP = 'oci:Core/publicIp:PublicIp'
VOLUME = 'oci:Core/volume:Volume'
ATTACHMENT = 'oci:Core/volumeAttachment:VolumeAttachment'

#: The nodes and the forwards the stand-in program declares, as its own
#: `NODES` and `FORWARDS` do: a backend per node and per forward.
NODES = ('cp1', 'cp2', 'cp3')
FORWARDS = ('kubernetes', 'talos', 'kubernetes-ipv6', 'talos-ipv6')

#: The `physical` of the runbook, for the scratch stack: every command names
#: the stack, and an `up` or a `refresh` is confirmed, as the operator confirms
#: it once its preview has been read.
PHYSICAL = """\
set -e
physical() {
    if [ "$1" = up ] || [ "$1" = refresh ]; then
        command=$1
        shift
        pulumi --non-interactive "$command" --yes --skip-preview "$@" --stack physical
    else
        pulumi --non-interactive "$@" --stack physical
    fi
}
"""


def continued(line: str) -> bool:
    """Whether a line of the block continues onto the next: it ends in an unescaped backslash, outside a comment.

    A run of trailing backslashes continues the line when it is odd, an
    even run being escaped backslashes. A comment starts at a `#` that
    opens a word outside quotes and runs to the end of the line, so a
    backslash at its end is the comment's.

    Two shapes bash reads otherwise are outside the rule: a `$'…'` is
    read as a plain single quote, so a `\\'` inside it ends the quote, and
    a `#` after an escaped blank (`x\\ #y`) is read as opening a comment.
    """
    quote = ''
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == '\\' and quote != "'":
            escaped = True
        elif quote:
            quote = '' if char == quote else quote
        elif char in '\'"':
            quote = char
        elif char == '#' and (index == 0 or line[index - 1] in ' \t;&|()<>'):
            return False
    return (len(line) - len(line.rstrip('\\'))) % 2 == 1


def lost_node_block(runbook: str) -> list[str]:
    """The lost-node block of operations.md §3.1, a command apiece, from its URN reads on.

    The block is the code block, indented 8 spaces, that runs the gated
    refresh, taken whole as CommonMark reads an indented code block: a
    line indented deeper continues it, and so does a line indented 8
    spaces or more after blank lines. A line ending in an unescaped
    backslash, outside a comment, joins the next into one element, so a
    split between commands never falls inside one.

    The lines ahead of those act on the real cloud and the cluster, the
    instance's read and the Node object's delete, and gate nothing the
    engine runs.
    """
    blocks = [
        textblock
        for textblock in re.findall(r'(?<=\n\n)( {8}\S.*\n(?:(?:[ \t]*\n)* {8}.*\S.*\n)*)', runbook)
        if 'refresh --run-program' in textblock and 'urns()' in textblock
    ]
    assert len(blocks) == 1, blocks
    commands: list[str] = []
    for line in (line[8:] for line in blocks[0].splitlines()):
        if commands and continued(commands[-1].rsplit('\n', 1)[-1]):
            commands[-1] += '\n' + line
        else:
            commands.append(line)
    start = next(index for index, command in enumerate(commands) if command.startswith('urns()'))
    return commands[start:]


def on_the_vip_node(lines: list[str], runbook: str) -> list[str]:
    """The block as §3.1's prose extends it on the dedicated VIP's node: a pattern that gains more, and a count."""
    text = ' '.join(runbook.split())
    gained = re.search(r'the pattern gains `([^`]+)` and the count is `([^`]+)`', text)
    assert gained is not None, 'operations.md §3.1 no longer says what the pattern gains on the dedicated VIP node'
    addition, count = gained.groups()
    (targets,) = [index for index, line in enumerate(lines) if line.startswith('T=(')]
    (gate,) = [index for index, line in enumerate(lines) if 'refresh --run-program' in line]
    extended = list(lines)
    assert extended[targets].endswith(")$'))"), extended[targets]
    extended[targets] = extended[targets].removesuffix("'))") + addition + "'))"
    assert '$((1 + S))' in extended[gate], extended[gate]
    extended[gate] = extended[gate].replace('$((1 + S))', count)
    return extended


def recovery(node: str, runbook: str) -> tuple[str, str]:
    """The block for `node`, split after the refresh: the gated refresh, then the preview and the `up`."""
    lines = lost_node_block(runbook)
    if node == conventions.DEDICATED_VIP_NODE:
        lines = on_the_vip_node(lines, runbook)
    (gate,) = [index for index, line in enumerate(lines) if 'refresh --run-program' in line]
    script = [line.replace('<node>', node) for line in lines]
    return '\n'.join(script[: gate + 1]) + '\n', '\n'.join(script[gate + 1 :]) + '\n'


#: A runbook whose lost-node block takes a shape an edit of operations.md §3.1
#: may give it -- a command continued onto lines indented deeper than the
#: block, a blank line inside the block, a line ending in a backslash that
#: does not continue it -- and what each half of the block runs: the gated
#: refresh, then what follows it.
CONTINUED = [
    pytest.param(
        """\
A paragraph ahead of the block:

        urns() { :; }
        physical refresh --run-program \\
            --target=a \\
            --target=b
        physical up

A paragraph after it.
""",
        (['refresh --run-program --target=a --target=b'], ['up']),
        id='the gated refresh',
    ),
    pytest.param(
        """\
A paragraph ahead of the block:

        urns() { :; }
        physical refresh --run-program --target=a
        physical preview --diff
        physical up \\
            --continued \\
            words

A paragraph after it.
""",
        (['refresh --run-program --target=a'], ['preview --diff', 'up --continued words']),
        id='the last command',
    ),
    pytest.param(
        """\
A paragraph ahead of the block:

        urns() { :; }
        physical refresh --run-program --target=a
        physical up

        physical preview --diff

A paragraph after it.
""",
        (['refresh --run-program --target=a'], ['up', 'preview --diff']),
        id='a command after a blank line',
    ),
    pytest.param(
        """\
A paragraph ahead of the block:

        urns() { :; }
        physical refresh --run-program --target=a  # the targets \\
        physical up

A paragraph after it.
""",
        (['refresh --run-program --target=a'], ['up']),
        id='a comment ending in a backslash',
    ),
    pytest.param(
        """\
A paragraph ahead of the block:

        urns() { :; }
        physical refresh --run-program --target=a\\\\
        physical up

A paragraph after it.
""",
        (['refresh --run-program --target=a\\'], ['up']),
        id='an escaped backslash',
    ),
    pytest.param(
        """\
A paragraph ahead of the block:

        urns() { :; }
        physical refresh --run-program "--note=a \\" #b" \\
            --target=b
        physical up

A paragraph after it.
""",
        (['refresh --run-program --note=a " #b --target=b'], ['up']),
        id='a hash inside quotes, after an escaped quote',
    ),
]


@pytest.mark.parametrize(('runbook', 'expected'), CONTINUED)
def test_a_continued_command_reaches_the_shell_whole_on_its_side_of_the_refresh(
    runbook: str, expected: tuple[list[str], list[str]]
) -> None:
    """Each half of the block, run by bash under a `physical` that prints its arguments, runs every command whole."""

    def ran(script: str) -> list[str]:
        result = process_sessions.run(
            ['bash', '-c', 'set -e\nphysical() { echo "$*"; }\n' + script], text=True, timeout=COMMAND_TIMEOUT
        )
        assert result.returncode == 0, _ran(result)
        return result.stdout.splitlines()

    gate, rest = recovery('cp2', runbook)

    assert (ran(gate), ran(rest)) == expected


@dataclass
class Engine:
    """One scratch project and its backend, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]
    log_file: Path

    def pulumi(self, *args: str, **env: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args],
            cwd=self.project,
            env=self.env | env,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )

    def shell(self, script: str, **env: str) -> sp.CompletedProcess[str]:
        return process_sessions.run(
            ['bash', '-c', PHYSICAL + script], cwd=self.project, env=self.env | env, text=True, timeout=COMMAND_TIMEOUT
        )

    def log(self) -> list[str]:
        return self.log_file.read_text().splitlines() if self.log_file.exists() else []

    def written(self, since: int) -> list[tuple[str, str, str]]:
        """Every create, update and delete since the log's `since`th line, as (verb, type, name)."""
        lines = [line.split(' ') for line in self.log()[since:]]
        return [(verb, typ, name) for verb, typ, name in lines if verb in {'create', 'update', 'delete'}]

    def resources(self) -> list[dict[str, Any]]:
        exported = self.pulumi('stack', 'export')
        assert exported.returncode == 0, exported.stderr
        return json.loads(exported.stdout)['deployment']['resources']

    def of(self, typ: str, name: str) -> dict[str, Any]:
        (found,) = [it for it in self.resources() if it['type'] == typ and it['urn'].endswith(f'::{name}')]
        return found


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


@pytest_asyncio.fixture
async def options() -> str:
    """The fleet's registered options, as the program reads them."""
    return json.dumps(await fleet_options())


@pytest.fixture
def launched(tmp_path: Path, options: str) -> Engine:
    """The fleet, applied whole."""
    log_file = tmp_path / 'engine.log'
    scratch = scratch_project(tmp_path, PROGRAM, name=PROJECT, providers={'oci': PROVIDER})
    env = scratch.env | {'PROBE_LOG': str(log_file), 'PROBE_OPTIONS': options}
    engine = Engine(project=scratch.directory, env=env, log_file=log_file)
    initialized = engine.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    applied = engine.pulumi('up', '--yes', '--skip-preview')
    assert applied.returncode == 0, _ran(applied)
    return engine


def test_a_targeted_replacement_of_an_instance_is_refused_with_nothing_created(launched: Engine) -> None:
    before = len(launched.log())
    original = launched.of(INSTANCE, 'kluster-cp2')

    replaced = launched.pulumi('up', '--yes', '--skip-preview', '--target-replace', original['urn'])

    assert replaced.returncode != 0, _ran(replaced)
    assert launched.written(before) == []
    assert launched.of(INSTANCE, 'kluster-cp2')['id'] == original['id']


def test_a_replacement_the_provider_plans_is_refused_with_nothing_created(launched: Engine) -> None:
    before = len(launched.log())
    original = launched.of(INSTANCE, 'kluster-cp2')

    moved = launched.pulumi('up', '--yes', '--skip-preview', PROBE_PLACEMENTS=json.dumps({'cp2': 'AD-9'}))

    assert moved.returncode != 0, _ran(moved)
    assert launched.written(before) == []
    assert launched.of(INSTANCE, 'kluster-cp2')['id'] == original['id']


def lost(engine: Engine, node: str) -> list[str]:
    """What the cloud drops with `node`'s instance: the instance, and what lives on it or names it as attached.

    Its secondary private IPs live on its VNIC, and an attachment ends with
    the instance it attaches to. A backend that names it stays.
    """
    instance = engine.of(INSTANCE, f'kluster-{node}')['id']
    on_it = [
        resource['id']
        for resource in engine.resources()
        if instance in (resource.get('inputs', {}).get('vnic_id'), resource.get('inputs', {}).get('instance_id'))
    ]
    return [instance, *on_it]


@pytest.fixture(params=['cp1', 'cp2'])
def refreshed(launched: Engine, request: pytest.FixtureRequest) -> tuple[Engine, str]:
    """The fleet after `node` was terminated and the runbook's gated refresh ran: `node` is the dedicated VIP's or not."""
    node: str = request.param
    gone = lost(launched, node)
    launched.env['PROBE_GONE'] = json.dumps(gone)
    gate, _ = recovery(node, RUNBOOK.read_text())

    ran = launched.shell(gate)

    assert ran.returncode == 0, _ran(ran)
    return launched, node


def test_the_gated_refresh_drops_what_the_cloud_lost_and_keeps_the_reserved_ip_and_the_backends(
    refreshed: tuple[Engine, str],
) -> None:
    engine, node = refreshed
    gone = json.loads(engine.env['PROBE_GONE'])
    resources = engine.resources()
    held = {resource.get('id') for resource in resources}
    names = {(resource['type'], resource['urn'].rsplit('::', 1)[1]) for resource in resources}

    assert not held & set(gone)
    assert (INSTANCE, f'kluster-{node}') not in names
    if node == conventions.DEDICATED_VIP_NODE:
        assert not {(PRIVATE_IP, 'kluster-vip1-private'), (ATTACHMENT, 'kluster-hath-cache-attachment')} & names
    assert {(BACKEND, f'kluster-nlb-{forward}-{each}') for forward in FORWARDS for each in NODES} <= names
    assert {(RESERVED_IP, 'kluster-vip1'), (VOLUME, 'kluster-hath-cache-volume')} <= names
    assert [resource['urn'] for resource in resources if resource.get('delete')] == []


def test_the_up_after_it_writes_the_lost_node_and_nothing_else(refreshed: tuple[Engine, str]) -> None:
    engine, node = refreshed
    _, rest = recovery(node, RUNBOOK.read_text())
    before = len(engine.log())

    recovered = engine.shell(rest)

    assert recovered.returncode == 0, _ran(recovered)
    backends = {
        (verb, BACKEND, f'kluster-nlb-{forward}-{node}') for verb in ('create', 'delete') for forward in FORWARDS
    }
    expected = {('create', INSTANCE, f'kluster-{node}'), *backends}
    if node == conventions.DEDICATED_VIP_NODE:
        expected |= {
            ('create', PRIVATE_IP, 'kluster-vip1-private'),
            ('create', ATTACHMENT, 'kluster-hath-cache-attachment'),
            ('update', RESERVED_IP, 'kluster-vip1'),
        }
    written = engine.written(before)
    assert set(written) == expected, written
    assert len(written) == len(expected), written
