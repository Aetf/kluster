"""The routing session the site learns the `lan` pool over.

The device routes for the site, and one of the routes it must know is not the
site's: the pool the cluster hands out to its own load balancers, learned from
the homelab worker over BGP (cluster-infra.md §2). The daemon that holds the
session ships with the firmware; what this program owns is the configuration it
reads, the converger that installs it, and the daemon's on-state — because a
configuration nothing reads holds no session.

**The on-state is two facts, and the firmware resets both.** The protocol
daemon is switched off in the firmware's own daemon list, and the daemon suite
is not enabled, so the site has no routing daemon until this program declares
one. The toggle is converged by the same executable that installs the
configuration, re-checked on every run because a firmware update restores the
firmware's file. What starts the daemon at boot is a `Wants=` edge in the
converger's unit rather than an enable: the enable is a mutation of `/etc` that
a boot script would have to re-assert and a second path would have to undo,
while the edge is a line in a file this program already delivers, converges and
retires. Its consequence is worth knowing before reading the device:
`systemctl is-enabled` for the daemon stays `disabled` forever, and says
nothing about whether the daemon runs.

**The configuration is desired state, the daemon's copy is not.** The file
lands under the custom root, where a firmware update leaves it, and the daemon
reads a path outside it — so something has to copy one to the other, at boot
with nothing else present and again after every push. That something is a unit
plus an executable rather than a script in the boot chain, because installing a
configuration file manipulates nothing of systemd's own (physical/gateway.md
§1.2), and the *same* executable is the configuration file's post-apply hook:
the boot path and the push path are one converger, so the recovery path cannot
rot unnoticed.

**The converger restarts the daemon**, because this firmware ships no reload.
The reload verb needs a helper script the image does not carry, and the daemon
holds no session besides this one for a restart to drop. What a restart costs
is the seconds the supervisor takes to bring the daemons back, on the runs
where the configuration or the toggle actually changed.

**A candidate configuration is parsed before it is installed.** The supervisor
pushes the file into the daemons after the unit has already returned, so its
rejection of a line fails nothing — an installed file and a stamp prove
installed, not accepted. Parsing the source first is what turns a firmware
update whose parser no longer likes one of these lines into a converge-time
failure rather than a peer that never establishes.

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
from kluster.providers.device_files.provider import Connection, DeviceDirectory, DeviceFile
from putils import Component

__all__ = (
    'BGP_DAEMON',
    'CONVERGER',
    'CONVERGER_UNIT',
    'FRR_APPLIED',
    'FRR_CONFIG',
    'FRR_DAEMON_LIST',
    'FRR_DIRECTORY',
    'FRR_GROUP',
    'FRR_LIVE_CONFIG',
    'FRR_MODE',
    'FRR_OWNER',
    'FRR_RESTART',
    'FRR_SERVICE',
    'FRR_SYNTAX_CHECK',
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

#: Who owns the daemon's copy: the daemon suite's own convention on this device,
#: and what an operator writing the configuration out from a running daemon
#: expects to overwrite. Not load-bearing — the supervisor that pushes the file
#: into the daemons keeps root and reads it whoever owns it.
FRR_OWNER = 'frr'
FRR_GROUP = 'frr'

#: The firmware's list of which daemons of the suite run, and the one entry in
#: it this program has an opinion about. The file is the firmware's own and an
#: update restores it, so the entry is converged on every run rather than
#: edited once; the protocol daemon is off in the stock file, which is why the
#: site has no session until this program switches it on.
FRR_DAEMON_LIST = '/etc/frr/daemons'
BGP_DAEMON = 'bgpd'

#: The converger, and the unit that runs it at boot.
CONVERGER = 'frr-config.sh'
CONVERGER_UNIT = 'frr-config.service'

#: The firmware's unit for the daemon suite. The converger's unit wants it, so
#: that starting the converger at boot starts the daemon, and the converger
#: restarts it by the same name — which is why a firmware that renamed the unit
#: is a change to one constant.
FRR_SERVICE = 'frr.service'

#: How a candidate configuration is parsed before anything is installed: the
#: file to parse is appended. It parses against the command tree the installed
#: daemons have, with none of them running, which is the only check available
#: here that a line will be accepted rather than merely written.
FRR_SYNTAX_CHECK = 'vtysh -C -f'

#: How the daemon is put onto the configuration. A restart rather than a
#: reload, because the reload verb needs a helper script this firmware does not
#: ship and the daemon holds no session besides this one to drop. It is not
#: silenced: a converger that cannot make the file take effect has failed, and
#: a hook's non-zero exit fails the apply that ran it.
FRR_RESTART = f'systemctl restart {FRR_SERVICE}'

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
    """What `frr-config.sh.j2` reads: the files it works between, and the commands.

    Ownership is parameters rather than literals in the template because the
    tests run the rendered file as an unprivileged user against a temporary
    tree, where the only owner it can install as is its own.
    """

    cluster: str
    source: str
    live: str
    stamp: str
    daemons: str
    daemon: str
    owner: str
    group: str
    mode: str
    check: str
    restart: str


@final
@dataclass(frozen=True)
class _UnitParams:
    """What `frr-config.service.j2` reads: what it runs, and what it starts."""

    cluster: str
    daemon_unit: str
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
    """The executable that gives the daemon its configuration and switches it on.

    Written in POSIX shell, for a device whose interpreters are whatever its
    firmware ships. It does nothing at all when the source is absent — the
    state after this program stops declaring the file, and not one in which
    taking the daemon's configuration away would be an improvement — and
    nothing when the daemon is already switched on and running what the source
    says.

    **The toggle is asserted, not merely edited.** Switching the protocol
    daemon on is a substitution on the firmware's own line, and a firmware that
    reshaped that file until the substitution matches nothing would leave a
    converger reporting success over a daemon that never starts. So the run
    fails unless the switched-on line is there when it is done, and a
    substitution that had to happen is itself a reason to restart.

    **What says the daemon holds it is the stamp, not the installed file.** The
    restart can fail after the write succeeded, so a rerun that compared only
    the two copies would find them equal and report success over a daemon still
    running the old configuration. The stamp is written after the restart
    returns, which is what makes the whole effect idempotent rather than only
    the copy.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{CONVERGER}.j2',
        _ConvergerParams(
            cluster=conventions.CLUSTER_NAME,
            source=FRR_CONFIG,
            live=FRR_LIVE_CONFIG,
            stamp=FRR_APPLIED,
            daemons=FRR_DAEMON_LIST,
            daemon=BGP_DAEMON,
            owner=FRR_OWNER,
            group=FRR_GROUP,
            mode=FRR_MODE,
            check=FRR_SYNTAX_CHECK,
            restart=FRR_RESTART,
        ),
    )


def converger_unit() -> str:
    """The oneshot that runs the converger at boot, and starts the daemon with it.

    `Wants=` on the daemon's unit is how the daemon comes up at all: nothing
    enables it, and this unit is enabled by the unit converger like every other
    unit here, so wanting the daemon is what pulls it into the boot. `After=`
    the same unit, because a converger ordered before it could not restart it
    synchronously — the daemon's start job would wait on this unit and this
    unit on the restart. There is deliberately no condition on the
    configuration's presence: a failed condition still pulls in and orders
    `Wants=` dependencies, so it would gate nothing that matters while implying
    that it does, and the converger already exits successfully when the source
    is absent.

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
            daemon_unit=FRR_SERVICE,
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

        self.directory: DeviceDirectory = mechanism.skeleton_dir(FRR_DIRECTORY, opts=self.child_opts())
        self.converger: DeviceFile = mechanism.executable(CONVERGER, converger_script(), opts=self.child_opts())
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
        # The unit is what runs the converger at boot and what starts the
        # daemon; it waits for the executable, because installing a unit starts
        # it, and for the configuration, because starting it any earlier would
        # start a daemon with the protocol switched off and restart it a moment
        # later — correct, and a restart nobody needed.
        self.unit: DeviceFile = mechanism.unit(
            CONVERGER_UNIT, converger_unit(), opts=self.child_opts(depends_on=[self.converger, self.config])
        )

        self.register_outputs({})
