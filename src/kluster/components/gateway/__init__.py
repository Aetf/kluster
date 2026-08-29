"""The gateway: the home site's router, and everything it must be told.

Declared from the `physical` stack (physical.md §4). The device is configured
through two doors, and `Gateway` owns both because each needs its own
credential and neither implies the other:

-   **The device itself**, over SSH. There is no API for most of what matters
    here — routing, the container services, the script that re-establishes both
    after a firmware update — but there is a proven convention: desired-state
    files under `/data`, written idempotently. That is
    `kluster.providers.device_files`, a dynamic provider whose `diff` reads the
    device and whose `create`/`update` writes and then runs a hook. Bulk
    artifacts (container root filesystems built by CI) travel as a URL and a
    digest, never as bytes in state, so a preview stays cheap. `Gateway` says
    where the device answers and which host key it must present; the credential
    that opens the session is the provider's own, read in its `configure` and
    handled by nothing here (rfc-002 §7.4).
-   **The UniFi controller**, over its API, for the firewall. Those resources
    live in this stack and not beside the applications whose traffic they
    admit — the one deliberate exception to co-location, because a gateway
    credential must not be handed to the environment that deploys
    applications. The key configures the controller provider and nothing else,
    so it is read where that provider is built (rfc-002 §8.1).

**The overlay is not under here** (rfc-002 §6). The gateway is a member of the
overlay with routes that bridge it to the site, which is a fact about the
gateway; the overlay's own configuration — who may join, what the rules are —
is not the gateway's business and does not go through it. The two meet only in
`conventions`: the roster says which address the gateway answers at, and both
read it.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster import conventions
from kluster.components.gateway.container import CaddyService, OverlayDaemon, ResolverService, Rootfs
from kluster.components.gateway.services import DeviceServices, RoutingSession
from kluster.components.gateway.unifi import SiteFirewall
from kluster.providers.device_files.provider import Connection
from putils import Component

#: The declaration types the stack program builds a `Gateway` out of are
#: re-exported here, so that wiring the gateway is one import.
__all__ = ('CaddyService', 'Gateway', 'OverlayDaemon', 'ResolverService', 'Rootfs', 'RoutingSession')


class Gateway(Component):
    """The device and its controller: the two doors, and what is behind each."""

    def __init__(
        self,
        name: str,
        *,
        host: str,
        host_key: pulumi.Input[str],
        caddy: CaddyService,
        resolvers: Sequence[ResolverService],
        overlay_daemon: OverlayDaemon,
        routing: RoutingSession,
        site: str,
        worker_gua: pulumi.Input[str] | None,
        peer_port: int,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the gateway.

        `host` is where the device answers today, and both doors dial it: the
        shell the desired state travels over and the controller API the
        firewall resources call terminate on the same box. During a first
        bring-up that is a LAN address, because the daemon answering at the
        gateway's overlay address is one of the services this run is delivering
        (physical/gateway.md §2.5).

        `host_key` is the pinned SSH host key: a bare `ssh-ed25519 <blob>` line
        with no host name in front of it, so it matches the device at whichever
        address the session dials. A public key by nature, and a pin is worth
        more when a reviewer can read it — but the stack program supplies it as
        a secret-typed configuration value, so previews redact it until the pin
        moves into `conventions` (rfc-002 §11). The client credential that
        answers it is not a parameter here at all.

        `worker_gua` is the worker VM's global IPv6 address: the one firewall
        rule that cannot be written against a stable object, because the
        zone-policy API matches literal addresses and the site's delegated
        prefix rotates. `None` is the state before the worker has one — the
        address is formed by SLAAC off the very network the firewall census
        declares — and it means the pinhole is not declared at all, leaving the
        worker's IPv6 outbound-only.
        """
        super().__init__(name, opts=opts)

        self.services = DeviceServices(
            f'{name}-services',
            connection=Connection(
                host=host,
                host_key=host_key,
                username=conventions.gateway.SSH_USER,
            ),
            caddy=caddy,
            resolvers=resolvers,
            overlay_daemon=overlay_daemon,
            routing=routing,
            opts=self.child_opts(),
        )
        self.firewall = SiteFirewall(
            f'{name}-firewall',
            # The same address the shell goes to, for the same reason: the
            # controller answers on the gateway itself. Recording it separately
            # would be a second copy of one fact, free to disagree with the
            # first.
            api_url=f'https://{host}',
            site=site,
            worker_gua=worker_gua,
            peer_port=peer_port,
            opts=self.child_opts(),
        )

        self.register_outputs({})
