"""The node fleet's shape.

The properties asserted here are the ones a later `pulumi diff` cannot show
because they are structural: that no two nodes share an availability domain,
that the legacy metadata endpoint is off on every one of them, and that the
dedicated VIP is a reserved address attached to a secondary private IP rather
than an ephemeral one.
"""

from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, run_with

from kluster.components.cloud.nodes import MANAGEMENT_PORTS, CloudNodes, NodeLoadBalancer

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


class Oci(Recorder):
    """What the account reads back: the balancer's addresses, and the node's VNIC."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'oci:NetworkLoadBalancer/networkLoadBalancer:NetworkLoadBalancer':
            v4_only = args.name.startswith(SINGLE_STACK)
            return {'ipAddresses': LB_IP_ADDRESSES[:2] if v4_only else LB_IP_ADDRESSES}
        return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'oci:Core/getVnicAttachments:getVnicAttachments':
            return {'vnicAttachments': [{'vnicId': VNIC_ID}]}
        return {}


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Oci:
    return await run_with(Oci(), stack='physical')


#: Three availability domains, each with the three fault domains OCI offers,
#: laid out the way the stack's `_placements` orders them: every AD used once
#: before any AD is used twice.
PLACEMENTS = [
    ('ZRbp:PHX-AD-1', 'FAULT-DOMAIN-1'),
    ('ZRbp:PHX-AD-2', 'FAULT-DOMAIN-1'),
    ('ZRbp:PHX-AD-3', 'FAULT-DOMAIN-1'),
]


def build_balancer(name: str = 'lb') -> NodeLoadBalancer:
    return NodeLoadBalancer(name, compartment_id=COMPARTMENT_ID, subnet_id=SUBNET_ID)


def build_nodes(placements: list[tuple[str, str]] = PLACEMENTS) -> CloudNodes:
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


@pytest.fixture
def nodes(monitor: Oci) -> CloudNodes:
    """The fleet as the stack declares it; only one case varies its placements."""
    return build_nodes()


@pytest.fixture
def balancer(monitor: Oci) -> NodeLoadBalancer:
    return build_balancer()


@pytest.mark.asyncio
async def test_no_two_nodes_share_an_availability_domain(nodes: CloudNodes) -> None:
    """An AD is the independent failure domain, and A1 capacity is per-AD (nodes.md §5)."""
    domains = [await instance.availability_domain.future() for instance in nodes.instances.values()]

    assert len(set(domains)) == len(domains) == 3


@pytest.mark.asyncio
async def test_a_single_ad_region_spreads_across_fault_domains_instead(monitor: Oci) -> None:
    one_ad = [('ZRbp:PHX-AD-1', f'FAULT-DOMAIN-{n}') for n in (1, 2, 3)]

    nodes = build_nodes(one_ad)

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
async def test_every_management_port_preserves_the_client_address(balancer: NodeLoadBalancer) -> None:
    assert set(balancer.backend_sets) == set(MANAGEMENT_PORTS)
    for backend_set in balancer.backend_sets.values():
        assert await backend_set.is_preserve_source.future() is True


@pytest.mark.asyncio
async def test_every_node_backs_every_management_port(nodes: CloudNodes) -> None:
    assert len(nodes.backends) == len(MANAGEMENT_PORTS) * 3


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
