"""The cluster VCN's shape, asserted against Pulumi's mock provider.

What matters here is what cannot be seen by reading a diff later: that the
subnet's IPv6 block is derived from the prefix OCI assigns rather than
declared, that Object Storage traffic is routed at the service gateway
instead of out through the internet gateway, and that the subnet carries a
list of this program's own that admits everything rather than the one OCI
would attach in its absence.
"""

from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, run_with

from kluster import conventions
from kluster.components.cloud import CloudNetwork

VCN_IPV6_PREFIX = '2603:c020:8000:1200::/56'
OBJECT_STORAGE_SERVICE_ID = 'ocid1.service.oc1.phx.objectstorage'
OBJECT_STORAGE_CIDR = 'oci-phx-objectstorage'


class Oci(Recorder):
    """The two answers this suite is about: the assigned prefix, and the catalog.

    Both are things the account decides rather than the program, which is why
    the subnet's block has to be derived from the first and the gateway's
    service picked out of the second.
    """

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'oci:Core/vcn:Vcn':
            return {'ipv6cidrBlocks': [VCN_IPV6_PREFIX]}
        return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'oci:Core/getServices:getServices':
            return {
                'services': [
                    {'id': 'ocid1.service.oc1.phx.other', 'name': 'All PHX Services', 'cidrBlock': 'all-phx'},
                    {
                        'id': OBJECT_STORAGE_SERVICE_ID,
                        'name': 'OCI PHX Object Storage',
                        'cidrBlock': OBJECT_STORAGE_CIDR,
                    },
                ]
            }
        return {}


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Oci:
    return await run_with(Oci(), stack='physical')


@pytest.fixture
def network(monitor: Oci) -> CloudNetwork:
    """The one network these cases read; nothing about it varies between them."""
    return CloudNetwork('kluster', compartment_id='ocid1.compartment.test')


@pytest.mark.asyncio
async def test_the_vcn_carries_both_families(network: CloudNetwork) -> None:
    assert await network.vcn.cidr_blocks.future() == [str(conventions.VCN_CIDR)]
    assert await network.vcn.is_ipv6enabled.future() is True


@pytest.mark.asyncio
async def test_the_subnets_ipv6_block_is_carved_from_the_prefix_oci_assigned(network: CloudNetwork) -> None:
    assert await network.subnet.ipv6cidr_block.future() == '2603:c020:8000:1200::/64'


@pytest.mark.asyncio
async def test_the_service_gateway_carries_object_storage_and_nothing_else(network: CloudNetwork) -> None:
    services = await network.service_gateway.services.future()

    assert services is not None
    assert [service.service_id for service in services] == [OBJECT_STORAGE_SERVICE_ID]


@pytest.mark.asyncio
async def test_object_storage_is_routed_at_the_service_gateway(network: CloudNetwork) -> None:
    """Not out through the internet gateway, which is where the default routes go.

    Object Storage answers on public addresses, so the traffic leaves either
    way; the difference is whether it is metered egress.
    """
    rules = await network.route_table.route_rules.future()
    service_gateway = await network.service_gateway.id.future()
    internet_gateway = await network.internet_gateway.id.future()

    assert rules is not None
    # The two gateways are two resources, or "which one" is no question at all.
    assert service_gateway != internet_gateway
    by_destination = {rule.destination: (rule.destination_type, rule.network_entity_id) for rule in rules}
    assert by_destination[OBJECT_STORAGE_CIDR] == ('SERVICE_CIDR_BLOCK', service_gateway)
    assert by_destination['0.0.0.0/0'] == ('CIDR_BLOCK', internet_gateway)
    assert by_destination['::/0'] == ('CIDR_BLOCK', internet_gateway)


#: The whole internet in each family, the two sources and destinations the
#: subnet's list names (physical.md §1).
ANYWHERE = {'0.0.0.0/0', '::/0'}


@pytest.mark.asyncio
async def test_the_subnet_carries_the_declared_list_and_no_other(network: CloudNetwork) -> None:
    """A subnet that names no list carries the VCN's default one, whose rules OCI chose.

    That list admits SSH and drops everything else the nodes serve, so the
    subnet's posture would be the platform's rather than this program's.
    """
    assert await network.subnet.security_list_ids.future() == [await network.security_list.id.future()]


@pytest.mark.asyncio
async def test_the_list_admits_everything_both_ways_without_tracking_state(network: CloudNetwork) -> None:
    """Every protocol, both families, both directions, and no connection tracking.

    The node's own firewall is the only filter (physical.md §2), so the list
    decides nothing, and a stateful rule would spend a connection-tracking
    table on every VNIC in the subnet to decide it.
    """
    ingress = await network.security_list.ingress_security_rules.future()
    egress = await network.security_list.egress_security_rules.future()

    assert ingress is not None
    assert egress is not None
    admitted = [
        (rule.protocol, rule.source, rule.source_type, rule.stateless, rule.tcp_options, rule.udp_options)
        for rule in ingress
    ]
    released = [
        (rule.protocol, rule.destination, rule.destination_type, rule.stateless, rule.tcp_options, rule.udp_options)
        for rule in egress
    ]
    assert sorted(admitted) == sorted(('all', cidr, 'CIDR_BLOCK', True, None, None) for cidr in ANYWHERE)
    assert sorted(released) == sorted(('all', cidr, 'CIDR_BLOCK', True, None, None) for cidr in ANYWHERE)
