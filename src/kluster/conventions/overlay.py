"""The overlay: its subnet, its roles, the routes it carries, its roster, its managed DNS.

The ZeroTier network every unattended run reaches the home site over
(physical/gateway.md §2).

Read qualified — `conventions.overlay.ROSTER` — because "the overlay" is what
tells an address, a member or a route apart from the site's own
(rfc-002 §3.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from ipaddress import IPv4Address, IPv4Network
from typing import final

from kluster.conventions.dns import OVERLAY_DOMAIN
from kluster.conventions.gateway import RESOLVERS
from kluster.conventions.site import CLUSTER_VLAN, CONTAINER_VLAN, IOT_VLAN, LAN_POOL, SERVER_LAN

#: The network this program adopts, as ZeroTier Central minted it. An identity
#: rather than a setting: it is what the network *is*, it is stable, and
#: changing it means adopting a different network. It is not a secret either —
#: joining takes an authorized member, not knowledge of the id — so the
#: administration token beside it stays configuration and this does not
#: (rfc-002 §11).
NETWORK_ID = '83048a0632b6ba9b'

SUBNET = IPv4Network('10.144.0.0/16')

#: Static managed overlay addresses. The UDM is the nexthop of every route to
#: the home (`MANAGED_ROUTES`); the two CI identities are confined by the
#: tag-based flow rules to exactly the four targets they need. There is one
#: identity per *stack* that joins, not one per kind of run: ZeroTier maps a
#: node to one endpoint at a time, so two jobs sharing an identity would flap
#: it (physical/gateway.md §2.6).
UDM = IPv4Address('10.144.1.1')
CI_PHYSICAL = IPv4Address('10.144.2.1')
CI_DNS = IPv4Address('10.144.2.2')

#: The tag the roles are carried in on the network.
TAG_ROLE_ID = 1000


@final
class Role(IntEnum):
    """What a member is on the network, as the value of its role tag.

    `PERSONAL` is the tag's own default, which is why it is the permissive one:
    the flow rules confine the roles they name and leave the default alone.
    Nothing rides on that default, because membership is declared from the
    roster and an undeclared member never joins to receive it.
    """

    PERSONAL = 0
    INFRA = 1
    CI = 2


#: The two identities that exist only for continuous integration, one per
#: stack that joins the overlay during a run (physical/gateway.md §2.6).
CI_MEMBERS = ('ci-physical', 'ci-dns')


@final
@dataclass(frozen=True)
class ManagedDns:
    """What the network pushes to a member that applies it: a domain, and who answers for it.

    ZeroTier's own term for the setting `allowDNS` admits. A member that opts
    in installs a resolver scoped to `domain` and nothing wider, so the domain
    is the whole reach of the push: names under it go to `servers` and to
    nothing else, names outside it are untouched. Opting in is the device's
    own setting, which the controller neither reads nor sets, so no field
    here says which members apply it (physical/gateway.md §2.7).
    """

    #: The search domain, and the scope of the resolver an opted-in member
    #: installs.
    domain: str
    #: The resolvers, in the order the member tries them. Addresses rather
    #: than names, because they are what the push carries, and every one of
    #: them has to be reachable from the overlay by a route the network
    #: already manages.
    servers: tuple[IPv4Address, ...]


#: The one managed-DNS block the network pushes. The domain is the overlay host
#: block's, so a member that opts in resolves `*.zt` at home and everything
#: else where it did before. The servers are the site's own resolvers at their
#: container-VLAN addresses: they are containers on the gateway and not
#: members, so they have no overlay address, and a member reaches them through
#: the managed route for that VLAN (`MANAGED_ROUTES`) via the gateway. What a
#: client keeps of the list is bounded by the client's `ZT_MAX_DNS_SERVERS`,
#: held in `test_conventions`.
MANAGED_DNS = ManagedDns(domain=OVERLAY_DOMAIN, servers=tuple(resolver.address for resolver in RESOLVERS))

#: The gateway, as the roster names it. It is the one member the roster may be
#: missing: its node id is minted by the overlay daemon's first run, and that
#: daemon is a container this program delivers, so the id does not exist until
#: the bring-up has happened. Step 2 of the ceremony reads it off the device
#: and adds the entry as a commit (physical/gateway.md §2.5).
MEMBER_UDM = 'udm'

#: The legacy deployment, as the roster names it. It is the one member a route
#: other than the gateway's is via: the legacy cluster's pod subnet stays
#: routed through it until the machine retires (cluster/migration.md §4).
MEMBER_VPS = 'Aetf-Arch-VPS'

#: The homelab host, as the roster names it. It is the one member the flow
#: rules and the libvirt session look up rather than take from a constant: the
#: session reaches it member to member, at the overlay address it was assigned
#: before this program existed.
MEMBER_HOMELAB = 'Aetf-Arch-Homelab'


@final
@dataclass(frozen=True)
class EnrolledMember:
    """A member that minted its own identity before this program saw it.

    A node id is minted by the device the daemon runs on and never changes, so
    it is an identity rather than a setting — it is recorded here beside the
    address ZeroTier Central assigned, not read from stack configuration. The
    role is neither: it is a decision.
    """

    #: What ZeroTier Central shows the member as. Display names are what they
    #: are — several contain spaces — and DNS normalizes rather than renames
    #: (`dns.base.overlay_label`).
    name: str
    #: The ten hexadecimal digits the device's daemon minted.
    node_id: str
    #: The overlay address the member holds.
    address: IPv4Address
    #: What the member is here as, carried as its role tag.
    role: Role
    #: Why the member is on the network, shown as its description in Central.
    note: str = ''


@final
@dataclass(frozen=True)
class GeneratedMember:
    """A member whose identity this program creates in state.

    It carries no node id for the same reason it is here at all: the id is an
    output of the resource that mints the key material, so writing one down
    would be writing down a value the run has yet to produce.
    """

    name: str
    #: The overlay address this program hands the member.
    address: IPv4Address
    role: Role
    note: str = ''


#: One entry of the roster. Two shapes rather than one with optional fields:
#: a generated member carrying a node id is a combination that cannot be
#: declared instead of one something has to refuse.
RosterEntry = EnrolledMember | GeneratedMember


#: Every member of the overlay. The order is the order the design lists them
#: in: the infrastructure the overlay exists to reach, then the identities that
#: reach it unattended, then the people.
#:
#: The table is a convention rather than one stack's data because two stacks
#: decide from it and neither owns it. `physical` declares the membership from
#: it, one authorized member per entry, and `dns` publishes the `*.zt` host
#: block from it, one A record per entry (`dns.base.overlay_records`). A
#: member is therefore admitted and named by the same declaration, so a member
#: with no record is not a state either stack can be in; a device that leaves
#: the overlay leaves this tuple, and both go with it.
#:
#: It is a census by construction. The role tag's default value is the
#: permissive one, so a member that arrived without a declared role would be
#: treated as a personal device — safe only because admission is gated by this
#: same table, so an undeclared member never reaches the default.
#:
#: The gateway is absent, and absence is the whole of what says so: no member
#: is declared for it and no `udm.zt` record is published until the ceremony
#: that reads its minted node id adds the entry (`MEMBER_UDM`).
ROSTER: tuple[RosterEntry, ...] = (
    EnrolledMember(
        name=MEMBER_HOMELAB,
        node_id='c3755c24d1',
        address=IPv4Address('10.144.180.10'),
        role=Role.INFRA,
        note='the homelab host: a plain member and the recovery side-door, never a router',
    ),
    EnrolledMember(
        name=MEMBER_VPS,
        node_id='fb6c235c67',
        address=IPv4Address('10.144.160.212'),
        role=Role.INFRA,
        note='the legacy deployment, retiring with its own route',
    ),
    EnrolledMember(
        name='haos',
        node_id='788d26ad08',
        address=IPv4Address('10.144.84.129'),
        role=Role.INFRA,
        note='home automation, reachable while the cluster is not',
    ),
    GeneratedMember(
        name='ci-physical',
        address=CI_PHYSICAL,
        role=Role.CI,
        note='the physical stack: plan, apply, and its drift check',
    ),
    GeneratedMember(
        name='ci-dns',
        address=CI_DNS,
        role=Role.CI,
        note="the dns stack: previews, proofs, and the resolvers' rewrites",
    ),
    EnrolledMember(
        name='Aetf-Arch-XPS', node_id='0d83052605', address=IPv4Address('10.144.175.24'), role=Role.PERSONAL
    ),
    EnrolledMember(
        name='Aetf-Win-XPS', node_id='02af51aec9', address=IPv4Address('10.144.188.195'), role=Role.PERSONAL
    ),
    EnrolledMember(
        name='Aetf-Handheld', node_id='d57f65f742', address=IPv4Address('10.144.117.120'), role=Role.PERSONAL
    ),
    EnrolledMember(name='PC-Homelab', node_id='3383aa0836', address=IPv4Address('10.144.147.56'), role=Role.PERSONAL),
    EnrolledMember(name='OnePlus6T', node_id='ecd8be1a4e', address=IPv4Address('10.144.164.143'), role=Role.PERSONAL),
    EnrolledMember(name='Pixel 7 Pro', node_id='f80515f135', address=IPv4Address('10.144.0.120'), role=Role.PERSONAL),
    EnrolledMember(name='S26 Ultra', node_id='1aaec45044', address=IPv4Address('10.144.92.151'), role=Role.PERSONAL),
)


def member(name: str) -> RosterEntry:
    """The roster entry a member name stands for.

    Raises:
        ValueError: no member of that name is declared. The gateway is the one
            name that is legitimately absent, and nothing looks it up — its
            absence is read by iterating the roster, not by asking for it.
    """
    for entry in ROSTER:
        if entry.name == name:
            return entry
    raise ValueError(f'{name} is not on the overlay roster')


@final
@dataclass(frozen=True)
class ManagedRoute:
    """One route the network carries: what it reaches, and which member forwards for it.

    A route is `{target, via}` on the network and nothing more: `via` names a
    member, and that member forwards only because forwarding is configured on
    the device itself (physical/gateway.md §2.2). `via=None` is the route
    ZeroTier itself installs on every member's interface, the one for the
    network's own subnet -- and it is what makes an address an address: the
    controller pushes a member's static assignment only when some route's
    target contains it, with that route's netmask, so a network without one
    hands every member no address at all.
    """

    target: IPv4Network
    via: IPv4Address | None


#: The legacy cluster's pod subnet, reached through the machine that still
#: runs it. Retires with that machine's roster entry (cluster/migration.md §4).
LEGACY_POD_SUBNET = IPv4Network('10.42.0.0/24')

#: The whole route table the network carries, and the only one: the network is
#: adopted with its routes declared in full, so what is absent here is deleted
#: at Central. First the overlay's own subnet, with no `via`, which is what
#: keeps every member addressed; then the home subnets the gateway forwards
#: for -- the cluster VLAN because a run reaches the worker's machine API over
#: the overlay, the pool because that is how a person off-site reaches a
#: cluster service; then the legacy route, via the retiring member's own
#: address so that the entry and the route leave in one commit.
MANAGED_ROUTES: tuple[ManagedRoute, ...] = (
    ManagedRoute(SUBNET, None),
    ManagedRoute(SERVER_LAN.v4, UDM),
    ManagedRoute(CLUSTER_VLAN.v4, UDM),
    ManagedRoute(IOT_VLAN.v4, UDM),
    ManagedRoute(CONTAINER_VLAN.v4, UDM),
    ManagedRoute(LAN_POOL.v4, UDM),  # reached via the UDM's BGP-learned route
    ManagedRoute(LEGACY_POD_SUBNET, member(MEMBER_VPS).address),
)
