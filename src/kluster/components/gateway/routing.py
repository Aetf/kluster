"""The routing session the site learns the `lan` pool over.

The device routes for the site, and one of the routes it must know is not the
site's: the pool the cluster hands out to its own load balancers, learned from
the homelab worker over BGP (cluster-infra.md §2). The daemon that holds the
session is the device's own; what this program owns is the configuration it
reads and the converger that installs it.

**The configuration is desired state, the daemon's copy is not.** The file
lands under the custom root, where a firmware update leaves it, and the daemon
reads a path outside it — so something has to copy one to the other, at boot
with nothing else present and again after every push. That something is a unit
plus an executable rather than a script in the boot chain, because installing a
configuration file manipulates nothing of systemd's own (physical/gateway.md
§1.2), and the *same* executable is the configuration file's post-apply hook:
the boot path and the push path are one converger, so the recovery path cannot
rot unnoticed.

**The converger reloads rather than restarts** where the daemon lets it. A
restart drops every session the device holds, including the ones this file is
not about; a reload is what the daemon offers for a configuration change, and
the fallback exists for the boot where the daemon is not running yet.

**The content is a secret**, because the session's authentication password is
in it — which is also why the file is not world-readable on the device.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway.persistence import (
    TEMPLATE_PACKAGE,
    DevicePersistence,
    executable_hook,
    executable_path,
    skeleton_path,
)
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceFile
from putils import Component

__all__ = (
    'CONVERGER',
    'CONVERGER_UNIT',
    'FRR_APPLIED',
    'FRR_CONFIG',
    'FRR_DIRECTORY',
    'FRR_LIVE_CONFIG',
    'FRR_MODE',
    'FRR_RELOAD',
    'MAX_PREFIXES',
    'RoutingSession',
    'SiteRouting',
    'converger_hook',
    'converger_script',
    'converger_unit',
    'frr_config',
)

#: The directory the daemon's configuration is delivered into, as a name under
#: the custom root: the layer below decides what that root is.
FRR_DIRECTORY = 'frr'

#: The configuration as desired state, and as the daemon reads it. The second
#: is off `/data` and therefore what a firmware update takes away, which is the
#: whole reason the first exists.
FRR_CONFIG = f'{skeleton_path(FRR_DIRECTORY)}/frr.conf'
FRR_LIVE_CONFIG = '/etc/frr/frr.conf'

#: What the converger last got the daemon to accept, as a checksum beside the
#: daemon's own copy. It is what makes "already done" mean the reload happened
#: rather than merely that the bytes are in place: a run whose reload failed
#: leaves the file installed, and without this the next run would find the two
#: copies equal and exit successfully with the daemon still on the old
#: configuration. Off `/data` with the file it describes, so a firmware update
#: takes both and the next boot installs and reloads from scratch.
FRR_APPLIED = f'{FRR_LIVE_CONFIG}.{conventions.CLUSTER_NAME}-applied'

#: The session password is in it, so it is not world-readable.
FRR_MODE = '0640'

#: The converger, and the unit that runs it at boot.
CONVERGER = 'frr-config.sh'
CONVERGER_UNIT = 'frr-config.service'

#: How the daemon is told to read the file again. Reload is what a
#: configuration change asks for — a restart would drop every routing session
#: the device holds, this one included — and the restart is the fallback for
#: the boot where the daemon is not up yet to be reloaded. Neither is silenced:
#: a converger that cannot make the file take effect has failed, and a hook's
#: non-zero exit fails the apply that ran it.
FRR_RELOAD = 'systemctl reload frr || systemctl restart frr'

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


@final
@dataclass(frozen=True)
class _ConvergerParams:
    """What `frr-config.sh.j2` reads: the two paths, and how the daemon is told."""

    cluster: str
    source: str
    live: str
    stamp: str
    mode: str
    reload: str


@final
@dataclass(frozen=True)
class _UnitParams:
    """What `frr-config.service.j2` reads: what it runs and what must be there."""

    cluster: str
    source: str
    executable: str


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


def converger_script() -> str:
    """The executable that installs the configuration and tells the daemon.

    Written in POSIX shell, for a device whose interpreters are whatever its
    firmware ships. It does nothing at all when the source is absent — the
    state after this program stops declaring the file, and not one in which
    taking the daemon's configuration away would be an improvement — and
    nothing when the daemon has already accepted what the source says.

    **What says it has is the stamp, not the installed file.** The reload can
    fail after the write succeeded, so a rerun that compared only the two
    copies would find them equal and report success over a daemon still running
    the old configuration. The stamp is written after the reload returns, which
    is what makes the whole effect idempotent rather than only the copy.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{CONVERGER}.j2',
        _ConvergerParams(
            cluster=conventions.CLUSTER_NAME,
            source=FRR_CONFIG,
            live=FRR_LIVE_CONFIG,
            stamp=FRR_APPLIED,
            mode=FRR_MODE,
            reload=FRR_RELOAD,
        ),
    )


def converger_unit() -> str:
    """The oneshot that runs the converger at boot.

    `RemainAfterExit` is what makes a finished run look finished: the unit
    converger starts a unit it finds inactive, and a oneshot without it is
    inactive the moment it succeeds — so every pass over the unit store would
    run this again.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{CONVERGER_UNIT}.j2',
        _UnitParams(
            cluster=conventions.CLUSTER_NAME,
            source=FRR_CONFIG,
            executable=executable_path(CONVERGER),
        ),
    )


def converger_hook() -> str:
    """Run the converger over whatever just landed, guarded as layer 1 guards it."""
    return executable_hook(CONVERGER)


class SiteRouting(Component):
    """The routing configuration on the device, and the converger that applies it."""

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        mechanism: DevicePersistence,
        session: RoutingSession,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the session's configuration and the pieces that install it.

        `mechanism` is the persistence layer, which decides where a directory,
        an executable and a unit go; `connection` is how the configuration file
        itself is delivered, since it is a file of this component's own rather
        than one of the mechanism's kinds.
        """
        super().__init__(name, opts=opts)

        self.directory: DeviceFile = mechanism.skeleton_dir(FRR_DIRECTORY, opts=self.child_opts())
        self.converger: DeviceFile = mechanism.executable(CONVERGER, converger_script(), opts=self.child_opts())
        # The unit is what runs the converger at boot; it waits for the
        # executable, because installing a unit starts it.
        self.unit: DeviceFile = mechanism.unit(
            CONVERGER_UNIT, converger_unit(), opts=self.child_opts(depends_on=[self.converger])
        )
        # The daemon's configuration, applied by the same executable the unit
        # runs. It waits for the directory it lands in and for the hook it will
        # run: a hook that is not on the device yet fails the write that
        # delivered the file.
        self.config: DeviceFile = DeviceFile(
            f'{name}-config',
            connection=connection,
            path=FRR_CONFIG,
            content=pulumi.Output.from_input(session.password).apply(
                lambda password: frr_config(neighbour=session.neighbour, password=password)
            ),
            mode=FRR_MODE,
            owner=conventions.gateway.SSH_USER,
            hook=converger_hook(),
            secret=True,
            opts=self.child_opts(depends_on=[self.directory, self.converger]),
        )

        self.register_outputs({})
