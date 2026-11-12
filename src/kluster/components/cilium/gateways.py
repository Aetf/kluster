"""The three Gateways: their namespace, their addresses, their listeners and the redirect off plain HTTP.

Every Gateway answers the same listeners (rfc-007 §5.2): one HTTPS listener per
served zone, on that zone's wildcard with that zone's certificate, and one HTTP
listener whose only route is a redirect to HTTPS. The ports are
`conventions.GATEWAY_PORTS`, the same structure the node firewall opens for
them (physical.md §2) and the balancer forwards, so a port moved there moves
all three.

Where a Gateway answers is decided by where its address is routed, since
Cilium hands its traffic to the Envoy on the node it arrives at (rfc-007
§5.1). So each Gateway asks for its addresses in its own spec, which Cilium's
operator turns into the address annotation of the Service it generates, and
its infrastructure labels and annotations are copied onto that Service, which
is how the pool label and the sharing annotations reach it:

-   `internet-gw`, the `internet` pool's node members and the balancer's two
    addresses, the latter answering on the caller's own node a connection
    that starts in the cluster (architecture.md §3.2). It shares those
    addresses with the raw Services of the census's `Balancer` ports
    (`conventions.routes.Port`), which live in their applications' namespaces.
-   `lan-gw`, the `lan` pool's default VIP; `media-gw`, its media VIP. The
    worker alone announces either.

The certificates are cert-manager's to issue (`components.certificates`), into
the Secrets this module names, in the Gateways' own namespace, so no listener
refers across a namespace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.k8s import lb_pool_labels, namespace
from putils import Component, async_output, resolve

__all__ = (
    'SHARING_CROSS_NAMESPACE',
    'SHARING_KEY',
    'Gateways',
    'InternetGatewayAddresses',
    'certificate_secret',
    'https_listener',
)

#: The annotations LB IPAM reads a shared address off (`lb-ipam.rst` at Cilium
#: v1.20.1, "Sharing Keys"). Every Service on the `internet` pool's node
#: addresses shares them, so the key is the pool's own name; and those Services
#: are in their applications' namespaces, which `internet-gw` cannot list
#: ahead of time, so it shares with any namespace and the other side names
#: the one it shares with.
SHARING_KEY = 'lbipam.cilium.io/sharing-key'
SHARING_CROSS_NAMESPACE = 'lbipam.cilium.io/sharing-cross-namespace'

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

    `zones` are the served zones (`conventions.routes.served_zones`): one HTTPS
    listener per zone, and `certificates` names the Secret each zone's
    certificate is to be issued into. `after` is what the Gateways are
    declared behind -- the Gateway API's definitions and the class they name
    -- stated on the children, since Pulumi does not push a component's own
    `depends_on` down to them.
    """

    def __init__(
        self,
        name: str,
        *,
        gateway_class: str,
        zones: Sequence[str],
        internet: InternetGatewayAddresses,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        ports = conventions.GATEWAY_PORTS
        #: Each served zone's certificate Secret, by zone.
        self.certificates: Mapping[str, str] = {zone: certificate_secret(zone) for zone in zones}

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
                annotations={SHARING_KEY: conventions.POOL_INTERNET, SHARING_CROSS_NAMESPACE: '*'},
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
        annotations: Mapping[str, str] | None = None,
        opts: pulumi.ResourceOptions,
    ) -> crds.gateway.v1.Gateway:
        """One Gateway, on its addresses and its pool, answering every served zone and redirecting plain HTTP."""
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
                    labels=lb_pool_labels(pool),
                    annotations=dict(annotations) if annotations is not None else None,
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
