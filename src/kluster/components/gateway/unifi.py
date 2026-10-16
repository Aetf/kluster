"""The cluster's network on the gateway, and the firewall census around it
(physical/gateway.md §4.2).

Two kinds of thing are declared here, and the first is why the second can be
written at all.

**The cluster VLAN, as a network object and a zone of its own.** Cluster nodes
do not share the untagged server LAN: they sit on the subnet and VLAN id
`conventions.CLUSTER_VLAN` declares, which the controller serves with
no DHCP server — every node states its own address in machine configuration
(`components/talos/`), so a lease would be a second opinion about an address
three other places already treat as constant. Being a network object is what
makes the population nameable in a policy; being alone in a firewall zone is
what makes it *separately* nameable from everything else the internal zone
holds.

**The rule census**, which is exactly the design's and nothing more: a rule on
the controller that is not declared here is drift. It is `CENSUS`, one row per
zone pair carrying policies and one for the port forward, each with the reason
it exists beside it, and `SiteFirewall` declares what the rows say and nothing
else. So a rule added or removed is a line in that structure's diff, and so
are the address groups a rule names, which exist because a row names them.

Every zone pair that carries a policy also carries a
`FirewallZonePolicyOrder`, and the order is the row's: a policy declared
without one takes whatever position creation happened to produce, and on a
pair with both a drop and an allow that is the difference between the design
and its opposite. Deriving the order from the row is what keeps a policy from
being declared without a place.

Three facts about the device shape the whole module (physical/gateway.md
§4.1, measured, and §4.2):

-   **The zone firewall classifies forwarded traffic by destination ipset.**
    The home VLANs sit in the internal zone, so IoT → server-LAN is an
    intra-zone pair; but the `lan` pool is deliberately not a network object
    (it would fight the host routes the cluster advertises), so it lands in
    no zone ipset at all and pool-bound traffic falls through to the
    internal → external pair. That is why rules about the pool are declared
    on that pair. The cluster VLAN is the deliberate opposite: it *is* an
    object, and alone in its zone, so a policy names it by naming the zone.
-   **A zone policy's `ips` holds single addresses, never a subnet.** The
    pinned provider validates every entry as one IPv4 or IPv6 address and
    refuses a CIDR at preview, and the controller's field specification,
    which the provider's client library is generated from, defines each
    entry as one address. A subnet a policy names is therefore an **address
    group**, through `ip_group_id`: the pool, because the controller has no
    object for it, and the IoT VLAN as a source, because this program holds
    the VLAN's addresses and not its controller-side identity — it neither
    declares that network nor records the name it could be looked up by, so
    a `network_ids` match would rest on a value nothing here states.
-   **Address groups are single-family.** One group cannot hold both an IPv4
    CIDR and its unique-local IPv6 prefix, so every rule naming a subnet
    comes as a pair, and each subnet's two groups are two resources.

Authentication is an API key belonging to a dedicated local administrator,
carried by a provider instance that exists only for these resources. It is
never the SSH credential the device's own desired state travels on, and it
never reaches the environment that deploys applications — that separation is
the reason these resources live in the `physical` stack rather than beside
the applications whose traffic they admit. The connection is told not to
verify the controller's certificate, and `ALLOW_INSECURE` is why and what
that costs.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Literal

import pulumi
import pulumi_unifi as unifi

from kluster import conventions
from putils import Component, own_provider_opts, with_provider

__all__ = ('CENSUS', 'SiteFirewall', 'controller_provider')

#: The predefined zones of a UniFi OS 9 controller, looked up by name rather
#: than by id: an id is per-site state, a name is stock. `Internal` holds
#: every LAN network object; `External` is the uplink — and, because the
#: `lan` pool is no network object, also where pool-bound traffic is
#: classified.
ZONE_INTERNAL = 'Internal'
ZONE_EXTERNAL = 'External'

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
CLUSTER_SUBNET = f'{conventions.CLUSTER_VLAN.require_gateway()}/{conventions.CLUSTER_VLAN.v4.prefixlen}'

#: How many times the provider may re-attempt a request the controller failed
#: transiently: three at most, and two is enough. The bound is there for the
#: console rather than for this program: the controller's login rate limit is
#: account-wide rather than per-address, so a retry storm from a runner does
#: not merely fail the run — it locks out the people using the console. Two
#: covers the transient responses the controller returns under parallel load;
#: anything past that is a fault to report, not to grind against, and raising
#: the number past three trades the console's availability for a run's.
HTTP_MAX_RETRIES = 2

#: Where the controller's API key is read. A key belonging to a dedicated local
#: administrator on the device, never the SSH credential, and read at the one
#: line that uses it.
API_KEY = 'unifiApiKey'

#: Whether the provider skips verifying the controller's certificate, passed
#: explicitly because the provider reads `UNIFI_INSECURE` from the environment
#: of whoever runs the program whenever the input is omitted.
#:
#: It skips it because nothing narrower exists. The controller presents a
#: self-signed certificate of the device's own, naming `unifi.local`,
#: `localhost`, and loopback and link-local addresses — none of the literal
#: addresses this program dials, the bootstrap address during the ceremony and
#: the gateway's overlay address otherwise. And
#: at the pinned filipowm/unifi release the provider's one TLS input is this
#: flag: it takes no CA bundle, no fingerprint, no server name and no resolver,
#: so it can neither pin that certificate nor verify it under another name.
#:
#: What it trades is the API key, sent as a header on every request to
#: whatever completes the handshake at the dialed address; the key is the
#: dedicated administrator's, enough to rewrite the site's firewall. A
#: continuous-integration run is out of reach: the overlay's flow rules let no
#: member but the gateway answer a run at the gateway's address
#: (`components/overlay/flow_rules.py`). Every other run is the residual — one
#: dialing the bootstrap address, which any host on the dialing workstation's
#: segment can answer, and one made from a personal overlay member, which any
#: member without the `ci` role can. Both answerers are trusted at the network
#: layer (docs/threat-model.md §2.10), so architecture.md §4.1 accepts the
#: residual as it stands for a dial over the server LAN or from a personal
#: overlay member; a dial over the cluster VLAN is not covered by that
#: acceptance.
ALLOW_INSECURE = True


def controller_provider(
    name: str,
    *,
    api_url: str,
    site: str,
    opts: pulumi.ResourceOptions | None = None,
) -> unifi.Provider:
    """The provider every call to the controller is made through.

    One function because the construction is a security posture, not a list of
    arguments: `SiteFirewall` builds its provider here, and so does the
    pre-window probe (physical/gateway-cutover.md §3), so what the probe proves
    is the construction the window will use.

    The key is read here, at the line that builds the provider (rfc-002 §8.1):
    a provider and the secret that opens it are one thing, and no caller
    handles the key.
    """
    return unifi.Provider(
        name,
        api_url=api_url,
        api_key=pulumi.Config().require_secret(API_KEY),
        site=site,
        http_max_retries=HTTP_MAX_RETRIES,
        allow_insecure=ALLOW_INSECURE,
        opts=opts,
    )


class Zone(enum.Enum):
    """The zones a policy is declared between.

    Two are the controller's own and looked up by name (`ZONE_INTERNAL`,
    `ZONE_EXTERNAL`); the cluster's is the one `SiteFirewall` creates.
    """

    INTERNAL = enum.auto()
    EXTERNAL = enum.auto()
    CLUSTER = enum.auto()


class Needs(enum.Enum):
    """What a conditional row waits for before it is declared at all."""

    #: The worker's global IPv6 address, which it forms by SLAAC off the
    #: cluster VLAN and so cannot have before that network is applied and the
    #: worker has booted on it.
    WORKER_GUA = enum.auto()
    #: The bulk-transfer application running on the worker rather than on the
    #: legacy host, which holds the WAN port until then.
    PEER_ON_WORKER = enum.auto()


class Worker(enum.Enum):
    """An address a row names that the program does not hold: it arrives as an input."""

    #: The worker's global IPv6 address (`Needs.WORKER_GUA`).
    GUA = enum.auto()


@dataclass(frozen=True)
class AddressGroup:
    """A subnet as the controller lets a policy name one: a group, in the one family a group holds.

    `key` is the resource's name after the component's, and so part of its
    URN: changing it replaces the group on the controller.
    """

    key: str
    #: What the console shows.
    name: str
    subnet: IPv4Network | IPv6Network

    @property
    def kind(self) -> str:
        return 'address-group' if self.subnet.version == 4 else 'ipv6-address-group'


#: The `lan` pool, which has no controller object of its own: a group is the
#: only thing a policy can name it by. Its names are the site's
#: (`conventions.LAN_POOL`), because the pool is.
POOL_V4 = AddressGroup('pool-v4', conventions.LAN_POOL.group_v4, conventions.LAN_POOL.v4)
POOL_V6 = AddressGroup('pool-v6', conventions.LAN_POOL.group_v6, conventions.LAN_POOL.v6)

#: The IoT VLAN, as the source of every rule that carves it out: a network
#: object on the controller, but not one this program declares or holds the
#: identity of, so it is named by the addresses the site's plan gives it.
#: Only this component reads these names, so they are spelled here, in the
#: same form as the pool's beside them on the console.
IOT_V4 = AddressGroup('iot-v4', f'{conventions.CLUSTER_NAME}-iot-v4', conventions.IOT_VLAN.v4)
IOT_V6 = AddressGroup('iot-v6', f'{conventions.CLUSTER_NAME}-iot-v6', conventions.IOT_VLAN.v6)


@dataclass(frozen=True)
class Policy:
    """One zone policy, on the pair whose row holds it.

    An allow also admits its return leg (`auto_allow_return_traffic`): an
    allow whose return leg is not allowed is not an allow, and the reverse
    zone pair is where the return leg is classified. A drop sets nothing
    there.

    `key` is the resource's name after the component's, and so part of its
    URN: changing it replaces the policy on the controller.
    """

    key: str
    #: What the console shows after the cluster's name. Its audience is a
    #: person looking at the console rather than anything in this program.
    title: str
    #: The console's one-line reason, which is the row's.
    description: str
    action: Literal['ALLOW', 'BLOCK']
    ip_version: Literal['IPV4', 'IPV6', 'BOTH']
    protocol: Literal['all', 'tcp', 'tcp_udp']
    #: Narrows the pair's source zone to one subnet; `None` is the whole zone.
    source: AddressGroup | None = None
    #: Narrows the pair's destination zone: a group, one literal address, or
    #: an address the program is handed. `None` is the whole zone.
    destination: AddressGroup | IPv4Address | IPv6Address | Worker | None = None
    port: int | None = None


@dataclass(frozen=True)
class Pair:
    """A zone pair's policies, in the order the pair evaluates them, all ahead of its predefined ones.

    The pair's `FirewallZonePolicyOrder` is this tuple, so a policy cannot be
    declared without a place, and the place is its position here. `order` is
    that resource's name after the component's, part of its URN as a
    policy's key is.
    """

    order: str
    source: Zone
    destination: Zone
    policies: tuple[Policy, ...]
    needs: Needs | None = None


@dataclass(frozen=True)
class Forward:
    """A WAN port forward. `key` is the resource's name after the component's, part of its URN."""

    key: str
    #: What the console shows after the cluster's name.
    title: str
    protocol: Literal['tcp', 'udp', 'tcp_udp']
    port: int
    to: IPv4Address
    needs: Needs | None = None


#: Everything this program declares on the controller's firewall, row by row,
#: each with its reason. The pairs come first and the forward last; within a
#: pair, the order is the rule.
CENSUS: tuple[Pair | Forward, ...] = (
    # IoT → the `lan` pool: one enumerated allow, then a drop. The allow is the
    # media Gateway's VIP on 443, which is what smart televisions and
    # streamers consume; the drop is the rest of the pool, where the cluster's
    # administrative interfaces live. The firewall names only the media VIP,
    # so deciding which applications the IoT VLAN may reach is a Gateway-layer
    # edit and never a firewall edit. On the uplink pair, because that is
    # where pool-bound traffic is classified (module docstring). Both allows
    # precede both drops, and all four precede the pair's predefined
    # policies: otherwise the predefined accept on the uplink pair would
    # answer first and the drop would never be reached. Family by family,
    # because the source is the VLAN's group and a group holds one family.
    Pair(
        order='iot-pool-order',
        source=Zone.INTERNAL,
        destination=Zone.EXTERNAL,
        policies=(
            Policy(
                key='iot-media-v4',
                title='IoT to media VIP (v4)',
                description='IoT VLAN may reach the media gateway VIP on HTTPS.',
                action='ALLOW',
                ip_version='IPV4',
                protocol='tcp',
                source=IOT_V4,
                destination=conventions.LAN_POOL.media_vip.v4,
                port=MEDIA_PORT,
            ),
            Policy(
                key='iot-media-v6',
                title='IoT to media VIP (v6)',
                description='IoT VLAN may reach the media gateway VIP on HTTPS.',
                action='ALLOW',
                ip_version='IPV6',
                protocol='tcp',
                source=IOT_V6,
                destination=conventions.LAN_POOL.media_vip.v6,
                port=MEDIA_PORT,
            ),
            Policy(
                key='iot-pool-v4',
                title='IoT to lan pool (v4)',
                description='IoT VLAN may not reach the rest of the cluster pool.',
                action='BLOCK',
                ip_version='IPV4',
                protocol='all',
                source=IOT_V4,
                destination=POOL_V4,
            ),
            Policy(
                key='iot-pool-v6',
                title='IoT to lan pool (v6)',
                description='IoT VLAN may not reach the rest of the cluster pool.',
                action='BLOCK',
                ip_version='IPV6',
                protocol='all',
                source=IOT_V6,
                destination=POOL_V6,
            ),
        ),
    ),
    # The cluster zone's way out. A zone the controller has only just been
    # told about is denied against every other zone in both directions, so
    # this is not a tightening but the thing that makes the zone usable at
    # all: a node whose control plane is in a cloud region has to be able to
    # leave the site. Both families in one policy, because the zone is
    # dual-stack and nothing here distinguishes them.
    Pair(
        order='cluster-egress-order',
        source=Zone.CLUSTER,
        destination=Zone.EXTERNAL,
        policies=(
            Policy(
                key='cluster-egress',
                title='cluster nodes outbound',
                description='Cluster nodes may reach the internet; their control plane is off-site.',
                action='ALLOW',
                ip_version='BOTH',
                protocol='all',
            ),
        ),
    ),
    # The cluster zone into the home's. Workloads on the nodes initiate toward
    # home services — the home-automation API on the IoT VLAN among them —
    # and that direction is where the recorded dependencies run. What a node's
    # own workloads may call is decided where those workloads are declared,
    # not here, so every protocol and both families, as for the egress.
    Pair(
        order='cluster-internal-order',
        source=Zone.CLUSTER,
        destination=Zone.INTERNAL,
        policies=(
            Policy(
                key='cluster-internal',
                title='cluster nodes to internal',
                description='Cluster workloads may reach home services; the recorded dependencies run this way.',
                action='ALLOW',
                ip_version='BOTH',
                protocol='all',
            ),
        ),
    ),
    # The way back in, minus the IoT VLAN, and on this pair the drops come
    # first. The trusted home VLANs reach the nodes directly — debugging
    # straight at a node, a ping, a host path that hairpins. The IoT VLAN does
    # not: the node subnet is where apid, the kubelet and the BGP session
    # live, and the one recorded IoT-originated dependency — a television
    # reaching the media VIP — targets the pool instead, so the drops sever
    # nothing known. An allow for the whole internal zone ahead of them would
    # answer for the IoT VLAN too, and the carve-out would match nothing. The
    # same shape as the first row one zone over: the zone stays open, the
    # untrusted subpopulation is carved out.
    Pair(
        order='internal-cluster-order',
        source=Zone.INTERNAL,
        destination=Zone.CLUSTER,
        policies=(
            Policy(
                key='iot-cluster-v4',
                title='IoT to cluster nodes (v4)',
                description='IoT VLAN may not reach the cluster node subnet.',
                action='BLOCK',
                ip_version='IPV4',
                protocol='all',
                source=IOT_V4,
            ),
            Policy(
                key='iot-cluster-v6',
                title='IoT to cluster nodes (v6)',
                description='IoT VLAN may not reach the cluster node subnet.',
                action='BLOCK',
                ip_version='IPV6',
                protocol='all',
                source=IOT_V6,
            ),
            Policy(
                key='internal-cluster',
                title='internal to cluster nodes',
                description='Trusted home VLANs may reach the cluster nodes directly.',
                action='ALLOW',
                ip_version='BOTH',
                protocol='all',
            ),
        ),
    ),
    # The inbound IPv6 pinhole for the bulk-transfer application's peer port,
    # into the cluster zone because the worker sits on the cluster VLAN. A
    # literal address rather than anything prefix-relative — the zone-policy
    # API matches literal addresses only, and the site's delegated prefix
    # rotates, so this row is re-declared when it does. A stale rule degrades
    # to outbound-only IPv6, the accepted first stage, rather than to
    # anything unsafe; and with no address yet to name, the pair is left
    # undeclared rather than admitting a guessed one, which is the same stage
    # reached from the other side.
    Pair(
        order='peer-order',
        source=Zone.EXTERNAL,
        destination=Zone.CLUSTER,
        policies=(
            Policy(
                key='peer-v6',
                title='inbound peer port (v6)',
                description='Inbound peer traffic to the worker VM on the bulk-transfer port.',
                action='ALLOW',
                ip_version='IPV6',
                protocol='tcp_udp',
                destination=Worker.GUA,
                port=conventions.QBITTORRENT_PEER_PORT,
            ),
        ),
        needs=Needs.WORKER_GUA,
    ),
    # The IPv4 half of the same flow, and the only port forward on the device.
    # Nothing else is published inbound: cluster and node management arrive
    # over the cloud load balancer, home-side management over ZeroTier. Both
    # halves take the port from the same place the public port census lives,
    # because two rules that disagree on it admit nothing.
    #
    # It lands on the worker VM's node address because that is the address
    # the application's outbound peer traffic already wears: the cluster
    # masquerades it to the node, so inbound has to arrive there for a peer to
    # see one endpoint rather than two. The address is stated by the address
    # plan rather than observed off a booted machine, so it never waits for
    # the worker.
    #
    # What it waits for is the application. A WAN port forwards to one host at
    # a time, and until the application moves onto the worker its legacy copy
    # on the host holds this one by whatever mechanism it uses today, a UPnP
    # lease or a rule kept by hand. A forward declared then either collides
    # with that rule or wins over the lease and takes the legacy copy's
    # inbound IPv4 away, and nothing reports either. Moving the port is a step
    # of the application's own migration (cluster/migration.md, Wave D).
    Forward(
        key='peer-v4',
        title='inbound peer port (v4)',
        protocol='tcp_udp',
        port=conventions.QBITTORRENT_PEER_PORT,
        to=conventions.HOMELAB_NODE_IPV4,
        needs=Needs.PEER_ON_WORKER,
    ),
)


class SiteFirewall(Component, pulumi_type='kluster:gateway:SiteFirewall'):
    """The cluster's network and zone, and the rows of `CENSUS` that apply.

    A row with `needs` is declared only once what it names is met — today the
    pinhole's pair, on `worker_gua`, and the peer's IPv4 forward, on
    `peer_on_worker` — and every other row unconditionally. A row's
    address groups are declared because a policy present names them, once
    each, and each pair's policies are followed by the one
    `unifi.FirewallZonePolicyOrder` that ranks them.

    `policies`, `orders`, `groups` and `forwards` hold what was declared, by
    the key the row gives it.

    `static_hosts` is the roll of names the device's own resolver answers for
    literally, and it arrives from the caller: which names have no lease behind
    them is a fact about the site rather than about the firewall.
    """

    def __init__(
        self,
        name: str,
        *,
        api_url: str,
        site: str,
        worker_gua: pulumi.Input[str] | None,
        peer_on_worker: bool = False,
        static_hosts: Mapping[str, IPv4Address | IPv6Address],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # A provider of its own, rather than ambient configuration: its key
        # authorizes changes to the home's firewall, and the resources it may
        # reach are exactly the ones below. It is built before the component
        # registers, because a provider reaches a subtree through the options
        # the component is registered with.
        provider = controller_provider(f'{name}-unifi', api_url=api_url, site=site, opts=own_provider_opts(opts))
        super().__init__(name, opts=with_provider(opts, provider))
        self.provider = provider
        self.site = site

        child = self.child_opts()
        # An invoke inherits a provider only through a parent: given one it
        # takes that parent's, and given neither it takes the default.
        invoke = pulumi.InvokeOptions(parent=self)

        # A zone of its own, and a zone this program creates rather than one
        # it looks up: the other two are stock, this one exists because the
        # cluster is a population the gateway should be able to police
        # separately from everything else on the internal side. Its membership
        # is stated from the network below rather than here — the controller
        # accepts the association from either end and two ends managing it
        # fight over it.
        self.zone = unifi.FirewallZone(
            f'{name}-zone',
            name=conventions.gateway.UNIFI_ZONE_CLUSTER,
            site=site,
            opts=child,
        )
        zones: dict[Zone, pulumi.Input[str]] = {
            Zone.INTERNAL: unifi.get_firewall_zone_output(name=ZONE_INTERNAL, site=site, opts=invoke).id,
            Zone.EXTERNAL: unifi.get_firewall_zone_output(name=ZONE_EXTERNAL, site=site, opts=invoke).id,
            Zone.CLUSTER: self.zone.id,
        }

        # The cluster VLAN itself. Static-only: `dhcp_enabled` is off because
        # every node states its own address in machine configuration, and a
        # server offering leases beside that is a second opinion nothing
        # needs. IPv6 is a delegated prefix with router advertisements on,
        # because the design's worker GUA is a SLAAC address formed from what
        # this network advertises.
        self.network = unifi.Network(
            f'{name}-network',
            name=conventions.gateway.UNIFI_NETWORK_CLUSTER,
            purpose=NETWORK_PURPOSE,
            subnet=CLUSTER_SUBNET,
            vlan_id=conventions.CLUSTER_VLAN.vlan_id,
            dhcp_enabled=False,
            dhcp_v6_enabled=False,
            ipv6_interface_type='pd',
            ipv6_pd_interface='wan',
            ipv6_ra_enable=True,
            firewall_zone_id=self.zone.id,
            site=site,
            opts=child,
        )

        # The addresses a row may name that the program is handed rather than
        # holds, present only once they have been handed in: a row naming one
        # is declared only when its `needs` is met, which is the same fact.
        handed_in: dict[Worker, pulumi.Input[str]] = {} if worker_gua is None else {Worker.GUA: worker_gua}
        met = {Needs.WORKER_GUA: Worker.GUA in handed_in, Needs.PEER_ON_WORKER: peer_on_worker}
        rows = [row for row in CENSUS if row.needs is None or met[row.needs]]
        pairs = [row for row in rows if isinstance(row, Pair)]

        self.groups: dict[str, unifi.FirewallGroup] = {}
        for policy in (policy for pair in pairs for policy in pair.policies):
            for group in (policy.source, policy.destination):
                if isinstance(group, AddressGroup) and group.key not in self.groups:
                    self.groups[group.key] = unifi.FirewallGroup(
                        f'{name}-{group.key}',
                        name=group.name,
                        type=group.kind,
                        members=[str(group.subnet)],
                        site=site,
                        opts=child,
                    )

        def destination(policy: Policy, zone: pulumi.Input[str]) -> unifi.FirewallZonePolicyDestinationArgs:
            match policy.destination:
                case AddressGroup() as group:
                    return unifi.FirewallZonePolicyDestinationArgs(
                        zone_id=zone, ip_group_id=self.groups[group.key].id, port=policy.port
                    )
                case IPv4Address() | IPv6Address() as address:
                    return unifi.FirewallZonePolicyDestinationArgs(zone_id=zone, ips=[str(address)], port=policy.port)
                case Worker() as handed:
                    return unifi.FirewallZonePolicyDestinationArgs(
                        zone_id=zone, ips=[handed_in[handed]], port=policy.port
                    )
                case None:
                    return unifi.FirewallZonePolicyDestinationArgs(zone_id=zone, port=policy.port)

        self.policies: dict[str, unifi.FirewallZonePolicy] = {}
        self.orders: dict[str, unifi.FirewallZonePolicyOrder] = {}
        for pair in pairs:
            declared = [
                unifi.FirewallZonePolicy(
                    f'{name}-{policy.key}',
                    name=f'{conventions.CLUSTER_NAME} {policy.title}',
                    description=policy.description,
                    action=policy.action,
                    ip_version=policy.ip_version,
                    protocol=policy.protocol,
                    source=unifi.FirewallZonePolicySourceArgs(
                        zone_id=zones[pair.source],
                        ip_group_id=None if policy.source is None else self.groups[policy.source.key].id,
                    ),
                    destination=destination(policy, zones[pair.destination]),
                    auto_allow_return_traffic=True if policy.action == 'ALLOW' else None,
                    enabled=True,
                    opts=child,
                )
                for policy in pair.policies
            ]
            self.policies.update(
                (policy.key, resource) for policy, resource in zip(pair.policies, declared, strict=True)
            )
            self.orders[pair.order] = unifi.FirewallZonePolicyOrder(
                f'{name}-{pair.order}',
                source_zone_id=zones[pair.source],
                destination_zone_id=zones[pair.destination],
                before_predefined_ids=[resource.id for resource in declared],
                site=site,
                opts=child,
            )

        self.forwards: dict[str, unifi.PortForward] = {
            row.key: unifi.PortForward(
                f'{name}-{row.key}',
                name=f'{conventions.CLUSTER_NAME} {row.title}',
                port_forward_interface='wan',
                protocol=row.protocol,
                src_ip='any',
                dst_port=str(row.port),
                fwd_ip=str(row.to),
                fwd_port=str(row.port),
                site=site,
                opts=child,
            )
            for row in rows
            if isinstance(row, Forward)
        }

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
            for host, address in static_hosts.items()
        }

        self.register_outputs({})
