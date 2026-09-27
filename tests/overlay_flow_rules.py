"""The overlay's rule program, rendered over addresses a suite can name, and a reading of it.

`flow_rules` is a pure function of what it is handed, and the program it returns
is rendered over whatever node addresses it is given, so the addresses here are
literals rather than the census's or the mint's: a suite asserting what the
program says about them reads them from here, beside the call that hands them
over.

`verdict` reads the rendered program the way the engine does, first match
wins, for the matchers the program uses and no others: a rule with any other
matcher is refused rather than read as matching or not, so the reading cannot
go on answering for a program that has outgrown it. It takes no tags because
the program names none, which a case asserts on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Literal, final

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

#: The node addresses the program is rendered over: what `Overlay` would mint
#: for the two continuous-integration identities, and what the other members'
#: daemons minted for themselves.
CI_PHYSICAL_NODE = 'a1a1a1a1a1'
CI_DNS_NODE = 'b2b2b2b2b2'
GATEWAY_NODE = 'c3c3c3c3c3'
HOMELAB_NODE = 'd4d4d4d4d4'
PERSONAL_NODE = 'e5e5e5e5e5'
NODE_IDS = {conventions.overlay.MEMBER_CI_PHYSICAL: CI_PHYSICAL_NODE, conventions.overlay.MEMBER_CI_DNS: CI_DNS_NODE}


def program() -> rules_module.FlowRules:
    return rules_module.flow_rules(
        gateway_overlay_address=GATEWAY_OVERLAY,
        homelab_overlay_address=HOMELAB_OVERLAY,
        resolver_site_addresses=RESOLVERS,
    )


def rules() -> str:
    """The program as Central receives it, over `NODE_IDS`."""
    return program().render(NODE_IDS)


@final
@dataclass(frozen=True)
class Packet:
    """One frame as one end of it sees it.

    `ztsrc` and `ztdest` are the node addresses of the member that sent it and
    the one it is going to. `ipauth` is the engine's ownership check: whether
    the sender holds the address the frame speaks from (its IPv4 source, or an
    ARP sender address).
    """

    ztsrc: str
    ztdest: str
    ipsrc: IPv4Address
    ipdest: IPv4Address
    sport: int = 0
    dport: int = 0
    ethertype: Literal['ipv4', 'arp'] = 'ipv4'
    ipauth: bool = True


def rule_lines(rendered: str) -> list[str]:
    """Every rule after the stock base filter, each of which is one line.

    Raises:
        ValueError: a rule continues onto another line. Its first line alone
            would read as an unconditional action, so it is refused rather
            than read.
    """
    body = rendered.split('and not ethertype ipv6\n;', 1)[1]
    lines = [line for line in body.splitlines() if line.startswith(('accept', 'drop'))]
    unfinished = [line for line in lines if not line.endswith(';')]
    if unfinished:
        raise ValueError(f'rules this reading does not model, continued past their first line: {unfinished}')
    return lines


def _matches(matcher: str, packet: Packet) -> bool:
    match matcher.split():
        case ['ethertype', kind]:
            return packet.ethertype == kind
        case ['chr', 'ipauth']:
            return packet.ipauth
        case ['ztsrc', node]:
            return packet.ztsrc == node
        case ['ztdest', node]:
            return packet.ztdest == node
        case ['ipsrc', network]:
            return packet.ethertype == 'ipv4' and packet.ipsrc in IPv4Network(network)
        case ['ipdest', network]:
            return packet.ethertype == 'ipv4' and packet.ipdest in IPv4Network(network)
        case ['sport', port]:
            return packet.ethertype == 'ipv4' and packet.sport == int(port)
        case ['dport', port]:
            return packet.ethertype == 'ipv4' and packet.dport == int(port)
        case _:
            raise ValueError(f'{matcher!r} is a matcher this reading does not model')


def verdict(rendered: str, packet: Packet) -> Literal['accept', 'drop']:
    """What the program decides for `packet`: the action of the first rule it matches."""
    for line in rule_lines(rendered):
        action, _, rest = line.rstrip(';').partition(' ')
        if all(_matches(matcher, packet) for matcher in rest.split(' and ') if rest):
            return 'accept' if action == 'accept' else 'drop'
    raise AssertionError('the program ends in no rule this packet matched')
