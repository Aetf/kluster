# Pulumi's mock monitor and its gRPC message types carry no type information,
# and this file reaches inside them to read the dependency edges Pulumi
# records. The unknown-type family is suppressed here rather than repo-wide.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnnecessaryIsInstance=false
"""The day-1 chain: apply, bootstrap, credentials, and the health gate.

Day 1 is the part of the Talos design that cannot be re-run casually — a
second bootstrap starts a second etcd cluster, a simultaneous reboot takes the
quorum, and a kubeconfig handed out before the cluster is healthy sends the
next stack at an API that is not there yet. None of those show up in a diff,
so they are asserted here against the resource graph.

The other half of the file is the seam between the two days: what a machine
boots with, and what is only applied to it afterwards.
"""

from __future__ import annotations

import asyncio
import json
from ipaddress import IPv4Interface
from typing import Any, cast

import pulumi
import pulumi.runtime.mocks
import pytest
import pytest_asyncio
from pulumi.runtime.proto import resource_pb2

# The mock monitor drops propertyDependencies, so dependency assertions would
# always come back empty (framework/testing.md §3.1). It also keeps no record
# of `depends_on`, which is not reachable from any output — and `depends_on`
# is exactly what serializes the apply chain. Both are recovered here, before
# any Pulumi code runs.
DEPENDS_ON: dict[str, list[str]] = {}

_original_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource


def _patched_register_resource(self: Any, request: Any) -> Any:
    response = _original_register_resource(self, request)
    DEPENDS_ON[request.name] = list(request.dependencies)
    if isinstance(response, resource_pb2.RegisterResourceResponse):
        for key, value in request.propertyDependencies.items():
            response.propertyDependencies[key].urns.extend(value.urns)
    return response


pulumi.runtime.mocks.MockMonitor.RegisterResource = _patched_register_resource

ENDPOINT = 'https://203.0.113.10:6443'
SECRETBOX = 'c2VjcmV0Ym94LWtleS1tYXRlcmlhbC0zMi1ieXRlcw=='
KUBECONFIG = 'apiVersion: v1\nkind: Config\n'
TALOSCONFIG = 'context: kluster\n'
ADDRESSES = {'cp1': '10.20.0.11', 'cp2': '10.20.0.12', 'cp3': '10.20.0.13', 'homelab': '192.168.70.51'}
SECONDARY = '10.20.0.42'


class Fake(pulumi.runtime.Mocks):
    """A Talos provider that answers like the real one, minus a cluster.

    It carries the two behaviours the chain depends on: a secrets bundle that
    contains a generated secretbox key, and a health check that reports what
    it was asked about.
    """

    def __init__(self, *, secretbox: str | None = SECRETBOX) -> None:
        self.secretbox: str | None = secretbox
        #: Every invoke the program made, so a test can read what the health
        #: check and the client configuration were asked for.
        self.calls: dict[str, dict[str, Any]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'talos:machine/secrets:Secrets':
            secrets: dict[str, Any] = {'bootstrapToken': 'bootstrap.token'}
            if self.secretbox is not None:
                secrets['secretboxEncryptionSecret'] = self.secretbox
            outputs['machineSecrets'] = {
                'certs': {},
                'cluster': {'id': 'kluster', 'secret': 'cluster.secret'},
                'secrets': secrets,
                'trustdinfo': {'token': 'trustd.token'},
            }
            outputs['clientConfiguration'] = {
                'caCertificate': 'ca',
                'clientCertificate': 'crt',
                'clientKey': 'key',
            }
        if args.typ == 'talos:cluster/kubeconfig:Kubeconfig':
            outputs['kubeconfigRaw'] = KUBECONFIG
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        arguments = dict(cast('dict[str, Any]', args.args))
        self.calls[args.token] = arguments
        match args.token:
            case 'talos:machine/getConfiguration:getConfiguration':
                # The data source keeps its inputs, patches included.
                return {'machineConfiguration': 'machine: {}', 'configPatches': arguments.get('configPatches')}, []
            case 'talos:client/getConfiguration:getConfiguration':
                return {'talosConfig': TALOSCONFIG}, []
            case 'talos:cluster/getHealth:getHealth':
                return {'id': 'healthy'}, []
            case _:
                return {}, []


@pytest_asyncio.fixture
async def fake() -> Fake:
    DEPENDS_ON.clear()
    mocks = Fake()
    pulumi.runtime.set_mocks(mocks, project='kluster', stack='physical', preview=False)
    return mocks


def build_cluster(**kwargs: Any) -> Any:
    """Day 0: the PKI and the configuration each machine boots with."""
    from kluster.physical.talos import TalosCluster

    kwargs.setdefault('control_plane_nodes', ('cp1', 'cp2', 'cp3'))
    kwargs.setdefault('worker_nodes', ('homelab',))
    kwargs.setdefault('bgp_peers', {'homelab': '192.168.70.1/32'})
    return TalosCluster(
        'kluster',
        cluster_name='kluster',
        endpoint=ENDPOINT,
        cert_sans=['203.0.113.10'],
        talos_version='v1.11.0',
        **kwargs,
    )


def build(**kwargs: Any) -> Any:
    """Day 1, on top of a day 0 the caller may hand in to inspect."""
    from kluster.physical.talos import TalosDay1

    kwargs.setdefault('cluster', build_cluster())
    kwargs.setdefault('addresses', ADDRESSES)
    kwargs.setdefault('secondary_addresses', {'cp1': SECONDARY})
    return TalosDay1('kluster', **kwargs)


async def urns_of(output: pulumi.Output[Any]) -> set[str]:
    return {await resource.urn.future() or '' for resource in await output.resources()}


@pytest.mark.asyncio
async def test_every_node_gets_the_role_it_was_declared_with(fake: Fake) -> None:
    cluster = build_cluster()
    assert cluster.roles == {
        'cp1': 'controlplane',
        'cp2': 'controlplane',
        'cp3': 'controlplane',
        'homelab': 'worker',
    }
    assert set(cluster.machine_configs) == set(ADDRESSES)


@pytest.mark.asyncio
async def test_a_node_cannot_be_both_roles(fake: Fake) -> None:
    with pytest.raises(ValueError, match='both a control plane and a worker'):
        build_cluster(worker_nodes=('cp1',))


@pytest.mark.asyncio
async def test_day_one_needs_an_address_for_every_node(fake: Fake) -> None:
    # A cluster is not healthy because the nodes somebody listed are.
    with pytest.raises(ValueError, match=r'an address for every node.*homelab'):
        build(addresses={'cp1': '10.20.0.11', 'cp2': '10.20.0.12', 'cp3': '10.20.0.13'})


@pytest.mark.asyncio
async def test_day_zero_stands_on_its_own(fake: Fake) -> None:
    # Day 0 delivers the configuration out of band (instance metadata, a seed
    # ISO); day 1 needs machines that answer, which the stack only has later.
    cluster = build_cluster()
    await asyncio.gather(*(config.future() for config in cluster.machine_configs.values()))
    assert 'kluster-bootstrap' not in DEPENDS_ON
    assert not [name for name in DEPENDS_ON if name.endswith('-config')]


@pytest.mark.asyncio
async def test_configuration_is_applied_over_apid_without_rebooting_the_quorum(fake: Fake) -> None:
    day1 = build()
    for node, applied in day1.applies.items():
        assert await applied.node.future() == ADDRESSES[node]
        # A reboot-needing change is staged, so the reboot is an operator's
        # decision rather than a side effect of an apply.
        assert await applied.apply_mode.future() == 'staged_if_needing_reboot'


@pytest.mark.asyncio
async def test_destroying_an_apply_never_wipes_the_disk(fake: Fake) -> None:
    # `reset` wipes STATE and EPHEMERAL — every partition (provider issue
    # #205). Node replacement is an explicit procedure, never a side effect.
    day1 = build()
    for applied in day1.applies.values():
        on_destroy = await applied.on_destroy.future()
        assert on_destroy is not None
        assert on_destroy.reset is False


@pytest.mark.asyncio
async def test_nodes_are_applied_one_after_another(fake: Fake) -> None:
    # A configuration change that reboots the machine is applied to one node
    # at a time, so the quorum never goes down together.
    day1 = build()
    order = list(day1.applies)
    # Registration is asynchronous; the graph is only complete once it is.
    await asyncio.gather(*(applied.urn.future() for applied in day1.applies.values()))
    for earlier, later in zip(order, order[1:]):
        waits_for = DEPENDS_ON[f'kluster-{later}-config']
        assert await day1.applies[earlier].urn.future() in waits_for, f'{later} does not wait for {earlier}'


@pytest.mark.asyncio
async def test_only_the_first_control_plane_bootstraps(fake: Fake) -> None:
    # A second bootstrap does not join etcd, it starts a second etcd cluster.
    day1 = build()
    assert await day1.bootstrap.node.future() == ADDRESSES['cp1']
    assert await day1.applies['cp1'].urn.future() in DEPENDS_ON['kluster-bootstrap']


@pytest.mark.asyncio
async def test_the_kubeconfig_comes_from_the_bootstrapped_node(fake: Fake) -> None:
    day1 = build()
    assert await day1.kubeconfig_source.node.future() == ADDRESSES['cp1']
    assert await day1.bootstrap.urn.future() in DEPENDS_ON['kluster-kubeconfig']


@pytest.mark.asyncio
async def test_the_health_check_knows_which_node_is_which(fake: Fake) -> None:
    day1 = build()
    _ = await day1.health.future()
    health = fake.calls['talos:cluster/getHealth:getHealth']
    assert health['controlPlaneNodes'] == [ADDRESSES[node] for node in ('cp1', 'cp2', 'cp3')]
    assert health['workerNodes'] == [ADDRESSES['homelab']]
    # The health client talks to a control plane, not to the worker.
    assert health['endpoints'] == [ADDRESSES[node] for node in ('cp1', 'cp2', 'cp3')]


@pytest.mark.asyncio
async def test_the_kubeconfig_is_gated_on_the_health_check(fake: Fake) -> None:
    day1 = build()
    assert await day1.kubeconfig.future() == KUBECONFIG
    # The gate is the dependency: whatever consumes the kubeconfig is ordered
    # behind a cluster that reported healthy, not merely behind a resource
    # that finished registering.
    assert await urns_of(day1.health) <= await urns_of(day1.kubeconfig)
    assert await day1.bootstrap.urn.future() in await urns_of(day1.kubeconfig)


@pytest.mark.asyncio
async def test_the_credentials_are_secret(fake: Fake) -> None:
    # Both are cluster-admin: a stack export must not print them.
    day1 = build()
    assert await day1.kubeconfig.is_secret()
    assert await day1.talosconfig.is_secret()


@pytest.mark.asyncio
async def test_the_talosconfig_names_every_node_and_the_control_plane_endpoints(fake: Fake) -> None:
    day1 = build()
    assert await day1.talosconfig.future() == TALOSCONFIG
    configuration = fake.calls['talos:client/getConfiguration:getConfiguration']
    assert configuration['nodes'] == [ADDRESSES[node] for node in ('cp1', 'cp2', 'cp3', 'homelab')]
    assert configuration['endpoints'] == [ADDRESSES[node] for node in ('cp1', 'cp2', 'cp3')]


async def patches_of(component: Any, node: str) -> list[dict[str, Any]]:
    """The patches one node's machine configuration was generated from."""
    configuration = await cast('pulumi.Output[Any]', component.configurations[node]).future()
    assert configuration is not None
    patches = cast('list[str]', configuration.config_patches or [])
    return [cast('dict[str, Any]', json.loads(patch)) for patch in patches]


@pytest.mark.asyncio
async def test_the_generated_secretbox_key_reaches_the_control_planes(fake: Fake) -> None:
    cluster = build_cluster()
    for node in ('cp1', 'cp2', 'cp3'):
        sections = [patch['cluster'] for patch in await patches_of(cluster, node) if 'cluster' in patch]
        assert SECRETBOX in [section.get('secretboxEncryptionSecret') for section in sections]


@pytest.mark.asyncio
async def test_a_bundle_without_a_secretbox_key_is_an_error(fake: Fake, caplog: pytest.LogCaptureFixture) -> None:
    # Talos generates one for every new cluster. A bundle that arrives without
    # one would bring the cluster up with its secrets readable in every etcd
    # snapshot, so an error diagnostic is raised — which fails the deployment
    # — rather than inventing key material or saying nothing.
    fake.secretbox = None
    cluster = build_cluster()
    for patch in await patches_of(cluster, 'cp1'):
        assert 'secretboxEncryptionSecret' not in patch.get('cluster', {})
    assert 'secretbox' in caplog.text
    assert 'ERROR' in caplog.text


def interfaces(patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        interface
        for patch in patches
        for interface in patch.get('machine', {}).get('network', {}).get('interfaces', [])
    ]


@pytest.mark.asyncio
async def test_the_second_address_is_applied_and_never_booted_with(fake: Fake) -> None:
    # The address is assigned by OCI to the VNIC of an instance whose user_data
    # *is* the booted configuration. Naming it there would make the instance
    # wait on an address that waits on the instance, so it arrives on day 1.
    cluster = build_cluster()
    day1 = build(cluster=cluster)
    assert interfaces(await patches_of(day1, 'cp1'))[0]['addresses'] == [f'{SECONDARY}/32']
    assert not interfaces(await patches_of(cluster, 'cp1'))


@pytest.mark.asyncio
async def test_the_worker_boots_with_the_address_the_gateway_was_told_about(fake: Fake) -> None:
    from kluster import conventions
    from kluster.physical.talos import STATIC_ADDRESSES

    # Day 0, not day 1: the address is a constant this program decides, so it
    # is on the seed the machine boots from rather than applied to a machine
    # that came up on a lease. Day 1 could not apply it in any case — it
    # reaches the worker at that very address.
    cluster = build_cluster(worker_nodes=(conventions.HOMELAB_NODE,), bgp_peers={})
    interface = interfaces(await patches_of(cluster, conventions.HOMELAB_NODE))[0]
    assert interface['addresses'] == [str(STATIC_ADDRESSES[conventions.HOMELAB_NODE].address)]
    # The very constant the gateway's neighbour statement reads, not merely
    # some address: the two sides agreeing is the whole point.
    assert IPv4Interface(interface['addresses'][0]).ip == conventions.HOMELAB_NODE_IPV4
    assert interface['dhcp'] is False
    # Only that node. The cloud nodes are addressed by the platform they boot
    # on, and a worker under some other name is not this one.
    for node in ('cp1', 'cp2', 'cp3'):
        assert not interfaces(await patches_of(cluster, node))
    assert not interfaces(await patches_of(build_cluster(), 'homelab'))


@pytest.mark.asyncio
async def test_only_the_augmented_node_is_configured_twice(fake: Fake) -> None:
    # Every other node is applied the very configuration it booted, rather than
    # a second rendering of it that happens to come out the same.
    cluster = build_cluster()
    day1 = build(cluster=cluster)
    for node in ('cp2', 'cp3', 'homelab'):
        assert day1.configurations[node] is cluster.configurations[node]
        assert not interfaces(await patches_of(day1, node))


@pytest.mark.asyncio
async def test_only_the_worker_takes_bgp(fake: Fake) -> None:
    from kluster.physical.talos import BGP_PORT

    cluster = build_cluster()

    async def bgp_rules(node: str) -> list[dict[str, Any]]:
        return [
            patch
            for patch in await patches_of(cluster, node)
            if patch.get('kind') == 'NetworkRuleConfig' and BGP_PORT in patch['portSelector']['ports']
        ]

    assert (await bgp_rules('homelab'))[0]['ingress'] == [{'subnet': '192.168.70.1/32'}]
    for node in ('cp1', 'cp2', 'cp3'):
        assert not await bgp_rules(node)


@pytest.mark.asyncio
async def test_a_stranger_cannot_be_named_by_either_component(fake: Fake) -> None:
    with pytest.raises(ValueError, match='BGP peers name nodes that are not in the cluster'):
        build_cluster(bgp_peers={'nowhere': '192.168.70.1/32'})
    with pytest.raises(ValueError, match='secondary addresses name nodes that are not in the cluster'):
        build(secondary_addresses={'nowhere': SECONDARY})
