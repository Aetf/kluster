"""The home site's address plan: the subnets the gateway serves, and the pool it routes to.

Every IPv6 prefix here is *derived* from its IPv4 sibling rather than written
beside it. The site numbers each /64 out of `SITE_ULA` after the third octet of
the IPv4 subnet, spelled as those same digits, so a pair that disagrees cannot
be declared. An address an interface forms from its MAC is derived too — a
link-local, or a host's ULA on its network's /64 — from the MAC it is formed
from (`eui64_address`).
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


#: The /64 every link-local address is formed in (RFC 4291 §2.5.6).
_LINK_LOCAL = IPv6Network('fe80::/64')

#: How many octets a MAC address has, and the bit of its first octet that
#: the modified EUI-64 inverts: the universal/local bit (RFC 4291 Appendix A).
_MAC_OCTETS = 6
_UNIVERSAL_LOCAL_BIT = 0x02

#: The prefix length an interface identifier completes: the identifier is the
#: low 64 bits (RFC 4291 §2.5.1).
_IDENTIFIER_PREFIXLEN = 64


def _mac_octets(mac: str) -> bytes:
    """The six octets of a MAC written as colon-separated hex, or a refusal naming it."""
    try:
        octets = bytes.fromhex(mac.replace(':', ''))
    except ValueError:
        octets = b''
    if len(octets) != _MAC_OCTETS:
        raise ValueError(f'{mac!r} is not a MAC address')
    return octets


def eui64_address(network: IPv6Network, mac: str) -> IPv6Address:
    """The address a NIC forms on a /64 from its MAC: the prefix and the MAC's modified EUI-64.

    The identifier is the MAC split in half around `ff:fe`, with the
    universal/local bit of its first octet inverted (RFC 4291 Appendix A).
    It is the one Linux forms while the interface's `addr_gen_mode` is
    EUI-64 — the kernel's default, which a `stable_secret` would replace with
    stable-privacy — and NetworkManager's `ipv6.addr-gen-mode eui64` asks for
    the same. The interface puts it under `fe80::/64` for its link-local, and
    under every /64 a router advertises for stateless autoconfiguration
    (RFC 4862 §5.5.3), so a declaration that names such an address derives it
    from the MAC the machine is declared with, and a replaced machine that
    keeps the MAC forms the same address.
    """
    if network.prefixlen != _IDENTIFIER_PREFIXLEN:
        raise ValueError(f'{network} is not a /64, which is what an interface identifier completes')
    octets = _mac_octets(mac)
    identifier = bytes((octets[0] ^ _UNIVERSAL_LOCAL_BIT, *octets[1:3], 0xFF, 0xFE, *octets[3:]))
    return network.network_address + int.from_bytes(identifier, 'big')


def eui64_link_local(mac: str) -> IPv6Address:
    """The link-local address a NIC forms from its MAC: `eui64_address` under `fe80::/64`."""
    return eui64_address(_LINK_LOCAL, mac)


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
#: announce it read as neighbors without ever being one network.
LAN_POOL = AddressPool(
    name='lan',
    v4=IPv4Network('192.168.71.0/24'),
    group_v4='kluster-lan-pool-v4',
    group_v6='kluster-lan-pool-v6',
    default_vip=Vip(v4=IPv4Address('192.168.71.1'), v6=IPv6Address('fd1a:665f:8bcb:71::1')),
    media_vip=Vip(v4=IPv4Address('192.168.71.2'), v6=IPv6Address('fd1a:665f:8bcb:71::2')),
)


@dataclass(frozen=True)
class SiteHost:
    """A machine at the site that a declaration names by address, in both families, on its own network.

    Its ULA is not written beside its MAC but derived from it: it is the
    address the host forms on its network's /64 by stateless
    autoconfiguration, with the modified EUI-64 of the MAC as its identifier
    (`eui64_address`). So the two cannot be declared apart, and a host is one
    this type declares only while its interface forms its ULA that way.
    """

    network: SiteNetwork
    v4: IPv4Address
    #: The MAC of the host's interface on its network, which its ULA is formed from.
    mac: str

    def __post_init__(self) -> None:
        if self.v4 not in self.network.v4:
            raise ValueError(f'{self.v4} is not on the {self.network.name} network')
        _ = _mac_octets(self.mac)

    @property
    def v6(self) -> IPv6Address:
        """The host's ULA: its network's /64 and its MAC's modified EUI-64."""
        return eui64_address(self.network.v6, self.mac)


#: Home Assistant, on the IoT VLAN with the devices it drives. Its
#: integrations are how the home automates what the cluster's applications
#: do, so it reaches the whole `lan` pool (physical/gateway.md §4.2), and its
#: name is answered by a relay to its IPv4 address (rfc-008 §4.1). Both its
#: addresses follow its MAC, the one its domain's definition on the homelab
#: host sets (physical/homelab-host.md §4): the controller's fixed-IP
#: reservation for the MAC holds the IPv4 address, and the ULA is the EUI-64
#: one because its interface runs `addr-gen-mode eui64`, a setting of the host
#: and not of this program (physical/gateway.md §4.2). A reinstall leaves that
#: setting at its default, which forms a different ULA under the same prefix,
#: so it is made again before the allow can match; a changed MAC moves both
#: addresses.
HOME_ASSISTANT = SiteHost(
    network=IOT_VLAN,
    v4=IPv4Address('192.168.90.243'),
    mac='52:54:00:8c:5d:10',
)
