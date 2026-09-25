"""The overlay's roster and the network declared from it, asserted without Central.

Two things are checked here, and the rule program is neither of them: it arrives
as a parameter and has its own suite (`test_flow_rules.py`).

The roster is asserted as invariants, and here that is the *only* way. The
roster is static code, so nothing can break one of its invariants at runtime
that these cases did not already catch -- which is why the program itself checks
none of them (rfc-002 §10.2).

The declaration is asserted against the roster rather than against a fixture of
its own, because a member exists for one reason: an entry exists.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable
from dataclasses import replace
from ipaddress import IPv4Address
from typing import Any

import pulumi
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components import overlay as overlay_module

NAME = 'kluster'
NETWORK_ID = '0123456789abcdef'
API_TOKEN = 'a-central-token'

NETWORK = 'zerotier:index/network:Network'
MEMBER = 'zerotier:index/member:Member'

#: The rule program the fixture hands the component. It is a sentinel rather
#: than the real thing: what these cases are about is that the component
#: carries what it is given and composes nothing, and the program's own content
#: is `test_flow_rules.py`'s subject.
RULES = '# handed in, not composed\naccept;\n'

#: The managed DNS the fixture hands the component, a sentinel for the same
#: reason as `RULES`: the case is that the network carries the object it was
#: given, and what the real one says is `test_conventions`' subject. Two
#: servers, so that a component keeping only the first is told apart from one
#: keeping the list.
DNS = conventions.overlay.ManagedDns(
    domain='handed.example', servers=(IPv4Address('192.0.2.1'), IPv4Address('192.0.2.2'))
)


class Central(Recorder):
    """The two identifiers Central computes: a node's identity, and the network's id."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'zerotier:index/identity:Identity':
            return {'identityId': f'{args.name}-node', 'publicKey': 'public', 'privateKey': 'private'}
        if args.typ == 'zerotier:index/network:Network':
            return {'networkId': NETWORK_ID}
        return {}


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> Central:
    """The network declared once, the way the `physical` stack declares it."""
    pulumi.runtime.set_all_config({f'kluster:{overlay_module.API_TOKEN}': API_TOKEN})
    monitor = await run_with(Central(), stack='physical')
    async with declaring():
        _ = overlay_module.Overlay(
            NAME,
            network_id=NETWORK_ID,
            flow_rules=RULES,
            roster=conventions.overlay.ROSTER,
            managed_routes=conventions.overlay.MANAGED_ROUTES,
            dns=DNS,
        )
    return monitor


##
## The roster
##


def test_every_member_is_named_identified_and_placed_exactly_once() -> None:
    """Three uniqueness rules, and each of them is a different collision.

    Two entries under one name would publish two `*.zt` records with the same
    label; two under one node id would declare two members of the same device,
    the second overwriting the first in Central; two at one address would leave
    a flow rule and a DNS record naming a machine that is not the one the
    reader meant.
    """
    names = [entry.name for entry in conventions.overlay.ROSTER]
    node_ids = [
        entry.node_id for entry in conventions.overlay.ROSTER if isinstance(entry, conventions.overlay.EnrolledMember)
    ]
    addresses = [entry.address for entry in conventions.overlay.ROSTER]

    assert len(set(names)) == len(names)
    assert len(set(node_ids)) == len(node_ids)
    assert len(set(addresses)) == len(addresses)


def test_every_enrolled_node_id_is_ten_hexadecimal_digits() -> None:
    """The shape a ZeroTier node identifier has, and the only guard on it.

    An enrolled id reaches the roster by being read off a device and typed in
    — step 2 of the bring-up ceremony does exactly that for the gateway — so a
    transcription slip is the realistic mistake, and Central answers one with a
    member that authorizes no device at all.
    """
    for entry in conventions.overlay.ROSTER:
        if isinstance(entry, conventions.overlay.EnrolledMember):
            assert re.fullmatch(r'[0-9a-f]{10}', entry.node_id), entry.name


def test_every_member_is_placed_inside_the_overlays_own_subnet() -> None:
    """An address outside it is not on this network at all.

    ZeroTier assigns statically out of the network's managed range, so an entry
    numbered outside it is one Central would reject — and, before that, one the
    confinement rules would name to no effect.
    """
    for entry in conventions.overlay.ROSTER:
        assert entry.address in conventions.overlay.SUBNET, entry.name


def gateway_faults(roster: Iterable[conventions.overlay.RosterEntry]) -> set[str]:
    """Which ways the gateway entries `roster` holds are wrong: `address`, `role`, or none.

    Two things the gateway's entry must say on the day the ceremony adds it.
    The device's SSH, the controller's API and the next hop of every route to
    the home all derive from `conventions.overlay.UDM`, so an entry at any
    other address would point all three somewhere the member is not. And the
    gateway is infrastructure: an entry carrying the permissive default role
    would put the box every route runs through on the same footing as a phone.
    Empty for a roster with no gateway entry.
    """
    faults: set[str] = set()
    for entry in roster:
        if entry.name != conventions.overlay.MEMBER_UDM:
            continue
        if entry.address != conventions.overlay.UDM:
            faults.add('address')
        if entry.role != conventions.overlay.Role.INFRA:
            faults.add('role')
    return faults


def test_the_gateway_entry_is_infrastructure_at_the_address_every_client_dials() -> None:
    """`gateway_faults` over the roster as it stands.

    The entry is absent until the ceremony reads the minted node id and adds
    it (physical/gateway.md §2.5), so on a roster without it this holds
    trivially; the case below is what shows the check refuses a bad entry.
    """
    assert gateway_faults(conventions.overlay.ROSTER) == set()


def test_the_gateway_check_refuses_an_entry_at_another_address_or_in_another_role() -> None:
    """The check applied to constructed rosters holding a gateway entry, good or bad.

    Each bad entry is wrong in one way only and is reported for that way, and
    the good one is reported for nothing, so a check that refused every entry
    fails here as surely as one that refused none. Every roster also holds a
    member that is not the gateway and would fail both checks if it were, so
    a check that forgot which entry it is about fails too.
    """
    laptop = conventions.overlay.EnrolledMember(
        name='a-laptop',
        node_id='abcdef0123',
        address=conventions.overlay.UDM + 2,
        role=conventions.overlay.Role.PERSONAL,
    )
    good = conventions.overlay.EnrolledMember(
        name=conventions.overlay.MEMBER_UDM,
        node_id='0123456789',
        address=conventions.overlay.UDM,
        role=conventions.overlay.Role.INFRA,
    )
    elsewhere = replace(good, address=conventions.overlay.UDM + 1)
    permissive = replace(good, role=conventions.overlay.Role.PERSONAL)

    assert gateway_faults([laptop, good]) == set()
    assert gateway_faults([laptop, elsewhere]) == {'address'}
    assert gateway_faults([laptop, permissive]) == {'role'}


def test_the_two_continuous_integration_identities_are_generated_and_confined() -> None:
    """One identity per stack that joins, and each of them tagged for confinement.

    Sharing one identity between two jobs would flap it, since a node maps to
    one endpoint at a time; sharing a tag with anything else would hand that
    thing the same four destinations.
    """
    generated = [
        entry for entry in conventions.overlay.ROSTER if isinstance(entry, conventions.overlay.GeneratedMember)
    ]

    assert [entry.name for entry in generated] == list(conventions.overlay.CI_MEMBERS)
    assert {entry.address for entry in generated} == {conventions.overlay.CI_PHYSICAL, conventions.overlay.CI_DNS}
    assert all(entry.role == conventions.overlay.Role.CI for entry in generated)
    assert [entry.name for entry in conventions.overlay.ROSTER if entry.role == conventions.overlay.Role.CI] == list(
        conventions.overlay.CI_MEMBERS
    )


def test_the_roster_stays_within_what_multicast_reaches() -> None:
    """Local discovery stops finding members past the multicast limit.

    The limit is a declared field rather than a default, so the constraint is
    on the record; this is the half that notices when the roster grows past it.
    """
    assert len(conventions.overlay.ROSTER) <= overlay_module.MULTICAST_LIMIT


##
## The declaration
##


def test_the_network_is_adopted_by_the_id_it_was_handed_protected_and_wholly_declared(stack: Central) -> None:
    """Three options on one registration, and each is a different outage.

    The network predates the program: without `import_` the first run creates
    a second network nobody is a member of, and the roster's members are
    upserted onto the wrong one. Without `protect` a replace -- a delete and a
    create under a new id -- or a `destroy` of the stack takes the overlay
    down with every member on it. And nothing is ignored, because an adopted
    resource graduates to declared (style/pulumi.md) and no field of this
    network belongs to another owner: an `ignore_changes` here would leave a
    field Central holds that no declaration states.
    """
    request = stack.options_of(f'{NAME}-network', NETWORK)

    assert request.importId == NETWORK_ID
    assert request.protect is True
    assert list(request.ignoreChanges) == []


def test_the_network_carries_the_census_routes_and_stamps_no_via_of_its_own(stack: Central) -> None:
    """The route table is the census, target and via, and nothing the census lacks.

    The network is adopted with its routes declared in full, so the list here
    is what Central holds after the update -- a route added or a `via` stamped
    by the component would be one the census never decided, and a route the
    census carries but the component drops would be one the update deletes at
    Central. `via` is present exactly where the census names a member: the
    overlay's own route carries none, and a `via` written onto it would make
    every member's address depend on a next hop.
    """
    network = stack.inputs_of(f'{NAME}-network')

    assert network['routes'] == [
        {'target': str(route.target)} | ({} if route.via is None else {'via': str(route.via)})
        for route in conventions.overlay.MANAGED_ROUTES
    ]
    assert network['private'] is True
    assert network['enableBroadcast'] is True
    assert network['multicastLimit'] == overlay_module.MULTICAST_LIMIT


def test_a_route_with_no_via_covers_every_address_the_roster_places() -> None:
    """The controller's condition for handing a member its address, held on the census.

    A member's static assignment is pushed only when some route's target
    contains it, with that route's netmask; a via-less route is the one
    ZeroTier installs on the member's own interface for the network's subnet.
    So a table whose via-less routes cover less than the roster is a table
    that leaves some member with no address at its next config refresh -- and
    a table with none leaves every member that way. Held over the roster and
    the gateway's address, not over the subnet constant: what has to be true
    is that every placed member is covered, whatever the table's targets are.
    """
    own = [route.target for route in conventions.overlay.MANAGED_ROUTES if route.via is None]
    assert own, 'no via-less route: the controller would push no member an address'

    placed = [entry.address for entry in conventions.overlay.ROSTER] + [conventions.overlay.UDM]
    for address in placed:
        assert any(address in target for target in own), address


def test_every_via_is_a_member_and_only_the_gateway_forwards_for_the_home() -> None:
    """A `via` is nothing but a member's address, and the home is routed by the router.

    A route via an address no member holds is a route to nothing. A home subnet
    via anything but the gateway would put a machine that is not a router on
    the management path; the gateway forwarding for anything but the home
    would make it the next hop of a subnet it has no leg on. The one route via
    another member is the legacy pod subnet, via the machine that still runs
    that cluster, so that the route leaves with the entry.
    """
    home = {network.v4 for network in conventions.SITE_NETWORKS} | {conventions.LAN_POOL.v4}
    members = {entry.address for entry in conventions.overlay.ROSTER} | {conventions.overlay.UDM}
    legacy = conventions.overlay.member(conventions.overlay.MEMBER_VPS).address

    for route in conventions.overlay.MANAGED_ROUTES:
        if route.via is None:
            continue
        assert route.via in members, route
        if route.target in home:
            assert route.via == conventions.overlay.UDM, route
        elif route.target == conventions.overlay.LEGACY_POD_SUBNET:
            assert route.via == legacy, route
        else:
            raise AssertionError(f'{route} is via a member for a subnet that is neither the home nor the legacy one')


def test_the_census_carries_the_cluster_vlan_and_the_pool_by_name() -> None:
    """Both halves of the cluster's home addressing are reachable off-site.

    They are two subnets and two reasons: the VLAN is where a run reaches the
    worker's machine API, and the pool is where a person off-site reaches a
    service the cluster publishes on the LAN. Named rather than numbered: the
    route table and the two subnets are one census, so what is assertable here
    is which subnets the table carries and not what either one is numbered.
    """
    targets = [route.target for route in conventions.overlay.MANAGED_ROUTES]

    # The pool is not a subnet anything is attached to: it is carried because
    # the gateway learns host routes into it over BGP.
    assert conventions.LAN_POOL.v4 in targets
    assert conventions.CLUSTER_VLAN.v4 in targets


def test_the_network_carries_the_rules_it_was_handed_and_composes_none(stack: Central) -> None:
    """Policy is the caller's, and the component is the delivery of it.

    What confines a run is a fact about how continuous integration reaches this
    site, not about ZeroTier, so it is composed where those facts live and
    passed in whole (rfc-002 §6). A component that reached for the roster or
    the resolver census itself would be a second place the policy is decided.
    """
    assert stack.inputs_of(f'{NAME}-network')['flowRules'] == RULES


def test_the_network_carries_the_managed_dns_it_was_handed_and_composes_none(stack: Central) -> None:
    """One block, the caller's, spelled the way the provider takes it.

    The domain and the servers are decided in `conventions` because the `dns`
    stack answers under the same domain; a component that read the resolver
    census or the domain for itself would be a second place that decision is
    made. Exactly one element, because the provider folds the whole list into
    the network's single DNS setting and a second element would overwrite
    the first rather than push a second domain.
    """
    assert stack.inputs_of(f'{NAME}-network')['dns'] == [
        {'domain': DNS.domain, 'servers': [str(server) for server in DNS.servers]}
    ]


def test_the_members_declared_are_exactly_the_roster_and_nothing_else_is_consulted(stack: Central) -> None:
    """A member exists because an entry exists, and for no other reason.

    That is what lets the gateway be absent during a first bring-up with no
    relaxation to switch on: there is no configured mapping the roster could
    be short against, so an entry that has not been written yet declares
    nothing and costs nothing. The routes to the home name the gateway's
    overlay address as their next hop either way — a route to a router that
    has not joined yet is the ordinary state of a bring-up.
    """
    declared_members = stack.names(MEMBER)

    assert declared_members == {f'{NAME}-member-{entry.name}' for entry in conventions.overlay.ROSTER}
    assert str(conventions.overlay.UDM) in {route.get('via') for route in stack.inputs_of(f'{NAME}-network')['routes']}


def test_no_member_is_handed_an_address_the_roster_did_not_choose(stack: Central) -> None:
    """A pool assignment would move a member the rules and records name.

    Both derived IPv6 schemes are off for the same reason, and because a
    continuous-integration member with an address in a family its drop rules
    cannot see would eat its own neighbour discovery.
    """
    network = stack.inputs_of(f'{NAME}-network')
    assert network['assignIpv6s'] == [{'rfc4193': False, 'sixplane': False, 'zerotier': False}]

    members = [declaration.inputs for declaration in stack.of_type(MEMBER)]
    assert len(members) == len(conventions.overlay.ROSTER)
    for member in members:
        assert member['noAutoAssignIps'] is True
        assert member['authorized'] is True
        assert len(member['ipAssignments']) == 1


def test_every_member_carries_a_declared_role_and_the_generated_ones_their_own_id(stack: Central) -> None:
    """The tag is the only thing that distinguishes a run from a laptop.

    A member declared without one would inherit the permissive default, which
    is exactly the hole the roster exists to close.
    """
    for entry in conventions.overlay.ROSTER:
        member = stack.inputs_of(f'{NAME}-member-{entry.name}')
        assert member['tags'] == [[conventions.overlay.TAG_ROLE_ID, entry.role]], entry.name
        assert member['name'] == entry.name

    assert stack.inputs_of(f'{NAME}-member-ci-physical')['memberId'] == f'{NAME}-identity-ci-physical-node'
    # An enrolled member carries the id its own device minted, straight off the
    # roster entry: there is nowhere else it could come from.
    haos = conventions.overlay.member('haos')
    assert isinstance(haos, conventions.overlay.EnrolledMember)
    assert stack.inputs_of(f'{NAME}-member-haos')['memberId'] == haos.node_id
    assert stack.inputs_of(f'{NAME}-member-haos')['ipAssignments'] == [str(haos.address)]


def test_the_central_credential_belongs_to_a_provider_of_its_own(stack: Central) -> None:
    """The token administers the whole account, Central minting nothing smaller.

    Giving it to a provider instance rather than to the run at large is what
    bounds the resources it can reach to the ones declared here.
    """
    settings = stack.inputs_of(f'{NAME}-zerotier')
    token = settings['zerotierCentralToken']

    assert isinstance(token, dict), 'the token is classified as a secret, so it is never plain text in state'
    assert token['value'] == API_TOKEN


##
## The provider
##


def test_the_administration_token_is_read_where_the_provider_is_built() -> None:
    """The token configures this provider and nothing else, so nothing else sees it.

    Central mints no credential smaller than the whole account, which is the
    reason the resources it may reach are exactly the ones this component
    declares -- and the reason the token is read at the line that builds the
    provider rather than travelling through a signature that has no other
    opinion about it (rfc-002 §8.1).
    """
    assert 'api_token' not in inspect.signature(overlay_module.Overlay.__init__).parameters


def test_every_resource_is_signed_by_the_overlays_own_provider(stack: Central) -> None:
    """Inherited from the component, never re-plumbed onto a child.

    The network, the two generated identities and every member are children of
    the component that built the provider, so each takes it from its parent's
    provider map. Nothing below names it.
    """
    overlay_resources = [d for d in stack.declared if d.typ.startswith('zerotier:index/')]

    assert overlay_resources, 'the fixture declared no overlay resources at all'
    for declaration in overlay_resources:
        assert f'{NAME}-zerotier' in declaration.provider, f'{declaration.name} is not signed by the provider'
