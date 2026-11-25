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

**The rule census**, which is exactly the design's and nothing more: a pair
the census orders belongs to it whole, so a custom policy there that is not
declared here is drift. It is `CENSUS`, one row per zone pair carrying policies,
one per port forward, one for the gateway's UPnP and NAT-PMP service and one
per fixed-IP reservation, each with the reason it exists beside it, and
`SiteFirewall` declares what the rows say and nothing else. So a rule added or
removed is a line in that structure's diff, and so are the address groups a
rule names, which exist because a row names them.

One of the objects the rows render is the controller's own, which the census
adopts rather than creates: the site's gateway settings (`PortMapping`). The
bridged resources are wholes, each write a PUT of the whole document, so an
adopting declaration states the fields the census owns and ignores every other
input of the resource, the console's (`adopt`).

Every zone pair that carries a policy also carries a `ZonePolicyOrder`, and
the order is the row's: a policy declared without one takes whatever position
creation happened to produce, and on a pair with both a drop and an allow that
is the difference between the design and its opposite. Deriving the order from
the row is what keeps a policy from being declared without a place. It is that
resource rather than the bridged `FirewallZonePolicyOrder` because the
controller takes a pair's rank only as a batch naming every custom policy of
the pair, which a declared list alone cannot be while the census's own edits
are in transit (`kluster.providers.unifi_zone_policy_order`, which also says
what removes it).

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
    a `network_ids` match would rest on a value nothing here states. A
    single host is a literal in `ips`, on either side: the media VIP as a
    destination, Home Assistant as a source.
-   **Address groups are single-family.** One group cannot hold both an IPv4
    CIDR and its unique-local IPv6 prefix, so every rule naming a subnet
    comes as a pair, and each subnet's two groups are two resources.

Authentication is an API key belonging to a dedicated local administrator,
carried by a provider instance that exists only for these resources and read
by the order's provider and the BGP configuration's for their own sessions. It is never the SSH credential
the device's own desired state travels on, and it never reaches the environment that deploys applications — that separation is
the reason these resources live in the `physical` stack rather than beside
the applications whose traffic they admit. The connection is told not to
verify the controller's certificate, and `ALLOW_INSECURE` is why and what
that costs.
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Any, Literal

import pulumi
import pulumi_unifi as unifi

from kluster import conventions
from kluster.conventions.homelab import HOMELAB_HOST, HOMELAB_HOST_NAME
from kluster.providers.unifi_bgp import BgpConfiguration
from kluster.providers.unifi_zone_policy_order import ZonePolicyOrder
from putils import Component, own_provider_opts, with_provider

__all__ = ('CENSUS', 'SiteFirewall', 'controller_provider', 'policy_order', 'return_flag', 'site_bgp')

#: The predefined zones of a UniFi OS 9 controller, looked up by name rather
#: than by id: an id is per-site state, a name is stock. `Internal` holds
#: every LAN network object; `External` is the uplink — and, because the
#: `lan` pool is no network object, also where pool-bound traffic is
#: classified; `Gateway` is the device itself, the destination of what
#: arrives at its own addresses, the WAN's among them.
ZONE_INTERNAL = 'Internal'
ZONE_EXTERNAL = 'External'
ZONE_GATEWAY = 'Gateway'

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

#: The address range a network on a delegated prefix states. The controller
#: refuses a delegated network without one (`api.err.InvalidIpv6Addr`), though
#: nothing here leases from it: the cluster VLAN has no DHCPv6 server, and
#: these are the bounds the controller gives every network on the site.
DELEGATED_RANGE = ('::2', '::7d1')

#: The WAN the gateway's UPnP and NAT-PMP service maps ports on, by the
#: controller's name for it: the primary WAN, `WAN`, "Internet 1", which
#: carries the uplink. The second port, `WAN2`, carries none
#: (physical/gateway.md §3, "The uplink's own record of a window").
UPNP_WAN = 'WAN'

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
#: administrator on the device, never the SSH credential. It has two readers,
#: each opening one session with it and handing it to nothing: the line in
#: `controller_provider` that builds the bridged provider, and the `configure`
#: of the order's provider, under its own `API_KEY_CONFIG`.
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
#:
#: The order's session and the BGP configuration's take the same posture
#: (`policy_order`, `site_bgp`). Their transport could pin the certificate, and
#: that would change nothing: the same key crosses the same connection on the
#: bridged provider's every request.
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


def policy_order(
    name: str,
    *,
    api_url: str,
    site: str,
    source_zone_id: pulumi.Input[str],
    destination_zone_id: pulumi.Input[str],
    policies: Sequence[unifi.FirewallZonePolicy],
    opts: pulumi.ResourceOptions | None = None,
) -> ZonePolicyOrder:
    """The rank of one zone pair: `policies` first, in this order, and nothing else custom on the pair.

    One function for the reason `controller_provider` is one: `SiteFirewall`
    builds every order here, and so does the pre-window probe, so what the
    probe proves is the construction the window will use. It is also the one
    line the order's layer touches here.
    """
    return ZonePolicyOrder(
        name,
        api_url=api_url,
        allow_insecure=ALLOW_INSECURE,
        site=site,
        source_zone_id=source_zone_id,
        destination_zone_id=destination_zone_id,
        policy_ids=[policy.id for policy in policies],
        opts=opts,
    )


def site_bgp(
    name: str,
    *,
    api_url: pulumi.Input[str],
    site: pulumi.Input[str],
    config: pulumi.Input[str],
    description: str,
    file_name: str,
    opts: pulumi.ResourceOptions | None = None,
) -> BgpConfiguration:
    """The site's BGP configuration: `config`, switched on, written through the controller's API key.

    One function for the reason `policy_order` is one: the posture is
    `ALLOW_INSECURE`, stated once, and this is the one line the
    configuration's layer touches here (`kluster.providers.unifi_bgp`).
    """
    return BgpConfiguration(
        name,
        api_url=api_url,
        allow_insecure=ALLOW_INSECURE,
        site=site,
        config=config,
        description=description,
        file_name=file_name,
        opts=opts,
    )


def adopt[R: pulumi.CustomResource](
    resource: Callable[..., R],
    args: type,
    name: str,
    *,
    import_id: str,
    owned: Mapping[str, Any],
    refused: Mapping[str, str],
    opts: pulumi.ResourceOptions,
) -> R:
    """`resource`, adopted from the controller's object `import_id`: the inputs in `owned` declared, every other input of `args` ignored.

    The bridged resources are wholes. Each write is a PUT of the whole
    document, so an input left undeclared is not left alone: it is sent at
    whatever the plan holds. So an adopting declaration states the fields the
    census owns, and every other input goes into `ignore_changes`, where the
    engine takes it from the inputs the stack last recorded, the console's
    values. The ignored list is the inputs of `args`, the resource's own
    input type in the SDK, less `owned`, and is never written out: a field a
    regenerated SDK adds is the console's from the start rather than a field
    the next write zeroes. `owned` holds what the resource requires stated as
    well, at the value the controller holds.

    `refused` is the inputs the provider refuses to be handed on this
    controller, each with its reason, and they are neither owned nor
    ignored. An ignored input reaches the provider as configuration, at the
    value the import recorded, and the provider fails the plan of a resource
    configured with an input it gates on a controller version the site is
    past. So the other half of a regenerated SDK's new field: one the
    provider gates that way fails the plan by name until it is listed here.

    One function because the construction is the rule: every adopting row of
    the census goes through it, so no adoption owns a field it does not state
    (physical/gateway.md §4.2).
    """
    inputs = [
        parameter.name
        for parameter in inspect.signature(args).parameters.values()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    if unknown := sorted(set(refused) - set(inputs)):
        raise ValueError(f'{args.__name__} has no input {unknown}, so nothing refuses it')
    if both := sorted(set(refused) & set(owned)):
        raise ValueError(f'{both} is owned and refused at once')
    ignored = [field for field in inputs if field not in owned and field not in refused]
    return resource(
        name,
        **owned,
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(import_=import_id, ignore_changes=ignored)),
    )


class Zone(enum.Enum):
    """The zones a policy is declared between.

    The controller's own are looked up by name (`ZONE_INTERNAL`,
    `ZONE_EXTERNAL`, `ZONE_GATEWAY`); the cluster's is the one `SiteFirewall`
    creates.
    """

    INTERNAL = enum.auto()
    EXTERNAL = enum.auto()
    GATEWAY = enum.auto()
    CLUSTER = enum.auto()


def return_flag(
    action: Literal['ALLOW', 'BLOCK'],
    destination: Zone,
    connection_state: Literal['ALL', 'RESPOND_ONLY'] = 'ALL',
) -> Literal[True] | None:
    """A zone policy's `auto_allow_return_traffic`: how an allow admits its return leg.

    Set, the controller creates a `<name> (Return)` allow on the reverse pair
    beside the policy. That is how an allow gets its return leg. The exceptions
    are allows whose reverse pair already admits the return leg. An allow to
    `External` is one: the controller gives every other zone, `Internal` and
    the one `SiteFirewall` creates alike, a predefined `External → <zone>`
    policy named `Allow Return Traffic`, which admits related and established
    traffic only (`RESPOND_ONLY`), and it refuses to create a second one — `400
    … Firewall policy create respond traffic not allowed`, on every pair whose
    destination is `External` — while it accepts the flag on every other pair
    the census declares (read off the controller 2026-11-05,
    Aetf/kluster-ops#709). So an allow to `External` sets no flag, and its
    return leg is that predefined policy.

    An allow to `Gateway` is the other, and sets no flag either. Its reply is
    the device's own traffic, and the census's allows to `Gateway` come from
    `External`, so the reply is the device's traffic toward the WAN: the
    chain `UBIOS_LOCAL_WAN_USER`, which ends in an unconditional accept in
    both families (read off the device 2026-11-22, Aetf/kluster-ops#881). So
    its return leg is the controller's predefined accept on `Gateway →
    External`. An allow to `Gateway` from another zone would rest on a
    reading of its own reverse pair, which nothing has taken.

    Both are dependencies on policies this program neither declares nor
    reads, and nothing in it would notice one going away: a refresh compares
    only what is declared. Their absence would surface as dropped replies —
    to the allows toward `External`, the cluster's egress and the IoT VLAN's
    reach to the media VIP, and to the gateway's overlay member — not as a
    diff. physical/gateway.md §4.2 records the same dependencies.

    A drop has no return leg to admit, so it sets nothing.
    """
    if action != 'ALLOW' or destination in (Zone.EXTERNAL, Zone.GATEWAY):
        return None
    # An allow of `RESPOND_ONLY` is itself a return leg: every packet it
    # matches answers a connection opened from the other side, whose own allow
    # admits that side's packets, so no third policy has anything to admit.
    # The controller stores the shape with the flag off (read 2026-11-05,
    # Aetf/kluster-ops#714).
    if connection_state == 'RESPOND_ONLY':
        return None
    return True


class Needs(enum.Enum):
    """What a conditional row waits for before it is declared at all."""

    #: The bulk-transfer application running on the worker rather than on the
    #: legacy host, which holds the WAN port until then.
    PEER_ON_WORKER = enum.auto()


class Worker(enum.Enum):
    """An address a row names that the program does not hold: it arrives as an input."""

    #: The worker's global IPv6 address, which derives from the site's
    #: delegated prefix as this run reads it (`Gateway.worker_gua`).
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


def _site_forwarded() -> conventions.routes.Port:
    """The raw port the site's forward and pinhole open: the census's one row through the site's forward.

    The forward lands on one address, the worker's, and the rows below wait
    on one application, the bulk-transfer one (`Needs.PEER_ON_WORKER`), so
    they are written for one such row. A second is a second forward, pinhole
    and condition, which this census grows rows for before it is admitted.
    """
    rows = [row for row in conventions.routes.ports() if isinstance(row.internet, conventions.routes.SiteForward)]
    if len(rows) != 1:
        raise ValueError(
            f'the site forwards one raw port to the worker, and the census has {[row.name for row in rows]}'
        )
    return rows[0]


#: The bulk-transfer application's peer port, which the pinhole and the
#: forward open on its port and transports.
PEER = _site_forwarded()

#: How a firewall row names a raw port's transports.
PROTOCOL: Mapping[conventions.Transports, Literal['tcp', 'udp', 'tcp_udp']] = {
    conventions.Transports.TCP: 'tcp',
    conventions.Transports.UDP: 'udp',
    conventions.Transports.TCP_AND_UDP: 'tcp_udp',
}


@dataclass(frozen=True)
class Policy:
    """One zone policy, on the pair whose row holds it.

    An allow also admits its return leg: an allow whose return leg is not
    allowed is not an allow, and the reverse zone pair is where the return leg
    is classified. Toward `External` and toward `Gateway` that leg is a policy
    the controller keeps on its own; on every other pair it is the one the
    allow asks for (`return_flag`). A drop admits nothing there, and an allow
    of `RESPOND_ONLY` is a return leg already.

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
    protocol: Literal['all', 'tcp', 'udp', 'tcp_udp']
    #: Narrows the pair's source zone: a group, or one literal address.
    #: `None` is the whole zone.
    source: AddressGroup | IPv4Address | IPv6Address | None = None
    #: Narrows the pair's destination zone: a group, one literal address, or
    #: an address the program is handed. `None` is the whole zone.
    destination: AddressGroup | IPv4Address | IPv6Address | Worker | None = None
    port: int | None = None
    #: Which packets of a connection the policy matches, by the controller's
    #: connection tracking: `ALL` is every packet, whatever its state, and
    #: `RESPOND_ONLY` the related and established ones.
    connection_state: Literal['ALL', 'RESPOND_ONLY'] = 'ALL'

    def __post_init__(self) -> None:
        # A group or a literal holds one family, so a policy naming one is a
        # policy of that family alone: of the other, or of both, it would
        # match nothing on that side while reading as if it narrowed it.
        for side in (self.source, self.destination):
            if isinstance(side, AddressGroup | IPv4Address | IPv6Address):
                version = side.subnet.version if isinstance(side, AddressGroup) else side.version
                if self.ip_version != f'IPV{version}':
                    raise ValueError(f'the policy {self.key!r} is {self.ip_version} and names an IPv{version} side')


@dataclass(frozen=True)
class Pair:
    """A zone pair's policies, in the order the pair evaluates them, all ahead of its predefined ones.

    The pair's `ZonePolicyOrder` is this tuple, so a policy cannot be declared
    without a place, and the place is its position here. The pair holds no
    other custom policy: one that has to answer on it is a row of this tuple,
    whatever traffic it is about. `order` is that resource's name after the
    component's, part of its URN as a policy's key is.
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


#: The site networks whose UPnP opt-in is held in the console rather than
#: declared: the pinned provider's update of a network writes the fields it
#: does not model at zero, so the census takes none of these network objects
#: over, and `gateway-health` reads their opt-in instead. Part of the layer
#: physical/gateway.md §4.2 rule 8 lists, and replaced by the networks the
#: census adopts once that layer comes out.
CONSOLE_HELD: frozenset[conventions.SiteNetwork] = frozenset({conventions.SERVER_LAN})


#: The site networks a reservation can be keyed on, by the id of the
#: controller's network object for each (`conventions.gateway`), which the
#: component looks up by that id, so a site that holds no such network fails
#: the preview by name. A reservation on another network is refused until its
#: network's id is recorded.
CONTROLLER_NETWORKS: Mapping[conventions.SiteNetwork, str] = {
    conventions.SERVER_LAN: conventions.gateway.UNIFI_NETWORK_SERVER_LAN_ID,
}


@dataclass(frozen=True)
class RequestedMapping:
    """A port a host of this repository asks the gateway to map for itself, the same number outside as on the host."""

    key: str
    protocol: Literal['tcp', 'udp', 'tcp_udp']
    port: int
    host: conventions.SiteHost


@dataclass(frozen=True)
class PortMapping:
    """The gateway's UPnP and NAT-PMP service, for the devices of `networks` alone.

    It renders the site's gateway settings, adopted, of which it owns the UPnP
    block: on, in secure mode, NAT-PMP beside UPnP, toward `UPNP_WAN`, while
    `networks` names any network, and declared absent, which turns the
    service off, while it names none. The settings have no delete, so a
    dropped row writes nothing and leaves the service as it was; the row
    emptied, and applied, is what turns it off.

    Which networks the service answers is each network's own opt-in, and
    `networks` names only networks whose opt-in something holds: the
    network the census declares, the cluster VLAN, whose opt-in it declares
    on exactly when `networks` names it, and the networks of
    `CONSOLE_HELD`, whose opt-in is set in the console and read by
    `gateway-health` against `networks` (physical/gateway.md §4.2 rule 8).
    A row naming any other network is refused here, since nothing would
    render or read its opt-in.

    `requested` is the mappings this repository's own hosts ask for. A
    mapping is run-time state of the device's port-mapping daemon rather
    than a controller object, so nothing declares one; they are named here
    because they hold ports at the WAN address beside the census's forwards.
    `key` is the settings resource's name after the component's, part of its
    URN.
    """

    key: str
    networks: tuple[conventions.SiteNetwork, ...]
    requested: tuple[RequestedMapping, ...] = ()

    def __post_init__(self) -> None:
        held = {conventions.CLUSTER_VLAN, *CONSOLE_HELD}
        for network in self.networks:
            if network not in held:
                raise ValueError(
                    f'the port mapping {self.key!r} opts in the {network.name} network, '
                    'whose opt-in the census neither declares nor holds in the console'
                )
        for mapping in self.requested:
            if mapping.host.network not in self.networks:
                raise ValueError(
                    f'the mapping {mapping.key!r} is asked for on the {mapping.host.network.name} network, '
                    f'which {self.key!r} does not opt in'
                )


@dataclass(frozen=True)
class Reservation:
    """A fixed-IP reservation: the DHCP server of the host's network hands the host its address and no other.

    Keyed on the host's MAC, on the controller's network object for the
    host's network (`CONTROLLER_NETWORKS`); a reservation on a network whose
    object's id is not recorded is refused here. The controller already
    knows a client that has taken a lease, so the reservation takes the
    client over rather than creating it. A destroy calls nothing on the controller: the client keeps
    its name, note and fixed IP. So removing the row, or a MAC change that
    replaces it, leaves the reservation in force until it is cleared in the
    console, which is the step that goes with the row's removal. `key` is the
    resource's name after the component's, part of its URN.
    """

    key: str
    #: What the console shows the client as.
    name: str
    #: The console's note on the client, which is the row's reason.
    note: str
    host: conventions.SiteHost

    def __post_init__(self) -> None:
        if self.host.network not in CONTROLLER_NETWORKS:
            raise ValueError(
                f'the reservation {self.key!r} is on the {self.host.network.name} network, '
                "whose controller object's id this program does not record"
            )


#: Everything this program declares on the controller's firewall, row by row,
#: each with its reason. The pairs come first, then the forward, then the
#: gateway's port-mapping service with the reservation that holds its one
#: requested mapping's address; within a pair, the order is the rule.
CENSUS: tuple[Pair | Forward | PortMapping | Reservation, ...] = (
    # IoT → the `lan` pool: one enumerated allow for the VLAN, then a drop. The
    # allow is the media Gateway's VIP on 443, which is what the IoT VLAN's clients
    # consume, the televisions and streamers among them; the drop is the rest
    # of the pool, where the cluster's administrative interfaces live. The
    # firewall names only the media VIP, so deciding which applications the
    # IoT VLAN may reach is a Gateway-layer edit and never a firewall edit:
    # the rows of the exposure census on `media`. On the uplink pair, because
    # that is where pool-bound traffic is classified (module docstring).
    #
    # Home Assistant, on that VLAN, reaches the whole pool on every protocol:
    # its integrations are how the home automates what the cluster's
    # applications do, so its reach is a requirement and not a cost of a row,
    # and every VIP a row lands on is reachable from it on the day it lands.
    # Its source is its own address in each family, a literal, so the rest of
    # the VLAN still meets the drop.
    #
    # Every allow precedes both drops, and all of them precede the pair's
    # predefined policies: otherwise the predefined accept on the uplink pair
    # would answer first and the drop would never be reached. Family by
    # family, because each source is a group or an address of one family.
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
                key='home-assistant-pool-v4',
                title='Home Assistant to lan pool (v4)',
                description='Home Assistant may reach every VIP of the cluster pool; its integrations need them.',
                action='ALLOW',
                ip_version='IPV4',
                protocol='all',
                source=conventions.HOME_ASSISTANT.v4,
                destination=POOL_V4,
            ),
            Policy(
                key='home-assistant-pool-v6',
                title='Home Assistant to lan pool (v6)',
                description='Home Assistant may reach every VIP of the cluster pool; its integrations need them.',
                action='ALLOW',
                ip_version='IPV6',
                protocol='all',
                source=conventions.HOME_ASSISTANT.v6,
                destination=POOL_V6,
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
    # told about is blocked to and from `Internal` and to new connections
    # from `External`, but its way to `External` is a predefined `Allow All
    # Traffic`. So this policy opens nothing the default has not: it declares
    # the egress the cluster relies on — a node whose control plane is in a
    # cloud region has to be able to leave the site — rather than leaving it
    # to a default the program does not own. Both families in one policy,
    # because the zone is dual-stack and nothing here distinguishes them.
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
    # ahead of the allow. The trusted home VLANs reach the nodes directly —
    # debugging straight at a node, a ping, a host path that hairpins. The IoT
    # VLAN does not: the node subnet is where apid, the kubelet and the BGP
    # session live, and every recorded IoT-originated dependency — a
    # television reaching the media VIP, Home Assistant reaching the pool —
    # targets the pool instead, so the drops sever nothing known. An allow for the whole internal zone ahead of
    # them would answer for the IoT VLAN too, and the carve-out would match
    # nothing. The same shape as the first row one zone over: the zone stays
    # open, the untrusted subpopulation is carved out.
    #
    # What does rank ahead of the drops is the return-only allow. The drops
    # carry no state match and the controller keeps `cluster-internal
    # (Return)` where the pair's predefined policies start, behind them, so
    # without this row they take the reply to every connection a node opens
    # toward the IoT VLAN — and that is the direction the recorded
    # dependencies run. It admits related and established traffic alone, so
    # the IoT VLAN still opens nothing, and whatever is neither meets the
    # drops, `INVALID` packets included. A `CUSTOM` state list on the drops
    # is no way to say the same thing: at the pinned provider the list reads
    # back null, and `physical` plans an update on every run
    # (filipowm/terraform-provider-unifi#205).
    Pair(
        order='internal-cluster-order',
        source=Zone.INTERNAL,
        destination=Zone.CLUSTER,
        policies=(
            Policy(
                key='cluster-replies',
                title='replies to cluster nodes',
                description='Replies to connections cluster nodes opened, ahead of the IoT drops.',
                action='ALLOW',
                ip_version='BOTH',
                protocol='all',
                connection_state='RESPOND_ONLY',
            ),
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
    # rotates — so the address is the one this run derives from the prefix it
    # reads, and the run that reads a new prefix re-declares the row. Until
    # that run is applied the rule is stale, which degrades to outbound-only
    # IPv6, the accepted first stage, rather than to anything unsafe.
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
                protocol=PROTOCOL[PEER.transports],
                destination=Worker.GUA,
                port=PEER.port,
            ),
        ),
    ),
    # ZeroTier's own UDP to the gateway's overlay member, from anyone on the
    # internet, at the device's own WAN addresses in both families. The
    # member runs in the device's network namespace and holds those
    # addresses itself, and the WAN-local chain drops every new packet, so
    # without this row a peer reaches it directly only by hole punching from
    # both ends and is relayed until then (physical/gateway.md §2.6, §4.1).
    # The port is the one the daemon pins in its `local.conf`
    # (`conventions.overlay.UDM_PORT`), and it binds no other.
    #
    # UDP alone, never `tcp_udp`: TCP on the same number is the daemon's
    # control API, which stays behind the drop. No address on either side,
    # since the device's WAN addresses are the zone and a ZeroTier peer may
    # be anywhere. ZeroTier authenticates every packet against its sender's
    # identity before it acts on it (threat-model.md §2.9).
    Pair(
        order='overlay-member-order',
        source=Zone.EXTERNAL,
        destination=Zone.GATEWAY,
        policies=(
            Policy(
                key='overlay-member',
                title='inbound overlay member',
                description="ZeroTier's UDP to the gateway's overlay member, on the one port its daemon binds.",
                action='ALLOW',
                ip_version='BOTH',
                protocol='udp',
                port=conventions.overlay.UDM_PORT,
            ),
        ),
    ),
    # The IPv4 half of the bulk-transfer flow, and the only port forward the
    # census declares. No management inbound is declared: cluster and node
    # management arrive over the cloud load balancer, home-side management
    # over ZeroTier. Both halves take the port and its transports from the
    # census row they open (`PEER`), because two rules that disagree on it
    # admit nothing.
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
    # on the host holds this one by the UPnP lease it asks for (the
    # port-mapping row below). The gateway's port-mapping daemon does not see
    # the controller's forwards, so a forward declared then stands beside
    # the lease, and which of the two answers the port is the order of their
    # chains, with nothing reporting it. Moving the port is a step of the
    # application's own migration (cluster/migration.md, Wave D).
    Forward(
        key='peer-v4',
        title='inbound peer port (v4)',
        protocol=PROTOCOL[PEER.transports],
        port=PEER.port,
        to=conventions.HOMELAB_NODE_IPV4,
        needs=Needs.PEER_ON_WORKER,
    ),
    # UPnP and NAT-PMP, for the server LAN alone. The homelab host's overlay
    # member sits behind the gateway's masquerade, so without a mapping a peer
    # reaches it directly only by hole punching from both ends, relayed until
    # then (physical/gateway.md §2.6). Its daemon asks the gateway for a
    # mapping of its third port, the same number outside
    # (`conventions.overlay.HOMELAB_PORT`), NAT-PMP first and UPnP second, and
    # pushes the mapped address to its peers, so a runner's first packet
    # reaches it. The mapping renews itself, and the address translation is
    # what the port-mapping daemon renders for it.
    #
    # Every device on the server LAN may map ports for itself: that network is
    # trusted at the network layer, and what such a device opens is its own
    # (threat-model.md §2.9, §2.10). Secure mode holds each UPnP request to
    # the requester's own address, as NAT-PMP always is. The IoT VLAN, the
    # container VLAN and the cluster VLAN stay out: the cluster network is the
    # census's and declares its opt-in off, and the other two are the
    # console's objects, opted in nowhere.
    PortMapping(
        key='port-mapping',
        networks=(conventions.SERVER_LAN,),
        requested=(
            RequestedMapping(
                key='homelab-overlay-member',
                protocol='udp',
                port=conventions.overlay.HOMELAB_PORT,
                host=HOMELAB_HOST,
            ),
        ),
    ),
    # The homelab member's mapping's address, held. The host's address on the
    # server LAN is otherwise a lease, which the DHCP server may move. A
    # secure-mode mapping goes to whatever address the host holds, and after a
    # move the old mapping holds the port until it lapses, so the host's
    # daemon would take the next port up: an endpoint no reader names.
    #
    # The console shows the client by the name the device plane answers for
    # the host, so the reservation renames nothing a resolver could answer by.
    Reservation(
        key='homelab-host',
        name=HOMELAB_HOST_NAME.partition('.')[0],
        note="kluster: the homelab overlay member's port mapping lands on this address.",
        host=HOMELAB_HOST,
    ),
)


class SiteFirewall(Component, pulumi_type='kluster:gateway:SiteFirewall'):
    """The cluster's network and zone, and the rows of `CENSUS` that apply.

    A row with `needs` is declared only once what it names is met — today the
    peer's IPv4 forward, on `peer_on_worker` — and every other row
    unconditionally. `worker_gua` is the address the pinhole names, which
    derives from the delegated prefix as the run reads it. A row's
    address groups are declared because a policy present names them, once
    each, and each pair's policies are followed by the one `ZonePolicyOrder`
    that ranks them.

    `policies`, `orders`, `groups`, `forwards` and `reservations` hold what
    was declared, by the key the row gives it, and `settings` the
    site's gateway settings.

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
        worker_gua: pulumi.Input[str],
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
        # fight over it. It holds the association from either end as well, so
        # a refresh reads the network back into the zone's `networks`, which
        # the provider plans as an empty list wherever it is left unset and
        # sends whole on every write of the zone. `ignore_changes` is what
        # keeps the single owner single: the zone's diff leaves the list
        # alone, and an update the zone does take sends the list its state
        # holds, which is the membership once a refresh has read it.
        self.zone = unifi.FirewallZone(
            f'{name}-zone',
            name=conventions.gateway.UNIFI_ZONE_CLUSTER,
            site=site,
            opts=self.child_opts(ignore_changes=['networks']),
        )
        zones: dict[Zone, pulumi.Input[str]] = {
            Zone.INTERNAL: unifi.get_firewall_zone_output(name=ZONE_INTERNAL, site=site, opts=invoke).id,
            Zone.EXTERNAL: unifi.get_firewall_zone_output(name=ZONE_EXTERNAL, site=site, opts=invoke).id,
            Zone.GATEWAY: unifi.get_firewall_zone_output(name=ZONE_GATEWAY, site=site, opts=invoke).id,
            Zone.CLUSTER: self.zone.id,
        }

        # The cluster VLAN itself. Static-only: `dhcp_enabled` is off because
        # every node states its own address in machine configuration, and a
        # server offering leases beside that is a second opinion nothing
        # needs. IPv6 is a delegated prefix with router advertisements on,
        # because the design's worker GUA is a SLAAC address formed from what
        # this network advertises. Which /64 of the prefix it advertises is
        # the firmware's to assign, and the run reads it off the device
        # (`Gateway.worker_gua`). The multicast reflector is the site's
        # setting, on for every network, and the controller derives this field
        # from it, so the declaration states what it holds: `False` here would
        # be ignored and planned again on every run.
        #
        # No prefix id, `ipv6_pd_prefixid`, is declared: UniFi OS 6.0.17 does
        # not carry it to the device. The network configuration the device
        # applies, `/data/udapi-config/udapi-net-cfg.json`, holds no prefix-id
        # field for any bridge, and the gateway allocates each bridge's /64
        # itself (physical/gateway.md §1).
        # The site has one gateway settings object, so one row renders it.
        mapping = port_mapping()
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
            ipv6_pd_start=DELEGATED_RANGE[0],
            ipv6_pd_stop=DELEGATED_RANGE[1],
            ipv6_ra_enable=True,
            multicast_dns=True,
            upnp_lan_enabled=conventions.CLUSTER_VLAN in mapping.networks,
            firewall_zone_id=self.zone.id,
            site=site,
            opts=child,
        )

        # The site's gateway settings, adopted, owning the UPnP block. The
        # settings resource takes an import id with the site in front of it.
        self.settings = adopt(
            unifi.SettingUsg,
            unifi.SettingUsgArgs,
            f'{name}-{mapping.key}',
            import_id=f'{site}:{conventions.gateway.UNIFI_SETTING_USG_ID}',
            owned={
                'upnp': unifi.SettingUsgUpnpArgs(nat_pmp_enabled=True, secure_mode=True, wan_interface=UPNP_WAN)
                if mapping.networks
                else None,
                'site': site,
            },
            refused={
                'multicast_dns_enabled': (
                    'the provider refuses it on a controller above 7.0, and the site runs UniFi Network 10'
                ),
            },
            opts=child,
        )

        # The addresses a row may name that the program is handed rather than
        # holds.
        handed_in: dict[Worker, pulumi.Input[str]] = {Worker.GUA: worker_gua}
        met = {Needs.PEER_ON_WORKER: peer_on_worker}
        rows = [row for row in CENSUS if not isinstance(row, Pair | Forward) or row.needs is None or met[row.needs]]
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
                        # The name is stated, and the controller refuses a
                        # second firewall group under it, so a replacement
                        # created before the delete would fail.
                        opts=self.child_opts(delete_before_replace=True),
                    )

        def source(policy: Policy, zone: pulumi.Input[str]) -> unifi.FirewallZonePolicySourceArgs:
            match policy.source:
                case AddressGroup() as group:
                    return unifi.FirewallZonePolicySourceArgs(zone_id=zone, ip_group_id=self.groups[group.key].id)
                case IPv4Address() | IPv6Address() as address:
                    return unifi.FirewallZonePolicySourceArgs(zone_id=zone, ips=[str(address)])
                case None:
                    return unifi.FirewallZonePolicySourceArgs(zone_id=zone)

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
        self.orders: dict[str, ZonePolicyOrder] = {}
        for pair in pairs:
            declared = [
                unifi.FirewallZonePolicy(
                    f'{name}-{policy.key}',
                    name=f'{conventions.CLUSTER_NAME} {policy.title}',
                    description=policy.description,
                    action=policy.action,
                    ip_version=policy.ip_version,
                    protocol=policy.protocol,
                    source=source(policy, zones[pair.source]),
                    destination=destination(policy, zones[pair.destination]),
                    connection_state_type=policy.connection_state,
                    auto_allow_return_traffic=return_flag(policy.action, pair.destination, policy.connection_state),
                    enabled=True,
                    opts=child,
                )
                for policy in pair.policies
            ]
            self.policies.update(
                (policy.key, resource) for policy, resource in zip(pair.policies, declared, strict=True)
            )
            self.orders[pair.order] = policy_order(
                f'{name}-{pair.order}',
                api_url=api_url,
                site=site,
                source_zone_id=zones[pair.source],
                destination_zone_id=zones[pair.destination],
                policies=declared,
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

        # Each network a reservation is keyed on, looked up once by the id of
        # its controller object. `allow_existing` takes over the client the
        # controller already knows by the MAC, which it does once the host has
        # taken a lease; `skip_forget_on_destroy` makes a destroy call
        # nothing, since the provider otherwise has the controller forget the
        # client, its history with it, and so the reservation outlives its
        # row.
        reservations = [row for row in rows if isinstance(row, Reservation)]
        networks = {
            network: unifi.get_network_output(id=CONTROLLER_NETWORKS[network], site=site, opts=invoke).id
            for network in dict.fromkeys(row.host.network for row in reservations)
        }
        self.reservations: dict[str, unifi.User] = {
            row.key: unifi.User(
                f'{name}-{row.key}',
                mac=row.host.mac,
                name=row.name,
                note=row.note,
                fixed_ip=str(row.host.v4),
                network_id=networks[row.host.network],
                allow_existing=True,
                skip_forget_on_destroy=True,
                site=site,
                opts=child,
            )
            for row in reservations
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


def port_mapping(census: Sequence[object] = CENSUS) -> PortMapping:
    """The census's one `PortMapping` row: the site has one gateway settings object, which one row renders."""
    rows = [row for row in census if isinstance(row, PortMapping)]
    if len(rows) != 1:
        raise ValueError(f'the site has one gateway settings object, and the census has {len(rows)} rows for it')
    return rows[0]
