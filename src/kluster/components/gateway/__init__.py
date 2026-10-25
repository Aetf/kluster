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
    read in its `configure` and handled by nothing here (framework/pulumi.md
    §5.2).
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
`conventions`: the roster says which address the gateway answers at, and the
network id which network its daemon asks to join, and both read them.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway import access, routing
from kluster.components.gateway.access import AuthorizedKeys, PublicKey
from kluster.components.gateway.container import CaddyService, Container, OverlayDaemon, ResolverService, Rootfs
from kluster.components.gateway.nspawn import WATCHDOG_UNIT, NspawnRuntime
from kluster.components.gateway.persistence import (
    PACKAGES_SCRIPT,
    TEMPLATE_PACKAGE,
    UDM_BOOT_UNIT,
    DevicePersistence,
    on_boot_path,
)
from kluster.components.gateway.routing import RoutingSession, SiteRouting
from kluster.components.gateway.unifi import SiteFirewall
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceFile
from putils import Component

#: The declaration types the stack program builds a `Gateway` out of are
#: re-exported here, so that wiring the gateway is one import.
__all__ = (
    'BACKUP_PULL_PACKAGES',
    'DELIVERED_UNITS',
    'HEALTH_PROGRAM',
    'PLAYBOOKS',
    'ZEROTIER_CLI',
    'CaddyService',
    'Gateway',
    'OverlayDaemon',
    'Playbooks',
    'PublicKey',
    'ResolverService',
    'Rootfs',
    'RoutingSession',
    'health_program',
)

#: What the device must hold for a consumer that is not on it: the homelab
#: host's backup pull, which copies the controller's autobackups off the device
#: with `rsync` and so runs `rsync` on this end of its session too. The pull is a
#: host job that reads the device on either layout and runs across the cutover
#: window (physical/gateway.md §1.6), and a firmware update wipes every package
#: it did not ship, so the package set carries it beside what the layers here
#: require.
BACKUP_PULL_PACKAGES: tuple[str, ...] = ('rsync',)

#: The read-only executable the homelab host's daily check runs over SSH: the
#: device read against this declaration (physical/gateway.md §1.6). It is an
#: executable with no unit, because it runs nothing on its own (§1.2).
HEALTH_PROGRAM = 'gateway-health'

#: The units the persistence layer delivers, which the health reading holds
#: enabled and active: the boot chain's anchor, the runtime's bridge watchdog,
#: and the routing and key convergers. A unit delivered through the mechanism
#: and missing here is one nothing reads, which the gateway suite catches by
#: comparing this reading against every unit source the program declares.
DELIVERED_UNITS: tuple[str, ...] = (UDM_BOOT_UNIT, WATCHDOG_UNIT, routing.CONVERGER_UNIT, access.CONVERGER_UNIT)

#: Where the overlay image installs its client, which the health reading runs in
#: the member's mount namespace, entered through the machine's leader as the
#: cutover runbook's verification does: the image runs s6-overlay rather than
#: systemd, so `machinectl shell` has no guest systemd to start a command with.
ZEROTIER_CLI = '/usr/sbin/zerotier-cli'


@final
@dataclass(frozen=True)
class Playbooks:
    """The playbook each finding of the health reading names: a bullet title of physical/gateway.md §3.

    A finding ships with its playbook (architecture.md §4.3), so each title is
    spelled exactly as the bullet in that section, and the suite holds every
    title the executable prints to one of them.
    """

    machine: str = 'A declared machine not running'
    boot_chain: str = "The boot chain's last run failed"
    unit: str = 'A delivered unit disabled or failed'
    routing: str = 'The routing daemon not running'
    overlay: str = 'ZeroTier container down on the UDM'
    packages: str = 'The offline cache would not carry an offline boot'


PLAYBOOKS = Playbooks()


@final
@dataclass(frozen=True)
class _HealthParams:
    """What `gateway-health.j2` reads, every value already quoted for the shell."""

    cluster: str
    machine_units: tuple[str, ...]
    overlay_unit: str
    overlay_machine: str
    zerotier_cli: str
    units: tuple[str, ...]
    boot_chain_unit: str
    routing_daemon: str
    packages_script: str
    packages_check: str
    certificates: tuple[str, ...]
    playbooks: Mapping[str, str]


def health_program(
    *,
    caddy: CaddyService,
    resolvers: Sequence[ResolverService],
    overlay_daemon: OverlayDaemon,
    packages_script: str = on_boot_path(PACKAGES_SCRIPT),
) -> str:
    """The executable that reads the device against the declaration, rendered from the declaration.

    The machines are the declarations `Gateway` builds its containers from,
    the units are `DELIVERED_UNITS`, and the routing daemon is the one the
    routing converger starts. The offline cache is judged by `10-packages.sh
    --check` itself, so the rule for a cache that would carry an offline boot is
    the mechanism's alone. `packages_script` defaults to where the boot chain
    holds that script, and is a parameter so the executable can be run against
    a tree that is not a device.

    `--certificates` lists one name per site block of the proxy's `Caddyfile`,
    each block being one wildcard certificate: the controller's vhost, and the
    first legacy vhost while the census has one (gateway.md §1.5). A probe of a
    name is a probe of the certificate that covers it, and it goes to the
    proxy's own address because an unmatched name gets no answer.
    """
    machines = (caddy, *resolvers, overlay_daemon)
    names = (conventions.gateway.VHOST_CONTROLLER, *(vhost.host for vhost in caddy.legacy[:1]))
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{HEALTH_PROGRAM}.j2',
        _HealthParams(
            cluster=conventions.CLUSTER_NAME,
            machine_units=tuple(shlex.quote(machine.unit_name) for machine in machines),
            overlay_unit=shlex.quote(overlay_daemon.unit_name),
            overlay_machine=shlex.quote(overlay_daemon.service.name),
            zerotier_cli=shlex.quote(ZEROTIER_CLI),
            units=tuple(shlex.quote(unit) for unit in DELIVERED_UNITS),
            boot_chain_unit=shlex.quote(UDM_BOOT_UNIT),
            routing_daemon=shlex.quote(routing.FRR_SERVICE),
            packages_script=shlex.quote(packages_script),
            packages_check=shlex.quote(f'{PACKAGES_SCRIPT} --check'),
            certificates=tuple(shlex.quote(f'{caddy.service.address} {name}') for name in names),
            playbooks={field: shlex.quote(title) for field, title in asdict(PLAYBOOKS).items()},
        ),
    )


def url_host(host: str) -> str:
    """`host` as a URL's authority spells it.

    The two doors read one value, and they spell an IPv6 literal differently:
    an SSH session takes it bare, a URL takes it in brackets. The value stays
    the shape the device is dialed with — bare, which is also the only shape
    the stack's `gatewayBootstrapHost` guard admits — and the URL is the one
    place that adds the brackets. An IPv4 literal and a name are the same in
    both spellings. A zoned literal (`fe80::1%eth0`) is not a shape the
    bring-up ceremony offers: `ip_address` admits it, but a URL wants the zone
    escaped and the provider's parser refuses it unescaped, and no address a
    gateway is dialed at is link-local (physical/gateway.md §2.5).
    """
    try:
        literal = ip_address(host)
    except ValueError:
        return host
    return f'[{host}]' if isinstance(literal, IPv6Address) else host


class Gateway(Component, pulumi_type='kluster:gateway:Gateway'):
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
        keys: Sequence[PublicKey],
        site: str,
        worker_gua: pulumi.Input[str] | None,
        peer_on_worker: bool = False,
        static_hosts: Mapping[str, IPv4Address | IPv6Address],
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
        a parameter here either — it is the provider's own (framework/pulumi.md
        §5.2).

        `keys` are the public keys the device's one account accepts, this
        program's own among them. They are the parameter that keeps the SSH
        door open across a firmware update, and the private half of each
        belongs to whoever holds it rather than to anything here.

        `worker_gua` is the worker VM's global IPv6 address: the one firewall
        rule that cannot be written against a stable object, because the
        zone-policy API matches literal addresses and the site's delegated
        prefix rotates. `None` is the state before the worker has one — the
        address is formed by SLAAC off the very network the firewall census
        declares — and it means the pinhole is not declared at all, leaving the
        worker's IPv6 outbound-only.

        `peer_on_worker` says the bulk-transfer application runs on the worker,
        and it is what declares the IPv4 forward of the application's peer
        port. `False`, the default, is the state before the application moves:
        its legacy copy on the host holds that WAN port, and no forward is
        declared to take it away.

        `static_hosts` is the site's roll of literal name-to-address entries on
        the device's own resolver, passed through to the firewall component
        that declares them.
        """
        super().__init__(name, opts=opts)
        connection = Connection(
            host=host,
            host_key=conventions.gateway.HOST_KEY,
            username=conventions.gateway.SSH_USER,
        )

        # The mechanism under everything else on the device: what puts the
        # customization back after a firmware update, and the way the layers
        # above deliver a script, an executable, a unit or a directory. The
        # package set it renders is the union of what its consumers require, so
        # each of them states its own requirement and none of them writes the
        # script.
        self.persistence = DevicePersistence(
            f'{name}-persistence',
            connection=connection,
            packages=(*NspawnRuntime.REQUIRED_PACKAGES, *BACKUP_PULL_PACKAGES),
            opts=self.child_opts(),
        )
        # The framework the services run on, told nothing about them. What
        # machines exist is what the `Container` declarations below put on the
        # disk, and both convergers read that off the device -- so this
        # component's files are the same on a gateway running one service and
        # on one running five, and adding a service pushes none of them again.
        self.runtime = NspawnRuntime(f'{name}-nspawn', mechanism=self.persistence, opts=self.child_opts())
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
        # The two customizations with no machine behind them: what the device
        # is told about the site's routing, and who may open a session on it.
        # Each is its own files plus its own converger, built through the
        # mechanism the same way the runtime's are.
        self.routing = SiteRouting(
            f'{name}-routing',
            connection=connection,
            mechanism=self.persistence,
            session=routing,
            opts=self.child_opts(),
        )
        self.access = AuthorizedKeys(
            f'{name}-access',
            connection=connection,
            mechanism=self.persistence,
            keys=keys,
            opts=self.child_opts(),
        )
        self.firewall = SiteFirewall(
            f'{name}-firewall',
            # The same address the shell goes to, for the same reason: the
            # controller answers on the gateway itself. Recording it separately
            # would be a second copy of one fact, free to disagree with the
            # first.
            api_url=f'https://{url_host(host)}',
            site=site,
            worker_gua=worker_gua,
            peer_on_worker=peer_on_worker,
            static_hosts=static_hosts,
            opts=self.child_opts(),
        )

        # The device read against this declaration, for the homelab host's daily
        # check. It is delivered before the overlay member for the reason that
        # member is last: a session may be riding the tunnel it carries.
        self.health: DeviceFile = self.persistence.executable(
            HEALTH_PROGRAM,
            health_program(caddy=caddy, resolvers=resolvers, overlay_daemon=overlay_daemon),
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
            after=(
                self.persistence,
                self.runtime,
                *self.containers,
                self.routing,
                self.access,
                self.firewall,
                self.health,
            ),
            opts=self.child_opts(),
        )

        self.register_outputs({})
