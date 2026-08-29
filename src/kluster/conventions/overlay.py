"""The ZeroTier overlay: its subnet, its roles, the routes it carries, its roster.

The management network every unattended run reaches the home site over
(physical/gateway.md §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network

from kluster.conventions.site import CLUSTER_VLAN, CONTAINER_VLAN, IOT_VLAN, LAN_POOL, SERVER_LAN

ZT_SUBNET = IPv4Network('10.144.0.0/16')

#: Static managed addresses. The UDM is the nexthop of every managed route;
#: the two CI identities are confined by the tag-based flow rules to exactly
#: the four targets they need. There is one identity per *stack* that joins,
#: not one per kind of run: ZeroTier maps a node to one endpoint at a time, so
#: two jobs sharing an identity would flap it (physical/gateway.md §2.6).
ZT_UDM = IPv4Address('10.144.1.1')
ZT_CI_PHYSICAL = IPv4Address('10.144.2.1')
ZT_CI_DNS = IPv4Address('10.144.2.2')

#: Role tags on the network (tag id 1000). `personal` is the permissive
#: default; membership itself is Pulumi-gated, so an undeclared member never
#: joins to receive it.
ZT_TAG_ROLE_ID = 1000
ZT_ROLE_PERSONAL = 0
ZT_ROLE_INFRA = 1
ZT_ROLE_CI = 2

#: Home subnets the UDM member routes for ZT clients. The cluster VLAN is here
#: because a run reaches the worker's machine API over the overlay, and the
#: pool because that is how a person off-site reaches a cluster service.
ZT_MANAGED_ROUTES = (
    SERVER_LAN.v4,
    CLUSTER_VLAN.v4,
    IOT_VLAN.v4,
    CONTAINER_VLAN.v4,
    LAN_POOL.v4,  # reached via the UDM's BGP-learned route
)

#: The two identities that exist only for continuous integration, one per
#: stack that joins the overlay during a run (physical/gateway.md §2.6).
ZT_CI_MEMBERS = ('ci-physical', 'ci-dns')

#: The gateway, as the roster names it. It is the only member whose identity
#: is minted by work this program does — the daemon runs on the device as a
#: container of the estate — which is why it is also the only member a caller
#: ever declares unminted (`gateway.zerotier.parse_members`).
ZT_MEMBER_UDM = 'udm'

#: The homelab host, as the roster names it. It is the one member the flow
#: rules have to look up rather than take from a constant: the libvirt session
#: a run opens reaches it member to member, at whatever overlay address it was
#: assigned before this program existed.
ZT_MEMBER_HOMELAB = 'Aetf-Arch-Homelab'


@dataclass(frozen=True)
class ZtMember:
    """A member of the overlay: what it is called, what it may do, where it sits.

    `address` is set where the address is a convention this program owns — the
    gateway's, and the two continuous-integration identities' — and left unset
    where it is a fact about a device that existed first, in which case the
    address arrives beside the node identifier as `physical` stack
    configuration and leaves again as that stack's `zerotier_addresses` output.

    `generated` marks the members whose key material this program creates.
    They have no configured identifier for the same reason they have no
    configured address: both are outputs of the resource that makes them.
    """

    #: What ZeroTier Central shows the member as. Display names are what they
    #: are — several contain spaces — and DNS normalizes rather than renames
    #: (`dns.zones.zt_label`).
    name: str
    #: One of the three `ZT_ROLE_*` values, carried as the member's role tag.
    role: int
    #: The overlay address, where this program decides it.
    address: IPv4Address | None = None
    #: Whether the identity behind the member is created in state.
    generated: bool = False
    #: Why the member is on the network, shown as its description in Central.
    note: str = ''


#: Every member of the overlay. The order is the order the design lists them
#: in: the infrastructure the overlay exists to reach, then the identities that
#: reach it unattended, then the people.
#:
#: The table is a convention rather than one stack's data because two stacks
#: decide from it and neither owns it. `physical` admits members by it — a
#: name in configuration the roster does not carry is refused, and a roster
#: entry with nothing configured for it is refused as well
#: (`gateway.zerotier.parse_members`) — and `dns` publishes the `*.zt` host
#: block from it, one A record per entry (`dns.zones.zt_records`). A member is
#: therefore admitted and named by the same declaration, so a member with no
#: record is not a state either stack can be in; a device that leaves the
#: overlay leaves this tuple, and both go with it.
#:
#: It is a census by construction. The role tag's default value is the
#: permissive one, so a member that arrived without a declared role would be
#: treated as a personal device — safe only because admission is gated by this
#: same table, so an undeclared member never reaches the default.
ZT_ROSTER: tuple[ZtMember, ...] = (
    ZtMember(
        name=ZT_MEMBER_UDM,
        role=ZT_ROLE_INFRA,
        address=ZT_UDM,
        note='the gateway: nexthop of every managed route',
    ),
    ZtMember(
        name=ZT_MEMBER_HOMELAB,
        role=ZT_ROLE_INFRA,
        note='the homelab host: a plain member and the recovery side-door, never a router',
    ),
    ZtMember(
        name='Aetf-Arch-VPS',
        role=ZT_ROLE_INFRA,
        note='the legacy deployment, retiring with its own route',
    ),
    ZtMember(
        name='haos',
        role=ZT_ROLE_INFRA,
        note='home automation, reachable while the cluster is not',
    ),
    ZtMember(
        name='ci-physical',
        role=ZT_ROLE_CI,
        address=ZT_CI_PHYSICAL,
        generated=True,
        note='the physical stack: plan, apply, and its drift check',
    ),
    ZtMember(
        name='ci-dns',
        role=ZT_ROLE_CI,
        address=ZT_CI_DNS,
        generated=True,
        note="the dns stack: previews, proofs, and the resolvers' rewrites",
    ),
    ZtMember(name='Aetf-Arch-XPS', role=ZT_ROLE_PERSONAL),
    ZtMember(name='Aetf-Win-XPS', role=ZT_ROLE_PERSONAL),
    ZtMember(name='Aetf-Handheld', role=ZT_ROLE_PERSONAL),
    ZtMember(name='PC-Homelab', role=ZT_ROLE_PERSONAL),
    ZtMember(name='OnePlus6T', role=ZT_ROLE_PERSONAL),
    ZtMember(name='Pixel 7 Pro', role=ZT_ROLE_PERSONAL),
    ZtMember(name='S26 Ultra', role=ZT_ROLE_PERSONAL),
)
