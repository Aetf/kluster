"""The overlay's roster, routes and flow rules, asserted without Central.

The flow rules are the interesting half. They are a string in a resource, so a
reviewer sees them as a blob and a mistake in them shows up as a job that cannot
reach the gateway — or, far worse, as one that can reach everything. The suite
therefore asserts the properties the design argues for rather than the text: that
each allowed flow is declared in both directions, that a run reaches four
destinations and no fifth one, that nothing may open a connection towards a run,
and that everyone else falls through untouched.

The roster is asserted the same way: what makes it a census is that it is checked
in both directions against configuration, so both directions are tested.
"""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address
from typing import Any, cast

import pulumi
import pulumi.runtime.settings
import pytest
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions, gateway
from kluster.gateway import zerotier

NAME = 'kluster'
NETWORK_ID = '0123456789abcdef'
API_TOKEN = 'a-central-token'

HOMELAB_ZT = IPv4Address('10.144.180.10')
ADGUARD = (IPv4Address('10.0.5.11'), IPv4Address('10.0.5.12'))

#: A node identifier is ten hexadecimal digits. These are made up, which is all
#: a test needs and all a public repository should carry.
CONFIGURED: dict[str, object] = {
    'udm': {'id': 'a0a0a0a0a0'},
    'Aetf-Arch-Homelab': {'id': 'b1b1b1b1b1', 'address': str(HOMELAB_ZT)},
    'Aetf-Arch-VPS': {'id': 'c2c2c2c2c2', 'address': '10.144.160.212'},
    'haos': {'id': 'd3d3d3d3d3', 'address': '10.144.84.129'},
    'Aetf-Arch-XPS': {'id': 'e4e4e4e4e4', 'address': '10.144.175.24'},
    'Aetf-Win-XPS': {'id': 'f5f5f5f5f5', 'address': '10.144.175.25'},
    'Aetf-Laptop': {'id': '0606060606', 'address': '10.144.127.147'},
    'Aetf-Handheld': {'id': '1717171717', 'address': '10.144.127.148'},
    'PC-Homelab': {'id': '2828282828', 'address': '10.144.180.11'},
    'OnePlus6T': {'id': '3939393939', 'address': '10.144.160.97'},
    'Pixel 7 Pro': {'id': '4a4a4a4a4a', 'address': '10.144.160.98'},
    'S26 Ultra': {'id': '5b5b5b5b5b', 'address': '10.144.160.99'},
}

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
        members=zerotier.parse_members(CONFIGURED),
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


def test_the_roster_and_the_configuration_check_each_other() -> None:
    """A census is only a census if it is closed in both directions.

    A member configured but not rostered would join with no declared role and
    inherit the permissive default; a member rostered but not configured would
    be a name the program believes in and the network has never heard of.
    """
    parsed = zerotier.parse_members(CONFIGURED)
    assert set(parsed) == {entry.name for entry in zerotier.ROSTER if not entry.generated}

    with pytest.raises(ValueError, match='intruder is not on the ZeroTier roster'):
        zerotier.parse_members({**CONFIGURED, 'intruder': {'id': '9999999999', 'address': '10.144.9.9'}})
    with pytest.raises(ValueError, match='no configured node id for haos'):
        zerotier.parse_members({name: facts for name, facts in CONFIGURED.items() if name != 'haos'})
    with pytest.raises(ValueError, match='takes no configured node id'):
        zerotier.parse_members({**CONFIGURED, 'ci-dns': {'id': '8888888888'}})


def test_an_address_is_either_a_convention_or_a_configured_fact_never_both() -> None:
    """The gateway's address is the one the desired-state push dials.

    If configuration could move it, the estate would be pushed to one address
    and the overlay would route to another, and the two would disagree in
    whichever direction nobody checked.
    """
    assert zerotier.parse_members(CONFIGURED)['udm'].address is None

    with pytest.raises(ValueError, match="udm's address is a convention"):
        zerotier.parse_members({**CONFIGURED, 'udm': {'id': 'a0a0a0a0a0', 'address': '10.144.1.9'}})
    with pytest.raises(ValueError, match='carries no address'):
        zerotier.parse_members({**CONFIGURED, 'haos': {'id': 'd3d3d3d3d3'}})


def test_the_two_continuous_integration_identities_are_generated_and_confined() -> None:
    """One identity per stack that joins, and each of them tagged for confinement.

    Sharing one identity between two jobs would flap it, since a node maps to
    one endpoint at a time; sharing a tag with anything else would hand that
    thing the same four destinations.
    """
    generated = [entry for entry in zerotier.ROSTER if entry.generated]

    assert [entry.name for entry in generated] == list(zerotier.CI_MEMBERS)
    assert {entry.address for entry in generated} == {conventions.ZT_CI_PHYSICAL, conventions.ZT_CI_DNS}
    assert all(entry.role == conventions.ZT_ROLE_CI for entry in generated)
    assert [entry.name for entry in zerotier.ROSTER if entry.role == conventions.ZT_ROLE_CI] == list(
        zerotier.CI_MEMBERS
    )


def test_the_roster_stays_within_what_multicast_reaches() -> None:
    """Local discovery stops finding members past the multicast limit.

    The limit is a declared field rather than a default, so the constraint is
    on the record; this is the half that notices when the roster grows past it.
    """
    assert len(zerotier.ROSTER) <= zerotier.MULTICAST_LIMIT


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
        (f'{ADGUARD[0]}/32', zerotier.ADGUARD_API_PORT),
        (f'{ADGUARD[1]}/32', zerotier.ADGUARD_API_PORT),
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


def test_no_member_is_handed_an_address_the_roster_did_not_choose() -> None:
    """A pool assignment would move a member the rules and records name.

    Both derived IPv6 schemes are off for the same reason, and because a
    continuous-integration member with an address in a family its drop rules
    cannot see would eat its own neighbour discovery.
    """
    network = registered(f'{NAME}-network')
    assert network['assignIpv6s'] == [{'rfc4193': False, 'sixplane': False, 'zerotier': False}]

    members = [inputs for typ, _, inputs in declared if typ == 'zerotier:index/member:Member']
    assert len(members) == len(zerotier.ROSTER)
    for member in members:
        assert member['noAutoAssignIps'] is True
        assert member['authorized'] is True
        assert len(member['ipAssignments']) == 1


def test_every_member_carries_a_declared_role_and_the_generated_ones_their_own_id() -> None:
    """The tag is the only thing that distinguishes a run from a laptop.

    A member declared without one would inherit the permissive default, which
    is exactly the hole the roster exists to close.
    """
    for entry in zerotier.ROSTER:
        member = registered(f'{NAME}-member-{entry.name}')
        assert member['tags'] == [[conventions.ZT_TAG_ROLE_ID, entry.role]], entry.name
        assert member['name'] == entry.name

    assert registered(f'{NAME}-member-ci-physical')['memberId'] == f'{NAME}-identity-ci-physical-node'
    assert registered(f'{NAME}-member-udm')['memberId'] == 'a0a0a0a0a0'
    assert registered(f'{NAME}-member-udm')['ipAssignments'] == [str(conventions.ZT_UDM)]


def test_the_central_credential_belongs_to_a_provider_of_its_own() -> None:
    """The token administers the whole account, Central minting nothing smaller.

    Giving it to a provider instance rather than to the run at large is what
    bounds the resources it can reach to the ones declared here.
    """
    settings = registered(f'{NAME}-zerotier')
    token = settings['zerotierCentralToken']

    assert isinstance(token, dict), 'the token is classified as a secret, so it is never plain text in state'
    assert token['value'] == API_TOKEN
