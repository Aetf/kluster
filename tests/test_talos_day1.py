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
import itertools
import json
import logging
import typing
from collections.abc import Callable
from ipaddress import IPv4Interface
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pulumiverse_talos import client, machine
from pulumiverse_talos.cluster import KubeconfigArgs, get_health_output
from talos_secrets import declared_input_type, field_hint, generated_secrets, names_in, wire_names

from kluster import conventions
from kluster.components.talos import Refusal, RollRefused, TalosCluster, TalosDay1

#: The balancer in front of the control planes, in the two spellings the two
#: ports are named with: a URL for the Kubernetes endpoint, a bare address for
#: the machine API, which carries its own port. No node holds it, so a call
#: that dials a node's own address dials something other than this.
BALANCER = '203.0.113.10'
ENDPOINT = f'https://{BALANCER}:6443'
SECRETBOX = 'c2VjcmV0Ym94LWtleS1tYXRlcmlhbC0zMi1ieXRlcw=='
KUBECONFIG = 'apiVersion: v1\nkind: Config\n'
TALOSCONFIG = 'context: kluster\n'
ADDRESSES = {'cp1': '10.20.0.11', 'cp2': '10.20.0.12', 'cp3': '10.20.0.13', 'homelab': '192.168.70.51'}
SECONDARY = '10.20.0.42'


class Talos(Recorder):
    """A Talos provider that answers like the real one, minus a cluster.

    It carries the two behaviors the chain depends on: a secrets bundle that
    contains a generated secretbox key, and a health check that reports what
    it was asked about.
    """

    def __init__(self, *, secretbox: str | None = SECRETBOX) -> None:
        super().__init__()
        self.secretbox: str | None = secretbox
        #: Every invoke the program made, so a test can read what the health
        #: check and the client configuration were asked for.
        self.calls: dict[str, dict[str, Any]] = {}
        #: The structures the provider generated for `Secrets`, by output.
        self.generated: dict[str, Any] = {}

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'talos:cluster/kubeconfig:Kubeconfig':
            return {'kubeconfigRaw': KUBECONFIG}
        if args.typ != 'talos:machine/secrets:Secrets':
            return {}
        generated = generated_secrets()
        secrets = cast('dict[str, Any]', generated['machineSecrets']['secrets'])
        if self.secretbox is None:
            del secrets['secretboxEncryptionSecret']
        else:
            secrets['secretboxEncryptionSecret'] = self.secretbox
        self.generated = generated
        return generated

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


def installer_of(node: str) -> str:
    """An installer image of one node's own, different for every node."""
    return f'factory.talos.dev/example-installer/{node}:v1.11.0'


def build_cluster(name: str = 'kluster', **kwargs: Any) -> TalosCluster:
    """Day 0: the PKI and the configuration each machine boots with.

    `name` is the logical name, which a case only passes when it builds a
    second cluster in one run: two components under one name are one URN, and
    the recorder refuses that the way the engine does. The cluster's own
    identity -- the name in every machine configuration -- is fixed either way.
    """
    kwargs.setdefault('control_plane_nodes', ('cp1', 'cp2', 'cp3'))
    kwargs.setdefault('worker_nodes', ('homelab',))
    kwargs.setdefault('bgp_peers', {'homelab': '192.168.70.1/32'})
    kwargs.setdefault(
        'installers',
        {node: installer_of(node) for node in (*kwargs['control_plane_nodes'], *kwargs['worker_nodes'])},
    )
    kwargs.setdefault('volumes', {})
    return TalosCluster(
        name,
        cluster_name='kluster',
        endpoint=ENDPOINT,
        management_address=BALANCER,
        talos_version='v1.11.0',
        **kwargs,
    )


def refused(build_it: Callable[[], object]) -> RollRefused:
    """The refusal `build_it` raises, for a case to compare by its rule and its nodes, never by its wording."""
    with pytest.raises(RollRefused) as caught:
        _ = build_it()
    return caught.value


class Forward(pulumi.CustomResource):
    """A stand-in for what carries a call from the management address to apid: a listener and its backends."""

    def __init__(self, name: str) -> None:
        super().__init__('kluster:test:Forward', name, {})


def build(name: str = 'kluster', **kwargs: Any) -> TalosDay1:
    """Day 1, on top of a day 0 the caller may hand in to inspect.

    Day 0 is built only when none is handed in: a second `build_cluster()`
    beside the caller's would be a second cluster under the same logical
    name, which the recorder refuses as the engine does. The forward is built
    the same way. `name` is the logical name, for the reason `build_cluster`
    takes one.
    """
    if 'cluster' not in kwargs:
        kwargs['cluster'] = build_cluster()
    if 'forward' not in kwargs:
        kwargs['forward'] = [Forward(f'{name}-forward')]
    kwargs.setdefault('addresses', ADDRESSES)
    kwargs.setdefault('secondary_addresses', {'cp1': SECONDARY})
    return TalosDay1(name, **kwargs)


#: The invokes that dial apid or hand on where to dial it, each carrying an
#: `endpoints` list: the health check, and the client configuration a human
#: then dials with.
DIALING_INVOKES = ('talos:cluster/getHealth:getHealth', 'talos:client/getConfiguration:getConfiguration')


async def dialed(fake: Talos, day1: TalosDay1) -> list[str]:
    """Every address a Talos call of day 1 is dialed at: each resource's endpoint, each invoke's endpoints."""
    _ = await asyncio.gather(day1.health.future(), day1.talosconfig.future())
    resources = [*day1.applies.values(), day1.bootstrap, day1.kubeconfig_source]
    addresses = [str(await resource.endpoint.future()) for resource in resources]
    return addresses + [str(endpoint) for token in DIALING_INVOKES for endpoint in fake.calls[token]['endpoints']]


async def named(day1: TalosDay1) -> dict[str, str]:
    """Node name to the address its configuration apply names it by."""
    return {node: str(await applied.node.future()) for node, applied in day1.applies.items()}


async def urns_of(output: pulumi.Output[Any]) -> set[str]:
    return {await resource.urn.future() or '' for resource in await output.resources()}


async def ordered_behind(monitor: Recorder, output: pulumi.Output[Any]) -> set[str]:
    """Every resource `output` waits on, directly or through the dependencies of one it waits on."""
    behind: set[str] = set()
    pending = await urns_of(output)
    while pending:
        urn = pending.pop()
        behind.add(urn)
        pending |= set(monitor.registrations[urn].dependencies) - behind
    return behind


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
    refusal = refused(lambda: build_cluster(worker_nodes=('cp1',)))
    assert (refusal.refusal, refusal.nodes) == (Refusal.BOTH_ROLES, ('cp1',))


@pytest.mark.asyncio
async def test_a_node_named_twice_in_one_roll_is_refused_as_that_and_not_as_both_roles(fake: Talos) -> None:
    control = refused(lambda: build_cluster(control_plane_nodes=('cp1', 'cp1', 'cp2')))
    assert (control.refusal, control.nodes) == (Refusal.NAMED_TWICE, ('cp1',))
    worker = refused(lambda: build_cluster('kluster-worker', worker_nodes=('homelab', 'homelab')))
    assert (worker.refusal, worker.nodes) == (Refusal.NAMED_TWICE, ('homelab',))


@pytest.mark.asyncio
async def test_a_cluster_with_no_control_plane_is_refused(fake: Talos) -> None:
    refusal = refused(lambda: build_cluster(control_plane_nodes=(), worker_nodes=('homelab',)))
    assert (refusal.refusal, refusal.nodes) == (Refusal.NO_CONTROL_PLANE, ())


@pytest.mark.asyncio
async def test_day_one_needs_an_address_for_every_node(fake: Talos) -> None:
    # A cluster is not healthy because the nodes somebody listed are.
    refusal = refused(lambda: build(addresses={'cp1': '10.20.0.11', 'cp2': '10.20.0.12', 'cp3': '10.20.0.13'}))
    assert (refusal.refusal, refusal.nodes) == (Refusal.ADDRESS_MISSING, ('homelab',))


@pytest.mark.asyncio
async def test_day_zero_stands_on_its_own(fake: Talos) -> None:
    # Day 0 delivers the configuration out of band (instance metadata, a seed
    # ISO); day 1 needs machines that answer, which the stack only has later.
    # Every registration the build scheduled has landed once the barrier
    # exits, so an absence read below is one the run left, not one it had
    # not reached yet.
    async with declaring():
        _ = build_cluster()
    # By the provider's type tokens: day 1 is a bootstrap and configuration
    # applies, whatever this program names them.
    assert not fake.types & {'talos:machine/bootstrap:Bootstrap', 'talos:machine/configurationApply:ConfigurationApply'}


@pytest.mark.asyncio
async def test_configuration_is_applied_over_apid_without_rebooting_the_quorum(fake: Talos) -> None:
    day1 = build()
    # Every node is applied, so the set of them is pinned before it is walked.
    assert set(day1.applies) == set(ADDRESSES)
    for node, applied in day1.applies.items():
        assert await applied.node.future() == ADDRESSES[node]
        # A reboot-needing change is staged, so the reboot is an operator's
        # decision rather than a side effect of an apply.
        assert await applied.apply_mode.future() == 'staged_if_needing_reboot'


def apid_sans(patches: list[dict[str, Any]]) -> set[str]:
    """The SANs a node's patches put into apid's certificate: `machine.certSANs` of its `v1alpha1` patches."""
    return {
        str(san)
        for patch in patches
        if 'kind' not in patch
        for san in cast('list[str]', patch.get('machine', {}).get('certSANs', []))
    }


@pytest.mark.asyncio
async def test_every_address_a_call_dials_is_in_every_control_planes_apid_certificate(fake: Talos) -> None:
    """A call verifies apid's certificate against the address it dialed, on whichever control plane answers.

    The balancer may hand a connection to any control plane, so the address
    has to be in each one's certificate, in the rendering it boots with and in
    the one day 1 applies. No node holds the balancer's address, so a call
    dialed at a node's own address dials something these renderings do not
    carry.
    """
    cluster = build_cluster()
    day1 = build(cluster=cluster)
    addresses = set(await dialed(fake, day1))
    assert addresses, 'day 1 dialed nothing'
    for node in cluster.control_plane_nodes:
        for rendering in (cluster, day1):
            sans = apid_sans(await patches_of(rendering, node))
            assert addresses <= sans, f'{node} carries {sans}, and day 1 dials {addresses}'


@pytest.mark.asyncio
async def test_the_bootstrap_names_the_first_control_plane_and_dials_what_every_other_call_dials(
    fake: Talos,
) -> None:
    # A control plane's apid forwards a call to the node it names without
    # etcd, so the cluster's first contact needs no address of its own: it
    # is dialed where every apply is, and the kubeconfig read behind it too.
    day1 = build()
    first = (await named(day1))[day1.cluster.control_plane_nodes[0]]
    assert await day1.bootstrap.node.future() == first
    assert await day1.kubeconfig_source.node.future() == first
    assert len(set(await dialed(fake, day1))) == 1


@pytest.mark.asyncio
async def test_destroying_an_apply_never_wipes_the_disk(fake: Talos) -> None:
    # `reset` wipes STATE and EPHEMERAL — every partition (provider issue
    # #205). Node replacement is an explicit procedure, never a side effect.
    day1 = build()
    # Every node is applied, so the set of them is pinned before it is walked.
    assert set(day1.applies) == set(ADDRESSES)
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
    for earlier, later in itertools.pairwise(order):
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
async def test_the_health_check_names_each_node_as_its_apply_does(fake: Talos) -> None:
    # The check compares the names it is given with the addresses Kubernetes
    # reports for its nodes, and the apply's name is the one the node holds.
    day1 = build()
    _ = await day1.health.future()
    health = fake.calls['talos:cluster/getHealth:getHealth']
    names = await named(day1)
    assert health['controlPlaneNodes'] == [names[node] for node in day1.cluster.control_plane_nodes]
    assert health['workerNodes'] == [names[node] for node in day1.cluster.worker_nodes]


@pytest.mark.asyncio
async def test_no_call_is_made_before_the_forward_it_is_dialed_through(fake: Talos) -> None:
    """Every call goes through the listener and the backends, which no input of it carries.

    The management address exists before the forward behind it does, so an
    order is declared or is missing. Each resource that dials is read by its
    own registration, and the health check by what it waits on.
    """
    forward = Forward('kluster-forward')
    day1 = build(forward=[forward])
    urn = await forward.urn.future()
    calls = {'bootstrap': day1.bootstrap, 'kubeconfig': day1.kubeconfig_source} | {
        f'{node}-config': applied for node, applied in day1.applies.items()
    }
    _ = await asyncio.gather(*(call.urn.future() for call in calls.values()))
    for name in calls:
        assert urn in fake.depends_on(f'kluster-{name}'), f'{name} is not ordered behind the forward'
    assert urn in await urns_of(day1.health), 'the health check is not ordered behind the forward'


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
async def test_the_health_gate_waits_for_every_node_to_be_applied(fake: Talos) -> None:
    """Not only the node that bootstraps: a check run while a node is still being applied reads a cluster that is not done.

    The applies are node-serial, so the last one finishes long after the
    bootstrap does. A gate ordered behind the bootstrap alone would release the
    kubeconfig while the remaining nodes are still taking their configuration,
    and the next stack would be sent at a cluster short of those members.

    Read transitively, because what is required is the order and not which
    edge carries it: the first node's apply is already behind the bootstrap.
    """
    day1 = build()
    gate = await ordered_behind(fake, day1.health)
    released = await ordered_behind(fake, day1.kubeconfig)

    # Every node is applied, so the set of them is pinned before it is walked.
    assert set(day1.applies) == set(ADDRESSES)
    for node, applied in day1.applies.items():
        urn = await applied.urn.future()
        assert urn in gate, f'the health check does not wait for {node} to be applied'
        assert urn in released, f'the kubeconfig is released before {node} is applied'


@pytest.mark.asyncio
async def test_the_credentials_are_secret(fake: Talos) -> None:
    # Both are cluster-admin: a stack export must not print them.
    day1 = build()
    assert await day1.kubeconfig.is_secret()
    assert await day1.talosconfig.is_secret()


@pytest.mark.asyncio
async def test_the_talosconfig_names_every_node_as_its_apply_does_and_dials_where_the_applies_do(fake: Talos) -> None:
    # The names an operator's talosctl uses are the names the gate holds the
    # nodes to, through the one way in every call takes.
    day1 = build()
    assert await day1.talosconfig.future() == TALOSCONFIG
    configuration = fake.calls['talos:client/getConfiguration:getConfiguration']
    names = await named(day1)
    assert configuration['nodes'] == [names[node] for node in day1.cluster.roles]
    endpoints = {str(await applied.endpoint.future()) for applied in day1.applies.values()}
    assert set(configuration['endpoints']) == endpoints


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
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def links(patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `LinkConfig` documents among a node's patches: what it states about its own link."""
    return [patch for patch in patches if patch.get('kind') == 'LinkConfig']


@pytest.mark.asyncio
async def test_the_second_address_is_applied_and_never_booted_with(fake: Talos) -> None:
    # The address is assigned by OCI to the VNIC of an instance whose user_data
    # *is* the booted configuration. Naming it there would make the instance
    # wait on an address that waits on the instance, so it arrives on day 1.
    cluster = build_cluster()
    day1 = build(cluster=cluster)
    assert links(await patches_of(day1, 'cp1'))[0]['addresses'] == [{'address': f'{SECONDARY}/32'}]
    assert not links(await patches_of(cluster, 'cp1'))


@pytest.mark.asyncio
async def test_the_worker_boots_with_the_address_the_gateway_was_told_about(fake: Talos) -> None:
    from kluster import conventions
    from kluster.components.talos import STATIC_ADDRESSES

    # Day 0, not day 1: the address is a constant this program decides, so it
    # is on the seed the machine boots from rather than applied to a machine
    # that came up on a lease. Day 1 could not apply it in any case — it
    # reaches the worker at that very address.
    cluster = build_cluster(worker_nodes=(conventions.HOMELAB_NODE,), bgp_peers={})
    link = links(await patches_of(cluster, conventions.HOMELAB_NODE))[0]
    assert link['addresses'] == [{'address': str(STATIC_ADDRESSES[conventions.HOMELAB_NODE].address)}]
    # The very constant the gateway's neighbor statement reads, not merely
    # some address: the two sides agreeing is the whole point.
    assert IPv4Interface(link['addresses'][0]['address']).ip == conventions.HOMELAB_NODE_IPV4
    assert not [
        patch for patch in await patches_of(cluster, conventions.HOMELAB_NODE) if patch.get('kind') == 'DHCPv4Config'
    ]
    # Only that node. The cloud nodes are addressed by the platform they boot
    # on, and a worker under some other name is not this one.
    for node in ('cp1', 'cp2', 'cp3'):
        assert not links(await patches_of(cluster, node))
    assert not links(await patches_of(build_cluster('kluster-other-worker'), 'homelab'))


@pytest.mark.asyncio
async def test_only_the_node_holding_the_dedicated_vip_is_configured_twice(fake: Talos) -> None:
    # Every other node is applied its day-0 rendering itself, rather than a
    # second rendering of it that happens to come out the same.
    cluster = build_cluster()
    day1 = build(cluster=cluster)
    for node in ('cp2', 'cp3', 'homelab'):
        assert day1.configurations[node] is cluster.configurations[node]
        assert not links(await patches_of(day1, node))


def hostnames_in(patches: list[dict[str, Any]]) -> list[str]:
    """The hostnames a node's patches state, in the order they state them."""
    return [str(patch['hostname']) for patch in patches if patch.get('kind') == 'HostnameConfig']


@pytest.mark.asyncio
async def test_every_node_is_named_what_the_declaration_names_it(fake: Talos) -> None:
    """Each node's configuration names it as the rolls do, in both of its renderings.

    The name is otherwise the platform's: the hostname OCI's VCN hands an
    instance whose label another instance still holds, or one Talos
    generates. A control plane joining with an empty etcd data directory
    removes the member of its own name, a live one included, so a rendering
    that gave one node's name to another would take that node out of etcd.
    The rolls are the declaration's own, and the dedicated-VIP node is
    rendered twice, once to boot with and once to be applied.
    """
    from kluster import conventions

    nodes = (*conventions.CLOUD_NODES, conventions.HOMELAB_NODE)
    cluster = build_cluster(
        control_plane_nodes=conventions.CLOUD_NODES, worker_nodes=(conventions.HOMELAB_NODE,), bgp_peers={}
    )
    day1 = build(
        cluster=cluster,
        addresses={node: f'10.20.0.{11 + index}' for index, node in enumerate(nodes)},
        secondary_addresses={conventions.DEDICATED_VIP_NODE: SECONDARY},
    )
    for node in nodes:
        assert hostnames_in(await patches_of(cluster, node)) == [node]
        assert hostnames_in(await patches_of(day1, node)) == [node]


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
    # Each refused component is registered before it refuses, so each is
    # named apart from the others, and the three day-1 refusals stand on one
    # day 0 rather than on one apiece.
    peer = refused(lambda: build_cluster('kluster-peer', bgp_peers={'nowhere': '192.168.70.1/32'}))
    assert (peer.refusal, peer.nodes) == (Refusal.BGP_PEER_STRANGER, ('nowhere',))
    cluster = build_cluster()
    secondary = refused(lambda: build('kluster-secondary', cluster=cluster, secondary_addresses={'nowhere': SECONDARY}))
    assert (secondary.refusal, secondary.nodes) == (Refusal.SECONDARY_ADDRESS_STRANGER, ('nowhere',))
    address = refused(
        lambda: build('kluster-address', cluster=cluster, addresses={**ADDRESSES, 'nowhere': '10.20.0.99'})
    )
    assert (address.refusal, address.nodes) == (Refusal.ADDRESS_STRANGER, ('nowhere',))


def installers_in(patches: list[dict[str, Any]]) -> list[str]:
    """The installer images a node's patches name, in the order they name them."""
    return [
        cast('dict[str, str]', patch['machine']['install'])['image']
        for patch in patches
        if 'kind' not in patch and 'install' in patch.get('machine', {})
    ]


@pytest.mark.asyncio
async def test_each_node_names_its_own_installer_and_no_other(fake: Talos) -> None:
    """Each node's configuration names the installer it was handed, in both of its renderings.

    The installers differ from node to node here, so a rendering that read
    another node's entry, or one node's for all of them, names the wrong one.
    The node holding the dedicated VIP is rendered twice, and the rendering
    day 1 applies is the one a running node holds, which the upgrade runbook
    reads the installer from (operations.md §2.1).
    """
    day1 = build()
    for node in ('cp1', 'cp2', 'cp3', 'homelab'):
        assert installers_in(await patches_of(day1.cluster, node)) == [installer_of(node)]
        assert installers_in(await patches_of(day1, node)) == [installer_of(node)]


@pytest.mark.asyncio
async def test_every_node_names_an_installer_and_only_nodes_do(fake: Talos) -> None:
    every = {node: installer_of(node) for node in ('cp1', 'cp2', 'cp3', 'homelab')}
    missing = refused(
        lambda: build_cluster(
            'kluster-missing', installers={node: image for node, image in every.items() if node != 'homelab'}
        )
    )
    assert (missing.refusal, missing.nodes) == (Refusal.INSTALLER_MISSING, ('homelab',))
    stranger = refused(
        lambda: build_cluster('kluster-stranger', installers={**every, 'nowhere': installer_of('nowhere')})
    )
    assert (stranger.refusal, stranger.nodes) == (Refusal.INSTALLER_STRANGER, ('nowhere',))


def volumes_in(patches: list[dict[str, Any]]) -> list[str]:
    """The node volumes a node's patches mount, by name: its `partition` user volumes."""
    return [
        patch['name']
        for patch in patches
        if patch.get('kind') == 'UserVolumeConfig' and patch['volumeType'] == 'partition'
    ]


def labels_in(patches: list[dict[str, Any]]) -> dict[str, str]:
    """The node labels a node's patches set."""
    return {
        key: value
        for patch in patches
        if 'kind' not in patch
        for key, value in cast('dict[str, str]', patch.get('machine', {}).get('nodeLabels', {})).items()
    }


@pytest.mark.asyncio
async def test_a_volume_is_booted_with_by_the_node_it_attaches_to_and_by_no_other(fake: Talos) -> None:
    # Day 0: the census is known before any instance exists, while the
    # attachment waits on the instance, so the disk arrives on a machine that
    # already runs this configuration.
    cluster = build_cluster(volumes={'data': conventions.NodeVolumeEntry(node='cp2', size_gb=1)})
    patches = await patches_of(cluster, 'cp2')
    assert volumes_in(patches) == ['data']
    assert labels_in(patches) == {conventions.NODE_VOLUME_LABEL: 'data'}
    for node in ('cp1', 'cp3', 'homelab'):
        assert not volumes_in(await patches_of(cluster, node))
        assert not labels_in(await patches_of(cluster, node))


@pytest.mark.asyncio
async def test_a_volume_that_follows_the_dedicated_vip_is_mounted_where_the_vip_is(fake: Talos) -> None:
    # Grouped by the node the volume resolves to, not by what its row says:
    # the row says "whichever node holds the VIP", and that is a node only
    # once the sentinel is resolved.
    following = conventions.NodeVolumeEntry(node=conventions.FOLLOWS_DEDICATED_VIP, size_gb=1)
    cluster = build_cluster(volumes={'cache': following})
    day1 = build(cluster=cluster)
    assert volumes_in(await patches_of(cluster, conventions.DEDICATED_VIP_NODE)) == ['cache']
    # And only there: a second node carrying the label would let a `local`
    # PersistentVolume's pod schedule onto a node without the path.
    for node in sorted(set(cluster.roles) - {conventions.DEDICATED_VIP_NODE}):
        assert not volumes_in(await patches_of(cluster, node)), node
        assert not labels_in(await patches_of(cluster, node)), node
    # And the second rendering, which day 1 applies to that node with its
    # extra address: applying it without the volume would unmount the disk.
    assert volumes_in(await patches_of(day1, conventions.DEDICATED_VIP_NODE)) == ['cache']


@pytest.mark.asyncio
async def test_a_volume_on_a_node_outside_the_cluster_is_refused(fake: Talos) -> None:
    refusal = refused(lambda: build_cluster(volumes={'stray': conventions.NodeVolumeEntry(node='nowhere', size_gb=1)}))
    assert (refusal.refusal, refusal.nodes, refusal.volumes) == (Refusal.VOLUME_STRANGER, ('nowhere',), ('stray',))


@pytest.mark.asyncio
async def test_two_volumes_on_one_node_are_refused(fake: Talos) -> None:
    # The disk selector finds the data disk as the one that is not the boot
    # disk, which cannot tell two data disks apart.
    crowded = refused(
        lambda: build_cluster(
            volumes={
                'one': conventions.NodeVolumeEntry(node='cp2', size_gb=1),
                'two': conventions.NodeVolumeEntry(node='cp2', size_gb=1),
            }
        )
    )
    assert (crowded.refusal, crowded.nodes, crowded.volumes) == (Refusal.VOLUMES_CROWDED, ('cp2',), ('one', 'two'))
    # A following volume collides the same way, once it has resolved.
    following = refused(
        lambda: build_cluster(
            'kluster-following',
            volumes={
                'one': conventions.NodeVolumeEntry(node=conventions.DEDICATED_VIP_NODE, size_gb=1),
                'two': conventions.NodeVolumeEntry(node=conventions.FOLLOWS_DEDICATED_VIP, size_gb=1),
            },
        )
    )
    assert (following.refusal, following.nodes) == (Refusal.VOLUMES_CROWDED, (conventions.DEDICATED_VIP_NODE,))


#: Every invoke that is handed something `Secrets` generated: the function's
#: token, the generated function and its parameter, the name the argument
#: travels under, and the output of `Secrets` it is handed.
HANDED_SECRETS = [
    (
        'talos:machine/getConfiguration:getConfiguration',
        machine.get_configuration_output,
        'machine_secrets',
        'machineSecrets',
        'machineSecrets',
    ),
    (
        'talos:client/getConfiguration:getConfiguration',
        client.get_configuration_output,
        'client_configuration',
        'clientConfiguration',
        'clientConfiguration',
    ),
    (
        'talos:cluster/getHealth:getHealth',
        get_health_output,
        'client_configuration',
        'clientConfiguration',
        'clientConfiguration',
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('token', 'function', 'parameter', 'argument', 'output'), HANDED_SECRETS)
async def test_an_invoke_is_handed_what_secrets_generated_under_the_names_its_input_type_declares(
    fake: Talos, token: str, function: Callable[..., object], parameter: str, argument: str, output: str
) -> None:
    # `Secrets` reads its structures back as output types, which hold their
    # fields under Python names, and an invoke is serialized with no type to
    # translate a mapping by: handed on as they are, the provider would get
    # `k8s_aggregator` where it requires `k8sAggregator`. The names expected
    # here are the ones the function's declared input type carries.
    day1 = build()
    _ = await asyncio.gather(
        day1.talosconfig.future(),
        day1.health.future(),
        *(configuration.future() for configuration in day1.cluster.configurations.values()),
    )
    received = fake.calls[token][argument]
    declared_type = declared_input_type(typing.get_type_hints(function)[parameter])
    assert names_in(received) == wire_names(declared_type)
    assert received == fake.generated[output]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('typ', 'args'),
    [
        ('talos:machine/configurationApply:ConfigurationApply', machine.ConfigurationApplyArgs),
        ('talos:machine/bootstrap:Bootstrap', machine.BootstrapArgs),
        ('talos:cluster/kubeconfig:Kubeconfig', KubeconfigArgs),
    ],
)
async def test_a_resource_is_handed_the_generated_client_configuration_under_the_names_its_input_type_declares(
    fake: Talos, typ: str, args: type
) -> None:
    async with declaring():
        _ = build()
    declared_type = declared_input_type(field_hint(args, 'clientConfiguration'))
    declarations = fake.of_type(typ)
    assert declarations
    for declaration in declarations:
        received = declaration.inputs['clientConfiguration']
        assert names_in(received) == wire_names(declared_type)
        assert received == fake.generated['clientConfiguration']


@pytest.mark.asyncio
async def test_the_sdk_still_hands_an_output_type_to_an_invoke_under_python_names(fake: Talos) -> None:
    # The tripwire of the workaround layer at the end of
    # `kluster.components.talos` (pulumi/pulumi#15883): the output of
    # `Secrets`, handed on unchanged, reaches the provider under the Python
    # names its output type holds rather than under the provider's own.
    secrets = machine.Secrets('tripwire-secrets', talos_version='v1.11.0')
    rendered = machine.get_configuration_output(
        cluster_name='kluster',
        cluster_endpoint=ENDPOINT,
        machine_type='controlplane',
        machine_secrets=cast('Any', secrets.machine_secrets),
    )
    _ = await rendered.future()
    received = fake.calls['talos:machine/getConfiguration:getConfiguration']['machineSecrets']
    assert names_in(received) != wire_names(machine.MachineSecretsArgs), (
        'the SDK now hands an output type to an invoke under the provider names: once the output also '
        "type-checks against the consumer's parameter, delete the workaround section at the end of "
        'kluster.components.talos, hand `machine_secrets` and `client_configuration` on directly, '
        'and drop this case and its census entry in docs/style/README.md'
    )
