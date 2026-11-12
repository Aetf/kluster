"""The application base: what every application's component declares its exposure through.

An application has one `Exposure` (`conventions.routes`), the row of the census
that says how it is reached, and its component makes one call for it,
`expose(exposure)`, which emits the coherent set the rows derive
(declarative/dns.md §5, rfc-008 §4.1):

-   for each `Route`, its `HTTPRoute`s (`kluster.lib.k8s.route`): one per
    Gateway the row sets, the gate on `internet`'s alone, and the redirect of
    a bare name;
-   for each `Apex`, one `HTTPRoute` at `internet-gw`'s apex listeners, and
    one `Apex` row to an exposure;
-   for each `Port`, one LoadBalancer Service per Gateway it sets, at that
    front's addresses (`port`), and one allow admitting the world on the
    port and its transports alone.

A `Passthrough` row is refused, naming its host: Cilium drops a TLS
passthrough listener together with the zone's wildcard listener on the same
port, and how such a row is reached instead is Aetf/kluster-ops#723
(`kluster.lib.k8s.refuse_passthrough`). Every route without a gate removes the
portal's identity headers from the request, since nothing but the client
writes them there (`kluster.lib.k8s.Portal`).

It writes no rewrite, which is the `dns` stack's from the same rows, and no
record: the records of the rows on `internet` are not declared by this base
yet. A row the legacy VPS holds (`Held`) attaches all the same, which is how
an application is verified at the front door before its cut.

The namespace the component installs into is `kluster.lib.k8s.application_namespace`,
whose default-deny admits the Gateways' identity and the namespace's own
pods. That is right for what a Gateway's Envoy forwards and drops every
client of a raw port, which is why a `Port` brings its own allow.

`route` lives in `kluster.lib.k8s` rather than here, because a second stack
calls it; everything else here is `apps`' alone (rfc-007 §9.1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.gateways import apex_listener, sharing_annotations
from kluster.conventions.cluster import POOL_WORKER
from kluster.conventions.routes import (
    Apex,
    Balancer,
    Dedicated,
    Exposure,
    Held,
    Passthrough,
    Port,
    Route,
    SiteForward,
)
from kluster.lib.k8s import Backend, identity_stripped, lb_pool_labels, refuse_passthrough, route, row_gateways
from putils import async_output, resolve

__all__ = (
    'LB_IPAM_IPS',
    'WORLD_ENTITY',
    'Backend',
    'Exposed',
    'Fronts',
    'PortResources',
    'expose',
    'port',
)

#: The annotation a Service asks LB IPAM for its addresses by, a
#: comma-separated list (`lb-ipam.rst` at Cilium v1.20.1, "Requesting IPs").
LB_IPAM_IPS = 'lbipam.cilium.io/ips'

#: The identity Cilium gives a client outside the cluster, which a raw port's
#: allow admits: on the balancer's front the tunnel carries it to the pod's
#: node (rfc-008 §4.1), and a home client at a VIP is one.
WORLD_ENTITY = 'world'

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


@dataclass(frozen=True)
class Exposed:
    """What `expose` emitted, by kind."""

    http_routes: tuple[crds.gateway.v1.HTTPRoute, ...]
    ports: tuple[PortResources, ...]


def expose(
    resource_name: str,
    exposure: Exposure,
    *,
    namespace: pulumi.Input[str],
    backend: Backend | None,
    selector: Mapping[str, str] | None,
    fronts: Fronts,
    opts: pulumi.ResourceOptions | None = None,
) -> Exposed:
    """Everything an application's `Exposure` derives in its namespace, from the rows and nothing else.

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param exposure: The application's row of `conventions.routes.EXPOSURES`.
    :param namespace: The application's namespace, which carries the route
        label (`kluster.lib.k8s.application_namespace`).
    :param backend: The Service every route row forwards to: the
        application's, or a device's relay where the exposure names one.
        Required where the exposure has a route row.
    :param selector: The labels of the pods that serve the raw ports, at
        least one where the exposure has a port (`port`).
    :param fronts: The addresses of the `internet` fronts, as the program
        read them.
    """
    http_routes: list[crds.gateway.v1.HTTPRoute] = []
    if exposure.routes:
        if backend is None:
            raise ValueError(f'{resource_name}: an exposure with route rows needs the Service they forward to')
        apexes = [row for row in exposure.routes if isinstance(row, Apex)]
        if len(apexes) > 1:
            raise ValueError(f'{resource_name}: an exposure has one apex row, which names each of its zones')
        for held in exposure.routes:
            row = held.row if isinstance(held, Held) else held
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
    return Exposed(http_routes=tuple(http_routes), ports=ports)
