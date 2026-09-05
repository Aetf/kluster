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
from typing import Any

import pulumi
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components import overlay as overlay_module

NAME = 'kluster'
NETWORK_ID = '0123456789abcdef'
API_TOKEN = 'a-central-token'

MEMBER = 'zerotier:index/member:Member'

#: The rule program the fixture hands the component. It is a sentinel rather
#: than the real thing: what these cases are about is that the component
#: carries what it is given and composes nothing, and the program's own content
#: is `test_flow_rules.py`'s subject.
RULES = '# handed in, not composed\naccept;\n'


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


def test_the_gateway_entry_is_infrastructure_at_the_address_every_client_dials() -> None:
    """Two things the gateway's entry must say on the day the ceremony adds it.

    The device's SSH, the controller's API and every managed route's next hop
    all derive from `conventions.overlay.UDM`, so an entry at any other address
    would point all three somewhere the member is not. And the gateway is infrastructure: an
    entry carrying the permissive default role would put the box every route
    runs through on the same footing as a phone. The entry is absent until the
    ceremony reads the minted node id and adds it (physical/gateway.md §2.5),
    which is why both are stated as conditionals rather than as a lookup.
    """
    gateway_entries = [entry for entry in conventions.overlay.ROSTER if entry.name == conventions.overlay.MEMBER_UDM]

    assert all(entry.address == conventions.overlay.UDM for entry in gateway_entries)
    assert all(entry.role == conventions.overlay.Role.INFRA for entry in gateway_entries)


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


def test_the_network_is_adopted_and_carries_every_managed_route(stack: Central) -> None:
    """The network predates the program, and the routes are net-new.

    Creating a second network would leave every existing member on the first
    one; declaring the routes anywhere but through the gateway's member would
    put a machine that is not a router on the management path.
    """
    network = stack.inputs_of(f'{NAME}-network')

    assert [route['target'] for route in network['routes']] == [str(net) for net in conventions.overlay.MANAGED_ROUTES]
    assert {route['via'] for route in network['routes']} == {str(conventions.overlay.UDM)}
    assert network['private'] is True
    assert network['enableBroadcast'] is True
    assert network['multicastLimit'] == overlay_module.MULTICAST_LIMIT


def test_the_census_carries_the_cluster_vlan_and_the_pool_by_name() -> None:
    """Both halves of the cluster's home addressing are reachable off-site.

    They are two subnets and two reasons: the VLAN is where a run reaches the
    worker's machine API, and the pool is where a person off-site reaches a
    service the cluster publishes on the LAN. Spelled out rather than derived,
    so renumbering either one is a visible edit here as well as in
    `conventions`.
    """
    targets = [str(net) for net in conventions.overlay.MANAGED_ROUTES]

    assert '192.168.70.0/24' in targets, 'the cluster VLAN'
    assert '192.168.71.0/24' in targets, 'the `lan` pool'
    # The pool is not a subnet anything is attached to: it is carried because
    # the gateway learns host routes into it over BGP.
    assert conventions.LAN_POOL.v4 in conventions.overlay.MANAGED_ROUTES
    assert conventions.CLUSTER_VLAN.v4 in conventions.overlay.MANAGED_ROUTES


def test_the_network_carries_the_rules_it_was_handed_and_composes_none(stack: Central) -> None:
    """Policy is the caller's, and the component is the delivery of it.

    What confines a run is a fact about how continuous integration reaches this
    site, not about ZeroTier, so it is composed where those facts live and
    passed in whole (rfc-002 §6). A component that reached for the roster or
    the resolver census itself would be a second place the policy is decided.
    """
    assert stack.inputs_of(f'{NAME}-network')['flowRules'] == RULES


def test_the_members_declared_are_exactly_the_roster_and_nothing_else_is_consulted(stack: Central) -> None:
    """A member exists because an entry exists, and for no other reason.

    That is what lets the gateway be absent during a first bring-up with no
    relaxation to switch on: there is no configured mapping the roster could
    be short against, so an entry that has not been written yet declares
    nothing and costs nothing. The routes name the gateway's overlay address
    as their next hop either way — a route to a router that has not joined yet is the ordinary
    state of a bring-up.
    """
    declared_members = stack.names(MEMBER)

    assert declared_members == {f'{NAME}-member-{entry.name}' for entry in conventions.overlay.ROSTER}
    assert {route['via'] for route in stack.inputs_of(f'{NAME}-network')['routes']} == {str(conventions.overlay.UDM)}


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
