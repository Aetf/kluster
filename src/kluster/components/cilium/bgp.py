"""The BGP session the homelab worker announces the `lan` pool to the gateway over (rfc-007 §4.6).

Cilium's BGPv2 control plane, in its three objects
(`bgp-control-plane-configuration.rst` at Cilium v1.20.1):

-   **the cluster configuration** selects the nodes that run a BGP instance and
    names the instance's peer. It selects every node that is not a control
    plane node, which is the homelab worker alone: the control plane is the
    cloud nodes (nodes.md §4.2), Talos labels each node of that type
    `node-role.kubernetes.io/control-plane`
    ([`node_label_spec.go` L83–85](https://github.com/siderolabs/talos/blob/v1.13.9/internal/app/machined/pkg/controllers/k8s/node_label_spec.go#L83-L85)),
    and the worker is the one node `physical` declares with any other role.
    The peer is the gateway's leg on the cluster VLAN, the worker's default
    route, at the ASN the gateway's routing daemon is configured with, and the
    instance is the ASN that daemon expects of the worker
    (`components.gateway.routing`), so the session is external. The instance
    listens on the port the worker's firewall opens to the gateway, which the
    agent is granted the capability to bind (`AGENT_ADDED_CAPABILITIES`), so
    the session forms whichever end opens it;
-   **the peer configuration** carries the session's password and negotiates
    both families, IPv4 and IPv6 unicast, on the one IPv4 session the
    gateway's configuration names, each family advertising what the
    advertisement below selects. The IPv6 host routes go out with the
    session's IPv4 address as their next hop, an IPv4-mapped IPv6 address
    the gateway's kernel will not route through, and Cilium has no next-hop
    setting: the gateway's inbound route-map on that family replaces it with
    the worker's link-local (`components.gateway.routing`);
-   **the advertisement** advertises the load-balancer address of every Service
    the `lan` pool label selects, which Cilium announces as a host route, /32
    or /128. The gateway's inbound prefix-list admits the pool's range and
    nothing else (`components.gateway.routing`).

The password is the sealed value `conventions.sealed.BGP_PASSWORD`, a Secret in
the namespace Cilium reads BGP secrets from, under the key Cilium reads. While
it is absent from the stack's configuration, no SealedSecret is declared for
it and the three objects are declared all the same: Cilium then opens the
session with an empty password, which the gateway refuses, so the session
stays down until the value is sealed in (`kluster.lib.k8s.sealed_value`,
physical/bring-up.md §1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.talos import BGP_PORT
from kluster.lib.k8s import lb_pool_labels, sealed_value
from putils import Component

__all__ = (
    'ADVERTISEMENT_LABELS',
    'CONTROL_PLANE_LABEL',
    'FAMILIES',
    'PEER',
    'BgpSession',
)

#: The label Talos puts on every control plane node, and on no other.
CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'

#: The name of the peer, of its configuration and of the cluster
#: configuration: there is one session, and it is with the gateway.
PEER = 'gateway'

#: The families the session carries, as Cilium spells them (`afi`, `safi`):
#: the `lan` pool hands out an address of each.
FAMILIES = (('ipv4', 'unicast'), ('ipv6', 'unicast'))

#: What the peer configuration selects its advertisements by. The
#: advertisement is the `lan` pool's, so the label names the pool.
ADVERTISEMENT_LABELS: Mapping[str, str] = {f'{conventions.LABEL_DOMAIN}/bgp-advertisement': conventions.LAN_POOL.name}


class BgpSession(Component, pulumi_type='kluster:cilium:BgpSession'):
    """The worker's BGP session with the gateway: the cluster and peer configurations, the advertisement, the password.

    `after` is what the session is installed behind: Cilium, whose operator
    registers the three objects' definitions, and the sealing controller, whose
    chart defines the SealedSecret's. It is stated on the children rather than
    left to the component's own `depends_on`, which Pulumi does not push down
    to a component's children.

    `password` is the ciphertext the stack's configuration holds for
    `conventions.sealed.BGP_PASSWORD`, `None` while it holds none; the `None`
    is the bring-up's tolerance, and goes with it (physical/bring-up.md §1).
    """

    def __init__(
        self,
        name: str,
        *,
        after: Sequence[pulumi.Resource],
        password: Mapping[str, str] | None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))
        secret = conventions.sealed.BGP_PASSWORD

        self.password = sealed_value(f'{name}-password', secret, ciphertext=password, opts=behind)

        self.advertisement = crds.cilium.v2.CiliumBGPAdvertisement(
            f'{name}-advertisement',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=conventions.LAN_POOL.name, labels=dict(ADVERTISEMENT_LABELS)),
            spec=crds.cilium.v2.CiliumBGPAdvertisementSpecArgs(
                advertisements=[
                    crds.cilium.v2.CiliumBGPAdvertisementSpecAdvertisementsArgs(
                        advertisement_type='Service',
                        service=crds.cilium.v2.CiliumBGPAdvertisementSpecAdvertisementsServiceArgs(
                            addresses=['LoadBalancerIP']
                        ),
                        selector=crds.cilium.v2.CiliumBGPAdvertisementSpecAdvertisementsSelectorArgs(
                            match_labels=lb_pool_labels(conventions.LAN_POOL.name)
                        ),
                    )
                ]
            ),
            opts=behind,
        )
        self.peer_config = crds.cilium.v2.CiliumBGPPeerConfig(
            f'{name}-peer',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=PEER),
            spec=crds.cilium.v2.CiliumBGPPeerConfigSpecArgs(
                auth_secret_ref=secret.name,
                families=[
                    crds.cilium.v2.CiliumBGPPeerConfigSpecFamiliesArgs(
                        afi=afi,
                        safi=safi,
                        advertisements=crds.cilium.v2.CiliumBGPPeerConfigSpecFamiliesAdvertisementsArgs(
                            match_labels=dict(ADVERTISEMENT_LABELS)
                        ),
                    )
                    for afi, safi in FAMILIES
                ],
            ),
            opts=behind,
        )
        self.cluster_config = crds.cilium.v2.CiliumBGPClusterConfig(
            f'{name}-cluster',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=PEER),
            spec=crds.cilium.v2.CiliumBGPClusterConfigSpecArgs(
                node_selector=crds.cilium.v2.CiliumBGPClusterConfigSpecNodeSelectorArgs(
                    match_expressions=[
                        crds.cilium.v2.CiliumBGPClusterConfigSpecNodeSelectorMatchExpressionsArgs(
                            key=CONTROL_PLANE_LABEL, operator='DoesNotExist'
                        )
                    ]
                ),
                bgp_instances=[
                    crds.cilium.v2.CiliumBGPClusterConfigSpecBgpInstancesArgs(
                        name=conventions.CLUSTER_NAME,
                        local_asn=conventions.CLUSTER_ASN,
                        local_port=BGP_PORT,
                        peers=[
                            crds.cilium.v2.CiliumBGPClusterConfigSpecBgpInstancesPeersArgs(
                                name=PEER,
                                peer_asn=conventions.UDM_ASN,
                                peer_address=str(conventions.CLUSTER_VLAN.require_gateway()),
                                peer_config_ref=crds.cilium.v2.CiliumBGPClusterConfigSpecBgpInstancesPeersPeerConfigRefArgs(
                                    name=PEER
                                ),
                            )
                        ],
                    )
                ],
            ),
            # After the peer configuration it names, so no instance is created
            # naming a configuration the cluster does not hold yet.
            opts=self.child_opts(depends_on=[*after, self.peer_config]),
        )

        self.register_outputs({})
