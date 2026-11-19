"""The gateway: the home site's router, and everything it must be told.

Declared from the `physical` stack (physical.md §4). The device is configured
through two doors, and `Gateway` owns both because each needs its own
credential and neither implies the other:

-   **The device itself**, over SSH. There is no API for most of what matters
    here — the container services, and the boot chain that re-establishes
    them after a firmware update — but there is a proven convention:
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
    The site's delegated prefix is read over the same session, on every run:
    the controller does not report it, and the device's PD lease does
    (`prefix`).
-   **The UniFi controller**, over its API, for the firewall and for the
    site's BGP configuration, which the routing session is (physical/gateway.md
    §1.3). Those resources live in this stack and not beside the applications whose traffic they
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
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway import access, nspawn
from kluster.components.gateway.access import AuthorizedKeys, PublicKey
from kluster.components.gateway.container import (
    CaddyService,
    Container,
    OverlayDaemon,
    ResolverService,
    Rootfs,
    ServiceDeclaration,
)
from kluster.components.gateway.nspawn import WATCHDOG_UNIT, NspawnRuntime
from kluster.components.gateway.persistence import (
    NETWORKD_UNIT,
    PACKAGES_SCRIPT,
    TEMPLATE_PACKAGE,
    UDM_BOOT_UNIT,
    DevicePersistence,
    on_boot_path,
)
from kluster.components.gateway.prefix import WAN_INTERFACE, SubnetNotHeld, held_prefix
from kluster.components.gateway.routing import RoutingSession, SiteRouting
from kluster.components.gateway.unifi import SiteFirewall
from kluster.lib import templates
from kluster.providers import pd_lease
from kluster.providers.device_files.provider import Connection, DeviceFile
from putils import Component

#: The declaration types the stack program builds a `Gateway` out of are
#: re-exported here, so that wiring the gateway is one import.
__all__ = (
    'BACKUP_PULL_PACKAGES',
    'DELIVERED_UNITS',
    'DEVICE_BASE',
    'HEALTH_PROGRAM',
    'NETWORKD_CONFIG',
    'NETWORK_BRIDGES',
    'PLAYBOOKS',
    'POLICY_RULE_FAMILIES',
    'SYSTEMD_TERMINATE_RELEASE',
    'VTYSH',
    'ZEROTIER_CLI',
    'CaddyService',
    'DeviceBase',
    'Gateway',
    'OverlayDaemon',
    'Playbooks',
    'PublicKey',
    'ResolverService',
    'Rootfs',
    'RoutingSession',
    'SubnetNotHeld',
    'gateway_address',
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
#: and the key converger. A unit delivered through the mechanism
#: and missing here is one nothing reads, which the gateway suite catches by
#: comparing this reading against every unit source the program declares.
DELIVERED_UNITS: tuple[str, ...] = (UDM_BOOT_UNIT, WATCHDOG_UNIT, access.CONVERGER_UNIT)

#: The address families whose policy rules the health reading holds to a rule
#: that looks up the main table: the rule the gateway's own packets are routed
#: by, and one networkd under its defaults removes with every other rule it did
#: not create (physical/gateway.md §1.2). The healthy device holds that rule
#: in both families.
POLICY_RULE_FAMILIES: tuple[str, ...] = ('4', '6')

#: The name `systemd-analyze cat-config` reads networkd's configuration by,
#: main file and drop-ins together in the order networkd applies them.
NETWORKD_CONFIG = 'systemd/networkd.conf'

#: The routing table a policy rule must look up for the gateway to route its
#: own packets. The name is the kernel's.
_MAIN_TABLE = 'main'

#: Every network this program declares on the site's delegated prefix, and the
#: bridge the device carries it on. Each one's /64 is the firmware's
#: assignment, which the run reads off the device with the prefix (`prefix`),
#: and the health reading holds its bridge to that /64 (physical/gateway.md
#: §1.6). The console's networks join once they are declared.
NETWORK_BRIDGES: Mapping[conventions.SiteNetwork, str] = {conventions.CLUSTER_VLAN: 'br7'}


def gateway_address(subnet: IPv6Network) -> IPv6Address:
    """The address the gateway gives itself on a /64 of the delegated prefix: the subnet's first.

    That is the firmware's choice rather than this program's, read on the
    device on every bridge the prefix numbers (physical/gateway.md §1.6).
    """
    return subnet.network_address + 1


#: Where the overlay image installs its client, which the health reading runs in
#: the member's mount namespace, entered through the machine's leader as the
#: cutover runbook's verification does: the image runs s6-overlay rather than
#: systemd, so `machinectl shell` has no guest systemd to start a command with.
ZEROTIER_CLI = '/usr/sbin/zerotier-cli'

#: The routing suite's shell, which the health reading asks the BGP daemon
#: through. The firmware ships it on the device's `PATH` beside the suite it
#: runs for the controller (physical/gateway.md §1.3).
VTYSH = 'vtysh'


@final
@dataclass(frozen=True)
class DeviceBase:
    """The base under the vendor's firmware that this program is written for (physical/gateway.md §1).

    These are the facts a firmware update can move that something here
    depends on, and nothing else about the device: a release that keeps them
    moves nothing, so the vendor's own release string is not one of them. The
    health reading compares each with the device, and a mismatch is a finding
    whose playbook says what depends on it and what to move; moving the
    declaration once that is done is what clears the finding.
    """

    #: The Debian major release, which `/etc/debian_version` starts with: the
    #: release whose dash `mise.toml`'s `http:dash` pin follows.
    debian_release: str
    #: The version of the `dash` package, as `dpkg-query -W -f '${Version}' dash`
    #: prints it. `mise.toml`'s `http:dash` pin is the amd64 build of that
    #: package at that version: Debian's patches are part of the language the
    #: device's scripts are written in, so a revision within the release can
    #: change what they may say.
    dash_version: str
    #: What `readlink -f /bin/sh` resolves to: the shell the device runs its
    #: `#!/bin/sh` scripts under, which the suite's shell for their templates
    #: stands in for (`tests/device_shell.py`).
    shell: str
    #: Whether the device's systemd is `SYSTEMD_TERMINATE_RELEASE` or later,
    #: whose `machinectl terminate` signals `systemd-nspawn` instead of stopping
    #: the machine's unit: the side of that release physical/gateway.md §1's
    #: account of a machine's restarts is written for.
    terminate_signals_nspawn: bool


#: The systemd release from which `machinectl terminate` signals
#: `systemd-nspawn` rather than stopping the machine's unit (physical/gateway.md
#: §1).
SYSTEMD_TERMINATE_RELEASE = 258

#: The device's base, as read on the device (physical/gateway.md §1). It moves
#: last in §3's "The device's base moved", once what depends on it has moved:
#: moved alone, it silences the finding and leaves the dash pin behind. Its
#: `dash_version` cannot move alone: `tests/test_dash_pin.py` holds it equal to
#: the pin's version.
DEVICE_BASE = DeviceBase(
    debian_release='13', dash_version='0.5.12-12', shell='/usr/bin/dash', terminate_signals_nspawn=False
)


@final
@dataclass(frozen=True)
class Playbooks:
    """The playbook each finding of the health reading names: a bullet title of physical/gateway.md §3.

    A finding ships with its playbook (architecture.md §4.3), so each title is
    spelled exactly as the bullet in that section, and the suite holds every
    title the executable prints to one of them.
    """

    machine: str = 'A declared machine not running'
    port: str = "A machine's port off its bridge"
    boot_chain: str = "The boot chain's last run failed"
    unit: str = 'A delivered unit disabled or failed'
    routing: str = 'The routing daemon not holding the declared peer'
    overlay: str = 'ZeroTier container down on the UDM'
    packages: str = 'The offline cache would not carry an offline boot'
    base: str = "The device's base moved"
    networkd: str = "The firmware's policy rules exposed to removal"
    prefix: str = "The site's delegated prefix rotated"
    lease: str = 'The PD lease no longer reads as declared'


PLAYBOOKS = Playbooks()


@final
@dataclass(frozen=True)
class _HealthParams:
    """What `gateway-health.j2` reads, every value already quoted for the shell."""

    cluster: str
    machine_units: tuple[str, ...]
    #: Each machine on a bridge as three words: its unit, its name, and its bridge.
    bridged_machines: tuple[str, ...]
    overlay_unit: str
    overlay_machine: str
    zerotier_cli: str
    units: tuple[str, ...]
    boot_chain_unit: str
    vtysh: str
    peer: str
    peer_asn: int
    packages_script: str
    packages_check: str
    certificates: tuple[str, ...]
    root: str
    debian_version: str
    bin_sh: str
    debian_release: str
    dash_version: str
    shell: str
    systemd_release: int
    terminate_signals_nspawn: bool
    rule_families: tuple[str, ...]
    main_table: str
    networkd_unit: str
    networkd_config: str
    networkd_section: str
    networkd_settings: tuple[str, ...]
    #: Each network on the delegated prefix as three words: its bridge, the
    #: gateway's address on the /64 the run read for it, and that /64.
    delegations: tuple[str, ...]
    pd_leases: str
    pd_wan: str
    playbooks: Mapping[str, str]


def health_program(
    *,
    caddy: CaddyService,
    resolvers: Sequence[ResolverService],
    overlay_daemon: OverlayDaemon,
    peer: IPv4Address,
    delegated: Mapping[str, IPv6Network],
    packages_script: str = on_boot_path(PACKAGES_SCRIPT),
    root: str = '',
) -> str:
    """The executable that reads the device against the declaration, rendered from the declaration.

    The machines are the declarations `Gateway` builds its containers from,
    the units are `DELIVERED_UNITS`, and `peer` is the routing session's
    neighbor, which the BGP daemon must hold at the cluster's AS. Each machine whose declaration names a bridge is
    read for its port having that bridge as its master; one in the host's
    network namespace has no port and is not read. The offline cache is judged by `10-packages.sh
    --check` itself, so the rule for a cache that would carry an offline boot is
    the mechanism's alone. `packages_script` defaults to where the boot chain
    holds that script, and is a parameter so the executable can be run against
    a tree that is not a device.

    The base is `DEVICE_BASE`, read off `/etc/debian_version`, `dpkg-query`'s
    answer for the `dash` package, `/bin/sh` and `systemctl --version`. `root`
    is the tree the two paths are read under, empty for the device's own, and a
    parameter for the same reason.

    networkd is read three ways: the consequence, a policy rule that looks up
    the main table in each of `POLICY_RULE_FAMILIES`; the trigger, a link
    networkd manages; and the guard, every option of
    `nspawn.FOREIGN_MANAGEMENT` reading as declared in networkd's effective
    configuration. The options are the declaration's, rendered in, so the
    drop-in and the reading cannot part.

    `delegated` is each bridge on the site's delegated prefix and the /64 the
    run read for it. The bridge is read for the gateway's address on that /64
    (`gateway_address`) — which is the reading of a /64 that moved since that
    run, by a rotation or by the firmware numbering the bridge anew — and,
    while it holds it, the PD lease's line for the bridge is read for the same
    /64 under `root`, which is the reading of the lease's format and part of
    the lease read's layer (`kluster.providers.pd_lease`).

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
            bridged_machines=tuple(
                ' '.join(shlex.quote(word) for word in (machine.unit_name, machine.service.name, machine.bridge))
                for machine in machines
                if machine.bridge is not None
            ),
            overlay_unit=shlex.quote(overlay_daemon.unit_name),
            overlay_machine=shlex.quote(overlay_daemon.service.name),
            zerotier_cli=shlex.quote(ZEROTIER_CLI),
            units=tuple(shlex.quote(unit) for unit in DELIVERED_UNITS),
            boot_chain_unit=shlex.quote(UDM_BOOT_UNIT),
            vtysh=shlex.quote(VTYSH),
            peer=shlex.quote(str(peer)),
            peer_asn=conventions.CLUSTER_ASN,
            packages_script=shlex.quote(packages_script),
            packages_check=shlex.quote(f'{PACKAGES_SCRIPT} --check'),
            certificates=tuple(shlex.quote(f'{caddy.service.address} {name}') for name in names),
            root=shlex.quote(root),
            debian_version=shlex.quote(f'{root}/etc/debian_version'),
            bin_sh=shlex.quote(f'{root}/bin/sh'),
            debian_release=shlex.quote(DEVICE_BASE.debian_release),
            dash_version=shlex.quote(DEVICE_BASE.dash_version),
            shell=shlex.quote(DEVICE_BASE.shell),
            systemd_release=SYSTEMD_TERMINATE_RELEASE,
            terminate_signals_nspawn=DEVICE_BASE.terminate_signals_nspawn,
            rule_families=tuple(shlex.quote(family) for family in POLICY_RULE_FAMILIES),
            main_table=shlex.quote(_MAIN_TABLE),
            networkd_unit=shlex.quote(NETWORKD_UNIT),
            networkd_config=shlex.quote(NETWORKD_CONFIG),
            networkd_section=shlex.quote(nspawn.NETWORKD_SECTION),
            networkd_settings=tuple(
                shlex.quote(f'{option}={value}') for option, value in nspawn.foreign_management_settings()
            ),
            delegations=tuple(
                ' '.join(
                    shlex.quote(word) for word in (bridge, f'{gateway_address(subnet)}/{subnet.prefixlen}', str(subnet))
                )
                for bridge, subnet in delegated.items()
            ),
            pd_leases=shlex.quote(f'{root}{pd_lease.LEASES}'),
            pd_wan=shlex.quote(WAN_INTERFACE),
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

        The site's delegated prefix is read off the device on every run
        (`prefix`), and three values leave the component from it:
        `delegated_prefix`, the prefix with its length; `held_subnets`, the
        /64s the site's networks hold on it; and `worker_gua`, the worker VM's
        global IPv6 address. That address is the cluster network's /64 — the
        one the firmware assigned it, which the read reports for its bridge —
        with the EUI-64 of the worker's MAC, which is the address the worker
        forms by SLAAC off that network's advertisement. The firewall's
        pinhole names it, since the zone-policy API matches literal addresses
        only, and the stack exports it, so a /64 that moved re-declares both
        in the run that reads it. A lease with no line for a declared
        network's bridge refuses the run, naming both (`SubnetNotHeld`).

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
        # above deliver a script, an executable, a unit, a file of networkd's
        # configuration or a directory. The package set it renders is the union
        # of what its consumers require, so each of them states its own
        # requirement and none of them writes the script.
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

        def machine(declaration: ServiceDeclaration, after: Sequence[pulumi.Resource] = ()) -> Container:
            return Container(
                f'{name}-{declaration.service.name}',
                declaration=declaration,
                runtime=self.runtime,
                connection=connection,
                after=after,
                opts=self.child_opts(),
            )

        proxy = machine(caddy)
        # The resolvers go one at a time, each behind the one declared before
        # it. Their pins move in one change, and machines left unordered would
        # restart together, leaving the site with no resolver for as long as
        # the restarts overlap. Every piece of a machine runs the hook that
        # restarts it where its definition moved and then holds it to systemd
        # reporting it active, so the next resolver starts moving only once
        # that holds for the one before it -- which is not the same as that one
        # answering on port 53 (physical/gateway.md §1.1).
        ordered: list[Container] = []
        for declaration in resolvers:
            ordered.append(machine(declaration, after=ordered[-1:]))
        self.resolvers: tuple[Container, ...] = tuple(ordered)
        self.containers: tuple[Container, ...] = (proxy, *self.resolvers)
        # The same address the shell goes to, for the same reason: the
        # controller answers on the gateway itself. Recording it separately
        # would be a second copy of one fact, free to disagree with the first.
        api_url = f'https://{url_host(host)}'

        # The prefix the ISP delegates and the /64 the firmware gave each
        # declared network on it, read over the same session on every run, and
        # the worker's address on the cluster network's.
        held = held_prefix(f'{name}-prefix', connection=connection, bridges=NETWORK_BRIDGES, opts=self.child_opts())
        self.prefix_read = held.read
        self.delegated_prefix: pulumi.Output[str] = held.prefix
        self.held_subnets: pulumi.Output[list[str]] = held.subnets
        cluster_bridge = NETWORK_BRIDGES[conventions.CLUSTER_VLAN]
        self.worker_gua: pulumi.Output[str] = held.declared.apply(
            lambda subnets: str(conventions.eui64_address(subnets[cluster_bridge], conventions.HOMELAB_NODE_MAC))
        )

        # The site's routing, which the controller holds, and who may open a
        # session on the device, which is its own files plus its own converger,
        # built through the mechanism the same way the runtime's are.
        self.routing = SiteRouting(
            f'{name}-routing', api_url=api_url, site=site, session=routing, opts=self.child_opts()
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
            api_url=api_url,
            site=site,
            worker_gua=self.worker_gua,
            peer_on_worker=peer_on_worker,
            static_hosts=static_hosts,
            opts=self.child_opts(),
        )

        # The device read against this declaration, for the homelab host's daily
        # check. It is delivered before the overlay member for the reason that
        # member is last: a session may be riding the tunnel it carries.
        self.health: DeviceFile = self.persistence.executable(
            HEALTH_PROGRAM,
            held.declared.apply(
                lambda subnets: health_program(
                    caddy=caddy,
                    resolvers=resolvers,
                    overlay_daemon=overlay_daemon,
                    peer=routing.neighbor,
                    delegated=subnets,
                )
            ),
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
                self.prefix_read,
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
