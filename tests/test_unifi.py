"""The cluster's network on the gateway and the rules around it, against mocks.

Firewall rules are the worst thing to review from a diff: the interesting
properties are relationships between rules — which one is evaluated first,
which one names a moving target, which one is missing — and none of those are
visible in a rendered resource. So the suite asserts them directly:

-   the census is *exactly* the design's, because a rule on the controller
    that nobody declared is drift and a rule declared beyond the census is
    the same thing arriving the other way;
-   the drop is preceded by its allow, and both precede the predefined
    policies of the pair, which is the difference between a tightened VLAN
    and a severed television — and on the way *into* the cluster zone the
    order is the other way round, the IoT drop ahead of the zone-wide allow
    that would otherwise answer for it;
-   the pool is named through address groups and never as a literal, and the
    only literal in the whole census is the media VIP the design says is one;
-   the IoT rules are sourced from the IoT VLAN alone, not from the internal
    zone at large, which is what keeps the rest of the internal side's own
    access to the cluster intact;
-   the cluster VLAN is a network object with no DHCP server, alone in a zone
    of its own, and everything that names the worker names that zone.

No controller is contacted: the boundary is Pulumi's mock monitor, which
answers the zone lookups and hands back the inputs each resource was given.
"""

import inspect
from collections.abc import Mapping
from ipaddress import IPv4Address, IPv4Interface, IPv6Address
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.gateway import unifi

NAME = 'kluster'
API_URL = 'https://gateway.invalid'
API_KEY = 'unifi-api-key'
SITE = 'default'
WORKER_GUA = '2001:db8:1:70::10'


class Controller(Recorder):
    """A monitor that answers the zone lookups; every zone exists and is empty."""

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'unifi:index/getFirewallZone:getFirewallZone':
            name = str(cast('dict[str, Any]', args.args)['name'])
            return {'id': f'zone-{name}', 'name': name, 'networks': [], 'site': SITE}
        return {}


@pytest_asyncio.fixture(autouse=True)
async def mocks() -> Controller:
    pulumi.runtime.set_all_config({f'kluster:{unifi.API_KEY}': API_KEY})
    return await run_with(Controller(), stack='physical')


def build(static_hosts: Mapping[str, IPv4Address | IPv6Address] | None = None) -> unifi.SiteFirewall:
    return unifi.SiteFirewall(
        NAME,
        api_url=API_URL,
        site=SITE,
        worker_gua=WORKER_GUA,
        static_hosts={} if static_hosts is None else static_hosts,
    )


@pytest.mark.asyncio
async def test_the_census_is_exactly_the_designed_set(mocks: Controller) -> None:
    """Nothing beyond the census, and nothing missing from it.

    The design's sentence is "a controller rule not on this census is drift";
    this is that sentence as an assertion, in the one direction a program can
    make it — that the program declares the census and stops there.
    """
    async with declaring():
        build()

    by_type = {typ: sorted(mocks.names(typ)) for typ in mocks.types}

    assert by_type['unifi:index/firewallGroup:FirewallGroup'] == [
        f'{NAME}-pool-v4',
        f'{NAME}-pool-v6',
    ]
    assert sorted(by_type['unifi:index/firewallZonePolicy:FirewallZonePolicy']) == [
        f'{NAME}-cluster-egress',
        f'{NAME}-cluster-internal',
        f'{NAME}-internal-cluster',
        f'{NAME}-iot-cluster-v4',
        f'{NAME}-iot-cluster-v6',
        f'{NAME}-iot-media-v4',
        f'{NAME}-iot-media-v6',
        f'{NAME}-iot-pool-v4',
        f'{NAME}-iot-pool-v6',
        f'{NAME}-peer-v6',
    ]
    # One order resource per zone pair that carries a policy, and no pair
    # left without one: a policy whose position is whatever creation produced
    # is a policy the design does not actually own.
    assert sorted(by_type['unifi:index/firewallZonePolicyOrder:FirewallZonePolicyOrder']) == [
        f'{NAME}-cluster-egress-order',
        f'{NAME}-cluster-internal-order',
        f'{NAME}-internal-cluster-order',
        f'{NAME}-iot-pool-order',
        f'{NAME}-peer-order',
    ]
    # One network and one zone, both the cluster's: the pool is deliberately
    # neither, and no other network on the site is this program's to declare.
    assert by_type['unifi:index/network:Network'] == [f'{NAME}-network']
    assert by_type['unifi:index/firewallZone:FirewallZone'] == [f'{NAME}-zone']
    # The only port forward on the device.
    assert by_type['unifi:index/portForward:PortForward'] == [f'{NAME}-peer-v4']
    assert 'unifi:index/dnsRecord:DnsRecord' not in by_type


@pytest.mark.asyncio
async def test_the_cluster_vlan_is_a_network_object_the_gateway_terminates() -> None:
    """The opposite decision from the pool, and the reason nodes are policeable.

    The pool is deliberately no object, because an object would fight the host
    routes the cluster advertises. The VLAN is one for the mirror-image reason:
    a subnet the controller has an object for is a subnet a policy can name,
    and the population sharing the untagged LAN could never be named at all.
    """
    firewall = build()

    assert await firewall.network.name.future() == conventions.gateway.UNIFI_NETWORK_CLUSTER
    assert await firewall.network.vlan_id.future() == conventions.CLUSTER_VLAN.vlan_id
    # The controller spells the addressing as its own interface address plus
    # the prefix, so this field carries the gateway and the subnet at once.
    subnet = await firewall.network.subnet.future()
    assert subnet is not None
    assert IPv4Interface(subnet).ip == conventions.CLUSTER_VLAN.require_gateway()
    assert IPv4Interface(subnet).network == conventions.CLUSTER_VLAN.v4
    # `vlan-only` would describe a VLAN the gateway does not terminate, which
    # is a VLAN with no default route, no BGP peer and no zone.
    assert await firewall.network.purpose.future() == 'corporate'


@pytest.mark.asyncio
async def test_the_cluster_vlan_hands_out_no_leases() -> None:
    """Every node states its own address, so a lease is a second opinion.

    Three other places treat the worker's address as a constant — the FRR
    neighbour statement, the port forward and day 1's apid endpoint — and a
    DHCP server on this VLAN is exactly the thing that could hand out a
    different one.
    """
    firewall = build()

    assert await firewall.network.dhcp_enabled.future() is False
    assert await firewall.network.dhcp_v6_enabled.future() is False
    # IPv6 is not switched off with it: the design's worker GUA is a SLAAC
    # address formed from what this network advertises, which needs the
    # delegated prefix and the router advertisements that carry it.
    assert await firewall.network.ipv6_ra_enable.future() is True
    assert await firewall.network.ipv6_interface_type.future() == 'pd'


@pytest.mark.asyncio
async def test_the_cluster_sits_alone_in_a_zone_this_program_creates() -> None:
    """A zone of its own is what makes the nodes separately policeable.

    The two stock zones are looked up because they are the controller's; this
    one is declared because it exists for the cluster. Membership is stated
    once, from the network — the controller takes the association from either
    end, and two ends managing it fight over it.
    """
    firewall = build()

    assert await firewall.zone.name.future() == conventions.gateway.UNIFI_ZONE_CLUSTER
    assert await firewall.network.firewall_zone_id.future() == f'{NAME}-zone_id'
    assert await firewall.zone.networks.future() is None


@pytest.mark.asyncio
async def test_the_new_zone_is_given_the_egress_its_nodes_cannot_work_without() -> None:
    """A zone the controller has just been told about is denied in both directions.

    So this policy is not a tightening: it is what makes the zone usable at
    all. A cluster node's control plane is in a cloud region, and a node that
    cannot leave the site cannot join the cluster it is a member of.
    """
    firewall = build()

    assert await firewall.cluster_egress.action.future() == 'ALLOW'
    source = await firewall.cluster_egress.source.future()
    destination = await firewall.cluster_egress.destination.future()
    assert source is not None and destination is not None
    assert source.zone_id == f'{NAME}-zone_id'
    assert destination.zone_id == f'zone-{unifi.ZONE_EXTERNAL}'
    # The whole zone, both families and every protocol: what a node reaches on
    # the internet is not a decision the gateway is in a position to make.
    assert source.ips is None
    assert await firewall.cluster_egress.ip_version.future() == 'BOTH'
    assert await firewall.cluster_egress.protocol.future() == 'all'
    assert await firewall.cluster_egress.auto_allow_return_traffic.future() is True


@pytest.mark.asyncio
async def test_the_cluster_zone_talks_to_the_home_in_both_directions() -> None:
    """The two directions between the new zone and the stock internal one.

    Outward carries the recorded workload dependencies — the home-automation
    API among them — and inward is the reachability the nodes already had
    while they shared the untagged LAN. Both are whole-zone, both families,
    every protocol: what a node's workloads may call is decided where those
    workloads are declared, and narrowing it here would put that decision
    behind a gateway credential.
    """
    firewall = build()

    outward = firewall.cluster_internal
    assert await outward.action.future() == 'ALLOW'
    source = await outward.source.future()
    destination = await outward.destination.future()
    assert source is not None and destination is not None
    assert source.zone_id == f'{NAME}-zone_id'
    assert destination.zone_id == f'zone-{unifi.ZONE_INTERNAL}'
    assert source.ips is None and destination.ips is None

    inward = firewall.internal_cluster
    assert await inward.action.future() == 'ALLOW'
    source = await inward.source.future()
    destination = await inward.destination.future()
    assert source is not None and destination is not None
    assert source.zone_id == f'zone-{unifi.ZONE_INTERNAL}'
    assert destination.zone_id == f'{NAME}-zone_id'
    # No source literal: the carve-out is a separate drop ahead of it, not a
    # narrowing of this one, so that what it excludes is a rule of its own.
    assert source.ips is None

    for policy in (outward, inward):
        assert await policy.ip_version.future() == 'BOTH'
        assert await policy.protocol.future() == 'all'
        # An allow whose return leg is classified on the reverse pair and not
        # admitted there is an allow that admits a request and drops the reply.
        assert await policy.auto_allow_return_traffic.future() is True


@pytest.mark.asyncio
async def test_the_iot_vlan_is_carved_out_of_the_way_into_the_cluster() -> None:
    """The node subnet is what the LAN's least-trusted population loses.

    apid, the kubelet and the BGP session live on it, and the one recorded
    IoT-originated dependency — a television reaching the media VIP — targets
    the pool instead, so this drop severs nothing known. Sourced from the
    VLAN and not from the internal zone at large, or it would take the
    operator's own machines with it.
    """
    firewall = build()

    families = (
        (firewall.iot_cluster_v4, 'IPV4', str(conventions.IOT_VLAN.v4)),
        (firewall.iot_cluster_v6, 'IPV6', str(conventions.IOT_VLAN.v6)),
    )
    for policy, version, expected in families:
        assert await policy.action.future() == 'BLOCK'
        assert await policy.ip_version.future() == version
        assert await policy.protocol.future() == 'all'
        source = await policy.source.future()
        destination = await policy.destination.future()
        assert source is not None and destination is not None
        assert source.zone_id == f'zone-{unifi.ZONE_INTERNAL}'
        assert source.ips == [expected]
        # The whole zone on the far side: the node subnet *is* a network
        # object, so unlike the pool it needs no group to be named.
        assert destination.zone_id == f'{NAME}-zone_id'
        assert destination.ips is None
        assert destination.ip_group_id is None


@pytest.mark.asyncio
async def test_the_pinhole_lands_in_the_cluster_zone_rather_than_the_internal_one() -> None:
    """The rule follows the worker, which is no longer on the internal side.

    A pinhole still pointing at the internal zone would be admitted into a
    zone the destination address is not in — a rule that reads as if the peer
    port were published and quietly publishes nothing.
    """
    firewall = build()

    # Built with an address, so the conditional half of the census is here.
    assert firewall.peer_v6 is not None
    destination = await firewall.peer_v6.destination.future()
    assert destination is not None
    assert destination.zone_id == f'{NAME}-zone_id'

    # The IoT rules are untouched by the move: the pool they name is still no
    # object at all, so they still fall through to the uplink pair.
    for policy in (firewall.iot_media_v4, firewall.iot_pool_v4):
        iot_destination = await policy.destination.future()
        assert iot_destination is not None
        assert iot_destination.zone_id == f'zone-{unifi.ZONE_EXTERNAL}'


@pytest.mark.asyncio
async def test_the_pool_is_named_by_group_and_the_groups_are_single_family() -> None:
    """One subnet, two groups, because a group holds one address family.

    The consequence worth guarding is the failure mode: a v4 CIDR quietly
    accepted into the v6 group would produce a rule that matches nothing and
    reads as if it matches everything.
    """
    firewall = build()

    assert await firewall.pool_v4.type.future() == 'address-group'
    assert await firewall.pool_v4.members.future() == [str(conventions.LAN_POOL.v4)]
    assert await firewall.pool_v4.name.future() == conventions.LAN_POOL.group_v4

    assert await firewall.pool_v6.type.future() == 'ipv6-address-group'
    assert await firewall.pool_v6.members.future() == [str(conventions.LAN_POOL.v6)]
    assert await firewall.pool_v6.name.future() == conventions.LAN_POOL.group_v6

    for policy in (firewall.iot_pool_v4, firewall.iot_pool_v6):
        destination = await policy.destination.future()
        assert destination is not None
        # Through the group, never as a literal: the pool is not a network
        # object on the controller and never will be.
        assert destination.ip_group_id is not None
        assert not destination.ips


@pytest.mark.asyncio
async def test_only_the_media_vip_is_reachable_from_the_iot_vlan() -> None:
    """The firewall names the media VIP and no application.

    Which applications the IoT VLAN may consume is decided by attaching a
    route to the media Gateway. If any application address ever appeared
    here, that decision would have moved into the firewall and every
    membership change would become a gateway credential's problem.
    """
    firewall = build()

    for policy, vip in (
        (firewall.iot_media_v4, conventions.LAN_POOL.media_vip.v4),
        (firewall.iot_media_v6, conventions.LAN_POOL.media_vip.v6),
    ):
        assert await policy.action.future() == 'ALLOW'
        destination = await policy.destination.future()
        assert destination is not None
        assert destination.ips == [str(vip)]
        assert destination.port == unifi.MEDIA_PORT
        assert await policy.protocol.future() == 'tcp'
        # The reverse zone pair is where the answer is classified; an allow
        # without it admits a request and drops the reply.
        assert await policy.auto_allow_return_traffic.future() is True


@pytest.mark.asyncio
async def test_every_pool_rule_is_sourced_from_the_iot_vlan_alone() -> None:
    """The drop must not reach the rest of the internal side.

    The home VLANs share the internal zone, so a rule that named the zone
    instead of the VLAN would cut the homelab host off from the cluster's own
    service addresses.
    """
    firewall = build()

    families = (
        (firewall.iot_media_v4, str(conventions.IOT_VLAN.v4)),
        (firewall.iot_pool_v4, str(conventions.IOT_VLAN.v4)),
        (firewall.iot_media_v6, str(conventions.IOT_VLAN.v6)),
        (firewall.iot_pool_v6, str(conventions.IOT_VLAN.v6)),
    )
    for policy, expected in families:
        source = await policy.source.future()
        assert source is not None
        assert source.ips == [expected]

    assert str(conventions.IOT_VLAN.v6).endswith(':90::/64'), 'the IoT ULA follows the VLAN numbering scheme'


#: Each ordering resource, the zone pair it orders, and the policies it puts
#: ahead of that pair's predefined rules. Four rows because the design uses
#: four pairs; the claim is one, and it is the same for every one of them.
ORDERINGS = [
    (
        'pool_order',
        f'zone-{unifi.ZONE_INTERNAL}',
        f'zone-{unifi.ZONE_EXTERNAL}',
        [f'{NAME}-iot-media-v4_id', f'{NAME}-iot-media-v6_id', f'{NAME}-iot-pool-v4_id', f'{NAME}-iot-pool-v6_id'],
    ),
    ('peer_order', f'zone-{unifi.ZONE_EXTERNAL}', f'{NAME}-zone_id', [f'{NAME}-peer-v6_id']),
    ('cluster_egress_order', f'{NAME}-zone_id', f'zone-{unifi.ZONE_EXTERNAL}', [f'{NAME}-cluster-egress_id']),
    ('cluster_internal_order', f'{NAME}-zone_id', f'zone-{unifi.ZONE_INTERNAL}', [f'{NAME}-cluster-internal_id']),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('attribute', 'source', 'destination', 'ahead'), ORDERINGS)
async def test_a_pair_evaluates_its_declared_policies_before_its_predefined_ones(
    attribute: str, source: str, destination: str, ahead: list[str]
) -> None:
    """Order is declared rather than inherited from creation order.

    Every pair carries a predefined accept or deny of its own, and it would
    otherwise answer first -- which makes a declared policy behind it a
    resource that changes nothing. None of this is observable in the policy
    resources themselves, which is why it is asserted here.
    """
    order = getattr(build(), attribute)

    assert order is not None
    assert await order.before_predefined_ids.future() == ahead
    assert await order.source_zone_id.future() == source
    assert await order.destination_zone_id.future() == destination


@pytest.mark.asyncio
async def test_nothing_is_ordered_behind_a_pairs_predefined_rules() -> None:
    """The complement of the rule above: `after` is the placement nothing wants."""
    firewall = build()

    for attribute, *_ in ORDERINGS:
        order = getattr(firewall, attribute)
        assert await order.after_predefined_ids.future() is None, attribute


@pytest.mark.asyncio
async def test_the_iot_drop_precedes_the_allow_that_would_otherwise_answer_for_it() -> None:
    """Inward, the order is the reverse of the pool pair's, and it has to be.

    On the pool pair the enumerated allow comes first because the drop behind
    it is the broad one. Here the broad rule is the *allow* — the whole
    internal zone into the cluster — so a drop declared after it never
    matches, and the carve-out reads as if it were in force while the IoT
    VLAN keeps reaching apid.
    """
    firewall = build()

    order = firewall.internal_cluster_order
    assert await order.before_predefined_ids.future() == [
        f'{NAME}-iot-cluster-v4_id',
        f'{NAME}-iot-cluster-v6_id',
        f'{NAME}-internal-cluster_id',
    ]
    assert await order.after_predefined_ids.future() is None
    assert await order.source_zone_id.future() == f'zone-{unifi.ZONE_INTERNAL}'
    assert await order.destination_zone_id.future() == f'{NAME}-zone_id'


@pytest.mark.asyncio
async def test_the_pool_rules_are_declared_on_the_uplink_pair() -> None:
    """The pool sits in no zone ipset, so its traffic is classified as uplink.

    This is the one piece of measured device behaviour the whole IoT half
    depends on. Declaring these rules on the internal-to-internal pair would
    produce four resources that apply to nothing.
    """
    firewall = build()

    for policy in (firewall.iot_media_v4, firewall.iot_media_v6, firewall.iot_pool_v4, firewall.iot_pool_v6):
        source = await policy.source.future()
        destination = await policy.destination.future()
        assert source is not None and destination is not None
        assert source.zone_id == f'zone-{unifi.ZONE_INTERNAL}'
        assert destination.zone_id == f'zone-{unifi.ZONE_EXTERNAL}'


@pytest.mark.asyncio
async def test_both_halves_of_the_peer_flow_name_the_same_port_and_host() -> None:
    """The pinhole and the forward are one flow in two address families.

    A peer that reaches the same endpoint over IPv4 and IPv6 sees one
    participant; a mismatch between the two halves is invisible until a
    transfer is slow for reasons nobody can name.
    """
    firewall = build()

    assert firewall.peer_v6 is not None
    pinhole = await firewall.peer_v6.destination.future()
    assert pinhole is not None

    # The same port on both halves, each stated against the one constant that
    # decides it -- the provider hands the v6 side a number and the v4 side a
    # string, so the two cannot be compared to each other directly.
    port = conventions.QBITTORRENT_PEER_PORT
    assert pinhole.port == port
    assert await firewall.peer_v4.dst_port.future() == str(port)
    assert await firewall.peer_v4.fwd_port.future() == str(port)

    # The same host, in the address each family reaches it at: the worker's
    # global address on one side, its LAN address on the other.
    assert pinhole.ips == [WORKER_GUA], 'the pinhole matches a literal address, the prefix rotating'
    assert await firewall.peer_v6.ip_version.future() == 'IPV6'
    assert await firewall.peer_v4.fwd_ip.future() == str(conventions.HOMELAB_NODE_IPV4)
    assert await firewall.peer_v4.src_ip.future() == 'any'


@pytest.mark.asyncio
async def test_the_controller_credential_is_an_api_key_on_a_bounded_provider(mocks: Controller) -> None:
    """A key of its own, and a retry budget that cannot lock the account out.

    The controller's login rate limit is account-wide, so an unbounded retry
    from a runner is an outage for the people using the console. The number
    is small on purpose and asserted so that raising it is a visible edit.

    Read off the wire rather than off the resource, because a provider's
    settings are serialized as strings and the strings are what the run
    carries.
    """
    async with declaring():
        build()

    settings = mocks.inputs_of(f'{NAME}-unifi')

    assert settings['apiUrl'] == API_URL
    assert settings['site'] == SITE
    assert settings['httpMaxRetries'] == str(unifi.HTTP_MAX_RETRIES)
    assert unifi.HTTP_MAX_RETRIES <= 3

    key = settings['apiKey']
    assert isinstance(key, dict), 'the key is classified as a secret, so it is never plain text in state'
    assert key['value'] == API_KEY

    # No password and no user name: the API key is the whole credential, and
    # a local administrator's password would authenticate far more than this.
    assert 'password' not in settings
    assert 'username' not in settings


@pytest.mark.asyncio
async def test_an_empty_roll_declares_no_static_host() -> None:
    """The device name plane is DHCP-derived; a static entry is an exception.

    Every service is reached by its public name through the split-horizon
    rewrites, so a host entry here would be a second naming plane to keep in
    step with the first. Which entries there are is the stack program's census
    (`test_physical_stack`); what this pins is that an empty roll declares
    nothing rather than falling back to one of the component's own.
    """
    assert build().static_hosts == {}


@pytest.mark.asyncio
async def test_a_static_host_is_typed_by_the_family_of_its_address() -> None:
    """The exception the census does not use, exercised so it works the day it is.

    The record type is the whole of what the component decides here, and it
    decides it from the address rather than from a second argument.
    """
    firewall = build(
        {
            'printer.home.arpa': IPv4Address('192.168.80.9'),
            'sensor.iot.home.arpa': IPv6Address('fd1a:665f:8bcb:90::9'),
        }
    )

    assert set(firewall.static_hosts) == {'printer.home.arpa', 'sensor.iot.home.arpa'}
    assert await firewall.static_hosts['printer.home.arpa'].type.future() == 'A'
    assert await firewall.static_hosts['printer.home.arpa'].record.future() == '192.168.80.9'
    assert await firewall.static_hosts['sensor.iot.home.arpa'].type.future() == 'AAAA'
    assert await firewall.static_hosts['sensor.iot.home.arpa'].record.future() == 'fd1a:665f:8bcb:90::9'


##
## The controller connection
##


@pytest.mark.asyncio
async def test_the_controller_key_is_read_where_the_provider_is_built() -> None:
    """The key that can rewrite the home\'s firewall reaches one line.

    It authorizes changes to the site\'s policy and nothing else in the program
    has a use for it, so it is read at the line that builds the provider rather
    than threaded through a signature (rfc-002 §8.1). What is still an argument
    is the endpoint: which address the controller answers on is a site fact the
    stack derives, not a credential.
    """
    firewall = build()
    assert await firewall.provider.api_key.future() == API_KEY
    assert 'api_key' not in inspect.signature(unifi.SiteFirewall.__init__).parameters


@pytest.mark.asyncio
async def test_every_controller_resource_and_lookup_is_signed_by_it(mocks: Controller) -> None:
    """One provider, inherited by the whole subtree and by the zone lookups.

    The zone, the network, the address groups, the policies and the port
    forward are children of the component that built the provider, so each
    takes it from its parent rather than naming it. The two zone lookups are
    invokes, which inherit only through a parent -- so they name one, and the
    provider they end up signing with is the same one.
    """
    async with declaring():
        _ = build()

    controller = [d for d in mocks.declared if d.typ.startswith('unifi:index/')]
    assert controller, 'the build declared no controller resources at all'
    for declaration in controller:
        assert f'{NAME}-unifi' in declaration.provider, f'{declaration.name} is not signed by the provider'
    assert f'{NAME}-unifi' in mocks.call_providers['unifi:index/getFirewallZone:getFirewallZone']
