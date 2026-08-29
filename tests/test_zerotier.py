"""The overlay's roster, routes and flow rules, asserted without Central.

The flow rules are the interesting half. They are a string in a resource, so a
reviewer sees them as a blob and a mistake in them shows up as a job that cannot
reach the gateway — or, far worse, as one that can reach everything. The suite
therefore asserts the properties the design argues for rather than the text: that
each allowed flow is declared in both directions, that a run reaches four
destinations and no fifth one, that nothing may open a connection towards a run,
and that everyone else falls through untouched.

The roster is asserted the same way, and here it is the *only* way. The roster
is static code, so nothing can break one of its invariants at runtime that these
cases did not already catch — which is why the program itself checks none of
them (rfc-002 §10.2).
"""

from __future__ import annotations

import asyncio
import re
from ipaddress import IPv4Address
from typing import Any, cast

import pulumi
import pulumi.runtime.settings
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions
from kluster.components import gateway
from kluster.components import overlay as zerotier

NAME = 'kluster'
NETWORK_ID = '0123456789abcdef'
API_TOKEN = 'a-central-token'

#: The homelab host's overlay address, as the flow-rule cases name it.
#: `flow_rules` is a pure function of what it is handed, so those cases keep a
#: literal; that the roster is what hands it over is a case of its own.
HOMELAB_ZT = IPv4Address('10.144.180.10')
ADGUARD = (IPv4Address('10.0.5.11'), IPv4Address('10.0.5.12'))

#: Every resource the declaration fixture registered: type, name, inputs.
declared: list[tuple[str, str, dict[str, Any]]] = []


class Mocks(pulumi.runtime.Mocks):
    """A monitor that answers with the inputs, plus the two computed identifiers."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'zerotier:index/identity:Identity':
            outputs['identityId'] = f'{args.name}-node'
            outputs['publicKey'] = 'public'
            outputs['privateKey'] = 'private'
        if args.typ == 'zerotier:index/network:Network':
            outputs['networkId'] = NETWORK_ID
        declared.append((args.typ, args.name, outputs))
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> None:
    """Declare the network once, through the seam the `physical` stack calls."""
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)
    # A bridged SDK registers its own parameterized package before it may
    # register a resource, and it gates that on a feature flag read out of a
    # synchronous cache that only the async negotiation fills.
    _ = await pulumi.runtime.settings.monitor_supports_feature('parameterization')

    before = asyncio.all_tasks()
    gateway.declare_zerotier(
        NAME,
        api_token=API_TOKEN,
        network_id=NETWORK_ID,
        adguard=ADGUARD,
    )
    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def registered(name: str) -> dict[str, Any]:
    return next(inputs for _, declared_name, inputs in declared if declared_name == name)


def rules() -> str:
    return zerotier.flow_rules(udm=conventions.ZT_UDM, homelab=HOMELAB_ZT, adguard=ADGUARD)


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
    names = [entry.name for entry in conventions.ZT_ROSTER]
    node_ids = [entry.node_id for entry in conventions.ZT_ROSTER if isinstance(entry, conventions.EnrolledMember)]
    addresses = [entry.address for entry in conventions.ZT_ROSTER]

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
    for entry in conventions.ZT_ROSTER:
        if isinstance(entry, conventions.EnrolledMember):
            assert re.fullmatch(r'[0-9a-f]{10}', entry.node_id), entry.name


def test_every_member_is_placed_inside_the_overlays_own_subnet() -> None:
    """An address outside it is not on this network at all.

    ZeroTier assigns statically out of the network's managed range, so an entry
    numbered outside it is one Central would reject — and, before that, one the
    confinement rules would name to no effect.
    """
    for entry in conventions.ZT_ROSTER:
        assert entry.address in conventions.ZT_SUBNET, entry.name


def test_the_gateway_entry_is_infrastructure_at_the_address_every_client_dials() -> None:
    """Two things the gateway's entry must say on the day the ceremony adds it.

    The estate's SSH, the controller's API and every managed route's next hop
    all derive from `ZT_UDM`, so an entry at any other address would point all
    three somewhere the member is not. And the gateway is infrastructure: an
    entry carrying the permissive default role would put the box every route
    runs through on the same footing as a phone. The entry is absent until the
    ceremony reads the minted node id and adds it (physical/gateway.md §2.5),
    which is why both are stated as conditionals rather than as a lookup.
    """
    gateway_entries = [entry for entry in conventions.ZT_ROSTER if entry.name == conventions.ZT_MEMBER_UDM]

    assert all(entry.address == conventions.ZT_UDM for entry in gateway_entries)
    assert all(entry.role == conventions.ZT_ROLE_INFRA for entry in gateway_entries)


def test_the_two_continuous_integration_identities_are_generated_and_confined() -> None:
    """One identity per stack that joins, and each of them tagged for confinement.

    Sharing one identity between two jobs would flap it, since a node maps to
    one endpoint at a time; sharing a tag with anything else would hand that
    thing the same four destinations.
    """
    generated = [entry for entry in conventions.ZT_ROSTER if isinstance(entry, conventions.GeneratedMember)]

    assert [entry.name for entry in generated] == list(conventions.ZT_CI_MEMBERS)
    assert {entry.address for entry in generated} == {conventions.ZT_CI_PHYSICAL, conventions.ZT_CI_DNS}
    assert all(entry.role == conventions.ZT_ROLE_CI for entry in generated)
    assert [entry.name for entry in conventions.ZT_ROSTER if entry.role == conventions.ZT_ROLE_CI] == list(
        conventions.ZT_CI_MEMBERS
    )


def test_the_roster_stays_within_what_multicast_reaches() -> None:
    """Local discovery stops finding members past the multicast limit.

    The limit is a declared field rather than a default, so the constraint is
    on the record; this is the half that notices when the roster grows past it.
    """
    assert len(conventions.ZT_ROSTER) <= zerotier.MULTICAST_LIMIT


##
## The flow rules
##


def test_a_run_reaches_four_destinations_and_each_of_them_in_both_directions() -> None:
    """Evaluation is stateless, so a reply is a separate decision.

    An allow written only outbound produces a connection that opens and never
    answers — and the failure looks like an unreachable host rather than like a
    missing rule.
    """
    rendered = rules()
    ci = conventions.ZT_ROLE_CI
    expected = [
        (f'{conventions.ZT_UDM}/32', zerotier.SSH_PORT),
        (f'{conventions.ZT_UDM}/32', zerotier.UNIFI_API_PORT),
        (f'{ADGUARD[0]}/32', conventions.ADGUARD_API_PORT),
        (f'{ADGUARD[1]}/32', conventions.ADGUARD_API_PORT),
        (f'{HOMELAB_ZT}/32', zerotier.SSH_PORT),
    ]
    for destination, port in expected:
        assert f'accept tseq role {ci} and ipdest {destination} and dport {port};' in rendered
        assert f'accept treq role {ci} and ipsrc {destination} and sport {port};' in rendered

    # Four destinations, five flows: the gateway answers on two ports, being
    # both the box the desired state is pushed to and the controller it is
    # configured through.
    assert len({destination for destination, _ in expected}) == 4
    assert rendered.count(f'accept tseq role {ci}') == len(expected)


def test_a_run_may_reach_nothing_else_and_nothing_may_reach_a_run() -> None:
    """The drops close both directions, and they come after the allows.

    Order is the whole rule: a drop declared first would make the four allows
    unreachable, and one declared only outbound would leave a run addressable
    from any member of the network.
    """
    rendered = rules()
    ci = conventions.ZT_ROLE_CI
    lines = [line for line in rendered.splitlines() if line and not line.startswith('#')]

    assert f'drop tseq role {ci};' in lines
    assert f'drop treq role {ci};' in lines
    assert lines.index(f'drop tseq role {ci};') > max(
        index for index, line in enumerate(lines) if line.startswith(f'accept tseq role {ci}')
    )
    # And the fallthrough is last of all, or it would answer for everyone.
    assert lines[-1] == 'accept;'


def test_the_rules_never_negate_a_tag_or_an_address() -> None:
    """Negation over missing information misfires in this engine.

    A `not` combined with a tag or an address matcher inverts the zeros that
    stand for "not known yet" rather than the condition, and does so
    differently in each address family. The stock ethertype filter is the one
    exception: it ships that way and predates the quirk.
    """
    rendered = rules()
    negations = [line.strip() for line in rendered.splitlines() if 'not ' in line]

    assert negations == ['not ethertype ipv4', 'and not ethertype arp', 'and not ethertype ipv6']


def test_personal_members_are_untouched_by_every_rule_above_the_fallthrough() -> None:
    """The overlay is also the personal devices' own segment.

    Every rule the confinement adds names a tagged endpoint, so a personal
    device's traffic — unicast, broadcast and multicast discovery alike —
    reaches the final accept unchanged.
    """
    rendered = rules()
    # The base filter is a bare `drop` whose matchers are the lines under it,
    # so the block is taken out whole before the rest is examined.
    body = rendered.split('accept ethertype arp;', 1)[1]
    decisions = [line for line in body.splitlines() if line.startswith(('accept', 'drop')) and line != 'accept;']

    assert decisions, 'the confinement declared something'
    assert all(f'role {conventions.ZT_ROLE_CI}' in line for line in decisions)
    assert f'default {conventions.ZT_ROLE_PERSONAL}' in rendered
    assert f'  id {conventions.ZT_TAG_ROLE_ID}' in rendered


##
## The declaration
##


def test_the_network_is_adopted_and_carries_every_managed_route() -> None:
    """The network predates the program, and the routes are net-new.

    Creating a second network would leave every existing member on the first
    one; declaring the routes anywhere but through the gateway's member would
    put a machine that is not a router on the management path.
    """
    network = registered(f'{NAME}-network')

    assert [route['target'] for route in network['routes']] == [str(net) for net in conventions.ZT_MANAGED_ROUTES]
    assert {route['via'] for route in network['routes']} == {str(conventions.ZT_UDM)}
    assert network['private'] is True
    assert network['enableBroadcast'] is True
    assert network['multicastLimit'] == zerotier.MULTICAST_LIMIT


def test_the_census_carries_the_cluster_vlan_and_the_pool_by_name() -> None:
    """Both halves of the cluster's home addressing are reachable off-site.

    They are two subnets and two reasons: the VLAN is where a run reaches the
    worker's machine API, and the pool is where a person off-site reaches a
    service the cluster publishes on the LAN. Spelled out rather than derived,
    so renumbering either one is a visible edit here as well as in
    `conventions`.
    """
    targets = [str(net) for net in conventions.ZT_MANAGED_ROUTES]

    assert '192.168.70.0/24' in targets, 'the cluster VLAN'
    assert '192.168.71.0/24' in targets, 'the `lan` pool'
    # The pool is not a subnet anything is attached to: it is carried because
    # the gateway learns host routes into it over BGP.
    assert conventions.LAN_POOL.v4 in conventions.ZT_MANAGED_ROUTES
    assert conventions.CLUSTER_VLAN.v4 in conventions.ZT_MANAGED_ROUTES


def test_the_members_declared_are_exactly_the_roster_and_nothing_else_is_consulted() -> None:
    """A member exists because an entry exists, and for no other reason.

    That is what lets the gateway be absent during a first bring-up with no
    relaxation to switch on: there is no configured mapping the roster could
    be short against, so an entry that has not been written yet declares
    nothing and costs nothing. The routes name `ZT_UDM` as their next hop
    either way — a route to a router that has not joined yet is the ordinary
    state of a bring-up.
    """
    declared_members = {name for typ, name, _ in declared if typ == 'zerotier:index/member:Member'}

    assert declared_members == {f'{NAME}-member-{entry.name}' for entry in conventions.ZT_ROSTER}
    assert {route['via'] for route in registered(f'{NAME}-network')['routes']} == {str(conventions.ZT_UDM)}


def test_the_libvirt_flow_rule_names_the_address_the_roster_places_the_host_at() -> None:
    """The rule and the session dial one address, and the roster is that address.

    A run reaches the homelab host member to member, and the rule that lets it
    through is written from the same entry the session's URI is built from — so
    a second statement of that address, anywhere, would be free to disagree
    with the one the packets are actually matched against.
    """
    homelab = conventions.zt_member(conventions.ZT_MEMBER_HOMELAB).address
    rendered = cast('str', registered(f'{NAME}-network')['flowRules'])

    assert (
        f'accept tseq role {conventions.ZT_ROLE_CI} and ipdest {homelab}/32 and dport {zerotier.SSH_PORT};' in rendered
    )


def test_no_member_is_handed_an_address_the_roster_did_not_choose() -> None:
    """A pool assignment would move a member the rules and records name.

    Both derived IPv6 schemes are off for the same reason, and because a
    continuous-integration member with an address in a family its drop rules
    cannot see would eat its own neighbour discovery.
    """
    network = registered(f'{NAME}-network')
    assert network['assignIpv6s'] == [{'rfc4193': False, 'sixplane': False, 'zerotier': False}]

    members = [inputs for typ, _, inputs in declared if typ == 'zerotier:index/member:Member']
    assert len(members) == len(conventions.ZT_ROSTER)
    for member in members:
        assert member['noAutoAssignIps'] is True
        assert member['authorized'] is True
        assert len(member['ipAssignments']) == 1


def test_every_member_carries_a_declared_role_and_the_generated_ones_their_own_id() -> None:
    """The tag is the only thing that distinguishes a run from a laptop.

    A member declared without one would inherit the permissive default, which
    is exactly the hole the roster exists to close.
    """
    for entry in conventions.ZT_ROSTER:
        member = registered(f'{NAME}-member-{entry.name}')
        assert member['tags'] == [[conventions.ZT_TAG_ROLE_ID, entry.role]], entry.name
        assert member['name'] == entry.name

    assert registered(f'{NAME}-member-ci-physical')['memberId'] == f'{NAME}-identity-ci-physical-node'
    # An enrolled member carries the id its own device minted, straight off the
    # roster entry: there is nowhere else it could come from.
    haos = conventions.zt_member('haos')
    assert isinstance(haos, conventions.EnrolledMember)
    assert registered(f'{NAME}-member-haos')['memberId'] == haos.node_id
    assert registered(f'{NAME}-member-haos')['ipAssignments'] == [str(haos.address)]


def test_the_central_credential_belongs_to_a_provider_of_its_own() -> None:
    """The token administers the whole account, Central minting nothing smaller.

    Giving it to a provider instance rather than to the run at large is what
    bounds the resources it can reach to the ones declared here.
    """
    settings = registered(f'{NAME}-zerotier')
    token = settings['zerotierCentralToken']

    assert isinstance(token, dict), 'the token is classified as a secret, so it is never plain text in state'
    assert token['value'] == API_TOKEN
