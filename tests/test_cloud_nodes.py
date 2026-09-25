"""The node fleet's shape.

The properties asserted here are the ones a later `pulumi diff` cannot show
because they are structural -- that no two nodes share an availability
domain, for instance, that the dedicated VIP is a reserved address attached
to a secondary private IP rather than an ephemeral one, or that moving a
management port renames nothing that state or the balancer keys on.
"""

from collections import Counter
from itertools import product
from typing import Any, cast

import pulumi
import pulumi_oci as oci
import pytest
import pytest_asyncio
from mock_monitor import Declaration, Recorder, declaring, decline_every_invoke, run_with

from kluster import conventions
from kluster.components.cloud.nodes import CloudNodes, NodeLoadBalancer
from kluster.conventions import ManagementPorts
from kluster.stacks import physical

COMPARTMENT_ID = 'ocid1.compartment.test'
SUBNET_ID = 'ocid1.subnet.test'
VNIC_ID = 'ocid1.vnic.oc1.phx.augmented'

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
LB_ADDRESS_PRIVATE = '10.0.0.10'

#: What a dual-stack balancer reads back as: a public address of each family
#: and the private one it holds in its own subnet. The private entry is listed
#: first so a property that filtered on nothing but the family would pick it.
LB_IP_ADDRESSES = [
    {'ipAddress': LB_ADDRESS_PRIVATE, 'isPublic': False, 'ipVersion': 'IPV4'},
    {'ipAddress': LB_ADDRESS, 'isPublic': True, 'ipVersion': 'IPV4'},
    {'ipAddress': LB_ADDRESS_V6, 'isPublic': True, 'ipVersion': 'IPV6'},
]


#: A component name whose balancer reads back with the IPv4 alone, standing in
#: for a provider that has not handed out the second family.
SINGLE_STACK = 'lb-v4-only'


#: A region of three availability domains, each offering the three fault
#: domains OCI names the same way in every AD. Three of each is the smallest
#: region where spreading across ADs first and filling one AD first give
#: different fleets.
AVAILABILITY_DOMAINS = [f'ZRbp:PHX-AD-{n}' for n in (1, 2, 3)]
FAULT_DOMAINS = [f'FAULT-DOMAIN-{n}' for n in (1, 2, 3)]


class Oci(Recorder):
    """What the account reads back: the balancer's addresses, the node's VNIC, and the region's domains."""

    def __init__(self, availability_domains: list[str] = AVAILABILITY_DOMAINS) -> None:
        super().__init__()
        #: The ADs the region offers, which a case narrows to stand in for a
        #: smaller region.
        self.availability_domains: list[str] = availability_domains

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'oci:NetworkLoadBalancer/networkLoadBalancer:NetworkLoadBalancer':
            v4_only = args.name.startswith(SINGLE_STACK)
            return {'ipAddresses': LB_IP_ADDRESSES[:2] if v4_only else LB_IP_ADDRESSES}
        return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        match args.token:
            case 'oci:Core/getVnicAttachments:getVnicAttachments':
                return {'vnicAttachments': [{'vnicId': VNIC_ID}]}
            case 'oci:Identity/getAvailabilityDomains:getAvailabilityDomains':
                return {'availabilityDomains': [{'name': name} for name in self.availability_domains]}
            case 'oci:Identity/getFaultDomains:getFaultDomains':
                # Only for an AD the region has: a lookup that named another
                # would be reading a region that is not this one.
                assert cast('dict[str, Any]', args.args)['availabilityDomain'] in self.availability_domains
                return {'faultDomains': [{'name': name} for name in FAULT_DOMAINS]}
            case _:
                return {}


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Oci:
    return await run_with(Oci(), stack='physical')


async def placements() -> list[tuple[str, str]]:
    """The order the stack hands its nodes placements in, read off the region the monitor answers for.

    The stack's own function rather than a list written to match it: the fleet
    below is declared from what the program would give it, so a change to the
    order reaches these cases instead of passing beside them.
    """
    cloud = oci.Provider('placements-oci', region='us-phoenix-1')
    return await physical._placements(COMPARTMENT_ID, cloud)  # pyright: ignore[reportPrivateUsage]


def build_balancer(name: str = 'lb') -> NodeLoadBalancer:
    return NodeLoadBalancer(name, compartment_id=COMPARTMENT_ID, subnet_id=SUBNET_ID)


def build_nodes(placements: list[tuple[str, str]]) -> CloudNodes:
    return CloudNodes(
        'kluster',
        compartment_id=COMPARTMENT_ID,
        subnet_id=SUBNET_ID,
        image_id='ocid1.image.test',
        machine_configs={'cp1': 'config-1', 'cp2': 'config-2', 'cp3': 'config-3'},
        ocpus=1,
        memory_gb=8,
        boot_volume_gb=50,
        placements=placements,
        dedicated_vip_node='cp1',
        load_balancer=build_balancer('kluster'),
    )


@pytest_asyncio.fixture
async def nodes(monitor: Oci) -> CloudNodes:
    """The fleet as the stack declares it; only one case varies its placements."""
    return build_nodes(await placements())


@pytest.fixture
def balancer(monitor: Oci) -> NodeLoadBalancer:
    return build_balancer()


@pytest.mark.asyncio
async def test_no_two_nodes_share_an_availability_domain(nodes: CloudNodes) -> None:
    """An AD is the independent failure domain, and A1 capacity is per-AD (nodes.md §5)."""
    domains = [await instance.availability_domain.future() for instance in nodes.instances.values()]

    assert len(set(domains)) == len(domains) == 3


@pytest.mark.asyncio
async def test_every_availability_domain_is_used_once_before_any_is_used_twice(monitor: Oci) -> None:
    """An AD is the independent failure domain, and a fault domain only the tiebreak (nodes.md §5).

    Read over every prefix of the order, because a fleet takes the first
    placements and is any size: at no length may one AD hold two nodes while
    another holds none. In a region whose ADs each offer the same fault
    domains, as OCI's do, the whole order is every pairing once, so a fleet
    larger than the ADs is spread across fault domains rather than stacked on
    one.
    """
    order = await placements()

    assert sorted(order) == sorted(product(AVAILABILITY_DOMAINS, FAULT_DOMAINS))
    for length in range(1, len(order) + 1):
        taken = Counter(domain for domain, _ in order[:length])
        counts = [taken[domain] for domain in AVAILABILITY_DOMAINS]
        assert max(counts) - min(counts) <= 1, f'the first {length} placements are {order[:length]}'


@pytest.mark.asyncio
async def test_a_single_ad_region_spreads_across_fault_domains_instead() -> None:
    """The fleet the stack declares in a region of one AD, from the order the stack computes for it."""
    _ = await run_with(Oci(AVAILABILITY_DOMAINS[:1]), stack='physical')

    nodes = build_nodes(await placements())

    domains = [await instance.fault_domain.future() for instance in nodes.instances.values()]
    assert len(set(domains)) == 3


@pytest.mark.asyncio
async def test_the_legacy_metadata_endpoint_is_off_on_every_node(nodes: CloudNodes) -> None:
    # "Every node" is a claim about a fleet, so the fleet is pinned before it
    # is walked: an empty one satisfies the loop and nothing else here.
    assert set(nodes.instances) == {'cp1', 'cp2', 'cp3'}
    for instance in nodes.instances.values():
        options = await instance.instance_options.future()
        assert options is not None
        assert options.are_legacy_imds_endpoints_disabled is True


@pytest.mark.asyncio
async def test_each_node_boots_the_machine_config_it_was_given(nodes: CloudNodes) -> None:
    metadata = {node: await instance.metadata.future() for node, instance in nodes.instances.items()}

    assert {node: (fields or {}).get('user_data') for node, fields in metadata.items()} == {
        'cp1': 'config-1',
        'cp2': 'config-2',
        'cp3': 'config-3',
    }


@pytest.mark.asyncio
async def test_the_vip_is_a_reserved_address_on_a_secondary_private_ip(nodes: CloudNodes) -> None:
    """Pointing it at the node's primary address would tie it to the node.

    The workload's address has to survive a node rebuild, which is what
    reserving it and attaching it to a second private IP buys.
    """
    assert await nodes.reserved_ip.lifetime.future() == 'RESERVED'
    assert await nodes.secondary_ip.vnic_id.future() == VNIC_ID
    assert await nodes.reserved_ip.private_ip_id.future() == await nodes.secondary_ip.id.future()


@pytest.mark.asyncio
async def test_a_vnic_lookup_the_engine_declines_leaves_the_vip_unknown_rather_than_crashing() -> None:
    """The unknown degrades the one input, and nothing surfaces as a traceback.

    Awaiting the lookup through `resolve` is what puts it under the rule every
    other awaited value in the component follows (framework/pulumi.md §1.2):
    an unknown aborts the coroutine, that input alone becomes unknown, and the
    rest of the resource is declared as it would have been. Awaited directly,
    the same answer is a `None` for an `assert` to trip over -- a traceback on
    a run that may have converged everything it was asked to.

    A preview, because the mock's readback in an update turns an unknown
    resource input into a known `None` (framework/testing.md §3.3), which is
    exactly the difference this case exists to see; the abort itself does not
    ask which kind of run it is in.
    """
    _ = await run_with(Oci(), stack='physical', preview=True)
    # Read before the invokes are declined: the placements are the stack
    # program's lookups, and the one declined here is the component's.
    order = await placements()
    decline_every_invoke()

    nodes = build_nodes(order)

    assert await nodes.secondary_ip.vnic_id.is_known() is False
    assert await nodes.secondary_ip.display_name.is_known() is True


@pytest.mark.asyncio
async def test_every_management_port_preserves_the_client_address(balancer: NodeLoadBalancer) -> None:
    # The backend sets are the management ports and nothing else: the same
    # structure the node firewall opens and the cluster endpoint names.
    assert set(balancer.backend_sets) == set(ManagementPorts._fields)
    for backend_set in balancer.backend_sets.values():
        assert await backend_set.is_preserve_source.future() is True


@pytest.mark.asyncio
async def test_every_node_backs_every_management_port(nodes: CloudNodes) -> None:
    assert len(nodes.backends) == len(conventions.MANAGEMENT_PORTS) * 3


@pytest.mark.asyncio
async def test_the_balancer_publishes_a_public_address_of_each_family(balancer: NodeLoadBalancer) -> None:
    """Both halves of the cluster anchor, and neither of them the private one.

    The balancer is declared dual-stack, so the `dns` stack's anchor takes an
    A and an AAAA from here; the address list it reads them out of also holds
    the balancer's private address, which is not either of them.
    """
    assert await balancer.load_balancer.nlb_ip_version.future() == 'IPV4_AND_IPV6'
    assert await balancer.address.future() == LB_ADDRESS
    assert await balancer.address_v6.future() == LB_ADDRESS_V6


@pytest.mark.asyncio
async def test_a_family_the_provider_never_handed_out_is_refused(monitor: Oci) -> None:
    """An address that never arrived must not become an empty DNS record."""
    balancer = build_balancer(SINGLE_STACK)

    assert await balancer.address.future() == LB_ADDRESS
    with pytest.raises(ValueError, match='no public IPv6 address'):
        _ = await balancer.address_v6.future()


#: The types everything declared per management port is registered under.
BACKEND_SET = 'oci:NetworkLoadBalancer/backendSet:BackendSet'
LISTENER = 'oci:NetworkLoadBalancer/listener:Listener'
BACKEND = 'oci:NetworkLoadBalancer/backend:Backend'


async def declared_per_port(ports: ManagementPorts, monkeypatch: pytest.MonkeyPatch) -> list[Declaration]:
    """The fleet and its balancer declared under `ports`, read back per management port."""
    recorder = await run_with(Oci(), stack='physical')
    monkeypatch.setattr(conventions, 'MANAGEMENT_PORTS', ports)
    order = await placements()
    async with declaring():
        _ = build_nodes(order)
    return [it for it in recorder.declared if it.typ in {BACKEND_SET, LISTENER, BACKEND}]


def every_name(declarations: list[Declaration]) -> list[tuple[str, str, Any, Any, Any]]:
    """Every name a per-port resource carries: logical, OCI, and the backend set's it points at."""
    return sorted(
        (
            it.typ,
            it.name,
            it.inputs.get('name'),
            it.inputs.get('defaultBackendSetName'),
            it.inputs.get('backendSetName'),
        )
        for it in declarations
    )


def forwarded_ports(declarations: list[Declaration]) -> set[int]:
    """The ports the listeners and backends were declared on."""
    return {int(it.inputs['port']) for it in declarations if it.typ in {LISTENER, BACKEND}}


@pytest.mark.asyncio
async def test_moving_a_management_port_renames_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name is an identity in state and, for a backend set or a listener, immutable on the balancer.

    The census lets a port's number be edited (`ManagementPorts`), so a name
    spliced from the number would turn that edit into a delete and a create of
    the cluster endpoint's backend sets and listeners -- the stable address
    the management APIs are reached on. Named after the field, the edit
    changes inputs and no name.
    """
    census = conventions.MANAGEMENT_PORTS
    moved = ManagementPorts(*(port + 1 for port in census))
    declared = await declared_per_port(census, monkeypatch)
    redeclared = await declared_per_port(moved, monkeypatch)

    # The edit reached the declarations; without it, equal names prove nothing.
    assert forwarded_ports(declared) == set(census)
    assert forwarded_ports(redeclared) == set(moved)
    assert every_name(redeclared) == every_name(declared)
    # Unchanged is not enough: an index is as stable as a field. Each name
    # carries the field of the port it serves.
    for it in declared:
        field = it.inputs.get('backendSetName') or it.inputs.get('defaultBackendSetName') or it.inputs['name']
        assert field in ManagementPorts._fields, it.name
        assert f'-{field}' in it.name, it.name
    # And the name on the balancer is the field itself.
    assert {it.inputs['name'] for it in declared if it.typ in {BACKEND_SET, LISTENER}} == set(ManagementPorts._fields)
