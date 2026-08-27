"""The controller-side firewall census (physical/gateway.md §4.2).

Every rule the UniFi controller holds on this site's behalf, and nothing
else: a rule on the controller that is not declared here is drift. The set is
small on purpose — three rule families —

1.  **IoT → the `lan` pool: one enumerated allow, then a drop.** The allow is
    the media Gateway's VIP on 443, which is what smart televisions and
    streamers consume; the drop is the rest of the pool, where the cluster's
    administrative interfaces live. The firewall names only the media VIP, so
    deciding which applications the IoT VLAN may reach is a Gateway-layer
    edit and never a firewall edit.
2.  **The inbound IPv6 pinhole** for the bulk-transfer application's peer
    port, to the worker VM's global address.
3.  **The IPv4 peer-port forward** — the only port forward on the device.
    Nothing else is published inbound: cluster and node management arrive
    over the cloud load balancer, home-side management over ZeroTier.

Two facts about the device shape the whole module (physical/gateway.md §4.1,
measured):

-   **The zone firewall classifies forwarded traffic by destination ipset.**
    All three home VLANs sit in the internal zone, so IoT → server-LAN is an
    intra-zone pair; but the `lan` pool is deliberately not a network object
    (it would fight the host routes the cluster advertises), so it lands in
    no zone ipset at all and pool-bound traffic falls through to the
    internal → external pair. That is why rules about the pool are declared
    on that pair and why they name the pool through **address groups** — the
    one way to name a subnet the controller has no object for.
-   **Address groups are single-family.** One group cannot hold both the
    pool's IPv4 CIDR and its unique-local IPv6 prefix, so every rule about
    the pool comes as a pair, and the two groups are two resources.

Authentication is an API key belonging to a dedicated local administrator,
carried by a provider instance that exists only for these resources. It is
never the SSH credential the device's own desired state travels on, and it
never reaches the environment that deploys applications — that separation is
the reason these resources live in the `physical` stack rather than beside
the applications whose traffic they admit.
"""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import IPv4Address, IPv6Address, IPv6Network

import pulumi
import pulumi_unifi as unifi

from kluster import conventions
from putils import Component

__all__ = ('STATIC_HOSTS', 'Firewall')

#: The predefined zones of a UniFi OS 9 controller, looked up by name rather
#: than by id: an id is per-site state, a name is stock. `Internal` holds
#: every LAN network object; `External` is the uplink — and, because the
#: `lan` pool is no network object, also where pool-bound traffic is
#: classified.
ZONE_INTERNAL = 'Internal'
ZONE_EXTERNAL = 'External'

#: The IoT VLAN's unique-local prefix. The site numbers each VLAN's /64 after
#: the third octet of its IPv4 subnet (`conventions.LAN_POOL_V6` is the same
#: scheme applied to `192.168.70.0/24`), so the IoT VLAN's ULA follows from
#: `conventions.VLAN_IOT`. It is the ULA rather than a global prefix on
#: purpose: the site's delegated prefix rotates, while a rule has to keep
#: matching, and a client reaching a ULA destination sources from its own
#: ULA.
VLAN_IOT_V6 = IPv6Network('fd1a:665f:8bcb:90::/64')

#: The port the media Gateway serves. The one thing the IoT VLAN may reach in
#: the pool, and it is HTTPS because everything behind that Gateway is.
MEDIA_PORT = 443

#: How many times the provider may re-attempt a request the controller failed
#: transiently. Deliberately small: the controller's login rate limit is
#: account-wide rather than per-address, so a retry storm from a runner does
#: not merely fail the run — it locks out the people using the console. Two
#: covers the transient responses the controller returns under parallel load;
#: anything past that is a fault to report, not to grind against.
HTTP_MAX_RETRIES = 2

#: Static host entries on the device's own resolver. Empty, and that is the
#: design: the device name plane is DHCP-derived and served by the gateway's
#: resolver, while every service is named by its public hostname and steered
#: by the split-horizon rewrites. An entry belongs here only when a name must
#: resolve on the LAN with no lease behind it and no service plane to carry
#: it — a fully-qualified name mapped to one literal address.
STATIC_HOSTS: Mapping[str, IPv4Address | IPv6Address] = {}


class Firewall(Component):
    """The controller-side census: two address groups, five rules, one forward."""

    def __init__(
        self,
        name: str,
        *,
        api_url: str,
        api_key: pulumi.Input[str],
        site: str,
        worker_gua: pulumi.Input[str],
        peer_port: int,
        static_hosts: Mapping[str, IPv4Address | IPv6Address] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.site = site
        self.peer_port = peer_port
        hosts = STATIC_HOSTS if static_hosts is None else static_hosts

        # A provider of its own, rather than ambient configuration: this key
        # authorizes changes to the home's firewall, and the resources it may
        # reach are exactly the ones below.
        self.provider = unifi.Provider(
            f'{name}-unifi',
            api_url=api_url,
            api_key=api_key,
            site=site,
            http_max_retries=HTTP_MAX_RETRIES,
            opts=self.child_opts(),
        )
        child = self.child_opts(provider=self.provider)
        invoke = pulumi.InvokeOptions(parent=self, provider=self.provider)

        internal = unifi.get_firewall_zone_output(name=ZONE_INTERNAL, site=site, opts=invoke).id
        external = unifi.get_firewall_zone_output(name=ZONE_EXTERNAL, site=site, opts=invoke).id

        # The pool, as the only kind of object that can name it. Two groups
        # for one subnet because a group holds one address family.
        self.pool_v4 = unifi.FirewallGroup(
            f'{name}-pool-v4',
            name=conventions.UNIFI_GROUP_LAN_POOL_V4,
            type='address-group',
            members=[str(conventions.LAN_POOL_V4)],
            site=site,
            opts=child,
        )
        self.pool_v6 = unifi.FirewallGroup(
            f'{name}-pool-v6',
            name=conventions.UNIFI_GROUP_LAN_POOL_V6,
            type='ipv6-address-group',
            members=[str(conventions.LAN_POOL_V6)],
            site=site,
            opts=child,
        )

        # Family by family: the allow names the media VIP as a literal, the
        # drop names the whole pool through its group.
        self.iot_media_v4 = unifi.FirewallZonePolicy(
            f'{name}-iot-media-v4',
            name=f'{name} IoT to media VIP (v4)',
            description='IoT VLAN may reach the media gateway VIP on HTTPS.',
            action='ALLOW',
            ip_version='IPV4',
            protocol='tcp',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=internal, ips=[str(conventions.VLAN_IOT)]),
            destination=unifi.FirewallZonePolicyDestinationArgs(
                zone_id=external,
                ips=[str(conventions.VIP_MEDIA_V4)],
                port=MEDIA_PORT,
            ),
            # An allow whose return leg is not allowed is not an allow, and
            # the reverse zone pair is where the return leg is classified.
            auto_allow_return_traffic=True,
            enabled=True,
            opts=child,
        )
        self.iot_media_v6 = unifi.FirewallZonePolicy(
            f'{name}-iot-media-v6',
            name=f'{name} IoT to media VIP (v6)',
            description='IoT VLAN may reach the media gateway VIP on HTTPS.',
            action='ALLOW',
            ip_version='IPV6',
            protocol='tcp',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=internal, ips=[str(VLAN_IOT_V6)]),
            destination=unifi.FirewallZonePolicyDestinationArgs(
                zone_id=external,
                ips=[str(conventions.VIP_MEDIA_V6)],
                port=MEDIA_PORT,
            ),
            auto_allow_return_traffic=True,
            enabled=True,
            opts=child,
        )
        self.iot_pool_v4 = unifi.FirewallZonePolicy(
            f'{name}-iot-pool-v4',
            name=f'{name} IoT to lan pool (v4)',
            description='IoT VLAN may not reach the rest of the cluster pool.',
            action='BLOCK',
            ip_version='IPV4',
            protocol='all',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=internal, ips=[str(conventions.VLAN_IOT)]),
            destination=unifi.FirewallZonePolicyDestinationArgs(zone_id=external, ip_group_id=self.pool_v4.id),
            enabled=True,
            opts=child,
        )
        self.iot_pool_v6 = unifi.FirewallZonePolicy(
            f'{name}-iot-pool-v6',
            name=f'{name} IoT to lan pool (v6)',
            description='IoT VLAN may not reach the rest of the cluster pool.',
            action='BLOCK',
            ip_version='IPV6',
            protocol='all',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=internal, ips=[str(VLAN_IOT_V6)]),
            destination=unifi.FirewallZonePolicyDestinationArgs(zone_id=external, ip_group_id=self.pool_v6.id),
            enabled=True,
            opts=child,
        )

        # Order is the rule. A drop declared without its allow ahead of it is
        # a severed television, and a pair declared without an explicit order
        # takes whatever order creation happened to produce. Both allows
        # precede both drops, and all four precede the predefined policies of
        # the pair — otherwise the predefined accept on the uplink pair would
        # answer first and the drop would never be reached.
        self.pool_order = unifi.FirewallZonePolicyOrder(
            f'{name}-iot-pool-order',
            source_zone_id=internal,
            destination_zone_id=external,
            before_predefined_ids=[
                self.iot_media_v4.id,
                self.iot_media_v6.id,
                self.iot_pool_v4.id,
                self.iot_pool_v6.id,
            ],
            site=site,
            opts=child,
        )

        # The pinhole. A literal address rather than anything prefix-relative:
        # the zone-policy API matches literal addresses only, and the site's
        # delegated prefix rotates, so this rule is re-declared when it does.
        # A stale rule degrades to outbound-only IPv6 — the accepted first
        # stage — rather than to anything unsafe.
        self.peer_v6 = unifi.FirewallZonePolicy(
            f'{name}-peer-v6',
            name=f'{name} inbound peer port (v6)',
            description='Inbound peer traffic to the worker VM on the bulk-transfer port.',
            action='ALLOW',
            ip_version='IPV6',
            protocol='tcp_udp',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=external),
            destination=unifi.FirewallZonePolicyDestinationArgs(
                zone_id=internal,
                ips=[worker_gua],
                port=peer_port,
            ),
            auto_allow_return_traffic=True,
            enabled=True,
            opts=child,
        )
        self.peer_order = unifi.FirewallZonePolicyOrder(
            f'{name}-peer-order',
            source_zone_id=external,
            destination_zone_id=internal,
            before_predefined_ids=[self.peer_v6.id],
            site=site,
            opts=child,
        )

        # The IPv4 half of the same flow, and the only port forward on the
        # device. It lands on the worker VM's LAN address because that is the
        # address the application's outbound peer traffic already wears: the
        # cluster masquerades it to the node, so inbound has to arrive there
        # for a peer to see one endpoint rather than two.
        self.peer_v4 = unifi.PortForward(
            f'{name}-peer-v4',
            name=f'{name} inbound peer port (v4)',
            port_forward_interface='wan',
            protocol='tcp_udp',
            src_ip='any',
            dst_port=str(peer_port),
            fwd_ip=str(conventions.HOMELAB_NODE_IPV4),
            fwd_port=str(peer_port),
            site=site,
            opts=child,
        )

        self.static_hosts = {
            host: unifi.DnsRecord(
                f'{name}-host-{host}',
                name=host,
                type='AAAA' if isinstance(address, IPv6Address) else 'A',
                record=str(address),
                enabled=True,
                site=site,
                opts=child,
            )
            for host, address in hosts.items()
        }

        self.register_outputs({})
