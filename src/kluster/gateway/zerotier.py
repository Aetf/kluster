"""The ZeroTier network the gateway routes for: membership, routes, flow rules.

The overlay is how the home site is reached from anywhere that is not the home
site — by a person on a phone, and by a continuous-integration job that has to
configure the gateway itself. Three things are declared here, and each answers a
different question (gateway.md §2):

-   **Membership** — who is authorized, at what address, carrying which role
    tag. Membership is the authentication boundary for traffic the gateway's
    own firewall never classifies, so it is not bookkeeping: it is the
    admission decision. *Who* may be on the network is not decided here — the
    roster is `conventions.ZT_ROSTER`, because the `dns` stack publishes the
    `*.zt` host block from the same table — and this module is what turns that
    decision into authorized members.
-   **The managed routes** — which of the home's subnets the overlay carries,
    all of them through the gateway's own member, which is the one machine that
    was already routing them.
-   **The flow rules** — what a member may do once admitted. Two of the members
    are continuous-integration identities, and the rules confine each of them
    to exactly the four destinations its work needs, so that a leaked join
    credential does not buy general access to the LAN.

**Admission is checked in both directions.** A name in configuration that the
roster does not carry is refused, and a roster entry with nothing configured for
it is refused as well: both are the same mistake seen from opposite sides, and
the first of them is what keeps the role tag's permissive default out of reach
of anything undeclared. The single exception is a device whose identity does not
exist yet, because a ZeroTier identity is minted by the daemon's first run: a
caller that is delivering that daemon says so by name (`parse_members`,
`unminted`), and that member is left out of the desired state until it has been.

**Two continuous-integration identities, not one.** ZeroTier maps a node to one
endpoint at a time, so an identity live in two jobs at once flaps; there is one
identity per stack that joins — the one that configures the physical layer and
the one that writes the resolvers' rewrites — and each is serialized by a
job-level concurrency group named after it (gateway.md §2.6). Their key material
is generated in state rather than pre-authorized by hand, which is what keeps
the network-administration token out of every environment that joins.

**The rules are written in positive matches only.** The engine evaluates every
packet independently at both ends and keeps no connection state, so each allowed
flow is declared twice — once outbound, once as its own return leg — and a
negated matcher is avoided because negation over an unknown tag or an absent
address family inverts missing information rather than the intended condition
(ZeroTierOne #2200). The stock base filter is the one exception; it predates the
quirk and is left as the engine ships it.

**The overlay is IPv4-only.** No member carries a v6 assignment and no v6 route
is managed, which is what makes the confinement rules complete: a continuous-
integration member with a v6 address would have its own neighbour discovery
eaten by its own drop rule, and there is nothing on the overlay for it to reach
over v6 that it cannot reach over v4.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import final

import pulumi
import pulumi_zerotier as zerotier

from kluster import conventions
from kluster.gateway import facts
from putils import Component

__all__ = (
    'ADGUARD_API_PORT',
    'MULTICAST_LIMIT',
    'SSH_PORT',
    'UNIFI_API_PORT',
    'Enrolled',
    'Network',
    'flow_rules',
    'parse_members',
    'roles',
)

#: The gateway's own management ports, as reached over the overlay: the shell
#: the desired-state push writes through, and the controller API the firewall
#: resources call. Both terminate on the gateway itself.
SSH_PORT = 22
UNIFI_API_PORT = 443

#: The resolvers' administration port. The rewrites the `dns` stack writes go
#: here, which is why a continuous-integration member may reach it — and the
#: reason the estate and the rules have to agree on one number.
ADGUARD_API_PORT = 3000

#: The largest number of recipients a multicast or broadcast reaches. It has to
#: be at least the size of the roster or local discovery quietly stops finding
#: the last members to answer; the check below is why roster growth cannot
#: break it silently.
MULTICAST_LIMIT = 32


def roles() -> Mapping[str, int]:
    """The role enumeration as the rules language spells it."""
    return {
        'personal': conventions.ZT_ROLE_PERSONAL,
        'infra': conventions.ZT_ROLE_INFRA,
        'ci': conventions.ZT_ROLE_CI,
    }


@final
@dataclass(frozen=True)
class Enrolled:
    """The facts about an existing member that this program cannot derive.

    A node identifier is minted by the device when ZeroTier first runs on it,
    and the addresses of members that predate this program are already written
    into whatever names them. Both are read from stack configuration; the role
    is not, because the role is a decision.
    """

    node_id: str
    address: IPv4Address | None = None


def parse_members(raw: object, *, unminted: Collection[str] = ()) -> dict[str, Enrolled]:
    """Read the configured member facts, and refuse anything the roster omits.

    Both directions are checked here rather than at the resource, because both
    are the same mistake seen from opposite sides: a member configured but not
    rostered would be authorized without a declared role, and a member rostered
    but not configured would be a hole in the census that nothing else notices.

    `unminted` names entries whose node identifier does not exist yet, and it
    relaxes the second check for those names only. A ZeroTier identity comes
    into being when the daemon first runs on a device, so a device this program
    is *delivering* the daemon to has nothing to configure until the delivery
    has happened; naming it here is how a caller says so, and saying so is a
    deliberate act rather than the default. A name given here that turns out to
    be configured after all is read like any other — the relaxation is
    permission to be absent, not a refusal to look.
    """
    entries = facts.mapping(raw, 'the ZeroTier member configuration')

    rostered = {entry.name: entry for entry in conventions.ZT_ROSTER}
    unknown = sorted(set(entries) - set(rostered))
    if unknown:
        raise ValueError(f'{", ".join(unknown)} is not on the ZeroTier roster, so it cannot be authorized')
    generated = sorted(name for name in entries if rostered[name].generated)
    if generated:
        raise ValueError(f'{", ".join(generated)} has a generated identity, so it takes no configured node id')
    expected = [entry.name for entry in conventions.ZT_ROSTER if not entry.generated]
    missing = [name for name in expected if name not in entries and name not in unminted]
    if missing:
        raise ValueError(f'the ZeroTier roster has no configured node id for {", ".join(missing)}')

    return {name: _member(name, rostered[name], entries[name]) for name in expected if name in entries}


def _member(name: str, entry: conventions.ZtMember, raw: object) -> Enrolled:
    what = f'the ZeroTier facts for {name}'
    configured = facts.mapping(raw, what)
    node_id = facts.text(configured, 'id', what)
    if entry.address is not None:
        if 'address' in configured:
            raise ValueError(f"{name}'s address is a convention of this program and is not configured")
        return Enrolled(node_id=node_id)
    return Enrolled(node_id=node_id, address=IPv4Address(facts.text(configured, 'address', what)))


def flow_rules(*, udm: IPv4Address, homelab: IPv4Address, adguard: Sequence[IPv4Address]) -> str:
    """The network's rules: a base filter, the confinement, and a fallthrough.

    `homelab` is the homelab host's own overlay address rather than a LAN one:
    the libvirt session a run opens reaches it member to member, needing no
    managed route. The resolvers, by contrast, are named by their LAN addresses,
    because traffic to them is routed by the gateway and a routed packet still
    carries the destination it had before the forward.

    The final `accept` is what leaves personal devices with the reachability
    they would have sitting on the LAN, local discovery included: every rule
    above it matches a tagged continuous-integration endpoint and nothing else.
    """
    enumeration = '\n'.join(f'  enum {value} {label}' for label, value in roles().items())
    targets: list[tuple[str, int, str]] = [
        (f'{udm}/32', SSH_PORT, 'the gateway, for the desired-state push'),
        (f'{udm}/32', UNIFI_API_PORT, 'the controller API on the gateway, for the firewall resources'),
        *((f'{address}/32', ADGUARD_API_PORT, 'a resolver, for the split-horizon rewrites') for address in adguard),
        (f'{homelab}/32', SSH_PORT, 'the homelab host, for the libvirt session'),
    ]
    confinement: list[str] = []
    for destination, port, why in targets:
        confinement.append(f'# {why}')
        confinement.append(f'accept tseq role {conventions.ZT_ROLE_CI} and ipdest {destination} and dport {port};')
        confinement.append(f'accept treq role {conventions.ZT_ROLE_CI} and ipsrc {destination} and sport {port};')

    return '\n'.join(
        (
            f'# Managed by the {conventions.CLUSTER_NAME} physical stack. Edits in Central are overwritten.',
            '',
            'tag role',
            f'  id {conventions.ZT_TAG_ROLE_ID}',
            f'  default {conventions.ZT_ROLE_PERSONAL}',
            enumeration,
            ';',
            '',
            '# The stock base filter: IP and ARP, nothing else on the wire.',
            'drop',
            '  not ethertype ipv4',
            '  and not ethertype arp',
            '  and not ethertype ipv6',
            ';',
            'accept ethertype arp;',
            '',
            '# Continuous integration reaches four destinations and no others.',
            '# Every flow is declared twice: evaluation is stateless, so the',
            '# return leg is a rule of its own rather than a consequence.',
            *confinement,
            '',
            '# Nothing may open a connection towards a run, either: it is a',
            '# client on this network and never a service.',
            f'drop tseq role {conventions.ZT_ROLE_CI};',
            f'drop treq role {conventions.ZT_ROLE_CI};',
            '',
            '# Everyone else: parity with sitting on the LAN.',
            'accept;',
            '',
        )
    )


class Network(Component):
    """The overlay's configuration and its whole membership."""

    def __init__(
        self,
        name: str,
        *,
        api_token: pulumi.Input[str],
        network_id: str,
        members: Mapping[str, Enrolled],
        adguard: Sequence[IPv4Address],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        if len(conventions.ZT_ROSTER) > MULTICAST_LIMIT:
            raise ValueError(
                f'the roster has {len(conventions.ZT_ROSTER)} members but multicast reaches {MULTICAST_LIMIT}; '
                'local discovery would stop finding the difference'
            )
        homelab = members[conventions.ZT_MEMBER_HOMELAB].address
        assert homelab is not None, "the homelab host's overlay address is configured, not conventional"

        # A provider of its own: this token administers the whole ZeroTier
        # account, Central minting nothing smaller, so the resources it may
        # reach are exactly the ones below. It is marked secret here rather
        # than left to the bridged SDK, which -- unlike the controller's --
        # does not classify its own credential, and an unclassified provider
        # setting is written to state in plain text.
        self.provider = zerotier.Provider(
            f'{name}-zerotier',
            zerotier_central_token=pulumi.Output.secret(api_token),
            opts=self.child_opts(),
        )
        child = self.child_opts(provider=self.provider)

        # The network predates this program and is addressed by id, so the
        # first deployment adopts it rather than creating a second one that
        # nobody is a member of.
        self.network = zerotier.Network(
            f'{name}-network',
            name=conventions.CLUSTER_NAME,
            description=f'Home overlay for {conventions.CLUSTER_NAME}; managed by the physical stack.',
            private=True,
            enable_broadcast=True,
            multicast_limit=MULTICAST_LIMIT,
            # Every address on this network is one the roster assigned. Both
            # derived IPv6 schemes are off, which is what keeps the overlay
            # single-family and the confinement rules complete.
            assign_ipv4s=[zerotier.NetworkAssignIpv4Args(zerotier=True)],
            assign_ipv6s=[zerotier.NetworkAssignIpv6Args(zerotier=False, rfc4193=False, sixplane=False)],
            routes=[
                zerotier.NetworkRouteArgs(target=str(route), via=str(conventions.ZT_UDM))
                for route in conventions.ZT_MANAGED_ROUTES
            ],
            flow_rules=flow_rules(udm=conventions.ZT_UDM, homelab=homelab, adguard=adguard),
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(import_=network_id)),
        )

        # Generated identities first: a member cannot be declared before the
        # node it authorizes has an identifier.
        self.identities = {
            entry.name: zerotier.Identity(f'{name}-identity-{entry.name}', opts=child)
            for entry in conventions.ZT_ROSTER
            if entry.generated
        }

        # A member the mapping does not carry has no identity to authorize yet
        # (`parse_members`, `unminted`), so it is left out of the desired state
        # rather than declared against a placeholder. Whether that absence is
        # allowed at all was decided before the mapping got here; what is left
        # here is only what to do about it.
        self.members = {
            entry.name: self._declare(name, entry, members, child)
            for entry in conventions.ZT_ROSTER
            if entry.generated or entry.name in members
        }

        self.register_outputs({})

    def _declare(
        self,
        name: str,
        entry: conventions.ZtMember,
        members: Mapping[str, Enrolled],
        opts: pulumi.ResourceOptions,
    ) -> zerotier.Member:
        node_id: pulumi.Input[str] = (
            self.identities[entry.name].identity_id if entry.generated else members[entry.name].node_id
        )
        address = entry.address if entry.address is not None else members[entry.name].address
        assert address is not None, 'every roster entry is placed, either by convention or by configuration'
        return zerotier.Member(
            f'{name}-member-{entry.name}',
            network_id=self.network.network_id,
            member_id=node_id,
            name=entry.name,
            description=entry.note or f'{entry.name}, on the {conventions.CLUSTER_NAME} overlay',
            authorized=True,
            hidden=False,
            # The address is the roster's, not the pool's. A member that drew
            # from the pool would move, and the rules and the records that name
            # it would not move with it.
            no_auto_assign_ips=True,
            ip_assignments=[str(address)],
            tags=[[conventions.ZT_TAG_ROLE_ID, entry.role]],
            opts=opts,
        )
