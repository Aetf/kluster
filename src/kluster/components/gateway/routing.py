"""The routing session the site learns the `lan` pool over.

The device routes for the site, and one of the routes it must know is not the
site's: the pool the cluster hands out to its own load balancers, learned from
the homelab worker over BGP (cluster-infra.md §2), both families over one IPv4
session, the IPv6 routes through the worker's link-local (`frr_config`).

**The session has one manager, and it is the controller** (physical/gateway.md
§1.3). The firmware carries the routing suite and runs it as the controller's
own children while the site holds a BGP configuration, and runs none while it
holds none; it ships no unit of the suite's own. So what this program declares
is that configuration, one object on the controller (`unifi.site_bgp`),
carrying the text `frr_config` renders. It puts nothing under the suite's
directory on the device, starts nothing there and delivers no unit for it. A
configuration edited on the console's BGP page is drift, which the next preview
reports.

**The content is a secret**, because the session's authentication password is
in it. The controller stores it in the site's configuration, so it is in the
controller's autobackups too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Annotated, final

import pulumi

from kluster import conventions
from kluster.components.gateway.persistence import TEMPLATE_PACKAGE
from kluster.components.gateway.unifi import site_bgp
from kluster.lib import templates
from kluster.lib.secret import Secret
from kluster.providers.unifi_bgp import BgpConfiguration
from putils import Component

__all__ = (
    'CONFIG_DESCRIPTION',
    'CONFIG_FILE_NAME',
    'MAX_PREFIXES',
    'RoutingSession',
    'SiteRouting',
    'frr_config',
)

#: What the console shows beside the configuration: who declares it, and that
#: an edit made there is drift.
CONFIG_DESCRIPTION = f'Managed by the {conventions.CLUSTER_NAME} physical stack. Console edits are overwritten.'

#: The name the console shows the configuration as uploaded under.
CONFIG_FILE_NAME = f'{conventions.CLUSTER_NAME}-frr.conf'

#: The routing session's inbound cap. The prefix-list already confines what the
#: peer may announce to the pool; this bounds how many /32s out of it arrive, so
#: a peer that floods the table is dropped rather than believed.
MAX_PREFIXES = 64


@final
@dataclass(frozen=True)
class RoutingSession:
    """The BGP session the gateway learns the `lan` pool over.

    `neighbor` is the worker VM's address, which the routing configuration
    names. That address is a constant rather than another
    resource's output on purpose — the session must not depend on a lease.
    `neighbor_link_local` is the same machine's link-local address on the same
    link, the next hop of every IPv6 route the session brings in: a constant
    for the same reason, and refused here unless it is link-local (`frr_config`
    says why it has to be). `password` authenticates the session, so that
    claiming the peer's address is not enough to become the peer. It is a
    `Secret`, and does not print: the stack hands it over as an `Output`, which
    discloses nothing, but a test hands over the literal, and the field is
    hidden for what it holds rather than for how one of its types prints.
    """

    neighbor: IPv4Address
    neighbor_link_local: IPv6Address
    password: Annotated[pulumi.Input[str], Secret] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.neighbor_link_local.is_link_local:
            raise ValueError(f'{self.neighbor_link_local} is not a link-local address')


@final
@dataclass(frozen=True)
class _FrrParams:
    """What `frr.conf.j2` reads.

    The pools arrive whole rather than as text, so the file states the prefix
    length each family admits down to instead of being handed it.

    The password is the resolved session secret, and does not print.
    """

    cluster: str
    peer: str
    peer_description: str
    password: Annotated[str, Secret] = field(repr=False, compare=False)
    local_asn: int
    peer_asn: int
    pool_v4: IPv4Network
    pool_v6: IPv6Network
    v4_list: str
    v6_list: str
    v6_next_hop_map: str
    v6_next_hop: IPv6Address
    max_prefixes: int


def frr_config(
    *,
    neighbor: IPv4Address,
    neighbor_link_local: IPv6Address,
    password: str,
) -> str:
    """The routing daemon's configuration, rendered from the peer's addresses.

    The peer is the homelab worker, whose address is a constant rather than a
    lease precisely so that this file can name it. The session is defended
    three ways, and each of them is load-bearing:

    -   **an authentication password**, so that claiming the peer's address is
        not enough to become the peer;
    -   **an inbound prefix-list** admitting the `lan` pool and nothing else —
        without it a compromised worker could announce the resolvers' own
        addresses and take over the LAN's name service;
    -   **a prefix cap**, which bounds the damage of an announcement flood that
        the prefix-list would otherwise happily accept one /32 at a time.

    `no bgp ebgp-requires-policy` is deliberate: the peering is external, and
    the daemon's default would otherwise refuse to install anything until an
    outbound policy exists, which for a session that only ever *receives* would
    be ceremony with a failure mode.

    **The IPv6 family sets its next hop on the way in.** Both families ride the
    one IPv4 session, so the worker sends its IPv6 routes with its IPv4 address
    as an IPv4-mapped next hop, and the kernel refuses a route through that.
    An inbound route-map on the IPv6 family alone sets the worker's link-local
    as the routes' link-local next hop (`set ipv6 next-hop local`), which the
    daemon installs on the interface the session runs over and the kernel
    accepts with that interface alone. Validity still follows the session's
    IPv4 address, so a wrong link-local installs routes that go nowhere with
    nothing reporting it — which is why the address is derived from the MAC the
    worker is declared with rather than typed (`conventions.HOMELAB_NODE_MAC`).
    The route-map, the line that applies it and `neighbor_link_local` are one
    unit: a peer that sent a usable next hop of its own would make all three
    go, and nothing else would change shape.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/frr.conf.j2',
        _FrrParams(
            cluster=conventions.CLUSTER_NAME,
            peer=str(neighbor),
            peer_description=f'{conventions.CLUSTER_NAME} {conventions.HOMELAB_NODE}',
            password=password,
            local_asn=conventions.UDM_ASN,
            peer_asn=conventions.CLUSTER_ASN,
            pool_v4=conventions.LAN_POOL.v4,
            pool_v6=conventions.LAN_POOL.v6,
            v4_list=f'{conventions.CLUSTER_NAME}-lan-pool-v4',
            v6_list=f'{conventions.CLUSTER_NAME}-lan-pool-v6',
            v6_next_hop_map=f'{conventions.CLUSTER_NAME}-lan-pool-v6-next-hop',
            v6_next_hop=neighbor_link_local,
            max_prefixes=MAX_PREFIXES,
        ),
    )


class SiteRouting(Component, pulumi_type='kluster:gateway:SiteRouting'):
    """The site's routing configuration, declared on the controller."""

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        site: pulumi.Input[str],
        session: RoutingSession,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the session's configuration as the site's BGP configuration.

        `api_url` is the controller the configuration is written through, and
        `site` the site it belongs to; the key that writes it is the
        controller's API key, read by the configuration's own provider.
        """
        super().__init__(name, opts=opts)

        # The name is held. A rename or a re-parent without an alias plans a
        # create under the new URN and a delete of the old one, and that
        # delete removes the configuration the create took as its own: the
        # change that moves it carries a `pulumi.Alias` naming the old URN.
        self.config: BgpConfiguration = site_bgp(
            f'{name}-bgp',
            api_url=api_url,
            site=site,
            config=pulumi.Output.from_input(session.password).apply(
                lambda password: frr_config(
                    neighbor=session.neighbor, neighbor_link_local=session.neighbor_link_local, password=password
                )
            ),
            description=CONFIG_DESCRIPTION,
            file_name=CONFIG_FILE_NAME,
            opts=self.child_opts(),
        )

        self.register_outputs({})
