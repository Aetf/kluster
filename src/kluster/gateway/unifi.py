"""The cluster's network on the gateway, and the firewall census around it
(physical/gateway.md §4.2).

Two kinds of thing are declared here, and the first is why the second can be
written at all.

**The cluster VLAN, as a network object and a zone of its own.** Cluster nodes
do not share the untagged server LAN: they sit on `conventions.CLUSTER_VLAN_V4`
behind VLAN id `conventions.CLUSTER_VLAN_ID`, which the controller serves with
no DHCP server — every node states its own address in machine configuration
(`physical/talos.py`), so a lease would be a second opinion about an address
three other places already treat as constant. Being a network object is what
makes the population nameable in a policy; being alone in a firewall zone is
what makes it *separately* nameable from everything else the internal zone
holds.

**The rule census**, which is exactly the design's and nothing more: a rule on
the controller that is not declared here is drift.

1.  **IoT → the `lan` pool: one enumerated allow, then a drop.** The allow is
    the media Gateway's VIP on 443, which is what smart televisions and
    streamers consume; the drop is the rest of the pool, where the cluster's
    administrative interfaces live. The firewall names only the media VIP, so
    deciding which applications the IoT VLAN may reach is a Gateway-layer
    edit and never a firewall edit.
2.  **The cluster zone's egress.** A zone the controller has just been told
    about starts denied in both directions, and a node that cannot leave the
    site cannot reach the control plane it is a member of, so the one policy
    that must exist beside the zone is its way out.
3.  **The cluster zone into the internal one.** Workloads on the nodes
    initiate toward home services — the home-automation API on the IoT VLAN
    among them — and that direction is where the recorded dependencies run.
4.  **The internal zone into the cluster one, minus the IoT VLAN.** The
    trusted home VLANs keep reaching the nodes directly, because that is the
    reachability they had before the nodes moved off the untagged LAN. The
    IoT VLAN does not: the one recorded IoT-originated dependency is a
    television reaching the media VIP, which is in the pool and not on the
    node subnet, so a drop ahead of that allow takes the node ports — apid,
    kubelet, BGP — away from the LAN's least-trusted population without
    severing anything known. It is the same shape as rule 1 one zone over:
    the zone stays open, the untrusted subpopulation is carved out.
5.  **The inbound IPv6 pinhole** for the bulk-transfer application's peer
    port, to the worker VM's global address — now landing in the cluster zone
    rather than the internal one, because that is where the worker moved.
6.  **The IPv4 peer-port forward** — the only port forward on the device.
    Nothing else is published inbound: cluster and node management arrive
    over the cloud load balancer, home-side management over ZeroTier.

Every zone pair that carries a policy also carries a
`FirewallZonePolicyOrder`: a policy declared without one takes whatever
position creation happened to produce, and on a pair with both a drop and an
allow that is the difference between the design and its opposite.

Two facts about the device shape the whole module (physical/gateway.md §4.1,
measured):

-   **The zone firewall classifies forwarded traffic by destination ipset.**
    The home VLANs sit in the internal zone, so IoT → server-LAN is an
    intra-zone pair; but the `lan` pool is deliberately not a network object
    (it would fight the host routes the cluster advertises), so it lands in
    no zone ipset at all and pool-bound traffic falls through to the
    internal → external pair. That is why rules about the pool are declared
    on that pair and why they name the pool through **address groups** — the
    one way to name a subnet the controller has no object for. The cluster
    VLAN is the deliberate opposite: it *is* an object, and so it is named
    directly.
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

#: The IoT VLAN's unique-local prefix, out of `conventions.SITE_ULA` and
#: numbered after the third octet of `conventions.VLAN_IOT` like every other
#: /64 at the site. It is the ULA rather than a global prefix on purpose: the
#: site's delegated prefix rotates, while a rule has to keep matching, and a
#: client reaching a ULA destination sources from its own ULA.
VLAN_IOT_V6 = IPv6Network('fd1a:665f:8bcb:90::/64')

#: The port the media Gateway serves. The one thing the IoT VLAN may reach in
#: the pool, and it is HTTPS because everything behind that Gateway is.
MEDIA_PORT = 443

#: What the controller calls a routed LAN whose gateway holds an address on
#: it. `vlan-only` is the other candidate and the wrong one: it describes a
#: VLAN the gateway does not terminate, which would leave the nodes without
#: the default route, the BGP peer and the firewall zone that are the entire
#: reason for putting them on a VLAN.
NETWORK_PURPOSE = 'corporate'

#: The controller spells a network's addressing as its *own* interface
#: address plus the prefix — `10.0.0.1/24`, not `10.0.0.0/24` — so the subnet
#: and the gateway are one field.
CLUSTER_SUBNET = f'{conventions.CLUSTER_VLAN_GATEWAY_V4}/{conventions.CLUSTER_VLAN_V4.prefixlen}'

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
    """The cluster's network and zone, two address groups, ten rules, one forward."""

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

        # A zone of its own, and a zone this program creates rather than one
        # it looks up: the two above are stock, this one exists because the
        # cluster is a population the gateway should be able to police
        # separately from everything else on the internal side. Its membership
        # is stated from the network below rather than here — the controller
        # accepts the association from either end and two ends managing it
        # fight over it.
        self.zone = unifi.FirewallZone(
            f'{name}-zone',
            name=conventions.UNIFI_ZONE_CLUSTER,
            site=site,
            opts=child,
        )

        # The cluster VLAN itself. Static-only: `dhcp_enabled` is off because
        # every node states its own address in machine configuration, and a
        # server offering leases beside that is a second opinion nothing
        # needs. IPv6 is a delegated prefix with router advertisements on,
        # because the design's worker GUA is a SLAAC address formed from what
        # this network advertises.
        self.network = unifi.Network(
            f'{name}-network',
            name=conventions.UNIFI_NETWORK_CLUSTER,
            purpose=NETWORK_PURPOSE,
            subnet=CLUSTER_SUBNET,
            vlan_id=conventions.CLUSTER_VLAN_ID,
            dhcp_enabled=False,
            dhcp_v6_enabled=False,
            ipv6_interface_type='pd',
            ipv6_pd_interface='wan',
            ipv6_ra_enable=True,
            firewall_zone_id=self.zone.id,
            site=site,
            opts=child,
        )

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

        # The cluster zone's way out. A zone the controller has only just been
        # told about is denied against every other zone in both directions, so
        # this is not a tightening but the thing that makes the zone usable at
        # all: a node whose control plane is in a cloud region has to be able
        # to leave the site. Both families in one policy, because the zone is
        # dual-stack and nothing here distinguishes them.
        self.cluster_egress = unifi.FirewallZonePolicy(
            f'{name}-cluster-egress',
            name=f'{name} cluster nodes outbound',
            description='Cluster nodes may reach the internet; their control plane is off-site.',
            action='ALLOW',
            ip_version='BOTH',
            protocol='all',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=self.zone.id),
            destination=unifi.FirewallZonePolicyDestinationArgs(zone_id=external),
            auto_allow_return_traffic=True,
            enabled=True,
            opts=child,
        )
        self.cluster_egress_order = unifi.FirewallZonePolicyOrder(
            f'{name}-cluster-egress-order',
            source_zone_id=self.zone.id,
            destination_zone_id=external,
            before_predefined_ids=[self.cluster_egress.id],
            site=site,
            opts=child,
        )

        # The cluster zone into the home's. Workload-initiated traffic runs
        # this way — the home-automation API on the IoT VLAN is the recorded
        # example — and what a node's own workloads may call is decided where
        # those workloads are declared, not here. Both families and every
        # protocol, for the same reason the egress is.
        self.cluster_internal = unifi.FirewallZonePolicy(
            f'{name}-cluster-internal',
            name=f'{name} cluster nodes to internal',
            description='Cluster workloads may reach home services; the recorded dependencies run this way.',
            action='ALLOW',
            ip_version='BOTH',
            protocol='all',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=self.zone.id),
            destination=unifi.FirewallZonePolicyDestinationArgs(zone_id=internal),
            auto_allow_return_traffic=True,
            enabled=True,
            opts=child,
        )
        self.cluster_internal_order = unifi.FirewallZonePolicyOrder(
            f'{name}-cluster-internal-order',
            source_zone_id=self.zone.id,
            destination_zone_id=internal,
            before_predefined_ids=[self.cluster_internal.id],
            site=site,
            opts=child,
        )

        # The way back in, and the one carve-out in it. The IoT VLAN is
        # dropped ahead of the allow because the node subnet is where apid,
        # the kubelet and the BGP session live, and the only recorded
        # IoT-originated dependency — a television reaching the media VIP —
        # targets the pool instead. Family by family, because the source is a
        # literal subnet and a literal belongs to one family.
        self.iot_cluster_v4 = unifi.FirewallZonePolicy(
            f'{name}-iot-cluster-v4',
            name=f'{name} IoT to cluster nodes (v4)',
            description='IoT VLAN may not reach the cluster node subnet.',
            action='BLOCK',
            ip_version='IPV4',
            protocol='all',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=internal, ips=[str(conventions.VLAN_IOT)]),
            destination=unifi.FirewallZonePolicyDestinationArgs(zone_id=self.zone.id),
            enabled=True,
            opts=child,
        )
        self.iot_cluster_v6 = unifi.FirewallZonePolicy(
            f'{name}-iot-cluster-v6',
            name=f'{name} IoT to cluster nodes (v6)',
            description='IoT VLAN may not reach the cluster node subnet.',
            action='BLOCK',
            ip_version='IPV6',
            protocol='all',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=internal, ips=[str(VLAN_IOT_V6)]),
            destination=unifi.FirewallZonePolicyDestinationArgs(zone_id=self.zone.id),
            enabled=True,
            opts=child,
        )

        # Everything else on the internal side keeps the reachability it had
        # while the nodes shared the untagged LAN: the operator's own machines
        # debugging a node directly, a ping, a host path that hairpins. What
        # the move changes is that the openness is now declared.
        self.internal_cluster = unifi.FirewallZonePolicy(
            f'{name}-internal-cluster',
            name=f'{name} internal to cluster nodes',
            description='Trusted home VLANs may reach the cluster nodes directly.',
            action='ALLOW',
            ip_version='BOTH',
            protocol='all',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=internal),
            destination=unifi.FirewallZonePolicyDestinationArgs(zone_id=self.zone.id),
            auto_allow_return_traffic=True,
            enabled=True,
            opts=child,
        )

        # One order for the pair, and on this one the drops come first: an
        # allow for the whole internal zone declared ahead of them would
        # answer for the IoT VLAN too and the carve-out would match nothing.
        self.internal_cluster_order = unifi.FirewallZonePolicyOrder(
            f'{name}-internal-cluster-order',
            source_zone_id=internal,
            destination_zone_id=self.zone.id,
            before_predefined_ids=[
                self.iot_cluster_v4.id,
                self.iot_cluster_v6.id,
                self.internal_cluster.id,
            ],
            site=site,
            opts=child,
        )

        # The pinhole, into the cluster zone: the worker is the destination
        # and the worker is no longer on the internal side. A literal address
        # rather than anything prefix-relative — the zone-policy API matches
        # literal addresses only, and the site's delegated prefix rotates, so
        # this rule is re-declared when it does. A stale rule degrades to
        # outbound-only IPv6 — the accepted first stage — rather than to
        # anything unsafe.
        self.peer_v6 = unifi.FirewallZonePolicy(
            f'{name}-peer-v6',
            name=f'{name} inbound peer port (v6)',
            description='Inbound peer traffic to the worker VM on the bulk-transfer port.',
            action='ALLOW',
            ip_version='IPV6',
            protocol='tcp_udp',
            source=unifi.FirewallZonePolicySourceArgs(zone_id=external),
            destination=unifi.FirewallZonePolicyDestinationArgs(
                zone_id=self.zone.id,
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
            destination_zone_id=self.zone.id,
            before_predefined_ids=[self.peer_v6.id],
            site=site,
            opts=child,
        )

        # The IPv4 half of the same flow, and the only port forward on the
        # device. It lands on the worker VM's node address — read from
        # `conventions`, so it follows the VLAN wherever the address plan puts
        # it — because that is the address the application's outbound peer
        # traffic already wears: the cluster masquerades it to the node, so
        # inbound has to arrive there for a peer to see one endpoint rather
        # than two.
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
