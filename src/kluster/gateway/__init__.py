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

The SSH session crosses ZeroTier, so the device's **host key is pinned**: a
first contact that accepts whatever answers would hand an interposer root on
the router. The transport is `asyncssh` rather than a subprocess: each provider
operation runs on a gRPC worker thread and brings up its own event loop
(`asyncio.run`), and inside that loop an asyncio-native client needs no
further bridging; it ships its own type information, which a shelled-out
`ssh` cannot; and pinning a host key is a parameter to it rather than a
`known_hosts` file assembled on the runner.

Not implemented: each `declare_*` raises, naming what is missing.
"""

from __future__ import annotations

from ipaddress import IPv4Address

import pulumi

__all__ = ('declare_estate', 'declare_firewall', 'declare_zerotier')


def declare_estate(
    name: str,
    *,
    host: str,
    host_key: pulumi.Input[str],
    private_key: pulumi.Input[str],
    bgp_neighbour: IPv4Address,
    opts: pulumi.ResourceOptions | None = None,
) -> None:
    """Declare the device's desired state: routing, the estate, the scripts.

    `host_key` is the pinned SSH host key and `private_key` the client
    credential; `bgp_neighbour` is the worker VM's address, which the routing
    daemon's configuration names. That address is a constant rather than
    another resource's output on purpose — the session must not depend on a
    lease.
    """
    raise NotImplementedError(
        'physical §4 gateway: the gw-config provider and the desired state it pushes '
        '(routing, the nspawn estate, the boot-time recovery scripts, device secrets) '
        'are not declared yet — see docs/cluster/architecture.md §5.2 and '
        'docs/physical/gateway.md §1'
    )


def declare_firewall(
    name: str,
    *,
    api_url: str,
    api_key: pulumi.Input[str],
    site: str,
    worker_gua: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> None:
    """Declare the controller-side firewall census.

    `worker_gua` is the worker VM's global IPv6 address: the one rule that
    cannot be written against a stable object, because the zone-policy API
    matches literal addresses and the site's delegated prefix rotates.
    Authentication is an API key belonging to a dedicated local
    administrator — never the SSH credential — and retries are throttled,
    the controller's login rate limit being account-wide rather than
    per-address.
    """
    raise NotImplementedError(
        'physical §4 gateway: the UniFi firewall census (the IoT-to-pool zone policy with '
        'its address groups, the IPv6 pinhole, the one port forward) is not declared yet — '
        'see docs/declarative/physical.md §4 and docs/physical/gateway.md §4.2'
    )


def declare_zerotier(
    name: str,
    *,
    api_token: pulumi.Input[str],
    network_id: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> None:
    """Declare the ZeroTier network: routes, roster, flow rules.

    The network already exists and is addressed by id; what is declared is its
    configuration and its membership. The roster is the census — a member
    whose role is not declared cannot exist in the desired state, which
    matters because the default role is the permissive one.
    """
    raise NotImplementedError(
        'physical §4 gateway: the ZeroTier network configuration (managed routes, the member '
        'roster with its role tags, the flow rules confining the CI identities) is not '
        'declared yet — see docs/cluster/architecture.md §5.3 and docs/physical/gateway.md §2'
    )
