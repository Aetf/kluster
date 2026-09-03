"""The home site's address plan: the subnets the gateway serves, and the pool it routes to.

Every IPv6 prefix here is *derived* from its IPv4 sibling rather than written
beside it. The site numbers each /64 out of `SITE_ULA` after the third octet of
the IPv4 subnet, spelled as those same digits, so a pair that disagrees cannot
be declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

#: The site's unique-local prefix, and the source of every /64 below.
#: Unique-local rather than global because the site's delegated prefix rotates
#: while the things that name these addresses — firewall rules, resolver
#: rewrites — have to keep matching.
SITE_ULA = IPv6Network('fd1a:665f:8bcb::/48')

#: How many hextets of `SITE_ULA` a derived /64 keeps before the subnet's own.
_ULA_HEXTETS = 3


def ula_subnet(v4: IPv4Network) -> IPv6Network:
    """The /64 out of `SITE_ULA` that belongs with an IPv4 subnet.

    The subnet's third octet is the hextet, spelled as the same digits: the
    server LAN's `192.168.80.0/24` gets `:80::`, the containers' `10.0.5.0/24`
    gets `:5::`.
    """
    prefix = ':'.join(SITE_ULA.network_address.exploded.split(':')[:_ULA_HEXTETS])
    return IPv6Network(f'{prefix}:{v4.network_address.packed[2]}::/64')


@dataclass(frozen=True)
class SiteNetwork:
    """One subnet the gateway serves, and the gateway's own leg on it."""

    name: str
    v4: IPv4Network
    #: The tag the subnet is carried on, where this program names one. `None`
    #: is the untagged LAN, and the networks the site had before this program.
    vlan_id: int | None = None
    #: The gateway's address on the subnet, where this program decides it —
    #: the nodes' default route, and the address a routing session is held at.
    gateway_v4: IPv4Address | None = None

    @property
    def v6(self) -> IPv6Network:
        """The subnet's unique-local /64, numbered by the site's addressing rule."""
        return ula_subnet(self.v4)

    def require_gateway(self) -> IPv4Address:
        """The gateway's leg, or a refusal naming the network that has none declared."""
        if self.gateway_v4 is None:
            raise ValueError(f'the {self.name} network states no gateway address of its own')
        return self.gateway_v4


#: The untagged server LAN: the homelab host itself and the LAN's general
#: population.
SERVER_LAN = SiteNetwork(name='server', v4=IPv4Network('192.168.80.0/24'))

#: Home Assistant and its devices — the LAN's least-trusted population.
IOT_VLAN = SiteNetwork(name='iot', v4=IPv4Network('192.168.90.0/24'))

#: The gateway's own containers, on the bridge the device calls `br5`.
CONTAINER_VLAN = SiteNetwork(
    name='container',
    v4=IPv4Network('10.0.5.0/24'),
    vlan_id=5,
    gateway_v4=IPv4Address('10.0.5.1'),
)

#: Where every Talos node on this site lives, with static addressing and no
#: DHCP server (physical/gateway.md §4). A VLAN of its own rather than a corner
#: of the server LAN, because a population in its own subnet is one the gateway
#: can name in a policy and a population sharing the untagged LAN is not.
CLUSTER_VLAN = SiteNetwork(
    name='cluster',
    v4=IPv4Network('192.168.70.0/24'),
    vlan_id=7,
    gateway_v4=IPv4Address('192.168.70.1'),
)

#: Every subnet the gateway serves, in the order the design lists them.
SITE_NETWORKS = (SERVER_LAN, IOT_VLAN, CONTAINER_VLAN, CLUSTER_VLAN)


@dataclass(frozen=True)
class Vip:
    """A fixed address out of a pool, in both families, named from outside the cluster."""

    v4: IPv4Address
    v6: IPv6Address


@dataclass(frozen=True)
class AddressPool:
    """A dual-stack range of service addresses, and everything that names it.

    The pool is deliberately not a network object on the controller — it would
    fight the BGP host routes the cluster announces (architecture.md §3.4) — so
    the only way to name it in a firewall rule is an address group per family.
    Those two names, the range they hold and the VIPs carved out of it are one
    decision, and none of them is right without the others.
    """

    name: str
    v4: IPv4Network
    #: The controller-side address groups, one per family: a group holds one
    #: address family, so every rule about the pool comes as a pair.
    group_v4: str
    group_v6: str
    #: The addresses things outside the cluster name: the resolver rewrites'
    #: targets (dns.md §3) and the gateway's IoT allow (physical/gateway.md
    #: §4.2). Literals rather than pool-allocated, because those readers cannot
    #: wait for an allocation.
    default_vip: Vip
    media_vip: Vip

    @property
    def v6(self) -> IPv6Network:
        """The pool's unique-local /64, numbered like every other subnet at the site."""
        return ula_subnet(self.v4)


#: The `lan` pool: BGP-announced to the gateway as host routes, and
#: deliberately not a subnet the gateway serves — not the cluster VLAN the
#: announcing node sits on and not the server LAN either. It takes the third
#: octet one along from the cluster VLAN's, so the pool and the nodes that
#: announce it read as neighbours without ever being one network.
LAN_POOL = AddressPool(
    name='lan',
    v4=IPv4Network('192.168.71.0/24'),
    group_v4='kluster-lan-pool-v4',
    group_v6='kluster-lan-pool-v6',
    default_vip=Vip(v4=IPv4Address('192.168.71.1'), v6=IPv6Address('fd1a:665f:8bcb:71::1')),
    media_vip=Vip(v4=IPv4Address('192.168.71.2'), v6=IPv6Address('fd1a:665f:8bcb:71::2')),
)
