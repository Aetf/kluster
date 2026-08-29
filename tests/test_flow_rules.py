"""The overlay's rule program, asserted as properties rather than as text.

The rules are a string in a resource, so a reviewer sees them as a blob and a
mistake in them shows up as a job that cannot reach the gateway — or, far worse,
as one that can reach everything. These cases therefore assert what the design
argues for rather than the rendering: that each allowed flow is declared in both
directions, that a run reaches four destinations and no fifth one, that nothing
may open a connection towards a run, and that everyone else falls through
untouched.

`flow_rules` is a pure function of what it is handed, so the addresses here are
literals. That the roster and the resolver census are what hand them over is a
fact about the stack program and is asserted there.
"""

from __future__ import annotations

from ipaddress import IPv4Address

from kluster import conventions
from kluster.components.overlay import flow_rules as rules_module

#: The gateway's overlay address, as a member of the network holds one.
GATEWAY_OVERLAY = IPv4Address('10.144.1.1')
#: The homelab host's overlay address: the libvirt session reaches it member to
#: member, needing no managed route.
HOMELAB_OVERLAY = IPv4Address('10.144.180.10')
#: The resolvers' container-VLAN addresses. They are named at their site
#: addresses because they are containers on the device rather than members of
#: the overlay, and a routed packet still carries the destination it had before
#: the forward.
RESOLVERS = (IPv4Address('10.0.5.11'), IPv4Address('10.0.5.12'))


def rules() -> str:
    return rules_module.flow_rules(
        gateway_overlay_address=GATEWAY_OVERLAY,
        homelab_overlay_address=HOMELAB_OVERLAY,
        resolver_site_addresses=RESOLVERS,
    )


def test_a_run_reaches_four_destinations_and_each_of_them_in_both_directions() -> None:
    """Evaluation is stateless, so a reply is a separate decision.

    An allow written only outbound produces a connection that opens and never
    answers — and the failure looks like an unreachable host rather than like a
    missing rule.
    """
    rendered = rules()
    ci = conventions.overlay.Role.CI
    expected = [
        (f'{GATEWAY_OVERLAY}/32', rules_module.SSH_PORT),
        (f'{GATEWAY_OVERLAY}/32', rules_module.UNIFI_API_PORT),
        (f'{RESOLVERS[0]}/32', conventions.gateway.ADGUARD_API_PORT),
        (f'{RESOLVERS[1]}/32', conventions.gateway.ADGUARD_API_PORT),
        (f'{HOMELAB_OVERLAY}/32', rules_module.SSH_PORT),
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
    ci = conventions.overlay.Role.CI
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
    assert all(f'role {conventions.overlay.Role.CI}' in line for line in decisions)
    assert f'default {conventions.overlay.Role.PERSONAL}' in rendered
    assert f'  id {conventions.overlay.TAG_ROLE_ID}' in rendered


def test_every_role_the_overlay_declares_is_spelled_out_for_the_engine() -> None:
    """The tag's enumeration is what makes a rule readable in Central.

    A role added to `conventions` and not to the tag would render as a bare
    number in the one place an operator reads the network's policy, and a rule
    naming it by label would not parse.
    """
    rendered = rules()

    for role in conventions.overlay.Role:
        assert f'  enum {role.value} {role.name.lower()}' in rendered
    assert rules_module.roles() == {role.name.lower(): role.value for role in conventions.overlay.Role}
