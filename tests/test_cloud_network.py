"""The cluster VCN's shape, asserted against Pulumi's mock provider.

What matters here is what cannot be seen by reading a diff later: that the
subnet's IPv6 block is derived from the prefix OCI assigns rather than
declared, that Object Storage traffic is routed at the service gateway
instead of out through the internet gateway, that the subnet carries a list
of this program's own rather than the one OCI would attach in its absence,
and what that list admits: everything over IPv4, and over IPv6 everything a
node serves except the ICMPv6 a flood from the internet would starve
neighbor discovery with.
"""

import ipaddress
from collections.abc import Sequence
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, root_of, run_with, the_run_failed_with
from pulumi_oci.core.outputs import SecurityListEgressSecurityRule, SecurityListIngressSecurityRule

from kluster.components.cloud import ICMPV6_FROM_ANYWHERE, CloudNetwork, NoObjectStorageService

VCN_IPV6_PREFIX = '2603:c020:8000:1200::/56'
OBJECT_STORAGE_SERVICE_ID = 'ocid1.service.oc1.phx.objectstorage'
OBJECT_STORAGE_CIDR = 'oci-phx-objectstorage'

#: A service the region offers beside Object Storage, listed first so a pick
#: that took the first entry would take it.
OTHER_SERVICE = {'id': 'ocid1.service.oc1.phx.other', 'name': 'All PHX Services', 'cidrBlock': 'all-phx'}

#: The region's catalog as the account reads it back.
SERVICES = [
    OTHER_SERVICE,
    {'id': OBJECT_STORAGE_SERVICE_ID, 'name': 'OCI PHX Object Storage', 'cidrBlock': OBJECT_STORAGE_CIDR},
]


class Oci(Recorder):
    """The two answers this suite is about: the assigned prefix, and the catalog.

    Both are things the account decides rather than the program, which is why
    the subnet's block has to be derived from the first and the gateway's
    service picked out of the second.
    """

    def __init__(self, services: Sequence[dict[str, str]] = SERVICES) -> None:
        super().__init__()
        #: The catalog `getServices` answers with, which a case replaces to
        #: stand in for a region that lacks a service.
        self.services: Sequence[dict[str, str]] = services

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'oci:Core/vcn:Vcn':
            return {'ipv6cidrBlocks': [VCN_IPV6_PREFIX]}
        return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'oci:Core/getServices:getServices':
            return {'services': list(self.services)}
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


@pytest.mark.asyncio
async def test_a_region_without_object_storage_is_refused() -> None:
    """Guessing would put a service other than Object Storage on the service gateway, and on the route to it.

    The service is picked out of the catalog when the gateway's input
    resolves, so that is where the refusal is: in the gateway's service and
    in the route that takes Object Storage traffic to it.
    """
    catalog = [OTHER_SERVICE, {'id': 'ocid1.service.oc1.phx.vault', 'name': 'OCI PHX Vault', 'cidrBlock': 'vault'}]
    _ = await run_with(Oci(services=catalog), stack='physical')
    network = CloudNetwork('kluster', compartment_id='ocid1.compartment.test')

    refusals: list[NoObjectStorageService] = []
    for read in (network.service_gateway.services, network.route_table.route_rules):
        with pytest.raises(ValueError) as failed:
            _ = await read.future()
        # The output fails with the SDK's exception around the program's own,
        # which names every service the region did offer.
        refusal = root_of(failed.value, NoObjectStorageService)
        assert refusal.services == tuple(service['name'] for service in catalog)
        refusals.append(refusal)
    # And the run fails on it, as the deployment would: the registrations of
    # the resources those inputs belong to meet the same refusal.
    assert await the_run_failed_with(NoObjectStorageService) in refusals


@pytest.mark.asyncio
async def test_the_subnet_carries_the_declared_list_and_no_other(network: CloudNetwork) -> None:
    """A subnet that names no list carries the VCN's default one, whose rules OCI chose.

    That list admits SSH and drops everything else the nodes serve, so the
    subnet's posture would be the platform's rather than this program's.
    """
    assert await network.subnet.security_list_ids.future() == [await network.security_list.id.future()]


# What the list admits is asked of it the way OCI decides it: a packet passes
# when any one rule matches it (docs.oracle.com, Networking, "Security
# Lists"). A rule matches on its protocol (`all` or the IANA number), on its
# source block, which carries the family, and for ICMP on its type and code
# where it names them. A question takes one of two forms. `admits` asks
# whether every packet of a kind passes — every port, every code — and a rule
# narrowed to some ports or to one code does not answer it yes. `admits_any`
# asks whether any packet of the kind passes, and such a rule answers it yes:
# a rule admitting neighbor solicitations of code 0 admits all of them, since
# that is the type's only code (RFC 4861 §4.3). Positive cases ask the first,
# negative cases the second, so neither can pass on a rule the model skipped.

TCP, UDP, ICMP, ICMPV6, GRE = 6, 17, 1, 58, 47

#: Sources the cases send from: a host on the internet in each family, and a
#: neighbor on the node's own link speaking from its link-local address.
INTERNET_V4 = '198.51.100.7'
INTERNET_V6 = '2001:db8:4::7'
ON_LINK = 'fe80::1'
#: Another instance on the subnet, inside the /64 the subnet takes from the
#: VCN's prefix; and an address inside that prefix but outside the subnet.
ON_SUBNET = '2603:c020:8000:1200::5'
OFF_SUBNET = '2603:c020:8000:1201::5'

#: ICMPv6 types (RFC 4443, RFC 4861): the errors, echo, and neighbor discovery.
ERRORS = (1, 2, 3, 4)
ECHO_REQUEST, ECHO_REPLY = 128, 129
NEIGHBOR_DISCOVERY = (133, 134, 135, 136, 137)


def _matches(
    rule: SecurityListIngressSecurityRule | SecurityListEgressSecurityRule,
    peer: str,
    protocol: int,
    icmp_type: int | None,
    *,
    every: bool,
) -> bool:
    block = rule.source if isinstance(rule, SecurityListIngressSecurityRule) else rule.destination
    if rule.protocol != 'all' and int(rule.protocol) != protocol:
        return False
    # `in` across families is False, which is how a block carries its family.
    if ipaddress.ip_address(peer) not in ipaddress.ip_network(block):
        return False
    narrowed = rule.tcp_options is not None or rule.udp_options is not None
    options = rule.icmp_options
    if options is not None:
        if icmp_type is not None and options.type != icmp_type:
            return False
        narrowed = narrowed or icmp_type is None or options.code is not None
    return not (every and narrowed)


def admits(
    rules: Sequence[SecurityListIngressSecurityRule], source: str, protocol: int, icmp_type: int | None = None
) -> bool:
    """Every packet of the kind passes: every port, and every code of `icmp_type` (or every type)."""
    return any(_matches(rule, source, protocol, icmp_type, every=True) for rule in rules)


def admits_any(
    rules: Sequence[SecurityListIngressSecurityRule], source: str, protocol: int, icmp_type: int | None = None
) -> bool:
    """Some packet of the kind passes: some port, or some code of `icmp_type` (or some type)."""
    return any(_matches(rule, source, protocol, icmp_type, every=False) for rule in rules)


@pytest_asyncio.fixture
async def ingress(network: CloudNetwork) -> Sequence[SecurityListIngressSecurityRule]:
    rules = await network.security_list.ingress_security_rules.future()
    assert rules is not None
    return rules


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', [TCP, UDP])
async def test_every_ipv6_port_the_nodes_serve_is_admitted_from_the_internet(
    ingress: Sequence[SecurityListIngressSecurityRule], protocol: int
) -> None:
    """The node's firewall decides ports (physical.md §2); the list narrows none."""
    assert admits(ingress, INTERNET_V6, protocol)


@pytest.mark.asyncio
@pytest.mark.parametrize(('protocol', 'icmp_type'), [(TCP, None), (UDP, None), (ICMP, 8), (GRE, None)])
async def test_ipv4_is_admitted_whole(
    ingress: Sequence[SecurityListIngressSecurityRule], protocol: int, icmp_type: int | None
) -> None:
    """IPv4 has no counterpart to the starvation: ARP never reaches the node's firewall."""
    assert admits(ingress, INTERNET_V4, protocol, icmp_type)


@pytest.mark.asyncio
@pytest.mark.parametrize('icmp_type', ERRORS)
async def test_icmpv6_errors_are_admitted_from_the_internet(
    ingress: Sequence[SecurityListIngressSecurityRule], icmp_type: int
) -> None:
    """A stateless list admits no reply by its connection, so the errors need rules of their own.

    Packet too big (2) is path MTU discovery: without it a TCP connection
    across a link narrower than the node's stalls once its segments fill.
    """
    assert admits(ingress, INTERNET_V6, ICMPV6, icmp_type)


@pytest.mark.asyncio
@pytest.mark.parametrize('icmp_type', [ECHO_REQUEST, ECHO_REPLY])
async def test_ping_answers_from_the_internet_both_ways(
    ingress: Sequence[SecurityListIngressSecurityRule], icmp_type: int
) -> None:
    """Request so the node answers ping, reply so it hears answers to its own.

    From anywhere, because the operator's address is dynamic — which is why
    an echo flood is the starvation residual that remains (security-audit.md M3).
    """
    assert admits(ingress, INTERNET_V6, ICMPV6, icmp_type)


@pytest.mark.asyncio
@pytest.mark.parametrize('icmp_type', [*NEIGHBOR_DISCOVERY, 130, 143])
async def test_every_icmpv6_type_is_admitted_from_the_link(
    ingress: Sequence[SecurityListIngressSecurityRule], icmp_type: int
) -> None:
    """Neighbor discovery and the router advertisement that carries the node's default route.

    Multicast listener reports (130, 143) are link-scoped the same way.
    """
    assert admits(ingress, ON_LINK, ICMPV6, icmp_type)


@pytest.mark.asyncio
@pytest.mark.parametrize('icmp_type', NEIGHBOR_DISCOVERY)
async def test_neighbor_discovery_is_admitted_from_the_subnets_own_block_and_no_wider(
    ingress: Sequence[SecurityListIngressSecurityRule], icmp_type: int
) -> None:
    """A node resolving another node on the subnet may solicit from its global address.

    The block admitted is the subnet's, derived from the prefix OCI assigns:
    a neighbor of the subnet's in the same VCN prefix is still off the link.
    """
    assert admits(ingress, ON_SUBNET, ICMPV6, icmp_type)
    assert not admits_any(ingress, OFF_SUBNET, ICMPV6, icmp_type)


@pytest.mark.asyncio
async def test_no_other_icmpv6_type_is_admitted_from_the_internet(
    ingress: Sequence[SecurityListIngressSecurityRule],
) -> None:
    """The narrowing itself: neighbor discovery's types, among every other, do not pass from off the link.

    Each one admitted would be one more way to spend the shared allowance
    neighbor discovery needs.
    """
    allowed = set(ICMPV6_FROM_ANYWHERE)
    admitted = {icmp_type for icmp_type in range(256) if admits_any(ingress, INTERNET_V6, ICMPV6, icmp_type)}

    assert admitted <= allowed


@pytest.mark.asyncio
async def test_no_other_ipv6_protocol_is_admitted_from_the_internet(
    ingress: Sequence[SecurityListIngressSecurityRule],
) -> None:
    """IPv6 from the internet is TCP, UDP and those ICMPv6 types, not `all`."""
    assert not admits_any(ingress, INTERNET_V6, GRE)


@pytest.mark.asyncio
async def test_no_ingress_rule_tracks_connections(ingress: Sequence[SecurityListIngressSecurityRule]) -> None:
    """A stateful rule would spend a connection-tracking table on every VNIC in the subnet."""
    assert ingress
    assert all(rule.stateless for rule in ingress)


@pytest.mark.asyncio
@pytest.mark.parametrize('destination', [INTERNET_V4, INTERNET_V6, ON_LINK])
@pytest.mark.parametrize(('protocol', 'icmp_type'), [(TCP, None), (UDP, None), (ICMPV6, 135), (GRE, None)])
async def test_everything_leaves_without_tracking_state(
    network: CloudNetwork, destination: str, protocol: int, icmp_type: int | None
) -> None:
    """Egress is not narrowed: a node's own traffic leaves whatever it is and wherever it goes."""
    egress = await network.security_list.egress_security_rules.future()

    assert egress is not None
    assert all(rule.stateless for rule in egress)
    assert any(_matches(rule, destination, protocol, icmp_type, every=True) for rule in egress)
