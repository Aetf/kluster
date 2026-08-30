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
import pytest
import pytest_asyncio
from mock_monitor import Recorder, run_with

from kluster.components.talos import TalosCluster, TalosDay1

#: The balancer in front of the control planes, in the two spellings the two
#: ports are named with: a URL for the Kubernetes endpoint, a bare address for
#: the machine API, which carries its own port.
BALANCER = '203.0.113.10'
ENDPOINT = f'https://{BALANCER}:6443'
SECRETBOX = 'c2VjcmV0Ym94LWtleS1tYXRlcmlhbC0zMi1ieXRlcw=='
KUBECONFIG = 'apiVersion: v1\nkind: Config\n'
TALOSCONFIG = 'context: kluster\n'
ADDRESSES = {'cp1': '10.20.0.11', 'cp2': '10.20.0.12', 'cp3': '10.20.0.13', 'homelab': '192.168.70.51'}
SECONDARY = '10.20.0.42'


class Talos(Recorder):
    """A Talos provider that answers like the real one, minus a cluster.

    It carries the two behaviours the chain depends on: a secrets bundle that
    contains a generated secretbox key, and a health check that reports what
    it was asked about.
    """

    def __init__(self, *, secretbox: str | None = SECRETBOX) -> None:
        super().__init__()
        self.secretbox: str | None = secretbox
        #: Every invoke the program made, so a test can read what the health
        #: check and the client configuration were asked for.
        self.calls: dict[str, dict[str, Any]] = {}

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'talos:cluster/kubeconfig:Kubeconfig':
            return {'kubeconfigRaw': KUBECONFIG}
        if args.typ != 'talos:machine/secrets:Secrets':
            return {}
        secrets: dict[str, Any] = {'bootstrapToken': 'bootstrap.token'}
        if self.secretbox is not None:
            secrets['secretboxEncryptionSecret'] = self.secretbox
        return {
            'machineSecrets': {
                'certs': {},
                'cluster': {'id': 'kluster', 'secret': 'cluster.secret'},
                'secrets': secrets,
                'trustdinfo': {'token': 'trustd.token'},
            },
            'clientConfiguration': {'caCertificate': 'ca', 'clientCertificate': 'crt', 'clientKey': 'key'},
        }

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        arguments = dict(cast('dict[str, Any]', args.args))
        self.calls[args.token] = arguments
        match args.token:
            case 'talos:machine/getConfiguration:getConfiguration':
                # The data source keeps its inputs, patches included.
                return {'machineConfiguration': 'machine: {}', 'configPatches': arguments.get('configPatches')}
            case 'talos:client/getConfiguration:getConfiguration':
                return {'talosConfig': TALOSCONFIG}
            case 'talos:cluster/getHealth:getHealth':
                return {'id': 'healthy'}
            case _:
                return {}


@pytest_asyncio.fixture
async def fake() -> Talos:
    return await run_with(Talos(), stack='physical')


def build_cluster(**kwargs: Any) -> TalosCluster:
    """Day 0: the PKI and the configuration each machine boots with."""
    kwargs.setdefault('control_plane_nodes', ('cp1', 'cp2', 'cp3'))
    kwargs.setdefault('worker_nodes', ('homelab',))
    kwargs.setdefault('bgp_peers', {'homelab': '192.168.70.1/32'})
    return TalosCluster(
        'kluster',
        cluster_name='kluster',
        endpoint=ENDPOINT,
        cert_sans=[BALANCER],
        talos_version='v1.11.0',
        **kwargs,
    )


def build(**kwargs: Any) -> TalosDay1:
    """Day 1, on top of a day 0 the caller may hand in to inspect."""
    kwargs.setdefault('cluster', build_cluster())
    kwargs.setdefault('addresses', ADDRESSES)
    kwargs.setdefault('secondary_addresses', {'cp1': SECONDARY})
    return TalosDay1('kluster', **kwargs)


async def urns_of(output: pulumi.Output[Any]) -> set[str]:
    return {await resource.urn.future() or '' for resource in await output.resources()}


@pytest.mark.asyncio
async def test_every_node_gets_the_role_it_was_declared_with(fake: Talos) -> None:
    cluster = build_cluster()
    assert cluster.roles == {
        'cp1': 'controlplane',
        'cp2': 'controlplane',
        'cp3': 'controlplane',
        'homelab': 'worker',
    }
    assert set(cluster.machine_configs) == set(ADDRESSES)


@pytest.mark.asyncio
async def test_a_node_cannot_be_both_roles(fake: Talos) -> None:
    with pytest.raises(ValueError, match='both a control plane and a worker'):
        build_cluster(worker_nodes=('cp1',))


@pytest.mark.asyncio
async def test_day_one_needs_an_address_for_every_node(fake: Talos) -> None:
    # A cluster is not healthy because the nodes somebody listed are.
    with pytest.raises(ValueError, match=r'an address for every node.*homelab'):
        build(addresses={'cp1': '10.20.0.11', 'cp2': '10.20.0.12', 'cp3': '10.20.0.13'})


@pytest.mark.asyncio
async def test_day_zero_stands_on_its_own(fake: Talos) -> None:
    # Day 0 delivers the configuration out of band (instance metadata, a seed
    # ISO); day 1 needs machines that answer, which the stack only has later.
    cluster = build_cluster()
    await asyncio.gather(*(config.future() for config in cluster.machine_configs.values()))
    assert 'kluster-bootstrap' not in fake.registrations
    assert not [name for name in fake.registrations if name.endswith('-config')]


@pytest.mark.asyncio
async def test_configuration_is_applied_over_apid_without_rebooting_the_quorum(fake: Talos) -> None:
    day1 = build()
    for node, applied in day1.applies.items():
        assert await applied.node.future() == ADDRESSES[node]
        # A reboot-needing change is staged, so the reboot is an operator's
        # decision rather than a side effect of an apply.
        assert await applied.apply_mode.future() == 'staged_if_needing_reboot'


@pytest.mark.asyncio
async def test_a_node_without_an_endpoint_is_dialled_where_it_is_named(fake: Talos) -> None:
    # The ordinary case, and the one the cloud nodes are in: the address that
    # names the node is also the address that reaches it.
    day1 = build()
    for node, applied in day1.applies.items():
        assert await applied.endpoint.future() == ADDRESSES[node]


@pytest.mark.asyncio
async def test_a_node_behind_the_mesh_is_named_by_its_own_address_and_dialled_elsewhere(fake: Talos) -> None:
    # apid routes by the node a call names, so a node that nothing outside the
    # site can open a connection to is still administered: it is named by its
    # own address and dialled at one that answers, and whichever member that
    # is proxies the call the rest of the way.
    day1 = build(endpoints={'homelab': BALANCER})
    worker = day1.applies['homelab']
    assert await worker.node.future() == ADDRESSES['homelab']
    assert await worker.endpoint.future() == BALANCER
    # And only that node: the others are untouched by the override.
    for node in ('cp1', 'cp2', 'cp3'):
        assert await day1.applies[node].endpoint.future() == ADDRESSES[node]


@pytest.mark.asyncio
async def test_the_bootstrap_dials_the_node_it_names_whatever_it_was_given(fake: Talos) -> None:
    # Bootstrap and the kubeconfig read behind it are the calls with nothing to
    # route through — there is no cluster yet — so they never take a proxy,
    # even for a node the caller handed an endpoint for.
    day1 = build(endpoints={'cp1': BALANCER})
    assert await day1.bootstrap.endpoint.future() == ADDRESSES['cp1']
    assert await day1.kubeconfig_source.endpoint.future() == ADDRESSES['cp1']
    # The apply on that same node did take it, so this is a decision rather
    # than an input that goes nowhere.
    assert await day1.applies['cp1'].endpoint.future() == BALANCER


@pytest.mark.asyncio
async def test_destroying_an_apply_never_wipes_the_disk(fake: Talos) -> None:
    # `reset` wipes STATE and EPHEMERAL — every partition (provider issue
    # #205). Node replacement is an explicit procedure, never a side effect.
    day1 = build()
    for applied in day1.applies.values():
        on_destroy = await applied.on_destroy.future()
        assert on_destroy is not None
        assert on_destroy.reset is False


@pytest.mark.asyncio
async def test_nodes_are_applied_one_after_another(fake: Talos) -> None:
    # A configuration change that reboots the machine is applied to one node
    # at a time, so the quorum never goes down together.
    day1 = build()
    order = list(day1.applies)
    # Registration is asynchronous; the graph is only complete once it is.
    await asyncio.gather(*(applied.urn.future() for applied in day1.applies.values()))
    for earlier, later in zip(order, order[1:]):
        waits_for = fake.depends_on(f'kluster-{later}-config')
        assert await day1.applies[earlier].urn.future() in waits_for, f'{later} does not wait for {earlier}'


@pytest.mark.asyncio
async def test_only_the_first_control_plane_bootstraps(fake: Talos) -> None:
    # A second bootstrap does not join etcd, it starts a second etcd cluster.
    day1 = build()
    assert await day1.bootstrap.node.future() == ADDRESSES['cp1']
    assert await day1.applies['cp1'].urn.future() in fake.depends_on('kluster-bootstrap')


@pytest.mark.asyncio
async def test_the_kubeconfig_comes_from_the_bootstrapped_node(fake: Talos) -> None:
    day1 = build()
    assert await day1.kubeconfig_source.node.future() == ADDRESSES['cp1']
    assert await day1.bootstrap.urn.future() in fake.depends_on('kluster-kubeconfig')


@pytest.mark.asyncio
async def test_the_health_check_knows_which_node_is_which(fake: Talos) -> None:
    day1 = build()
    _ = await day1.health.future()
    health = fake.calls['talos:cluster/getHealth:getHealth']
    assert health['controlPlaneNodes'] == [ADDRESSES[node] for node in ('cp1', 'cp2', 'cp3')]
    assert health['workerNodes'] == [ADDRESSES['homelab']]
    # The health client talks to a control plane, never to the worker: the
    # worker is named as a node and the call is routed to it from there.
    assert health['endpoints'] == [ADDRESSES[node] for node in ('cp1', 'cp2', 'cp3')]


@pytest.mark.asyncio
async def test_the_kubeconfig_is_gated_on_the_health_check(fake: Talos) -> None:
    day1 = build()
    assert await day1.kubeconfig.future() == KUBECONFIG
    # The gate is the dependency: whatever consumes the kubeconfig is ordered
    # behind a cluster that reported healthy, not merely behind a resource
    # that finished registering.
    assert await urns_of(day1.health) <= await urns_of(day1.kubeconfig)
    assert await day1.bootstrap.urn.future() in await urns_of(day1.kubeconfig)


@pytest.mark.asyncio
async def test_the_credentials_are_secret(fake: Talos) -> None:
    # Both are cluster-admin: a stack export must not print them.
    day1 = build()
    assert await day1.kubeconfig.is_secret()
    assert await day1.talosconfig.is_secret()


@pytest.mark.asyncio
async def test_the_talosconfig_names_every_node_and_the_control_plane_endpoints(fake: Talos) -> None:
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
async def test_the_generated_secretbox_key_reaches_the_control_planes(fake: Talos) -> None:
    cluster = build_cluster()
    for node in ('cp1', 'cp2', 'cp3'):
        sections = [patch['cluster'] for patch in await patches_of(cluster, node) if 'cluster' in patch]
        assert SECRETBOX in [section.get('secretboxEncryptionSecret') for section in sections]


@pytest.mark.asyncio
async def test_a_bundle_without_a_secretbox_key_is_an_error(fake: Talos, caplog: pytest.LogCaptureFixture) -> None:
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
async def test_the_second_address_is_applied_and_never_booted_with(fake: Talos) -> None:
    # The address is assigned by OCI to the VNIC of an instance whose user_data
    # *is* the booted configuration. Naming it there would make the instance
    # wait on an address that waits on the instance, so it arrives on day 1.
    cluster = build_cluster()
    day1 = build(cluster=cluster)
    assert interfaces(await patches_of(day1, 'cp1'))[0]['addresses'] == [f'{SECONDARY}/32']
    assert not interfaces(await patches_of(cluster, 'cp1'))


@pytest.mark.asyncio
async def test_the_worker_boots_with_the_address_the_gateway_was_told_about(fake: Talos) -> None:
    from kluster import conventions
    from kluster.components.talos import STATIC_ADDRESSES

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
async def test_only_the_node_holding_the_dedicated_vip_is_configured_twice(fake: Talos) -> None:
    # Every other node is applied the very configuration it booted, rather than
    # a second rendering of it that happens to come out the same.
    cluster = build_cluster()
    day1 = build(cluster=cluster)
    for node in ('cp2', 'cp3', 'homelab'):
        assert day1.configurations[node] is cluster.configurations[node]
        assert not interfaces(await patches_of(day1, node))


@pytest.mark.asyncio
async def test_only_the_worker_takes_bgp(fake: Talos) -> None:
    from kluster.components.talos import BGP_PORT

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
async def test_a_stranger_cannot_be_named_by_either_component(fake: Talos) -> None:
    with pytest.raises(ValueError, match='BGP peers name nodes that are not in the cluster'):
        build_cluster(bgp_peers={'nowhere': '192.168.70.1/32'})
    with pytest.raises(ValueError, match='secondary addresses name nodes that are not in the cluster'):
        build(secondary_addresses={'nowhere': SECONDARY})
    with pytest.raises(ValueError, match='endpoints name nodes that are not in the cluster'):
        build(endpoints={'nowhere': BALANCER})
