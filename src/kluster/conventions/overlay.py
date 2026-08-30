"""The overlay: its subnet, its roles, the routes it carries, its roster.

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

from kluster.conventions.site import CLUSTER_VLAN, CONTAINER_VLAN, IOT_VLAN, LAN_POOL, SERVER_LAN

#: The network this program adopts, as ZeroTier Central minted it. An identity
#: rather than a setting: it is what the network *is*, it is stable, and
#: changing it means adopting a different network. It is not a secret either —
#: joining takes an authorized member, not knowledge of the id — so the
#: administration token beside it stays configuration and this does not
#: (rfc-002 §11).
NETWORK_ID = '83048a0632b6ba9b'

SUBNET = IPv4Network('10.144.0.0/16')

#: Static managed overlay addresses. The UDM is the nexthop of every managed
#: route; the two CI identities are confined by the tag-based flow rules to
#: exactly the four targets they need. There is one identity per *stack* that
#: joins, not one per kind of run: ZeroTier maps a node to one endpoint at a
#: time, so two jobs sharing an identity would flap it
#: (physical/gateway.md §2.6).
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


#: Home subnets the UDM member routes for overlay clients. The cluster VLAN is
#: here because a run reaches the worker's machine API over the overlay, and the
#: pool because that is how a person off-site reaches a cluster service.
MANAGED_ROUTES = (
    SERVER_LAN.v4,
    CLUSTER_VLAN.v4,
    IOT_VLAN.v4,
    CONTAINER_VLAN.v4,
    LAN_POOL.v4,  # reached via the UDM's BGP-learned route
)

#: The two identities that exist only for continuous integration, one per
#: stack that joins the overlay during a run (physical/gateway.md §2.6).
CI_MEMBERS = ('ci-physical', 'ci-dns')

#: The gateway, as the roster names it. It is the one member the roster may be
#: missing: its node id is minted by the overlay daemon's first run, and that
#: daemon is a container this program delivers, so the id does not exist until
#: the bring-up has happened. Step 2 of the ceremony reads it off the device
#: and adds the entry as a commit (physical/gateway.md §2.5).
MEMBER_UDM = 'udm'

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
    #: (`dns.zones.zt_label`).
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
#: block from it, one A record per entry (`dns.zones.zt_records`). A member is
#: therefore admitted and named by the same declaration, so a member with no
#: record is not a state either stack can be in; a device that leaves the
#: overlay leaves this tuple, and both go with it.
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
        name='Aetf-Arch-VPS',
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
