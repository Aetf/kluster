"""The gateway: the home site's router, and everything it must be told.

Declared from the `physical` stack (physical.md §4). The device is configured
through two doors, and `Gateway` owns both because each needs its own
credential and neither implies the other:

-   **The device itself**, over SSH. There is no API for most of what matters
    here — routing, the container services, the boot chain that re-establishes
    both after a firmware update — but there is a proven convention:
    desired-state files under `/data`, written idempotently. That is
    `kluster.providers.device_files`, a dynamic provider whose `diff` reads the
    device and whose `create`/`update` writes and then runs a hook. Bulk
    artifacts (container root filesystems built by CI) are named by a pin the
    device pulls for itself, never carried as bytes in state, so a preview
    compares two digests. `Gateway` says
    where the device answers, and the key it must present is the pin in
    `conventions`; the credential that opens the session is the provider's own,
    read in its `configure` and handled by nothing here (rfc-002 §7.4).
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
from kluster.components.gateway.container import (
    CaddyService,
    Container,
    OverlayDaemon,
    ResolverService,
    Rootfs,
    machine,
)
from kluster.components.gateway.nspawn import NspawnRuntime
from kluster.components.gateway.persistence import DevicePersistence
from kluster.components.gateway.services import FRR_APPLY, FRR_CONFIG, FRR_MODE, RoutingSession, frr_config
from kluster.components.gateway.unifi import SiteFirewall
from kluster.providers.device_files.provider import Connection, DeviceFile
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
        caddy: CaddyService,
        resolvers: Sequence[ResolverService],
        overlay_daemon: OverlayDaemon,
        routing: RoutingSession,
        site: str,
        worker_gua: pulumi.Input[str] | None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the gateway.

        `host` is where the device answers today, and both doors dial it: the
        shell the desired state travels over and the controller API the
        firewall resources call terminate on the same box. During a first
        bring-up that is a LAN address, because the daemon answering at the
        gateway's overlay address is one of the services this run is delivering
        (physical/gateway.md §2.5).

        The key the device must present is not a parameter: it is a pin this
        repository decides, so it is `conventions.gateway.HOST_KEY` and a
        preview shows it, which is where a reviewer checks what a session will
        be held to (rfc-002 §11). The client credential that answers it is not
        a parameter here either — it is the provider's own (§7.4).

        `worker_gua` is the worker VM's global IPv6 address: the one firewall
        rule that cannot be written against a stable object, because the
        zone-policy API matches literal addresses and the site's delegated
        prefix rotates. `None` is the state before the worker has one — the
        address is formed by SLAAC off the very network the firewall census
        declares — and it means the pinhole is not declared at all, leaving the
        worker's IPv6 outbound-only.
        """
        super().__init__(name, opts=opts)
        connection = Connection(
            host=host,
            host_key=conventions.gateway.HOST_KEY,
            username=conventions.gateway.SSH_USER,
        )

        declarations = (caddy, *resolvers, overlay_daemon)

        # The mechanism under everything else on the device: what puts the
        # customization back after a firmware update, and the way the layers
        # above deliver a script, an executable, a unit or a directory. The
        # package set it renders is the union of what those layers require, so
        # each of them states its own requirement and none of them writes the
        # script.
        self.persistence = DevicePersistence(
            f'{name}-persistence',
            connection=connection,
            packages=NspawnRuntime.REQUIRED_PACKAGES,
            opts=self.child_opts(),
        )
        # The framework the services run on, handed the same declarations the
        # services are built from: what the converger acts on and what the
        # components declare are then one statement, and neither side has to
        # exist before the other.
        self.runtime = NspawnRuntime(
            f'{name}-nspawn',
            mechanism=self.persistence,
            machines=tuple(machine(declaration) for declaration in declarations),
            opts=self.child_opts(),
        )
        self.containers: tuple[Container, ...] = tuple(
            Container(
                f'{name}-{declaration.service.name}',
                declaration=declaration,
                runtime=self.runtime,
                connection=connection,
                opts=self.child_opts(),
            )
            for declaration in (caddy, *resolvers)
        )
        # The routing configuration answers to the daemon rather than to the
        # boot chain, so it applies itself. It carries the session password,
        # which is why it is secret.
        self.routing = DeviceFile(
            f'{name}-routing',
            connection=connection,
            path=FRR_CONFIG,
            content=pulumi.Output.from_input(routing.password).apply(
                lambda password: frr_config(neighbour=routing.neighbour, password=password)
            ),
            mode=FRR_MODE,
            owner=conventions.gateway.SSH_USER,
            hook=FRR_APPLY,
            secret=True,
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
            opts=self.child_opts(),
        )

        # Last, and behind every other child of this component. Once the device
        # is an overlay member, any resource's session may ride the tunnel this
        # container carries, so restarting it before the last write has landed
        # severs the apply mid-flight. The dependency is what says so, rather
        # than an order inside a script: at boot no apply is in progress and
        # nothing rides anything, and the converger is therefore free of it.
        self.overlay_daemon = Container(
            f'{name}-{overlay_daemon.service.name}',
            declaration=overlay_daemon,
            runtime=self.runtime,
            connection=connection,
            after=(self.persistence, self.runtime, *self.containers, self.routing, self.firewall),
            opts=self.child_opts(),
        )

        self.register_outputs({})
