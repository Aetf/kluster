"""The routing session the gateway learns the `lan` pool over, as the daemon reads it.

One rendered file naming the worker VM as a BGP neighbour, with the inbound
prefix-list and the prefix cap that keep a compromised (or impersonated) peer
from advertising the LAN out from under itself (cluster-infra.md §2). It
carries the session's authentication password, so its content is a secret input
and the file is declared secret.

**The file answers to the routing daemon rather than to the boot chain**, so it
applies itself: its post-apply hook is the install-and-reload the daemon needs,
not a converger of this program's. That is also why it is the one piece of the
device's desired state with no machine behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway.container import TEMPLATE_PACKAGE
from kluster.lib import templates

__all__ = (
    'FRR_APPLY',
    'FRR_CONFIG',
    'FRR_LIVE_CONFIG',
    'FRR_MODE',
    'MAX_PREFIXES',
    'RoutingSession',
    'frr_config',
)

#: The routing configuration, as desired state and as the daemon reads it. The
#: daemon's own path is not under `/data`, so a firmware update takes it away
#: and the copy under `/data` is what puts it back.
FRR_CONFIG = f'{conventions.gateway.DATA_ROOT}/frr/frr.conf'
FRR_LIVE_CONFIG = '/etc/frr/frr.conf'
#: The session password is in it, so it is not world-readable.
FRR_MODE = '0640'
FRR_APPLY = f'install -m {FRR_MODE} {FRR_CONFIG} {FRR_LIVE_CONFIG} && systemctl reload frr'

#: The routing session's inbound cap. The prefix-list already confines what the
#: peer may announce to the pool; this bounds how many /32s out of it arrive, so
#: a peer that floods the table is dropped rather than believed.
MAX_PREFIXES = 64


@final
@dataclass(frozen=True)
class RoutingSession:
    """The BGP session the gateway learns the `lan` pool over.

    `neighbour` is the worker VM's address, which the routing daemon's
    configuration names. That address is a constant rather than another
    resource's output on purpose — the session must not depend on a lease.
    `password` authenticates the session, so that claiming the peer's address is
    not enough to become the peer.
    """

    neighbour: IPv4Address
    password: pulumi.Input[str]


@final
@dataclass(frozen=True)
class _FrrParams:
    """What `frr.conf.j2` reads.

    The pools arrive whole rather than as text, so the file states the prefix
    length each family admits down to instead of being handed it.
    """

    cluster: str
    peer: str
    peer_description: str
    password: str
    local_asn: int
    peer_asn: int
    pool_v4: IPv4Network
    pool_v6: IPv6Network
    v4_list: str
    v6_list: str
    max_prefixes: int


def frr_config(
    *,
    neighbour: IPv4Address,
    password: str,
    local_asn: int = conventions.UDM_ASN,
    peer_asn: int = conventions.CLUSTER_ASN,
    pool_v4: IPv4Network = conventions.LAN_POOL.v4,
    pool_v6: IPv6Network = conventions.LAN_POOL.v6,
) -> str:
    """The routing daemon's configuration, rendered from the peer's address.

    The peer is the homelab worker, whose address is a constant rather than a
    lease precisely so that this file can name it. Three things are declared
    about the session and each of them is load-bearing:

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
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/frr.conf.j2',
        _FrrParams(
            cluster=conventions.CLUSTER_NAME,
            peer=str(neighbour),
            peer_description=f'{conventions.CLUSTER_NAME} {conventions.HOMELAB_NODE}',
            password=password,
            local_asn=local_asn,
            peer_asn=peer_asn,
            pool_v4=pool_v4,
            pool_v6=pool_v6,
            v4_list=f'{conventions.CLUSTER_NAME}-lan-pool-v4',
            v6_list=f'{conventions.CLUSTER_NAME}-lan-pool-v6',
            max_prefixes=MAX_PREFIXES,
        ),
    )
