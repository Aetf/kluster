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
-   no policy hands a subnet to a field that takes single addresses: the
    provider refuses it at preview, which no mock does;
-   the IoT rules are sourced from the IoT VLAN alone, through its address
    groups, not from the internal zone at large, which is what keeps the rest
    of the internal side's own access to the cluster intact;
-   the cluster VLAN is a network object with no DHCP server, alone in a zone
    of its own, and everything that names the worker names that zone;
-   the peer's IPv4 forward exists only once the application runs on the
    worker, because until then the port is the legacy host's.

No controller is contacted: the boundary is Pulumi's mock monitor, which
answers the zone lookups and hands back the inputs each resource was given.
"""

import inspect
from collections.abc import Mapping
from ipaddress import IPv4Address, IPv4Interface, IPv6Address, ip_address, ip_network

import pulumi
import pulumi_unifi as unifi_provider
import pytest
import pytest_asyncio
from mock_monitor import declaring, run_with
from unifi_controller import ZONE_LOOKUP, Controller, zone_id

from kluster import conventions
from kluster.components import talos
from kluster.components.gateway import unifi

NAME = 'kluster'
API_URL = 'https://gateway.invalid'
API_KEY = 'unifi-api-key'
SITE = 'default'
WORKER_GUA = '2001:db8:1:70::10'


@pytest_asyncio.fixture(autouse=True)
async def mocks() -> Controller:
    pulumi.runtime.set_all_config({f'kluster:{unifi.API_KEY}': API_KEY})
    return await run_with(Controller(site=SITE), stack='physical')


POLICY = 'unifi:index/firewallZonePolicy:FirewallZonePolicy'
ORDER = 'unifi:index/firewallZonePolicyOrder:FirewallZonePolicyOrder'


def _policy_pairs(mocks: Controller) -> dict[str, tuple[str, str]]:
    """Each declared zone policy's id, and the zone pair it is declared on."""
    return {
        f'{name}_id': (inputs['source']['zoneId'], inputs['destination']['zoneId'])
        for name, inputs in mocks.by_name(POLICY).items()
    }


async def _holds_its_own_family(group: unifi_provider.FirewallGroup) -> None:
    """A group's every member is a network of the family its type holds.

    A v4 CIDR quietly accepted into the v6 group would produce a rule that
    matches nothing and reads as if it matches everything.
    """
    kind = await group.type.future()
    assert kind is not None
    family = {'address-group': 4, 'ipv6-address-group': 6}[kind]
    members = await group.members.future()
    assert members
    assert {ip_network(member).version for member in members} == {family}


def build(
    static_hosts: Mapping[str, IPv4Address | IPv6Address] | None = None,
    *,
    peer_on_worker: bool = True,
) -> unifi.SiteFirewall:
    """The whole census: the worker's address known and the application on it."""
    return unifi.SiteFirewall(
        NAME,
        api_url=API_URL,
        site=SITE,
        worker_gua=WORKER_GUA,
        peer_on_worker=peer_on_worker,
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

    # Every policy is placed by exactly one order resource, the one for its
    # own zone pair, and an order places nothing that is not declared: a
    # policy whose position is whatever creation produced is a policy the
    # design does not actually own.
    pairs = _policy_pairs(mocks)
    orders = mocks.by_name(ORDER)
    placed = [policy for inputs in orders.values() for policy in inputs['beforePredefinedIds']]
    assert sorted(placed) == sorted(pairs)
    for name, inputs in orders.items():
        assert {pairs[policy] for policy in inputs['beforePredefinedIds']} == {
            (inputs['sourceZoneId'], inputs['destinationZoneId'])
        }, name
    # One network and one zone, both the cluster's: the pool is deliberately
    # neither, and no other network on the site is this program's to declare.
    assert len(by_type['unifi:index/network:Network']) == 1
    assert len(by_type['unifi:index/firewallZone:FirewallZone']) == 1
    # The only port forward on the device.
    assert len(by_type['unifi:index/portForward:PortForward']) == 1
    assert 'unifi:index/dnsRecord:DnsRecord' not in by_type


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_gua', [WORKER_GUA, None])
async def test_every_address_group_is_named_by_a_declared_policy(mocks: Controller, worker_gua: str | None) -> None:
    """A group exists because a rule names it, so none sits on the controller naming nothing.

    Checked with the pinhole's pair absent too: a group only a conditional row
    named would otherwise outlive it.
    """
    async with declaring():
        unifi.SiteFirewall(NAME, api_url=API_URL, site=SITE, worker_gua=worker_gua, static_hosts={})

    named = {
        side.get('ipGroupId')
        for inputs in mocks.by_name(POLICY).values()
        for side in (inputs['source'], inputs['destination'])
    }
    groups = {f'{name}_id' for name in mocks.names('unifi:index/firewallGroup:FirewallGroup')}
    assert groups
    assert groups <= named, sorted(groups - named)


@pytest.mark.asyncio
async def test_every_policy_is_declared_in_force(mocks: Controller) -> None:
    """A disabled policy is still on the census, still in its pair's order, and matches nothing.

    Every other case here reads a policy's shape — its zones, its sources, its
    rank — and a policy switched off keeps all of them. For a drop that is the
    worst way to be wrong: the carve-out reads as in force while the IoT VLAN
    keeps reaching whatever it was meant to be kept from.
    """
    async with declaring():
        build()

    policies = mocks.by_name('unifi:index/firewallZonePolicy:FirewallZonePolicy')

    # A loop is only a claim about what it visits. Which policies there are is
    # the census case's to pin; this one only refuses to pass by visiting none.
    assert policies, 'the build declared no zone policy'
    for name, inputs in policies.items():
        assert inputs.get('enabled') is True, name


@pytest.mark.asyncio
async def test_the_cluster_vlan_is_a_network_object_the_gateway_terminates() -> None:
    """The opposite decision from the pool, and the reason nodes are policeable.

    The pool is deliberately no object, because an object would fight the host
    routes the cluster advertises. The VLAN is one for the mirror-image reason:
    a subnet the controller has an object for is a subnet a policy can name,
    and the population sharing the untagged LAN could never be named at all.
    """
    firewall = build()

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
    neighbor statement, the port forward and day 1's apid endpoint — and a
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

    assert await firewall.policies['cluster-egress'].action.future() == 'ALLOW'
    source = await firewall.policies['cluster-egress'].source.future()
    destination = await firewall.policies['cluster-egress'].destination.future()
    assert source is not None and destination is not None
    assert source.zone_id == f'{NAME}-zone_id'
    assert destination.zone_id == zone_id(unifi.ZONE_EXTERNAL)
    # The whole zone, both families and every protocol: what a node reaches on
    # the internet is not a decision the gateway is in a position to make.
    assert source.ips is None
    assert await firewall.policies['cluster-egress'].ip_version.future() == 'BOTH'
    assert await firewall.policies['cluster-egress'].protocol.future() == 'all'
    assert await firewall.policies['cluster-egress'].auto_allow_return_traffic.future() is True


@pytest.mark.asyncio
async def test_the_cluster_zone_talks_to_the_home_in_both_directions() -> None:
    """The two directions between the new zone and the stock internal one.

    Outward carries the recorded workload dependencies — the home-automation
    API among them — and inward is the trusted VLANs reaching the nodes
    directly. Both are whole-zone, both families, every protocol: what a node's
    workloads may call is decided where those workloads are declared, and
    narrowing it here would put that decision behind a gateway credential.
    """
    firewall = build()

    outward = firewall.policies['cluster-internal']
    assert await outward.action.future() == 'ALLOW'
    source = await outward.source.future()
    destination = await outward.destination.future()
    assert source is not None and destination is not None
    assert source.zone_id == f'{NAME}-zone_id'
    assert destination.zone_id == zone_id(unifi.ZONE_INTERNAL)
    assert source.ips is None and destination.ips is None

    inward = firewall.policies['internal-cluster']
    assert await inward.action.future() == 'ALLOW'
    source = await inward.source.future()
    destination = await inward.destination.future()
    assert source is not None and destination is not None
    assert source.zone_id == zone_id(unifi.ZONE_INTERNAL)
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
        (firewall.policies['iot-cluster-v4'], 'IPV4', f'{NAME}-iot-v4_id'),
        (firewall.policies['iot-cluster-v6'], 'IPV6', f'{NAME}-iot-v6_id'),
    )
    for policy, version, group in families:
        assert await policy.action.future() == 'BLOCK'
        assert await policy.ip_version.future() == version
        assert await policy.protocol.future() == 'all'
        source = await policy.source.future()
        destination = await policy.destination.future()
        assert source is not None and destination is not None
        assert source.zone_id == zone_id(unifi.ZONE_INTERNAL)
        assert source.ip_group_id == group
        assert source.ips is None
        # The whole zone on the far side: the node subnet *is* a network
        # object, so unlike the pool it needs no group to be named.
        assert destination.zone_id == f'{NAME}-zone_id'
        assert destination.ips is None
        assert destination.ip_group_id is None


@pytest.mark.asyncio
async def test_the_pinhole_lands_in_the_cluster_zone_rather_than_the_internal_one() -> None:
    """The rule follows the worker, which sits on the cluster VLAN, not the internal side.

    A pinhole still pointing at the internal zone would be admitted into a
    zone the destination address is not in — a rule that reads as if the peer
    port were published and quietly publishes nothing.
    """
    firewall = build()

    # Built with an address, so the conditional half of the census is here.
    assert 'peer-v6' in firewall.policies
    destination = await firewall.policies['peer-v6'].destination.future()
    assert destination is not None
    assert destination.zone_id == f'{NAME}-zone_id'


@pytest.mark.asyncio
async def test_the_pool_is_named_by_group_and_the_groups_are_single_family() -> None:
    """One subnet, two groups, because a group holds one address family.

    The consequence worth guarding is the failure mode: a v4 CIDR quietly
    accepted into the v6 group would produce a rule that matches nothing and
    reads as if it matches everything.
    """
    firewall = build()

    for group in (firewall.groups['pool-v4'], firewall.groups['pool-v6']):
        await _holds_its_own_family(group)

    for policy in (firewall.policies['iot-pool-v4'], firewall.policies['iot-pool-v6']):
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
        (firewall.policies['iot-media-v4'], conventions.LAN_POOL.media_vip.v4),
        (firewall.policies['iot-media-v6'], conventions.LAN_POOL.media_vip.v6),
    ):
        assert await policy.action.future() == 'ALLOW'
        destination = await policy.destination.future()
        assert destination is not None
        assert destination.ips == [str(vip)]
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
        (firewall.policies['iot-media-v4'], f'{NAME}-iot-v4_id'),
        (firewall.policies['iot-pool-v4'], f'{NAME}-iot-v4_id'),
        (firewall.policies['iot-media-v6'], f'{NAME}-iot-v6_id'),
        (firewall.policies['iot-pool-v6'], f'{NAME}-iot-v6_id'),
    )
    for policy, group in families:
        source = await policy.source.future()
        assert source is not None
        assert source.ip_group_id == group
        assert source.ips is None


@pytest.mark.asyncio
async def test_the_iot_vlan_is_named_by_group_and_the_groups_hold_its_subnets() -> None:
    """The groups every IoT rule is sourced from hold the VLAN and nothing else.

    A rule sourced from a group is only as narrow as the group: a member
    beyond the VLAN would carve the trusted side out with it, and a v4 subnet
    in the v6 group would leave a family the drop reads as covering
    unmatched.
    """
    firewall = build()

    for group in (firewall.groups['iot-v4'], firewall.groups['iot-v6']):
        await _holds_its_own_family(group)


@pytest.mark.asyncio
async def test_no_policy_hands_a_subnet_to_a_field_of_single_addresses(mocks: Controller) -> None:
    """Every `ips` entry, source or destination, is one address.

    The pinned provider validates each entry as a single IPv4 or IPv6 address
    and refuses the whole resource at preview when one is a CIDR; the mock
    monitor accepts anything. A subnet goes through an address group instead,
    and this holds that every policy the census declares, today's and the
    next one alike, keeps to that.
    """
    async with declaring():
        build()

    policies = mocks.by_name('unifi:index/firewallZonePolicy:FirewallZonePolicy')

    assert policies, 'the build declared no zone policy'
    checked = 0
    for name, inputs in policies.items():
        for side in ('source', 'destination'):
            entries: list[str] = inputs[side].get('ips') or []
            for entry in entries:
                try:
                    ip_address(entry)
                except ValueError:
                    pytest.fail(f'{name} {side}.ips holds {entry!r}, which is not one address')
                checked += 1
    # The census does carry literals — the media VIP, the worker's address —
    # so a pass that read no entry at all is a pass that checked nothing.
    assert checked, 'no policy carried an ips entry to check'


#: Each ordering resource, the zone pair it orders, and the policies it puts
#: ahead of that pair's predefined rules: every pair the census orders, but
#: the inward one, which ranks its drops ahead of its allow — the reverse of the
#: pool pair — and has a case of its own below. For these the claim is one, and
#: it is the same for every one of them.
ORDERINGS = [
    ('iot-pool-order', zone_id(unifi.ZONE_INTERNAL), zone_id(unifi.ZONE_EXTERNAL)),
    ('peer-order', zone_id(unifi.ZONE_EXTERNAL), f'{NAME}-zone_id'),
    ('cluster-egress-order', f'{NAME}-zone_id', zone_id(unifi.ZONE_EXTERNAL)),
    ('cluster-internal-order', f'{NAME}-zone_id', zone_id(unifi.ZONE_INTERNAL)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('key', 'source', 'destination'), ORDERINGS)
async def test_a_pair_evaluates_its_declared_policies_before_its_predefined_ones(
    mocks: Controller, key: str, source: str, destination: str
) -> None:
    """Order is declared rather than inherited from creation order.

    Every pair carries a predefined accept or deny of its own, and it would
    otherwise answer first -- which makes a declared policy behind it a
    resource that changes nothing. None of this is observable in the policy
    resources themselves, which is why it is asserted here.
    """
    async with declaring():
        order = build().orders[key]
    on_pair = {policy for policy, pair in _policy_pairs(mocks).items() if pair == (source, destination)}

    placed = await order.before_predefined_ids.future()
    assert on_pair
    assert placed is not None
    assert set(placed) == on_pair
    assert await order.source_zone_id.future() == source
    assert await order.destination_zone_id.future() == destination


@pytest.mark.asyncio
async def test_nothing_is_ordered_behind_a_pairs_predefined_rules() -> None:
    """The complement of the rule above: `after` is the placement nothing wants."""
    firewall = build()

    for key, *_ in ORDERINGS:
        order = firewall.orders[key]
        assert await order.after_predefined_ids.future() is None, key


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

    order = firewall.orders['internal-cluster-order']
    assert await order.before_predefined_ids.future() == [
        f'{NAME}-iot-cluster-v4_id',
        f'{NAME}-iot-cluster-v6_id',
        f'{NAME}-internal-cluster_id',
    ]
    assert await order.after_predefined_ids.future() is None
    assert await order.source_zone_id.future() == zone_id(unifi.ZONE_INTERNAL)
    assert await order.destination_zone_id.future() == f'{NAME}-zone_id'


@pytest.mark.asyncio
async def test_the_pool_rules_are_declared_on_the_uplink_pair() -> None:
    """The pool sits in no zone ipset, so its traffic is classified as uplink.

    This is the one piece of measured device behavior the whole IoT half
    depends on. Declaring these rules on the internal-to-internal pair would
    produce four resources that apply to nothing.
    """
    firewall = build()

    for policy in (
        firewall.policies['iot-media-v4'],
        firewall.policies['iot-media-v6'],
        firewall.policies['iot-pool-v4'],
        firewall.policies['iot-pool-v6'],
    ):
        source = await policy.source.future()
        destination = await policy.destination.future()
        assert source is not None and destination is not None
        assert source.zone_id == zone_id(unifi.ZONE_INTERNAL)
        assert destination.zone_id == zone_id(unifi.ZONE_EXTERNAL)


@pytest.mark.asyncio
async def test_both_halves_of_the_peer_flow_name_the_same_port_and_host() -> None:
    """The pinhole and the forward are one flow in two address families.

    A peer that reaches the same endpoint over IPv4 and IPv6 sees one
    participant; a mismatch between the two halves is invisible until a
    transfer is slow for reasons nobody can name.
    """
    firewall = build()

    assert 'peer-v6' in firewall.policies
    assert 'peer-v4' in firewall.forwards
    pinhole = await firewall.policies['peer-v6'].destination.future()
    assert pinhole is not None

    # The same port on both halves -- the provider hands the v6 side a number
    # and the v4 side a string, so each is read as the number it names.
    dst_port = await firewall.forwards['peer-v4'].dst_port.future()
    fwd_port = await firewall.forwards['peer-v4'].fwd_port.future()
    assert dst_port is not None
    assert fwd_port is not None
    assert pinhole.port is not None
    assert int(dst_port) == int(fwd_port) == int(pinhole.port)

    # The same host, in the address each family reaches it at: the worker's
    # global address on one side, its LAN address on the other.
    assert pinhole.ips == [WORKER_GUA], 'the pinhole matches a literal address, the prefix rotating'
    assert await firewall.policies['peer-v6'].ip_version.future() == 'IPV6'
    assert await firewall.forwards['peer-v4'].fwd_ip.future() == str(
        talos.STATIC_ADDRESSES[conventions.HOMELAB_NODE].address.ip
    )
    assert await firewall.forwards['peer-v4'].src_ip.future() == 'any'


@pytest.mark.asyncio
async def test_no_port_forward_exists_until_the_application_runs_on_the_worker(mocks: Controller) -> None:
    """Before its move, the application's legacy copy holds the WAN port.

    Whatever holds it — a UPnP lease or a rule kept by hand — a forward declared
    beside it either collides with it or takes the legacy copy's inbound IPv4
    away, so the component declares none and leaves the port to its holder.
    The pinhole is untouched by the flag: it names the worker's own address,
    which nothing else holds.
    """
    async with declaring():
        firewall = build(peer_on_worker=False)

    assert 'peer-v4' not in firewall.forwards
    assert 'unifi:index/portForward:PortForward' not in mocks.types
    assert 'peer-v6' in firewall.policies


@pytest.mark.asyncio
async def test_the_controller_credential_is_an_api_key_on_a_bounded_provider(mocks: Controller) -> None:
    """A key of its own, on a provider built for these resources alone.

    Read off the wire rather than off the resource, because a provider's
    settings are serialized as strings and the strings are what the run
    carries.
    """
    async with declaring():
        build()

    settings = mocks.inputs_of(f'{NAME}-unifi')

    assert settings['apiUrl'] == API_URL
    assert settings['site'] == SITE

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
    (`stacks.physical.GATEWAY_STATIC_HOSTS`); what this pins is that an empty
    roll declares nothing rather than falling back to one of the component's
    own.
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
    assert 'api_key' not in inspect.signature(unifi.controller_provider).parameters


@pytest.mark.asyncio
async def test_whether_the_controller_is_verified_is_the_programs_decision(mocks: Controller) -> None:
    """The certificate check is an input the provider is always given.

    Omitted, the provider takes it from `UNIFI_INSECURE` in the environment of
    whoever runs the program, so one run would verify and the next would not,
    and no preview would show which. What the input is set to is the ruling
    `unifi.ALLOW_INSECURE` records, with why; this holds that the ruling, and
    not the runner, is what reaches the provider.
    """
    async with declaring():
        build()

    settings = mocks.inputs_of(f'{NAME}-unifi')

    assert 'allowInsecure' in settings, "omitted, the runner's environment decides"


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
    assert f'{NAME}-unifi' in mocks.called_through(ZONE_LOOKUP)
