"""The three Gateways: their namespace, their addresses, their listeners and the redirect off plain HTTP.

Every Gateway answers the same base listeners (rfc-007 §5.2): one HTTPS listener per
served zone, on that zone's wildcard with that zone's certificate, and one HTTP
listener whose only route is a redirect to HTTPS. The ports are
`conventions.GATEWAY_PORTS`, the same structure the node firewall opens for
them (physical.md §2) and the balancer forwards, so a port moved there moves
all three.

Two more kinds of listener are derived from the exposure census
(`conventions.routes`), each only where a row asks for it (rfc-008 §4.1):

-   **An apex listener** on `internet-gw` for each zone an `Apex` names: HTTPS
    on the zone's own name, with the zone's certificate, which covers the
    apex beside the wildcard.
-   **A bare listener** on `lan-gw` for each `Bare` row: plain HTTP on the
    row's label, where the route the row emits redirects the bare name to
    the full one (`kluster.lib.k8s.route`). The HTTP listener above admits
    only the Gateways' own namespace, so a route an application declares
    attaches here instead.

Cilium refuses two listeners of one protocol on one port only where their
hostnames are identical, which neither kind ever is. **A `Passthrough` row is
refused by name** (`kluster.lib.k8s.refuse_passthrough`): Cilium holds a TLS
passthrough listener and an HTTPS listener on one port to conflict whenever
their hostnames can match one name, and drops both, so a passthrough for a
name under a served zone would take the zone's wildcard listener, and every
name of the zone, off the Gateway (`listenerPairConflict` in
`operator/pkg/gateway-api/gateway_reconcile.go`, v1.20.1). How such a row is
reached instead is Aetf/kluster-ops#723.

Where a Gateway answers is decided by where its address is routed, since
Cilium hands its traffic to the Envoy on the node it arrives at (rfc-007
§5.1). So each Gateway asks for its addresses in its own spec, which Cilium's
operator turns into the address annotation of the Service it generates, and
its infrastructure labels and annotations are copied onto that Service, which
is how the pool label and the sharing annotations reach it:

-   `internet-gw`, the `internet` pool's node members and the balancer's two
    addresses, the latter answering on the caller's own node a connection
    that starts in the cluster (architecture.md §3.2).
-   `lan-gw`, the `lan` pool's default VIP; `media-gw`, its media VIP. The
    worker alone announces either.

Each Gateway shares its addresses with the raw Services of the census's
ports on its front (`conventions.routes.Port`): `internet-gw` with the
`Balancer` ports', `lan-gw` and `media-gw` with the home ports' at their VIP
(rfc-008 §4.1). Those Services live in their applications' namespaces, and
more than one application may share a front, so every side of a share --
each Gateway and each raw Service -- carries its front's sharing key
(`sharing_key`) and admits every namespace.

The certificates are cert-manager's to issue (`components.certificates`), into
the Secrets this module names, in the Gateways' own namespace, so no listener
refers across a namespace.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.conventions.routes import Apex, Bare, Exposure, Held, Passthrough, Route, served_zones
from kluster.lib.k8s import bare_listener, lb_pool_labels, namespace, refuse_passthrough
from putils import Component, async_output, resolve

__all__ = (
    'SHARING_CROSS_NAMESPACE',
    'SHARING_KEY',
    'Gateways',
    'InternetGatewayAddresses',
    'apex_listener',
    'apex_zones',
    'bare_labels',
    'certificate_secret',
    'https_listener',
    'passthroughs',
    'sharing_annotations',
    'sharing_key',
)

#: The annotations LB IPAM reads a shared address off (`lb-ipam.rst` at Cilium
#: v1.20.1, "Sharing Keys"). Services share an address only where both carry
#: one key, and across namespaces only where each admits the other's: every
#: side admits every namespace, since the raw Services that share a front are
#: in their applications' namespaces, which no side can list ahead of time.
SHARING_KEY = 'lbipam.cilium.io/sharing-key'
SHARING_CROSS_NAMESPACE = 'lbipam.cilium.io/sharing-cross-namespace'


def sharing_key(gateway: str) -> str:
    """The key the Services at a Gateway's addresses share them under.

    `internet-gw`'s is its pool's name, since every Service on the pool's node
    addresses shares them. `lan-gw` and `media-gw` hold one VIP each out of one
    pool, so each shares under its own name, and a raw port at one VIP is
    never handed the other.
    """
    if gateway == conventions.GATEWAY_INTERNET:
        return conventions.POOL_INTERNET
    if gateway in (conventions.GATEWAY_LAN, conventions.GATEWAY_MEDIA):
        return gateway
    raise ValueError(f'no such Gateway: {gateway}')


def sharing_annotations(gateway: str) -> dict[str, str]:
    """What a Service at a Gateway's addresses carries to share them: the Gateway's key, and every namespace admitted."""
    return {SHARING_KEY: sharing_key(gateway), SHARING_CROSS_NAMESPACE: '*'}


def _rows(exposures: Iterable[Exposure]) -> Iterator[Route | Passthrough | Apex]:
    """Every route row of the census, held or not: a held row's route attaches all the same."""
    for exposure in exposures:
        for row in exposure.routes:
            yield row.row if isinstance(row, Held) else row


def apex_zones(exposures: Iterable[Exposure]) -> tuple[str, ...]:
    """Each zone an `Apex` names, once, in census order."""
    zones: list[str] = []
    for row in _rows(exposures):
        if isinstance(row, Apex):
            zones.extend(zone for zone in row.zones if zone not in zones)
    return tuple(zones)


def passthroughs(exposures: Iterable[Exposure]) -> tuple[Passthrough, ...]:
    """Every `Passthrough` row, in census order, which the Gateways refuse."""
    return tuple(row for row in _rows(exposures) if isinstance(row, Passthrough))


def bare_labels(exposures: Iterable[Exposure]) -> tuple[str, ...]:
    """The label of every `Route` whose `lan` side is `Bare`, in census order."""
    return tuple(row.host for row in _rows(exposures) if isinstance(row, Route) and isinstance(row.lan, Bare))


def apex_listener(zone: str) -> str:
    """The name of `internet-gw`'s HTTPS listener on a zone's apex."""
    return f'apex-{zone}'


#: The listener every Gateway's redirect attaches to.
HTTP_LISTENER = 'http'


def certificate_secret(zone: str) -> str:
    """The Secret the zone's certificate is issued into, which every Gateway's listener for the zone reads."""
    return f'{zone}-tls'


def https_listener(zone: str) -> str:
    """The name of a Gateway's HTTPS listener for a zone."""
    return f'https-{zone}'


@dataclass(frozen=True, kw_only=True)
class InternetGatewayAddresses:
    """What `internet-gw` asks for: the `internet` pool's node members and the balancer's two addresses.

    The dedicated VIP is a member of the pool too, but it is hath's alone, and
    no listener of the balancer forwards to it.
    """

    node_private_ips: pulumi.Input[Mapping[str, str]]
    node_guas: pulumi.Input[Mapping[str, str]]
    balancer_v4: pulumi.Input[str]
    balancer_v6: pulumi.Input[str]


class Gateways(Component, pulumi_type='kluster:cilium:Gateways'):
    """The Gateways' namespace, `internet-gw`, `lan-gw` and `media-gw`, and the redirect off their HTTP listener.

    `exposures` is the exposure census, which the listeners derive from: one
    HTTPS listener per served zone (`conventions.routes.served_zones`), and
    `certificates` names the Secret each zone's certificate is to be issued
    into; and the apex and bare listeners its rows ask for. A census holding a
    `Passthrough` row is refused, naming it. `after` is what the Gateways are
    declared behind -- the Gateway API's definitions and the class they name
    -- stated on the children, since Pulumi does not push a component's own
    `depends_on` down to them.
    """

    def __init__(
        self,
        name: str,
        *,
        gateway_class: str,
        exposures: Sequence[Exposure],
        internet: InternetGatewayAddresses,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        ports = conventions.GATEWAY_PORTS
        #: Each served zone's certificate Secret, by zone.
        self.certificates: Mapping[str, str] = {zone: certificate_secret(zone) for zone in served_zones(exposures)}
        for row in passthroughs(exposures):
            refuse_passthrough(row.host)
        self._apex_zones = apex_zones(exposures)
        self._bare_labels = bare_labels(exposures)

        self.namespace = namespace(
            f'{name}-namespace', name=conventions.GATEWAY_NAMESPACE, opts=self.child_opts(depends_on=list(after))
        )
        behind = self.child_opts(depends_on=[*after, self.namespace])

        lan = conventions.LAN_POOL
        self.gateways = {
            conventions.GATEWAY_INTERNET: self._gateway(
                name,
                conventions.GATEWAY_INTERNET,
                gateway_class=gateway_class,
                ports=ports,
                addresses=async_output(lambda: _internet_addresses(internet)),
                pool=conventions.POOL_INTERNET,
                opts=behind,
            ),
            conventions.GATEWAY_LAN: self._gateway(
                name,
                conventions.GATEWAY_LAN,
                gateway_class=gateway_class,
                ports=ports,
                addresses=_ip_addresses([str(lan.default_vip.v4), str(lan.default_vip.v6)]),
                pool=lan.name,
                opts=behind,
            ),
            conventions.GATEWAY_MEDIA: self._gateway(
                name,
                conventions.GATEWAY_MEDIA,
                gateway_class=gateway_class,
                ports=ports,
                addresses=_ip_addresses([str(lan.media_vip.v4), str(lan.media_vip.v6)]),
                pool=lan.name,
                opts=behind,
            ),
        }

        # Plain HTTP is answered with a redirect and nothing else: the HTTP
        # listener admits routes from the Gateways' own namespace alone, and
        # this is the one route there.
        self.redirect = crds.gateway.v1.HTTPRoute(
            f'{name}-https-redirect',
            metadata=k8s.meta.v1.ObjectMetaArgs(name='https-redirect', namespace=conventions.GATEWAY_NAMESPACE),
            spec=crds.gateway.v1.HTTPRouteSpecArgs(
                parent_refs=[
                    crds.gateway.v1.HTTPRouteSpecParentRefsArgs(name=gateway, section_name=HTTP_LISTENER)
                    for gateway in self.gateways
                ],
                rules=[
                    crds.gateway.v1.HTTPRouteSpecRulesArgs(
                        filters=[
                            crds.gateway.v1.HTTPRouteSpecRulesFiltersArgs(
                                type='RequestRedirect',
                                request_redirect=crds.gateway.v1.HTTPRouteSpecRulesFiltersRequestRedirectArgs(
                                    scheme='https', status_code=301
                                ),
                            )
                        ]
                    )
                ],
            ),
            opts=self.child_opts(depends_on=[*after, self.namespace, *self.gateways.values()]),
        )

        self.register_outputs({})

    def _gateway(
        self,
        name: str,
        gateway: str,
        *,
        gateway_class: str,
        ports: conventions.GatewayPorts,
        addresses: pulumi.Input[Sequence[crds.gateway.v1.GatewaySpecAddressesArgs]],
        pool: str,
        opts: pulumi.ResourceOptions,
    ) -> crds.gateway.v1.Gateway:
        """One Gateway, on its addresses and its pool, answering every served zone and redirecting plain HTTP.

        Beside those, the listeners the census's rows ask of this Gateway:
        an apex listener per `Apex` zone on `internet-gw`, and a bare
        listener per `Bare` row on `lan-gw`.
        """
        # A route attaches to the HTTPS listeners from a namespace that
        # carries the route label, and from no other: one a chart installs
        # attaches to nothing.
        from_labeled = crds.gateway.v1.GatewaySpecListenersAllowedRoutesArgs(
            namespaces=crds.gateway.v1.GatewaySpecListenersAllowedRoutesNamespacesArgs(
                from_='Selector',
                selector=crds.gateway.v1.GatewaySpecListenersAllowedRoutesNamespacesSelectorArgs(
                    match_labels=dict(conventions.routes.ROUTE_LABEL)
                ),
            )
        )
        listeners = [
            crds.gateway.v1.GatewaySpecListenersArgs(
                name=https_listener(zone),
                hostname=f'*.{zone}',
                port=ports.https,
                protocol='HTTPS',
                tls=crds.gateway.v1.GatewaySpecListenersTlsArgs(
                    mode='Terminate',
                    certificate_refs=[crds.gateway.v1.GatewaySpecListenersTlsCertificateRefsArgs(name=secret)],
                ),
                allowed_routes=from_labeled,
            )
            for zone, secret in self.certificates.items()
        ]
        if gateway == conventions.GATEWAY_INTERNET:
            listeners.extend(
                crds.gateway.v1.GatewaySpecListenersArgs(
                    name=apex_listener(zone),
                    hostname=zone,
                    port=ports.https,
                    protocol='HTTPS',
                    tls=crds.gateway.v1.GatewaySpecListenersTlsArgs(
                        mode='Terminate',
                        certificate_refs=[
                            crds.gateway.v1.GatewaySpecListenersTlsCertificateRefsArgs(name=self.certificates[zone])
                        ],
                    ),
                    allowed_routes=from_labeled,
                )
                for zone in self._apex_zones
            )
        if gateway == conventions.GATEWAY_LAN:
            listeners.extend(
                crds.gateway.v1.GatewaySpecListenersArgs(
                    name=bare_listener(label),
                    hostname=label,
                    port=ports.http,
                    protocol='HTTP',
                    allowed_routes=from_labeled,
                )
                for label in self._bare_labels
            )
        listeners.append(
            crds.gateway.v1.GatewaySpecListenersArgs(
                name=HTTP_LISTENER,
                port=ports.http,
                protocol='HTTP',
                allowed_routes=crds.gateway.v1.GatewaySpecListenersAllowedRoutesArgs(
                    namespaces=crds.gateway.v1.GatewaySpecListenersAllowedRoutesNamespacesArgs(from_='Same')
                ),
            )
        )
        return crds.gateway.v1.Gateway(
            f'{name}-{gateway}',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=gateway, namespace=conventions.GATEWAY_NAMESPACE),
            spec=crds.gateway.v1.GatewaySpecArgs(
                gateway_class_name=gateway_class,
                addresses=addresses,
                infrastructure=crds.gateway.v1.GatewaySpecInfrastructureArgs(
                    labels=lb_pool_labels(pool), annotations=sharing_annotations(gateway)
                ),
                listeners=listeners,
            ),
            opts=opts,
        )


def _ip_addresses(addresses: Sequence[str]) -> list[crds.gateway.v1.GatewaySpecAddressesArgs]:
    return [crds.gateway.v1.GatewaySpecAddressesArgs(type='IPAddress', value=address) for address in addresses]


async def _internet_addresses(internet: InternetGatewayAddresses) -> list[crds.gateway.v1.GatewaySpecAddressesArgs]:
    """The node members, in node order, IPv4 then IPv6, then the balancer's IPv4 and IPv6."""
    node_private_ips, node_guas, balancer_v4, balancer_v6 = await resolve(
        internet.node_private_ips, internet.node_guas, internet.balancer_v4, internet.balancer_v6
    )
    return _ip_addresses(
        [
            *(node_private_ips[node] for node in sorted(node_private_ips)),
            *(node_guas[node] for node in sorted(node_guas)),
            balancer_v4,
            balancer_v6,
        ]
    )
