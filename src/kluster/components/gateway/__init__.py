"""The gateway: the UDM's own desired state, and the two services around it.

Everything the home site's router must be told, declared from the `physical`
stack (physical.md §4). Three channels, because the gateway is configured
through three different doors and each needs its own credential:

-   **The device itself**, over SSH. There is no API for most of what matters
    here — routing, the nspawn estate, the scripts that re-establish both
    after a firmware update — but there is a proven convention: desired-state
    files under `/data`, written idempotently. That becomes a dynamic
    provider whose `diff` reads the device and whose `create`/`update` writes
    and then runs a hook. Bulk artifacts (container root filesystems built by
    CI) travel as a URL and a digest, never as bytes in state, so a preview
    stays cheap.
-   **The UniFi controller**, over its API, for the firewall. Those resources
    live in this stack and not beside the applications whose traffic they
    admit — the one deliberate exception to co-location, because a gateway
    credential must not be handed to the environment that deploys
    applications.
-   **ZeroTier Central**, over its API, for the network the gateway is a
    member and router of: which subnets it routes, who may join, and the flow
    rules that confine the two continuous-integration identities to the four
    destinations they need. Membership is the authentication boundary for
    traffic the gateway's own firewall never classifies, so those rules are
    the only policing layer that traffic meets.

The first channel is a custom provider of its own,
`kluster.providers.device_files`, which is where the session and its pinned
host key are described.

The three functions below are the whole surface the `physical` stack uses. Each
takes the site facts its channel needs and nothing else, so what a channel can
touch is visible from its signature.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Address

import pulumi

from kluster import conventions
from kluster.components import overlay as zerotier_module
from kluster.components.gateway import estate as estate_module
from kluster.components.gateway.unifi import Firewall
from kluster.providers.device_files.provider import Connection

__all__ = ('declare_estate', 'declare_firewall', 'declare_zerotier')


def declare_estate(
    name: str,
    *,
    host: str,
    host_key: pulumi.Input[str],
    private_key: pulumi.Input[str],
    bgp_neighbour: IPv4Address,
    bgp_password: pulumi.Input[str],
    acme_token: pulumi.Input[str],
    rootfs: Mapping[str, estate_module.Rootfs],
    addresses: Mapping[str, IPv4Address],
    opts: pulumi.ResourceOptions | None = None,
) -> estate_module.Estate:
    """Declare the device's desired state: routing, the estate, the scripts.

    `host_key` is the pinned SSH host key and `private_key` the client
    credential; `bgp_neighbour` is the worker VM's address, which the routing
    daemon's configuration names. That address is a constant rather than
    another resource's output on purpose — the session must not depend on a
    lease.

    The two secrets are device secrets, delivered as files beside the estate and
    read by nothing else: `bgp_password` authenticates the routing session, and
    `acme_token` buys the gateway the certificates for its own vhosts — a
    credential separate from the cluster's issuer, because the gateway's TLS has
    to keep renewing while the cluster is down.

    `rootfs` and `addresses` are the estate's site facts: which build each
    container runs and where the bridged ones sit on the container VLAN. They
    are configuration rather than constants — a digest is whatever the build
    produced, and the resolvers' addresses were written into every lease on the
    LAN long before this program existed.
    """
    return estate_module.Estate(
        name,
        connection=Connection(
            host=host,
            private_key=private_key,
            host_key=host_key,
            username=conventions.GW_SSH_USER,
        ),
        containers=estate_module.census(rootfs=rootfs, addresses=addresses, acme_token=acme_token),
        bgp_neighbour=bgp_neighbour,
        bgp_password=bgp_password,
        opts=opts,
    )


def declare_firewall(
    name: str,
    *,
    api_url: str,
    api_key: pulumi.Input[str],
    site: str,
    worker_gua: pulumi.Input[str] | None,
    peer_port: int,
    opts: pulumi.ResourceOptions | None = None,
) -> Firewall:
    """Declare the controller-side firewall census.

    `worker_gua` is the worker VM's global IPv6 address: the one rule that
    cannot be written against a stable object, because the zone-policy API
    matches literal addresses and the site's delegated prefix rotates. `None`
    is the state before the worker has one — the address is formed by SLAAC
    off the very network this census declares — and it means the pinhole is
    not declared at all, leaving the worker's IPv6 outbound-only.
    `peer_port` is the bulk-transfer application's inbound peer port, which
    the pinhole and the one port forward both name — a number inherited from
    the deployment this cluster replaces rather than chosen here, so it is
    read from configuration rather than fixed in code.

    Authentication is an API key belonging to a dedicated local
    administrator — never the SSH credential — and retries are throttled,
    the controller's login rate limit being account-wide rather than
    per-address.
    """
    return Firewall(
        name,
        api_url=api_url,
        api_key=api_key,
        site=site,
        worker_gua=worker_gua,
        peer_port=peer_port,
        opts=opts,
    )


def declare_zerotier(
    name: str,
    *,
    api_token: pulumi.Input[str],
    network_id: str,
    members: Mapping[str, zerotier_module.Enrolled],
    adguard: Sequence[IPv4Address],
    opts: pulumi.ResourceOptions | None = None,
) -> zerotier_module.Network:
    """Declare the ZeroTier network: routes, roster, flow rules.

    The network already exists and is addressed by id; what is declared is its
    configuration and its membership. The roster is the census — a member
    whose role is not declared cannot exist in the desired state, which
    matters because the default role is the permissive one.

    `members` carries the node identifiers, and the addresses of the devices
    that predate this program; `adguard` names the resolvers, which the flow
    rules admit a continuous-integration member to and nothing else does.
    `network_id` is a plain value rather than an input because it is what the
    network is adopted by, and an adoption cannot wait on a computation.
    """
    return zerotier_module.Network(
        name,
        api_token=api_token,
        network_id=network_id,
        members=members,
        adguard=adguard,
        opts=opts,
    )
