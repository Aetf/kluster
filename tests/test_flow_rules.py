"""The overlay's rule program, asserted as properties rather than as text.

The rules are a string in a resource, so a reviewer sees them as a blob and a
mistake in them shows up as a job that cannot reach the gateway — or, far worse,
as one that can reach everything. These cases therefore assert what the design
argues for rather than the rendering: that each continuous-integration identity
reaches what its own stack needs in both directions and nothing else, that
nothing may open a connection toward a run, that none of it rests on a tag the
member itself would have to present, that everyone else falls through untouched
— and that wherever a run is at either end, a member speaks only for the
addresses assigned to it.

`flow_rules` is a pure function of what it is handed, so the addresses the
cases hand it and the node addresses the program is rendered over are literals,
kept in `overlay_flow_rules` beside the reading of the program the cases put
packets through. That the roster and the resolver census are what hand the
addresses over, and that the component renders the program over the identities
it mints, are facts about the stack program and the component and are asserted
there.
"""

from __future__ import annotations

import re
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from overlay_flow_rules import (
    CI_DNS_NODE,
    CI_PHYSICAL_NODE,
    GATEWAY_NODE,
    GATEWAY_OVERLAY,
    HOMELAB_NODE,
    HOMELAB_OVERLAY,
    NODE_IDS,
    PERSONAL_NODE,
    RESOLVERS,
    Packet,
    program,
    rule_lines,
    rules,
    verdict,
)

from kluster import conventions
from kluster.components import talos
from kluster.components.overlay import flow_rules as rules_module

CI_PHYSICAL = conventions.overlay.CI_PHYSICAL
CI_DNS = conventions.overlay.CI_DNS
SSH = rules_module.SSH_PORT
UNIFI = rules_module.UNIFI_API_PORT
ADGUARD = conventions.gateway.ADGUARD_API_PORT

#: A source port a client picks, standing for any of them.
EPHEMERAL = 40000

#: Which member answers at each destination: the gateway for its own address
#: and for the resolvers it routes to, the homelab host for its own.
ANSWERED_BY = {GATEWAY_OVERLAY: GATEWAY_NODE, HOMELAB_OVERLAY: HOMELAB_NODE, **dict.fromkeys(RESOLVERS, GATEWAY_NODE)}

#: Each identity, and every destination its stack calls.
REACHES = {
    CI_PHYSICAL_NODE: (CI_PHYSICAL, [(GATEWAY_OVERLAY, SSH), (GATEWAY_OVERLAY, UNIFI), (HOMELAB_OVERLAY, SSH)]),
    CI_DNS_NODE: (CI_DNS, [(resolver, ADGUARD) for resolver in RESOLVERS]),
}

#: The design's copy of the program, and the example addresses it is written in.
GATEWAY_MD = Path(__file__).parent.parent / 'docs' / 'physical' / 'gateway.md'
DRAFT_PLACEHOLDERS = {
    CI_PHYSICAL_NODE: '<ci-physical-node>',
    CI_DNS_NODE: '<ci-dns-node>',
    f'{GATEWAY_OVERLAY}/32': '<udm-zt-ip>/32',
    f'{HOMELAB_OVERLAY}/32': '<homelab-host>/32',
    f'{RESOLVERS[0]}/32': '<adguard-alice>/32',
    f'{RESOLVERS[1]}/32': '<adguard-bob>/32',
    f'dport {UNIFI}': 'dport <unifi-api>',
    f'sport {UNIFI}': 'sport <unifi-api>',
    f'dport {ADGUARD}': 'dport <adguard-api>',
    f'sport {ADGUARD}': 'sport <adguard-api>',
}


def _outbound(node: str, source: IPv4Address, destination: IPv4Address, port: int) -> Packet:
    return Packet(node, ANSWERED_BY[destination], source, destination, sport=EPHEMERAL, dport=port)


def _reply(node: str, source: IPv4Address, destination: IPv4Address, port: int) -> Packet:
    return Packet(ANSWERED_BY[destination], node, destination, source, sport=port, dport=EPHEMERAL)


def test_each_identity_reaches_what_its_own_stack_calls_in_both_directions() -> None:
    """Evaluation is stateless, so a reply is a separate decision.

    An allow written only outbound produces a connection that opens and never
    answers — and the failure looks like an unreachable host rather than like a
    missing rule. `physical` pushes the gateway's desired state over its shell,
    configures the firewall through the controller API and opens the libvirt
    session over the homelab host's shell; `dns` writes the rewrites to each
    resolver's API.
    """
    rendered = rules()

    for node, (source, destinations) in REACHES.items():
        for destination, port in destinations:
            assert verdict(rendered, _outbound(node, source, destination, port)) == 'accept', (node, destination)
            assert verdict(rendered, _reply(node, source, destination, port)) == 'accept', (node, destination)
    # Every accept that names an identity is one of those legs, and no other.
    legs = [line for line in rule_lines(rendered) if line.startswith(('accept ztsrc', 'accept ztdest'))]
    assert len(legs) == 2 * sum(len(destinations) for _, destinations in REACHES.values())


def test_each_identity_reaches_nothing_the_other_ones_legs_name() -> None:
    """A leaked `dns` identity is the resolvers' API and nothing more.

    The two identities are two stacks' worth of reach, and the point of keeping
    them apart is that one of them leaking does not buy the other's — in
    particular, the `dns` identity opens neither the gateway's shell nor the
    homelab host's.
    """
    rendered = rules()

    for node, (source, own) in REACHES.items():
        others = [flow for other, (_, flows) in REACHES.items() if other != node for flow in flows]
        for destination, port in others:
            assert (destination, port) not in own
            assert verdict(rendered, _outbound(node, source, destination, port)) == 'drop', (node, destination)
    # And the named case, spelled out.
    assert verdict(rendered, _outbound(CI_DNS_NODE, CI_DNS, GATEWAY_OVERLAY, SSH)) == 'drop'
    assert verdict(rendered, _outbound(CI_DNS_NODE, CI_DNS, HOMELAB_OVERLAY, SSH)) == 'drop'


def test_a_run_may_reach_nothing_else_and_nothing_may_reach_a_run() -> None:
    """The drops close both directions for each identity, after its legs.

    Order is the whole rule: a drop declared first would make the legs
    unreachable, and one declared only outbound would leave a run addressable
    from any member of the network.
    """
    rendered = rules()
    lines = rule_lines(rendered)
    last_leg = max(index for index, line in enumerate(lines) if line.startswith(('accept ztsrc', 'accept ztdest')))

    for node, (source, _) in REACHES.items():
        assert lines.index(f'drop ztsrc {node};') > last_leg
        assert lines.index(f'drop ztdest {node};') > last_leg
        # A LAN host no leg names, and a personal member opening a connection
        # toward the run.
        stray = Packet(node, GATEWAY_NODE, source, IPv4Address('10.0.1.10'), sport=EPHEMERAL, dport=SSH)
        inbound = Packet(PERSONAL_NODE, node, IPv4Address('10.144.175.24'), source, sport=EPHEMERAL, dport=SSH)
        assert verdict(rendered, stray) == 'drop'
        assert verdict(rendered, inbound) == 'drop'
    # And the fallthrough is last of all, or it would answer for everyone.
    assert lines[-1] == 'accept;'


def test_no_run_reaches_the_worker_and_a_personal_member_does() -> None:
    """The worker's machine API is reached through a control plane, so no leg names the worker.

    The `physical` stack dials the worker's configuration apply at the balancer
    and names the worker only as the node a control plane proxies the call to
    (`stacks/physical.py`); nothing a run does opens a connection into the
    cluster VLAN. So every port the worker's firewall opens is closed to both
    identities, in both directions. The cluster VLAN's managed route is for a
    person off-site, whose traffic to the worker's machine API falls through to
    the final accept.
    """
    rendered = rules()
    worker = conventions.HOMELAB_NODE_IPV4
    ports = sorted({*talos.HOST_PORTS, talos.BGP_PORT})

    assert conventions.MANAGEMENT_PORTS.talos in ports
    for node, (source, _) in REACHES.items():
        for port in ports:
            outbound = Packet(node, GATEWAY_NODE, source, worker, sport=EPHEMERAL, dport=port)
            reply = Packet(GATEWAY_NODE, node, worker, source, sport=port, dport=EPHEMERAL)
            assert verdict(rendered, outbound) == 'drop', (node, port)
            assert verdict(rendered, reply) == 'drop', (node, port)
    personal = IPv4Address('10.144.175.24')
    machine_api = conventions.MANAGEMENT_PORTS.talos
    outbound = Packet(PERSONAL_NODE, GATEWAY_NODE, personal, worker, sport=EPHEMERAL, dport=machine_api)
    reply = Packet(GATEWAY_NODE, PERSONAL_NODE, worker, personal, sport=machine_api, dport=EPHEMERAL, ipauth=False)
    assert verdict(rendered, outbound) == 'accept'
    assert verdict(rendered, reply) == 'accept'


def test_no_rule_rests_on_a_tag_the_member_would_have_to_present() -> None:
    """A member that never pushes its tag is confined exactly as one that does.

    A tag is a credential the sender pushes to its peer, and the peer admits a
    member on its certificate of membership alone. A rule on the sender's tag
    therefore fails at a receiver that was never shown it, and a drop on the
    receiver's tag is skipped by a sender that was never shown it — so a holder
    of a leaked identity who withholds the tag would pass every such drop and
    reach the fallthrough. The confinement names each identity by the node
    address of the packet's authenticated peer instead, and no rule consults a
    tag at all: the reading `verdict` gives, which takes no tags, is the whole
    of what the engine decides.
    """
    # Everything after the tag's own declaration, whatever line a matcher sits on.
    body = rules().split('\n;\n', 1)[1]
    assert not re.search(r'\bt(seq|req|and|or|xor|diff|eq)\b', body)


def test_the_reading_refuses_a_rule_it_would_misread() -> None:
    """A rule continued onto a second line would read as its first line, an unconditional action.

    The reading models single-line rules and the matchers the program uses; a
    program that outgrows either is refused, so no case above answers for a
    program the reading cannot see.
    """
    split = rules().replace(f'drop ztsrc {CI_DNS_NODE};', f'drop\n  ztsrc {CI_DNS_NODE};')

    with pytest.raises(ValueError, match='continued'):
        rule_lines(split)
    with pytest.raises(ValueError, match='does not model'):
        verdict(rules().replace('accept;', 'accept tseq role 2;'), Packet(PERSONAL_NODE, HOMELAB_NODE, CI_DNS, CI_DNS))


def test_a_run_and_the_members_it_reaches_speak_only_from_their_own_addresses() -> None:
    """A reply from a member's address is that member's, or it is dropped.

    The run carries the controller's API key to the gateway's overlay address
    over a connection that does not verify the controller's certificate, so
    whoever can complete a handshake from that address receives the key. The
    check that the sender holds the address is the engine's `chr ipauth`, and
    it goes on every leg whose source is an address assigned to a member: a
    run's own address outbound, and the gateway's and the homelab host's on the
    way back.

    A resolver is the other way round. It is no member, so its reply is
    forwarded by the gateway from an address assigned to no one, and the same
    check there would sever the rewrites rather than guard them.
    """
    rendered = rules()

    for node, (source, destinations) in REACHES.items():
        for destination, port in destinations:
            outbound = _outbound(node, source, destination, port)
            reply = _reply(node, source, destination, port)
            assert f'accept ztsrc {node} and ipdest {destination}/32 and dport {port} and chr ipauth;' in rendered
            assert verdict(rendered, replace(outbound, ipauth=False)) == 'drop'
            routed = destination in RESOLVERS
            ownership = '' if routed else ' and chr ipauth'
            assert f'accept ztdest {node} and ipsrc {destination}/32 and sport {port}{ownership};' in rendered
            spoofed = verdict(rendered, replace(reply, ipauth=False))
            assert spoofed == ('accept' if routed else 'drop'), (node, destination)


def test_no_member_answers_arp_for_an_address_it_does_not_hold_where_a_run_is_involved() -> None:
    """ARP is how a member would stand in for the gateway, so ARP is checked too.

    A member that answers a run's ARP for the gateway's overlay address receives
    everything the run sends through the gateway, the rewrites' traffic to the
    resolvers included, which the return-leg check cannot see because those
    replies come from routed addresses. So an ARP whose sender does not hold the
    address it claims is dropped when a run sends or receives it, and only then:
    the drops come after the authenticated accept and before the fallthrough
    that keeps every other member's ARP as a LAN has it.
    """
    rendered = rules()
    lines = rule_lines(rendered)
    arp = [line for line in lines if 'ethertype arp' in line]

    assert arp == [
        'accept ethertype arp and chr ipauth;',
        *(f'drop ethertype arp and {end} {node};' for node in NODE_IDS.values() for end in ('ztsrc', 'ztdest')),
        'accept ethertype arp;',
    ]
    # Ahead of every rule that names an address, so ARP never reaches them.
    assert lines.index('accept ethertype arp;') < min(
        index for index, line in enumerate(lines) if ' ipdest ' in line or ' ipsrc ' in line
    )
    for node, (source, _) in REACHES.items():
        impostor = Packet(PERSONAL_NODE, node, GATEWAY_OVERLAY, source, ethertype='arp', ipauth=False)
        gateway = Packet(GATEWAY_NODE, node, GATEWAY_OVERLAY, source, ethertype='arp')
        assert verdict(rendered, impostor) == 'drop'
        assert verdict(rendered, gateway) == 'accept'
    # Two personal members, one forwarding for an address it does not hold:
    # the LAN's posture.
    forwarded = Packet(
        GATEWAY_NODE, PERSONAL_NODE, IPv4Address('10.0.1.1'), GATEWAY_OVERLAY, ethertype='arp', ipauth=False
    )
    assert verdict(rendered, forwarded) == 'accept'


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

    Every drop the confinement adds names a continuous-integration identity's
    node address, so a personal device's traffic — unicast, broadcast and
    multicast discovery alike, and the ARP beneath them — reaches an accept
    unchanged.
    """
    rendered = rules()
    decisions = rule_lines(rendered)
    drops = [line for line in decisions if line.startswith('drop')]
    confined = tuple(f'{end} {node}' for node in NODE_IDS.values() for end in ('ztsrc', 'ztdest'))

    assert drops, 'the confinement declared something'
    assert all(line.removesuffix(';').endswith(confined) for line in drops)
    # The one accept that names no identity passes only what every member
    # sends of its own, and the ARP fallthrough right after the drops takes
    # the rest.
    unnamed = [line for line in decisions if not any(node in line for node in NODE_IDS.values())]
    assert unnamed == ['accept ethertype arp and chr ipauth;', 'accept ethertype arp;', 'accept;']
    personal = Packet(PERSONAL_NODE, HOMELAB_NODE, IPv4Address('10.144.175.24'), HOMELAB_OVERLAY, dport=SSH)
    assert verdict(rendered, personal) == 'accept'
    assert f'default {conventions.overlay.Role.PERSONAL}' in rendered
    assert f'  id {conventions.overlay.TAG_ROLE_ID}' in rendered


def test_the_program_confines_every_continuous_integration_member_of_the_roster() -> None:
    """A continuous-integration member the program did not name would have no rules at all.

    The fallthrough is permissive, so an identity the rules forget is an
    identity with a personal device's reach. The program names members by
    roster name and is rendered over exactly those, so the names it confines are
    the roster's continuous-integration members, every one of them.
    """
    ci = [entry.name for entry in conventions.overlay.ROSTER if entry.role == conventions.overlay.Role.CI]

    assert sorted(program().members) == sorted(ci) == sorted(conventions.overlay.CI_MEMBERS)


def test_the_rendered_program_names_each_identity_by_the_node_address_it_was_handed() -> None:
    """The program leaves nothing open once it is rendered.

    What the component hands it is the node address of each identity it
    minted, and a rendering that kept a placeholder, or named an identity by
    anything else, would be a program Central either refuses or reads as a
    rule about no member at all.
    """
    rendered = rules()

    for node in NODE_IDS.values():
        assert f'ztsrc {node} ' in rendered
        assert f'ztdest {node} ' in rendered
    assert not re.search(r'[<>{}]', rendered)
    for line in rule_lines(rendered):
        for node in re.findall(r'\bzt(?:src|dest) ([^\s;]+)', line):
            assert node in NODE_IDS.values(), line


def test_the_program_is_rendered_over_exactly_the_members_it_confines() -> None:
    """A node address missing, or one for a member the program does not name, is refused.

    A missing one would otherwise leave that identity with no rule naming it,
    and so with the fallthrough's reach.
    """
    with pytest.raises(ValueError, match='confine'):
        program().render({conventions.overlay.MEMBER_CI_PHYSICAL: CI_PHYSICAL_NODE})
    with pytest.raises(ValueError, match='confine'):
        program().render({**NODE_IDS, 'someone-else': 'f6f6f6f6f6'})


def test_the_design_draft_is_the_rendered_program() -> None:
    """gateway.md §2.3 carries the program as a reader reviews it, and it is this one.

    The draft is written over example addresses and node addresses a reader can
    tell apart, and with comments of its own; with both put back and the
    comments and column alignment taken out, it is the rendered program line
    for line.
    """
    text = GATEWAY_MD.read_text()
    section = text.split('### 2.3 ', 1)[1].split('\n### ', 1)[0]
    draft = section.split('```text\n', 1)[1].split('\n```', 1)[0]

    def normalized(program_text: str) -> list[str]:
        lines = (re.sub(r'\s+', ' ', line.split('#', 1)[0]).strip() for line in program_text.splitlines())
        return [line for line in lines if line]

    rendered = rules()
    for value, placeholder in DRAFT_PLACEHOLDERS.items():
        rendered = rendered.replace(value, placeholder)
    assert normalized(draft) == normalized(rendered)


def test_every_role_the_overlay_declares_is_spelled_out_for_the_engine() -> None:
    """The tag's enumeration is what makes a member's role readable in Central.

    A role added to `conventions` and not to the tag would render as a bare
    number in the one place an operator reads the network's members, and a rule
    naming it by label would not parse.
    """
    rendered = rules()

    for role in conventions.overlay.Role:
        assert f'  enum {role.value} {role.name.lower()}' in rendered
    assert rules_module.roles() == {role.name.lower(): role.value for role in conventions.overlay.Role}
