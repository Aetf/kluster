"""The application base: what every application's component declares its exposure through.

An application has one `Exposure` (`conventions.routes`), the row of the census
that says how it is reached, and its component makes one call for it,
`expose(exposure)`, which emits the coherent set the rows derive
(declarative/dns.md §5, rfc-008 §4.1):

-   for each `Route`, its `HTTPRoute`s (`kluster.lib.k8s.route`): one per
    Gateway the row sets, the gate on `internet`'s alone, and the redirect of
    a bare name;
-   for each `PortRedirect`, one `HTTPRoute` per Gateway the row sets, which
    answers every request on its name with a redirect to its raw port and
    forwards to nothing (`kluster.lib.k8s.redirect_routes`);
-   for each `Apex`, one `HTTPRoute` at `internet-gw`'s apex listeners, and
    one `Apex` row to an exposure;
-   for each `Port`, one LoadBalancer Service per Gateway it sets, at that
    front's addresses (`port`), and one allow admitting the world on the
    port and its transports alone; the names a client dials it at are
    `dialed_names`, which a component that names the port to a client, or
    certifies it, reads;
-   for each name the exposure publishes (`conventions.routes.public_names`),
    one CNAME to the cluster anchor in its zone, proxied exactly where its
    row is (`public_records`).

**Where an application's pods may run is the scheduler's**, within the
constraints a fact about nodes or the component's own declaration forces
(declarative/workloads.md §1, `kluster.lib.k8s.Constraints`). A component
derives them here, from what it declares and never by naming a site
(`declared_constraints`), and every pod it renders states them, its
database's and its claims' movers' included. The home site is derived from:

-   **the requirement that it keeps working while the uplink is down**
    (`UPLINK_DOWN`), which a component declares of itself (cluster/
    architecture.md §1.1): with the uplink down the worker reaches no cloud
    node, so its pods run at home;
-   **the device its exposure's relay forwards to**
    (`conventions.routes.Device`), and **a reach of its namespace to a home
    address** (`kluster.lib.k8s.Reach` to an address on one of
    `conventions.SITE_NETWORKS`): KubeSpan carries the members' addresses
    alone, so no cloud node has a route to either;
-   **a raw port that answers on the worker's own address** (a `Port` whose
    `internet` side is `SiteForward`): the site's forward and pinhole end at
    that address, so a pod elsewhere receives none of the port's traffic;
-   **bulk egress** (`BulkEgress`): every path out of a cloud node leaves
    through that node's metered egress, the tunnel to the worker included
    (architecture.md §3.5).

The function takes the exposure the component hands `expose` and the
namespace it installs into, whose reaches the namespace records, so the
derivation reads the declarations themselves rather than a second copy of
them.

An exposure's home side derives nothing: while the uplink is up, a `lan`
VIP's connection reaches a backend on any node. Beside these, a pod that
requests the GPU is kept to the node offering it (`GPU_HOLD`), and a claim
keeps its pod to the claim's node (`components.apps.storage`); neither states
an affinity, since the scheduler reads both from the pod, and both hold the
pod at the held priority.

A `Passthrough` row is refused, naming its host: Cilium drops a TLS
passthrough listener together with the zone's wildcard listener on the same
port, so a pod that terminates its own TLS is reached at a raw port instead,
and a client that names it with no port at a `PortRedirect` to that port, as
the discovery server is (Aetf/kluster-ops#723,
`kluster.lib.k8s.refuse_passthrough`). Every route that forwards a request
without a gate removes the portal's identity headers from it, since nothing
but the client writes them there (`kluster.lib.k8s.Portal`).

**A client of the portal reaches the issuer at its home address.** The
pod that serves an OpenID Connect client calls the issuer by its public name
to finish a sign-in, and carries a host alias of that name at the address the
`dns` stack's rewrite answers it with at home (`issuer_aliases`,
`conventions.sso.issuer`), so the call goes to the `lan` Gateway rather than
out to the public front.

It writes no rewrite, which is the `dns` stack's from the same rows. A row
the legacy VPS holds (`Held`) attaches all the same, which is how an
application is verified at the front door before its cut, and publishes no
record, since the legacy record holds its name until the cut.

The namespace the component installs into is `kluster.lib.k8s.application_namespace`,
whose default-deny admits the Gateways' identity and the namespace's own
pods. That is right for what a Gateway's Envoy forwards and drops every
client of a raw port, which is why a `Port` brings its own allow.

`route` lives in `kluster.lib.k8s` rather than here, because a second stack
calls it (rfc-007 §9.1). The records are not `route`'s: what a row publishes
turns on its `Held` wrapper, which `route` never sees, and a raw port's
`Host` publishes one with no route at all, so they are `public_names` over
the exposure, which is the derivation the `dns` stack's records are held
against too. `public_records` is that one function, and a component of
another stack that publishes a row calls it beside `route`, with a
Cloudflare provider and the `dns` stack's zone identifiers of its own
(rfc-008 §5.5). Everything else here is `apps`' alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Literal

import pulumi
import pulumi_cloudflare as cloudflare
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.gateways import apex_listener, sharing_annotations
from kluster.components.dns.record import cname
from kluster.components.gpu import GPU_RESOURCE
from kluster.conventions.cluster import POOL_WORKER
from kluster.conventions.routes import (
    Apex,
    Balancer,
    Dedicated,
    Exposure,
    Held,
    Passthrough,
    Port,
    PortRedirect,
    PublicName,
    Route,
    SiteForward,
    Vip,
    public_names,
    unwrapped,
)
from kluster.lib.k8s import (
    UNCONSTRAINED,
    ApplicationNamespace,
    Backend,
    Constraints,
    Keeper,
    at_home,
    identity_stripped,
    kept,
    lb_pool_labels,
    redirect_routes,
    refuse_passthrough,
    route,
    row_gateways,
)
from putils import async_output, resolve

__all__ = (
    'CLUSTER_ANCHOR',
    'GPU_HOLD',
    'LB_IPAM_IPS',
    'UPLINK_DOWN',
    'WORLD_ENTITY',
    'Backend',
    'BulkEgress',
    'Exposed',
    'Fronts',
    'PortResources',
    'declared_constraints',
    'dialed_names',
    'expose',
    'issuer_aliases',
    'port',
    'public_records',
    'published_once',
)

#: The annotation a Service asks LB IPAM for its addresses by, a
#: comma-separated list (`lb-ipam.rst` at Cilium v1.20.1, "Requesting IPs").
LB_IPAM_IPS = 'lbipam.cilium.io/ips'

#: The identity Cilium gives a client outside the cluster, which a raw port's
#: allow admits: on the balancer's front the tunnel carries it to the pod's
#: node (rfc-008 §4.1), and a home client at a VIP is one.
WORLD_ENTITY = 'world'

#: The requirement a component declares of itself when it answers a client at
#: home at its home Gateway or VIP without the home uplink, while the worker
#: stays up (cluster/architecture.md §1.1). The declaration covers every pod
#: the component renders, and every component it calls while it answers
#: declares it in turn.
UPLINK_DOWN = 'keeps working while the uplink is down, which its component declares (cluster/architecture.md §1.1)'

#: What a pod that requests the GPU states: kept to the one node that offers
#: it, which the scheduler reads from the request, so no affinity, and the
#: held priority.
GPU_HOLD = kept(Keeper.DEVICE, f'requests the GPU ({GPU_RESOURCE}), which one node offers')


def issuer_aliases(issuer: conventions.sso.Issuer) -> list[k8s.core.v1.HostAliasArgs] | None:
    """The host aliases of a pod that signs in at the portal: the issuer's name at each of its addresses, or none where it has none.

    A client's server calls the issuer by its public name, which no resolver
    the pod asks answers with an address inside the cluster: the cluster's DNS
    passes a public name on to the public resolvers, Talos' defaults on the
    worker, which a pod at home reaches only through the uplink, as it does
    the proxy's edge their answer names, since the portal's row is proxied.
    The alias answers the name
    with the issuer's addresses in the pod's own `/etc/hosts`
    (`conventions.sso.issuer`). Every pod that serves a client of the portal
    carries it, through this function.
    """
    if not issuer.addresses:
        return None
    return [k8s.core.v1.HostAliasArgs(ip=str(address), hostnames=[issuer.host]) for address in issuer.addresses]


@dataclass(frozen=True)
class BulkEgress:
    """That a component's pods send bulk traffic out of the cluster, and what: they run at home, whose uplink is not metered."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError('bulk egress says what leaves: it takes a reason')


def _at_home_address(address: IPv4Address | IPv6Address) -> bool:
    """Whether `address` is on one of the networks the home gateway serves, which no cloud node has a route to."""
    return any(address in (network.v4 if address.version == 4 else network.v6) for network in conventions.SITE_NETWORKS)


def declared_constraints(
    exposure: Exposure | None,
    namespace: ApplicationNamespace,
    *,
    uplink_down: bool = False,
    bulk_egress: BulkEgress | None = None,
) -> Constraints:
    """The constraints a component's declarations derive, which every pod it renders states: the home site, or none.

    :param exposure: The exposure the component hands `expose`, whose device
        its relay forwards to, at a home address, and whose raw ports answer
        on the worker's own address where their `internet` side is
        `SiteForward`; `None` for a component that exposes nothing. Its rows'
        home sides derive nothing.
    :param namespace: The namespace the component installs into
        (`kluster.lib.k8s.application_namespace`), whose reaches it
        declared: one to an address on a network the home gateway serves
        holds it at home, and one elsewhere states nothing.
    :param uplink_down: That the component keeps working while the uplink is
        down (`UPLINK_DOWN`).
    :param bulk_egress: That its pods send bulk traffic out.
    """
    constraints = UNCONSTRAINED
    if uplink_down:
        constraints &= at_home(UPLINK_DOWN)
    device = exposure.device if exposure is not None else None
    if device is not None:
        constraints &= at_home(f'relays to the device at {device.address}, a home address no cloud node reaches')
    for port in exposure.ports if exposure is not None else ():
        if isinstance(port.internet, SiteForward):
            constraints &= at_home(
                f"answers {port.name!r} on the worker's own address, where the site's forward and pinhole end"
            )
    for reach in namespace.reaches:
        if _at_home_address(reach.address):
            constraints &= at_home(f'reaches {reach.address}, a home address no cloud node reaches: {reach.reason}')
    if bulk_egress is not None:
        constraints &= at_home(
            f'sends bulk traffic out, which leaves a cloud node through its metered egress: {bulk_egress.reason}'
        )
    return constraints


type _Families = tuple[Literal['IPv4'], Literal['IPv6']] | tuple[Literal['IPv4']]
_DUAL: _Families = ('IPv4', 'IPv6')
_IPV4: _Families = ('IPv4',)


@dataclass(frozen=True, kw_only=True)
class Fronts:
    """The addresses of a raw port's `internet` fronts, which are machine facts `physical` publishes.

    The `apps` program reads them across a StackReference, each refused unless
    it is an address, and hands them to every component it wires; the home
    fronts' VIPs are conventions and are not among them.
    """

    internet: InternetPoolMembers
    """The `internet` pool's members: the node addresses and the balancer's
    two for a `Balancer` front, the dedicated VIP's for a `Dedicated` one."""
    worker_gua: pulumi.Input[str]
    """The worker's GUA, beside its IPv4, for a `SiteForward` front."""


@dataclass(frozen=True, kw_only=True)
class _Front:
    """Where one Gateway's Service of a raw port sits: its pool, its addresses and families, and what it shares."""

    pool: str
    addresses: pulumi.Input[Sequence[str]]
    families: _Families
    sharing: Mapping[str, str] | None


def _front(row: Port, gateway: str, fronts: Fronts) -> _Front:
    """The front a row sets on `gateway`, from its field's value."""
    lan = conventions.LAN_POOL
    if gateway == conventions.GATEWAY_INTERNET:
        match row.internet:
            case Balancer():
                return _Front(
                    pool=conventions.POOL_INTERNET,
                    addresses=async_output(lambda: _balancer_addresses(fronts.internet)),
                    families=_DUAL,
                    sharing=sharing_annotations(gateway),
                )
            case Dedicated():
                return _Front(
                    pool=conventions.POOL_INTERNET,
                    addresses=pulumi.Output.from_input(fronts.internet.dedicated_vip).apply(lambda vip: [vip]),
                    families=_IPV4,
                    sharing=None,
                )
            case SiteForward():
                return _Front(
                    pool=POOL_WORKER,
                    addresses=pulumi.Output.from_input(fronts.worker_gua).apply(
                        lambda gua: [str(conventions.HOMELAB_NODE_IPV4), gua]
                    ),
                    families=_DUAL,
                    sharing=None,
                )
            case None:
                raise ValueError(f'the port {row.name!r} sets no `internet` side')
    # A heard side is served at its Gateway's VIP as a `Vip` is: the relay on
    # the host sends what it hears there.
    vip = lan.default_vip if gateway == conventions.GATEWAY_LAN else lan.media_vip
    return _Front(
        pool=lan.name,
        addresses=[str(vip.v4), str(vip.v6)],
        families=_DUAL,
        sharing=sharing_annotations(gateway),
    )


async def _balancer_addresses(members: InternetPoolMembers) -> list[str]:
    """The node members, in node order, IPv4 then IPv6, then the balancer's IPv4 and IPv6.

    The balancer's two answer a connection that starts in the cluster on the
    caller's own node (architecture.md §3.2).
    """
    node_private_ips, node_guas, balancer_v4, balancer_v6 = await resolve(
        members.node_private_ips, members.node_guas, members.balancer_v4, members.balancer_v6
    )
    return [
        *(node_private_ips[node] for node in sorted(node_private_ips)),
        *(node_guas[node] for node in sorted(node_guas)),
        balancer_v4,
        balancer_v6,
    ]


def dialed_names(row: Port) -> tuple[str, ...]:
    """Each name a raw port is dialed at, once, under the primary zone: its balancer's hosts, held or not, then its home side's.

    A balancer front that publishes no host is dialed at its home side's name,
    which another row publishes, so that name is the front's too. The first
    name is the one a client is told to dial where it is told one.
    """
    labels: list[str] = []
    if isinstance(row.internet, Balancer):
        labels += [host.row.label if isinstance(host, Held) else host.label for host in row.internet.hosts]
    labels += [side.host for side in (row.lan, row.media) if isinstance(side, Vip)]
    return tuple(f'{label}.{conventions.ZONE_PRIMARY}' for label in dict.fromkeys(labels))


#: The field of a row each Gateway is, which a Service at it is named after.
_SIDE = {
    conventions.GATEWAY_INTERNET: 'internet',
    conventions.GATEWAY_LAN: 'lan',
    conventions.GATEWAY_MEDIA: 'media',
}


@dataclass(frozen=True)
class PortResources:
    """What one raw port emits: a Service per Gateway it sets, by Gateway, and its allow."""

    services: Mapping[str, k8s.core.v1.Service]
    allow: crds.cilium.v2.CiliumNetworkPolicy


def port(
    resource_name: str,
    row: Port,
    *,
    namespace: pulumi.Input[str],
    selector: Mapping[str, str],
    fronts: Fronts,
    opts: pulumi.ResourceOptions | None = None,
) -> PortResources:
    """A raw port: one LoadBalancer Service for each Gateway the row sets, and one allow for the world.

    Each Service sits at its front's addresses, asked for by name, in its
    front's pool:

    -   a `Balancer` front at the `internet` pool's node addresses and the
        balancer's two, both families required, under `internet-gw`'s
        sharing key; the balancer's listener for it is `physical`'s;
    -   a `Dedicated` front at the dedicated VIP's private address alone,
        which is IPv4;
    -   a `SiteForward` front at the worker's own two addresses, in the
        worker's pool, where the site's forward and pinhole end;
    -   a home side, `Vip` or `Heard`, at its Gateway's VIP, under that
        Gateway's sharing key, so no address is added: ports tell the
        Services apart (rfc-008 §4.1).

    Every one takes `Cluster` and allocates no node port: LB IPAM shares an
    address only between Services of one policy, which the Gateways' is, and
    a node port is answered at a cloud node's public address ahead of its
    firewall (cluster-infra.md §2). The pod listens on the row's port, which
    the Service forwards to unchanged.

    The allow admits the world on the row's port and transports alone, to the
    pods `selector` picks. The namespace's default-deny admits only what a
    Gateway forwards, so without it no client of a raw port gets through.

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param selector: The labels of the pods that serve the port, at least
        one: an empty one selects every pod in the namespace, and is refused.
    :param fronts: The addresses of the `internet` fronts, as the program
        read them.
    """
    if not selector:
        raise ValueError(
            f'{resource_name}: the raw port {row.name!r} needs the pods that serve it, named by a label at least: '
            'an empty selector would open every pod in the namespace to the world'
        )
    services: dict[str, k8s.core.v1.Service] = {}
    for gateway in row_gateways(row):
        front = _front(row, gateway, fronts)
        side = _SIDE[gateway]
        annotations: pulumi.Input[Mapping[str, pulumi.Input[str]]] = {
            LB_IPAM_IPS: pulumi.Output.from_input(front.addresses).apply(','.join),
            **(front.sharing or {}),
        }
        services[gateway] = k8s.core.v1.Service(
            f'{resource_name}-{side}',
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=f'{row.name}-{side}',
                namespace=namespace,
                labels=lb_pool_labels(front.pool),
                annotations=annotations,
            ),
            spec=k8s.core.v1.ServiceSpecArgs(
                type='LoadBalancer',
                external_traffic_policy='Cluster',
                allocate_load_balancer_node_ports=False,
                ip_family_policy='RequireDualStack' if len(front.families) == 2 else 'SingleStack',
                ip_families=list(front.families),
                selector=dict(selector),
                ports=[
                    k8s.core.v1.ServicePortArgs(
                        name=protocol, port=row.port, target_port=row.port, protocol=protocol.upper()
                    )
                    for protocol in row.transports.protocols
                ],
            ),
            opts=opts,
        )
    allow = crds.cilium.v2.CiliumNetworkPolicy(
        f'{resource_name}-world',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=f'{row.name}-world', namespace=namespace),
        spec=crds.cilium.v2.CiliumNetworkPolicySpecArgs(
            description=f'Admit any client to the raw port {row.name!r} on its port and transports alone.',
            endpoint_selector=crds.cilium.v2.CiliumNetworkPolicySpecEndpointSelectorArgs(match_labels=dict(selector)),
            ingress=[
                crds.cilium.v2.CiliumNetworkPolicySpecIngressArgs(
                    from_entities=[WORLD_ENTITY],
                    to_ports=[
                        crds.cilium.v2.CiliumNetworkPolicySpecIngressToPortsArgs(
                            ports=[
                                crds.cilium.v2.CiliumNetworkPolicySpecIngressToPortsPortsArgs(
                                    port=str(row.port), protocol=protocol.upper()
                                )
                                for protocol in row.transports.protocols
                            ]
                        )
                    ],
                )
            ],
        ),
        opts=opts,
    )
    return PortResources(services=services, allow=allow)


def _apex(
    resource_name: str,
    row: Apex,
    *,
    namespace: pulumi.Input[str],
    backend: Backend,
    opts: pulumi.ResourceOptions | None,
) -> crds.gateway.v1.HTTPRoute:
    """The `HTTPRoute` of a website's apex, at `internet-gw`'s apex listener of each of its zones."""
    return crds.gateway.v1.HTTPRoute(
        f'{resource_name}-apex',
        metadata=k8s.meta.v1.ObjectMetaArgs(name='apex', namespace=namespace),
        spec=crds.gateway.v1.HTTPRouteSpecArgs(
            parent_refs=[
                crds.gateway.v1.HTTPRouteSpecParentRefsArgs(
                    name=conventions.GATEWAY_INTERNET,
                    namespace=conventions.GATEWAY_NAMESPACE,
                    section_name=apex_listener(zone),
                )
                for zone in row.zones
            ],
            hostnames=list(row.zones),
            rules=[
                crds.gateway.v1.HTTPRouteSpecRulesArgs(
                    filters=[identity_stripped()],
                    backend_refs=[
                        crds.gateway.v1.HTTPRouteSpecRulesBackendRefsArgs(name=backend.service, port=backend.port)
                    ],
                )
            ],
        ),
        opts=opts,
    )


#: What every public record is a CNAME to: the cluster anchor, which the
#: `dns` stack declares in the primary zone alone, whatever zone the name is
#: in (declarative/dns.md §2).
CLUSTER_ANCHOR = f'{conventions.ANCHOR_CLUSTER}.{conventions.ZONE_PRIMARY}'

#: The comment each public record carries on Cloudflare's dashboard, which is
#: where an operator reading the zone learns which stack holds it.
_RECORD_COMMENT = "an application's name, declared beside the application by the apps stack"


def public_records(
    resource_name: str,
    exposure: Exposure,
    *,
    zone_ids: pulumi.Input[Mapping[str, str]],
    opts: pulumi.ResourceOptions | None = None,
) -> tuple[cloudflare.DnsRecord, ...]:
    """One CNAME to the cluster anchor for each name the exposure publishes, in that name's zone.

    The names are `conventions.routes.public_names` over this exposure alone:
    a row by name on `internet` publishes its host in each of its zones, a
    raw port's `Host` its label in the primary, a held row and an
    `Apex` nothing. Each record is proxied exactly where its row is, `Direct`
    and a raw port's host going past the proxy (declarative/dns.md §2), and
    its TTL is Cloudflare's automatic one: the anchor carries the short TTL a
    rebuild moves.

    :param resource_name: The logical name, which carries the declaring
        component's name; each record's is this with its name.
    :param zone_ids: Each zone's Cloudflare identifier, as the `dns` stack
        publishes them; a zone the exposure publishes in and the mapping
        lacks stops the run naming it.
    :param opts: Carries the Cloudflare provider the records are written
        through.
    """
    ids = pulumi.Output.from_input(zone_ids)
    return tuple(
        _public_record(
            resource_name, name, zone_id=ids.apply(lambda found, zone=name.zone: _zone_id(found, zone)), opts=opts
        )
        for name in public_names((exposure,))
    )


def _zone_id(found: Mapping[str, str], zone: str) -> str:
    if zone not in found:
        raise ValueError(f'the zone identifiers the program read hold none for {zone!r}, which a public name is in')
    return found[zone]


def _public_record(
    resource_name: str, name: PublicName, *, zone_id: pulumi.Input[str], opts: pulumi.ResourceOptions | None
) -> cloudflare.DnsRecord:
    record = cname(name.host, CLUSTER_ANCHOR, unproxied=name.unproxied, comment=_RECORD_COMMENT)
    fqdn = record.fqdn(name.zone)
    return cloudflare.DnsRecord(
        f'{resource_name}-record-{fqdn}',
        zone_id=zone_id,
        name=fqdn,
        type=record.type,
        content=record.content,
        ttl=record.ttl,
        proxied=record.proxied,
        comment=record.comment,
        # Cloudflare refuses a record identical to one the zone holds, so a
        # replacement created before the delete would fail.
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(delete_before_replace=True)),
    )


def published_once(exposures: Iterable[Exposure]) -> None:
    """Refuse exposures two of which publish one name, which their `expose` calls would each declare a record for.

    The census holds rows that share a host to one answer, and a raw port's
    `Host` may share its name with a row published direct in another
    application's exposure (declarative/dns.md §5). Cloudflare holds one
    record per name, and `public_records` declares an exposure's own, so a
    program wiring both would declare the name twice and fail at the apply.
    Which of the two publishes it is the land that wires the second's to
    decide.
    """
    claimed: dict[tuple[str, str], int] = {}
    for exposure in exposures:
        for name in public_names((exposure,)):
            key = (name.host, name.zone)
            claimed[key] = claimed.get(key, 0) + 1
    shared = sorted(f'{host}.{zone}' for (host, zone), count in claimed.items() if count > 1)
    if shared:
        raise ValueError(
            f'{", ".join(shared)} is published by more than one exposure the program wires; Cloudflare holds one '
            'record per name, so one exposure publishes it'
        )


@dataclass(frozen=True)
class Exposed:
    """What `expose` emitted, by kind."""

    http_routes: tuple[crds.gateway.v1.HTTPRoute, ...]
    ports: tuple[PortResources, ...]
    records: tuple[cloudflare.DnsRecord, ...]


def expose(
    resource_name: str,
    exposure: Exposure,
    *,
    namespace: pulumi.Input[str],
    backend: Backend | None,
    selector: Mapping[str, str] | None,
    fronts: Fronts,
    zone_ids: pulumi.Input[Mapping[str, str]],
    opts: pulumi.ResourceOptions | None = None,
) -> Exposed:
    """Everything an application's `Exposure` derives, in its namespace and its zones, from the rows and nothing else.

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param exposure: The application's row of `conventions.routes.EXPOSURES`.
    :param namespace: The application's namespace, which carries the route
        label (`kluster.lib.k8s.application_namespace`).
    :param backend: The Service every route row but a `PortRedirect`
        forwards to: the application's, or a device's relay where the
        exposure names one. Required where the exposure has such a row.
    :param selector: The labels of the pods that serve the raw ports, at
        least one where the exposure has a port (`port`).
    :param fronts: The addresses of the `internet` fronts, as the program
        read them.
    :param zone_ids: Each zone's Cloudflare identifier, as the program read
        them from the `dns` stack (`public_records`).
    :param opts: Carries the Kubernetes provider the cluster's objects are
        declared through and the Cloudflare provider the records are.
    """
    rows = [unwrapped(row) for row in exposure.routes]
    # A port redirect forwards to nothing, so it is the one row that needs no
    # backend (the `PortRedirect` layer).
    http_routes = [
        it
        for row in rows
        if isinstance(row, PortRedirect)
        for it in redirect_routes(f'{resource_name}-{row.host}', row, namespace=namespace, opts=opts)
    ]
    forwarded = [row for row in rows if not isinstance(row, PortRedirect)]
    if forwarded:
        if backend is None:
            raise ValueError(f'{resource_name}: an exposure with route rows needs the Service they forward to')
        if len([row for row in forwarded if isinstance(row, Apex)]) > 1:
            raise ValueError(f'{resource_name}: an exposure has one apex row, which names each of its zones')
        for row in forwarded:
            match row:
                case Route():
                    http_routes += route(
                        f'{resource_name}-{row.host}',
                        row,
                        namespace=namespace,
                        backend=backend,
                        opts=opts,
                    )
                case Passthrough():
                    refuse_passthrough(row.host)
                case Apex():
                    http_routes.append(_apex(resource_name, row, namespace=namespace, backend=backend, opts=opts))
    ports = tuple(
        port(f'{resource_name}-{row.name}', row, namespace=namespace, selector=selector or {}, fronts=fronts, opts=opts)
        for row in exposure.ports
    )
    records = public_records(resource_name, exposure, zone_ids=zone_ids, opts=opts)
    return Exposed(http_routes=tuple(http_routes), ports=ports, records=records)
