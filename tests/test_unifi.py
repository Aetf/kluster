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
    and a severed television;
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

from collections.abc import Mapping
from ipaddress import IPv4Address, IPv4Interface, IPv6Address
from typing import Any, cast

import pulumi
import pulumi.runtime.settings
import pytest
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions
from kluster.gateway import unifi

NAME = 'kluster'
API_URL = 'https://gateway.invalid'
API_KEY = 'unifi-api-key'
SITE = 'default'
WORKER_GUA = '2001:db8:1:70::10'
PEER_PORT = 51413


class Mocks(pulumi.runtime.Mocks):
    """A monitor that answers the zone lookups and remembers what was declared."""

    def __init__(self) -> None:
        super().__init__()
        self.declared: list[tuple[str, str]] = []
        self.inputs: dict[str, dict[str, Any]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        properties = dict(cast('dict[str, Any]', args.inputs))
        self.declared.append((args.typ, args.name))
        self.inputs[args.name] = properties
        return args.name + '_id', properties

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token == 'unifi:index/getFirewallZone:getFirewallZone':
            name = str(cast('dict[str, Any]', args.args)['name'])
            return {'id': f'zone-{name}', 'name': name, 'networks': [], 'site': SITE}, []
        return {}, []


@pytest_asyncio.fixture(autouse=True)
async def mocks() -> Mocks:
    recorded = Mocks()
    pulumi.runtime.set_mocks(recorded, project='kluster', stack='physical', preview=False)
    # Registrations are dispatched onto a queue that lives in module state and
    # therefore outlives a test's event loop. Emptying it here is what lets
    # `settled` mean "this test's declarations" rather than "every declaration
    # any test ever made", half of them belonging to a loop that is closed.
    pulumi.runtime.settings._get_rpc_manager().clear()  # pyright: ignore[reportPrivateUsage]
    # A bridged SDK registers its own parameterized package before it may
    # register a resource, and it gates that on a feature flag read out of a
    # synchronous cache that only the async negotiation fills.
    _ = await pulumi.runtime.settings.monitor_supports_feature('parameterization')
    return recorded


async def settled() -> None:
    """Wait until every declaration this test made has reached the monitor.

    Only the registration half of Pulumi's own barrier: the other half drains
    a set of output tasks that is likewise module state, and a task left there
    by an earlier test belongs to an event loop that no longer exists.
    """
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def build(static_hosts: Mapping[str, IPv4Address | IPv6Address] | None = None) -> unifi.Firewall:
    return unifi.Firewall(
        NAME,
        api_url=API_URL,
        api_key=API_KEY,
        site=SITE,
        worker_gua=WORKER_GUA,
        peer_port=PEER_PORT,
        static_hosts=static_hosts,
    )


@pytest.mark.asyncio
async def test_the_census_is_exactly_the_designed_set(mocks: Mocks) -> None:
    """Nothing beyond the census, and nothing missing from it.

    The design's sentence is "a controller rule not on this census is drift";
    this is that sentence as an assertion, in the one direction a program can
    make it — that the program declares the census and stops there.
    """
    build()
    await settled()

    by_type: dict[str, list[str]] = {}
    for typ, name in mocks.declared:
        by_type.setdefault(typ, []).append(name)

    assert sorted(by_type['unifi:index/firewallGroup:FirewallGroup']) == [
        f'{NAME}-pool-v4',
        f'{NAME}-pool-v6',
    ]
    assert sorted(by_type['unifi:index/firewallZonePolicy:FirewallZonePolicy']) == [
        f'{NAME}-cluster-egress',
        f'{NAME}-iot-media-v4',
        f'{NAME}-iot-media-v6',
        f'{NAME}-iot-pool-v4',
        f'{NAME}-iot-pool-v6',
        f'{NAME}-peer-v6',
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

    assert await firewall.network.name.future() == conventions.UNIFI_NETWORK_CLUSTER
    assert await firewall.network.vlan_id.future() == conventions.CLUSTER_VLAN_ID
    # The controller spells the addressing as its own interface address plus
    # the prefix, so this field carries the gateway and the subnet at once.
    subnet = await firewall.network.subnet.future()
    assert subnet is not None
    assert IPv4Interface(subnet).ip == conventions.CLUSTER_VLAN_GATEWAY_V4
    assert IPv4Interface(subnet).network == conventions.CLUSTER_VLAN_V4
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

    assert await firewall.zone.name.future() == conventions.UNIFI_ZONE_CLUSTER
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
async def test_the_pinhole_lands_in_the_cluster_zone_rather_than_the_internal_one() -> None:
    """The rule follows the worker, which is no longer on the internal side.

    A pinhole still pointing at the internal zone would be admitted into a
    zone the destination address is not in — a rule that reads as if the peer
    port were published and quietly publishes nothing.
    """
    firewall = build()

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
    assert await firewall.pool_v4.members.future() == [str(conventions.LAN_POOL_V4)]
    assert await firewall.pool_v4.name.future() == conventions.UNIFI_GROUP_LAN_POOL_V4

    assert await firewall.pool_v6.type.future() == 'ipv6-address-group'
    assert await firewall.pool_v6.members.future() == [str(conventions.LAN_POOL_V6)]
    assert await firewall.pool_v6.name.future() == conventions.UNIFI_GROUP_LAN_POOL_V6

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
        (firewall.iot_media_v4, conventions.VIP_MEDIA_V4),
        (firewall.iot_media_v6, conventions.VIP_MEDIA_V6),
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
        (firewall.iot_media_v4, str(conventions.VLAN_IOT)),
        (firewall.iot_pool_v4, str(conventions.VLAN_IOT)),
        (firewall.iot_media_v6, str(unifi.VLAN_IOT_V6)),
        (firewall.iot_pool_v6, str(unifi.VLAN_IOT_V6)),
    )
    for policy, expected in families:
        source = await policy.source.future()
        assert source is not None
        assert source.ips == [expected]

    assert str(unifi.VLAN_IOT_V6).endswith(':90::/64'), 'the IoT ULA follows the VLAN numbering scheme'


def test_every_unique_local_prefix_is_numbered_after_its_own_third_octet() -> None:
    """One scheme, so a renumbering that moves only one family is visible here.

    Each /64 at the site carries the third octet of its IPv4 subnet as the
    digits of its last group. Nothing derives one from the other — they are
    two literals — which is exactly why the relation is worth asserting: the
    failure it guards against is a v4 subnet moved and its v6 left behind,
    which produces rules that match half a network.
    """
    pairs = (
        (conventions.CLUSTER_VLAN_V4, conventions.CLUSTER_VLAN_V6),
        (conventions.LAN_POOL_V4, conventions.LAN_POOL_V6),
        (conventions.VLAN_IOT, unifi.VLAN_IOT_V6),
    )
    for v4, v6 in pairs:
        octet = str(v4).split('.')[2]
        assert str(v6).endswith(f':{octet}::/64'), f'{v6} does not follow {v4}'
        assert v6.subnet_of(conventions.SITE_ULA)
        assert v6.prefixlen == 64


@pytest.mark.asyncio
async def test_both_allows_precede_both_drops_and_all_of_them_precede_the_predefined() -> None:
    """Order is the rule, and it is declared rather than inherited from creation.

    Two orderings matter and neither is observable in a resource: the allow
    ahead of the drop, and the whole group ahead of the predefined accept the
    uplink pair carries — which would otherwise answer first and make the
    drop unreachable.
    """
    firewall = build()

    assert await firewall.pool_order.before_predefined_ids.future() == [
        f'{NAME}-iot-media-v4_id',
        f'{NAME}-iot-media-v6_id',
        f'{NAME}-iot-pool-v4_id',
        f'{NAME}-iot-pool-v6_id',
    ]
    assert await firewall.pool_order.after_predefined_ids.future() is None
    assert await firewall.pool_order.source_zone_id.future() == f'zone-{unifi.ZONE_INTERNAL}'
    assert await firewall.pool_order.destination_zone_id.future() == f'zone-{unifi.ZONE_EXTERNAL}'

    assert await firewall.peer_order.before_predefined_ids.future() == [f'{NAME}-peer-v6_id']
    assert await firewall.peer_order.source_zone_id.future() == f'zone-{unifi.ZONE_EXTERNAL}'
    assert await firewall.peer_order.destination_zone_id.future() == f'{NAME}-zone_id'

    # The egress is one policy on a pair of its own, and it too has to precede
    # the pair's predefined deny or it is a resource that changes nothing.
    assert await firewall.cluster_egress_order.before_predefined_ids.future() == [f'{NAME}-cluster-egress_id']
    assert await firewall.cluster_egress_order.source_zone_id.future() == f'{NAME}-zone_id'
    assert await firewall.cluster_egress_order.destination_zone_id.future() == f'zone-{unifi.ZONE_EXTERNAL}'


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

    destination = await firewall.peer_v6.destination.future()
    assert destination is not None
    assert destination.ips == [WORKER_GUA], 'the pinhole matches a literal address, the prefix rotating'
    assert destination.port == PEER_PORT
    assert await firewall.peer_v6.ip_version.future() == 'IPV6'

    assert await firewall.peer_v4.dst_port.future() == str(PEER_PORT)
    assert await firewall.peer_v4.fwd_port.future() == str(PEER_PORT)
    assert await firewall.peer_v4.fwd_ip.future() == str(conventions.HOMELAB_NODE_IPV4)
    assert await firewall.peer_v4.src_ip.future() == 'any'


@pytest.mark.asyncio
async def test_the_controller_credential_is_an_api_key_on_a_bounded_provider(mocks: Mocks) -> None:
    """A key of its own, and a retry budget that cannot lock the account out.

    The controller's login rate limit is account-wide, so an unbounded retry
    from a runner is an outage for the people using the console. The number
    is small on purpose and asserted so that raising it is a visible edit.

    Read off the wire rather than off the resource, because a provider's
    settings are serialized as strings and the strings are what the run
    carries.
    """
    build()
    await settled()
    settings = mocks.inputs[f'{NAME}-unifi']

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
async def test_static_host_entries_are_empty_by_design_and_typed_when_present() -> None:
    """The device name plane is DHCP-derived; a static entry is an exception.

    Keeping the census empty is the assertion that matters — every service is
    reached by its public name through the split-horizon rewrites, and a host
    entry added here would be a second naming plane to keep in step. The
    mechanism is still exercised, so the exception works the day it is needed.
    """
    assert unifi.STATIC_HOSTS == {}
    assert build().static_hosts == {}

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


@pytest.mark.asyncio
async def test_the_stack_seam_carries_the_peer_port_into_both_halves() -> None:
    """The seam the `physical` stack calls, exercised through its own front door.

    The port arrives as stack configuration rather than as a constant: it is
    inherited from the deployment this cluster replaces, so the program reads
    it instead of choosing it. A seam that dropped it would leave two rules
    admitting a port nobody listens on.
    """
    from kluster import gateway

    firewall = gateway.declare_firewall(
        NAME,
        api_url=API_URL,
        api_key=API_KEY,
        site=SITE,
        worker_gua=WORKER_GUA,
        peer_port=6881,
    )

    destination = await firewall.peer_v6.destination.future()
    assert destination is not None
    assert destination.port == 6881
    assert await firewall.peer_v4.dst_port.future() == '6881'
