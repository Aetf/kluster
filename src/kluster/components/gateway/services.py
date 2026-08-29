"""The device's desired state: the container services, routing, and recovery.

Everything the device must hold, expressed as files under `/data` — the one
directory a firmware update leaves alone (architecture.md §5.2). Three kinds of
thing, and the relationships between them are the design:

-   **The container services.** One `Container` each (`container.py`): a root
    filesystem pinned by digest, the unit that runs it under `systemd-nspawn`,
    and the files it reads. The root filesystems are built by another
    repository's continuous integration and travel as a URL and a digest —
    never as bytes in state — so a preview compares two hashes rather than
    megabytes.
-   **The routing configuration.** One rendered file naming the worker VM as a
    BGP neighbour, with the inbound prefix-list and the prefix cap that keep a
    compromised (or impersonated) peer from advertising the LAN out from under
    itself (cluster-infra.md §2). It carries the session's authentication
    password, so its content is a secret input and the file is declared secret.
-   **The recovery script**, under `on_boot.d`. This is the piece that makes
    the services survive a firmware update with nothing else present: it
    installs the units, retires the ones no longer declared, and starts what is
    left, autonomously, with no expectation that this program is reachable at
    boot.

**The recovery script is also the apply hook.** Every other file's post-apply
hook runs that same script, so the path exercised after a firmware update is the
path exercised on every deployment — the recovery story cannot rot unnoticed,
because it is the only story. That is why the script is declared first and
everything else depends on it, and why it converges whatever it finds rather
than assuming a particular starting point.

**A service is restarted only when something it reads changed.** The script
stamps each unit with a checksum over that service's stamped set and compares
before acting, because the services include the overlay daemon the deployment's
own session rides: restarting it unconditionally would sever the connection that
issued the restart, on every single apply. The one service whose restart *can*
sever the session is converged last, so everything else has converged before the
risk is taken; an apply that dies there fails its resource and the retry finds
the work already done.

**The order of the parameters below is the order of the restart loop.** The
overlay daemon comes last because it carries the session, and it is a parameter
of its own rather than an entry in a list that some sort has to put at the end.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway.container import (
    CONFIG_MODE,
    ROOT_DIR,
    STATE_DIR,
    TEMPLATE_PACKAGE,
    UNIT_DIR,
    UNIT_PREFIX,
    CaddyService,
    Container,
    OverlayDaemon,
    ResolverService,
    ServiceDeclaration,
    config_path,
    state_path,
)
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceFile
from putils import Component

__all__ = (
    'FRR_APPLY',
    'FRR_CONFIG',
    'FRR_LIVE_CONFIG',
    'FRR_MODE',
    'MAX_PREFIXES',
    'RECOVERY_HOOK',
    'RECOVERY_SCRIPT',
    'SCRIPT_MODE',
    'DeviceServices',
    'RoutingSession',
    'frr_config',
    'recovery_script',
)

#: The routing configuration, as desired state and as the daemon reads it. The
#: daemon's own path is not under `/data`, so the recovery script copies the
#: first to the second — which is also what the apply hook does, since the two
#: are the same script.
FRR_CONFIG = f'{conventions.gateway.DATA_ROOT}/frr/frr.conf'
FRR_LIVE_CONFIG = '/etc/frr/frr.conf'
#: The session password is in it, so it is not world-readable.
FRR_MODE = '0640'
FRR_APPLY = f'install -m {FRR_MODE} {FRR_CONFIG} {FRR_LIVE_CONFIG} && systemctl reload frr'

#: The boot-time recovery script, and the command that runs it. `20-` orders it
#: after whatever numbering the device's own scripts use and before nothing.
RECOVERY_SCRIPT = f'{conventions.gateway.ON_BOOT_D}/20-kluster-services.sh'
RECOVERY_HOOK = f'sh {RECOVERY_SCRIPT}'

SCRIPT_MODE = '0755'

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


@final
@dataclass(frozen=True)
class _Placement:
    """Where one service's initial state is delivered from, and where it lands."""

    source: str
    destination: str


@final
@dataclass(frozen=True)
class _UnitState:
    """One service as the recovery script deals with it.

    Both paths are resolved here rather than in the file: where a mounted file,
    a root filesystem tree or a state directory sits is this package's, and the
    script only has to compare and copy.
    """

    name: str
    stamped: tuple[str, ...]
    initial_state: _Placement | None


@final
@dataclass(frozen=True)
class _RecoveryParams:
    """What `recover-services.sh.j2` reads, with the services in restart order."""

    cluster: str
    data_root: str
    unit_dir: str
    state_dir: str
    root_dir: str
    unit_prefix: str
    config_mode: str
    frr_config: str
    frr_mode: str
    frr_live_config: str
    units: tuple[_UnitState, ...]


def recovery_script(declarations: Sequence[ServiceDeclaration]) -> str:
    """The script that re-establishes the services, at boot and at every apply.

    Written for the device's shell, which is BusyBox: no arrays, no
    `bash`-isms, and `cksum` rather than a digest tool, since all it has to do
    is notice a change.

    What it guarantees, in order:

    1.  every declared unit is installed and enabled;
    2.  a unit no longer declared is stopped and removed — which is what keeps
        the device from accumulating its own history;
    3.  the routing configuration is copied where the daemon reads it;
    4.  a service that owns its own configuration is given an initial state,
        but only where it has none;
    5.  a service is (re)started only if its stamped set changed, compared
        against the content stamp beside its state.

    The last point is why this can be the apply hook: without it, every
    deployment would restart the overlay daemon that the deployment's own
    session is riding on. `declarations` arrive in restart order, which is what
    leaves that service until last.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/recover-services.sh.j2',
        _RecoveryParams(
            cluster=conventions.CLUSTER_NAME,
            data_root=conventions.gateway.DATA_ROOT,
            unit_dir=UNIT_DIR,
            state_dir=STATE_DIR,
            root_dir=ROOT_DIR,
            unit_prefix=UNIT_PREFIX,
            config_mode=CONFIG_MODE,
            frr_config=FRR_CONFIG,
            frr_mode=FRR_MODE,
            frr_live_config=FRR_LIVE_CONFIG,
            units=tuple(_unit_state(declaration) for declaration in declarations),
        ),
    )


def _unit_state(declaration: ServiceDeclaration) -> _UnitState:
    """One service, named and located the way the recovery script needs it."""
    initial = declaration.initial_state
    return _UnitState(
        name=declaration.unit_name,
        stamped=declaration.stamped_set,
        initial_state=None
        if initial is None
        else _Placement(
            source=config_path(declaration.service.name, initial.name),
            destination=f'{state_path(declaration.service.name)}/{initial.into}',
        ),
    )


class DeviceServices(Component):
    """The device's desired state, as files and one script that applies them."""

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        caddy: CaddyService,
        resolvers: Sequence[ResolverService],
        overlay_daemon: OverlayDaemon,
        routing: RoutingSession,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        declarations: tuple[ServiceDeclaration, ...] = (caddy, *resolvers, overlay_daemon)

        # Declared first and depended on by everything else, because it is what
        # every other file's hook runs. A device that has only this file
        # converges the moment the rest arrives. It is rendered from the same
        # declarations the containers below are built from, so a stamped set
        # cannot name a file no resource declares.
        self.recovery = DeviceFile(
            f'{name}-recovery',
            connection=connection,
            path=RECOVERY_SCRIPT,
            content=recovery_script(declarations),
            mode=SCRIPT_MODE,
            owner=conventions.gateway.SSH_USER,
            hook=RECOVERY_HOOK,
            opts=self.child_opts(),
        )
        child = self.child_opts(depends_on=[self.recovery])

        # The routing configuration answers to the daemon rather than to the
        # recovery script, so it applies itself. It carries the session
        # password, which is why it is secret.
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
            opts=child,
        )

        self.containers: tuple[Container, ...] = tuple(
            Container(
                f'{name}-{declaration.service.name}',
                declaration=declaration,
                connection=connection,
                hook=RECOVERY_HOOK,
                after=(self.recovery,),
                opts=self.child_opts(),
            )
            for declaration in declarations
        )

        self.register_outputs({})
