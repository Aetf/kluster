"""One container service on the device: the machine it is, and the files it reads.

A workload on the nspawn runtime (`nspawn`), and everything it is made of lives
in that machine's own directory under the custom root: the root filesystem
unpacked from its pin, its settings file, the configuration the image reads,
the writable state bind-mounted into it, and — where the software behind it
rewrites its own configuration — an initial state, in a directory of its own so
that the initial state and the live file the service reads never share a
name.

**A service is declared by its own type.** What a service *is* — where it keeps
state, which device nodes it needs, which environment its image reads — is a
fact about its image, so it lives in that image's declaration type rather than
in a mapping every reader has to look up (rfc-002 §5.3). A declaration holds
the census entry it stands for rather than naming it, which is what makes a
resolver bound to a service with no address impossible to write.

**In-container paths are the image's, not this program's.** A resolver is
started with `-w /data/adguard`, caddy reads `$XDG_CONFIG_HOME/caddy/Caddyfile`:
those are baked into the images, so what a declaration decides is only which
host directory is bound onto them.

**The images are Alpine with s6-overlay, not systemd.** They ship that init at
`/sbin/init` so that `Boot=on` finds it, and a machine's settings therefore
declare nothing that only a systemd guest would honor. Two consequences run
through this module. A container is told things through **its PID 1's
environment**, because that is what its own startup scripts read; a drop-in
written for a network manager the image does not run is a file nobody opens.
And a container is stopped with **`SIGKILL`**: s6 treats a gentler signal as
advisory, returns from it with its supervisors still running, and they hold the
machine's control group open until the next start fails on it. What is expected
to bounce one is `SIGINT`, which `machinectl reboot` sends and s6 handles as a
reboot — an expectation physical/gateway.md §1 leaves to the first soak — so a
machine's environment may not carry the variable that would take that handler
away (`S6_CMD_RECEIVE_SIGNALS`).

**Host networking is the absence of a bridge.** Only a declaration built on a
bridged census entry can produce a bridge, so the overlay daemon — which must
be in the host's namespace for the gateway to route through the interface it
creates — cannot acquire one by accident.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from types import MappingProxyType
from typing import Annotated, final

import pulumi

from kluster import conventions
from kluster.components.gateway import nspawn
from kluster.components.gateway.nspawn import NspawnRuntime
from kluster.components.gateway.persistence import TEMPLATE_PACKAGE
from kluster.lib import templates
from kluster.lib.secret import Secret
from kluster.providers.device_files.provider import Connection, DeviceArtifact, DeviceFile
from putils import Component

__all__ = (
    'ADGUARD_INSTALL',
    'ADGUARD_STATE',
    'CADDY_CONFIG',
    'CADDY_CONFIG_HOME',
    'CADDY_RESOLV_CONF',
    'CADDY_STATE',
    'CADDY_TOKEN_PATH',
    'CAPABILITY',
    'CONFIG_MODE',
    'CONTAINER_BRIDGE',
    'ENV_IPV4_CIDR',
    'ENV_IPV4_GATEWAY',
    'ENV_IPV6_TOKEN',
    'KILL_SIGNAL',
    'OVERLAY_DAEMON_CONF',
    'OVERLAY_DAEMON_SETTINGS',
    'OVERLAY_NETWORKS',
    'OVERLAY_SETTINGS',
    'OVERLAY_STATE',
    'S6_CMD_RECEIVE_SIGNALS',
    'S6_KILL_GRACETIME',
    'SECRET_MODE',
    'TUN_DEVICE',
    'BridgedDeclaration',
    'CaddyService',
    'Container',
    'ContainerDeclaration',
    'InitialState',
    'InterceptedSignals',
    'MountedFile',
    'OverlayDaemon',
    'ResolverService',
    'Rootfs',
    'ServiceDeclaration',
    'caddyfile',
    'mounted_path',
    'net_setup_environment',
    'nspawn_file',
    'overlay_daemon_conf',
    'overlay_settings',
    'resolv_conf',
)

CONFIG_MODE = '0644'
SECRET_MODE = '0600'

# ---------------------------------------------------------------------------
# What the images are
# ---------------------------------------------------------------------------

#: The resolvers' own working directory, bind-mounted from the device so that a
#: digest bump replaces the software and keeps the configuration.
#:
#: The working directory is the image's, not a path of this program's choosing:
#: the resolver is started with `-w /data/adguard -c /data/adguard/AdGuardHome.yaml`
#: and its installation lives at `ADGUARD_INSTALL`, which the tree carries and a
#: digest bump replaces. Splitting them that way is what makes the software
#: disposable and the configuration durable, so the bind lands on the state half.
#: The configuration file is the instance's own from its first start, and its
#: absence is what puts a fresh instance in first run (`ResolverService`).
ADGUARD_STATE = '/data/adguard'
ADGUARD_INSTALL = '/opt/AdGuardHome'

#: Where caddy looks for its configuration and where it keeps what it must not
#: lose. Both are directories the image names through the environment rather than
#: paths this program picks: the server is started with
#: `--config $XDG_CONFIG_HOME/caddy/Caddyfile`, and it places the certificates
#: and account keys it buys under `XDG_DATA_HOME`. Naming those two is therefore
#: how this program decides which file is read and which directory outlives a
#: rootfs bump — the data half is bind-mounted from the device, so the software
#: is replaceable and the credential it earned is not. `/etc` is the config home
#: because the image's `$XDG_CONFIG_HOME/caddy/Caddyfile` then resolves to
#: `/etc/caddy/Caddyfile`, which is where a reader looks for it and where the
#: token it reads is delivered too.
CADDY_CONFIG_HOME = '/etc'
CADDY_STATE = '/var/lib/caddy'
CADDY_CONFIG = f'{CADDY_CONFIG_HOME}/caddy/Caddyfile'

#: Where caddy reads the zone-scoped token it answers DNS-01 challenges with.
#: A device secret of its own, read by nothing else on the box.
CADDY_TOKEN_PATH = '/etc/caddy/cloudflare.token'

#: Where the image's resolver library reads which resolver to ask. Which one is
#: offered is a site fact rather than an image fact, so it is delivered
#: rather than baked: the image's own default is a public resolver, which
#: answers none of the internal names this proxy's upstreams have.
CADDY_RESOLV_CONF = '/etc/resolv.conf'

#: The device node the overlay daemon needs, and the state directory whose
#: contents are its identity on the overlay and the networks it joins: the
#: directory the image's run script starts the daemon against
#: (`zerotier-one /var/lib/zerotier-one`), so it moves only with the image.
TUN_DEVICE = '/dev/net/tun'
OVERLAY_STATE = '/var/lib/zerotier-one'

#: The directory under `OVERLAY_STATE` that holds one file per network the
#: daemon is a member of: `<network id>.conf`, which it joins at start and
#: rewrites with what its controller sends, and `<network id>.local.conf`,
#: the member's own settings for that network, which it reads when the
#: network comes up. Both names are the daemon's
#: (`OneService.cpp` at ZeroTier 1.16.2).
OVERLAY_NETWORKS = 'networks.d'

#: The gateway's own settings on the overlay, in the daemon's names. They
#: decide which of what the controller pushes the gateway installs, and the
#: controller cannot read or set them:
#:
#: - `allowManaged`: the managed addresses and routes in private scope, the
#:   gateway's overlay address among them.
#: - `allowGlobal`: the same in global scope, which is every IPv6 address
#:   outside `fc00::/7`, `fe80::/10` and `ff00::/8`. It is what lets the
#:   gateway take a global address the network assigns it, and the connected
#:   route to that address's prefix.
#: - `allowDefault` off: the gateway is the default route's next hop, never
#:   its user.
#: - `allowDNS` off, its default: a Linux member applies no pushed DNS
#:   either way (physical/gateway.md §2.7).
#:
#: Nothing global is installed until the network assigns a global address,
#: so the file admits nothing while the overlay carries IPv4 alone.
OVERLAY_SETTINGS: Mapping[str, bool] = MappingProxyType(
    {
        'allowManaged': True,
        'allowGlobal': True,
        'allowDefault': False,
        'allowDNS': False,
    }
)

#: The daemon's own settings file, at the root of `OVERLAY_STATE`: the name
#: and the place the daemon reads it from at start (`OneService.cpp` at
#: ZeroTier 1.16.2).
OVERLAY_DAEMON_CONF = 'local.conf'

#: The gateway's daemon settings, in the daemon's names, which pin it to one
#: UDP port: the port the census's WAN-local allow admits
#: (`conventions.overlay.UDM_PORT`). Each is read at start, as the
#: `local.conf` section of the daemon's `service/README.md` and its
#: `OneService.cpp` at 1.16.2 have it:
#:
#: - `primaryPort`: the port bound. The daemon exits rather than hunting for
#:   another when the bind fails, so its primary port is never one the
#:   allow does not name.
#: - `allowSecondaryPort` off: no secondary port at start. That port is for
#:   NATs that misbehave when two hosts behind them share an internal port,
#:   and the gateway is behind no NAT.
#: - `secondaryPort` set, which is what keeps it so after an outage. Once the
#:   daemon has been offline for two ping periods, or reads a clock jump as
#:   a wake while offline, it binds a random secondary port whatever
#:   `allowSecondaryPort` says, unless `secondaryPort` is set: that path asks
#:   only whether it is. With `allowSecondaryPort` off, the value is never
#:   bound. It is the primary port, `UDM_PORT`, so that a reading of it that
#:   did bind it would name no port beyond the one the allow admits. Setting
#:   it makes the daemon log `WARNING: using manually-specified secondary
#:   and/or tertiary ports. This can cause NAT issues.` each time it reads
#:   the file, which is expected here.
#: - `portMappingEnabled` off: no UPnP or NAT-PMP mapping, and with it no
#:   tertiary port, which exists only for that mapping. The device runs no
#:   such service, so the port mapped nothing. No tertiary port is named,
#:   since nothing binds one while the mapping is off.
OVERLAY_DAEMON_SETTINGS: Mapping[str, int | bool] = MappingProxyType(
    {
        'primaryPort': conventions.overlay.UDM_PORT,
        'allowSecondaryPort': False,
        'secondaryPort': conventions.overlay.UDM_PORT,
        'portMappingEnabled': False,
    }
)

#: The device bridge a service on the container VLAN attaches to. It is that
#: VLAN's bridge — the container services are what the VLAN exists for.
CONTAINER_BRIDGE = 'br5'

#: The capability every image here configures its own interface with. It is
#: load-bearing for the machine in the host's network namespace: a machine with
#: a private network keeps `CAP_NET_ADMIN` in nspawn's default set anyway, so
#: naming it changes nothing for a bridged machine and is what a host-networked
#: one needs stated. A constant rather than a literal in the template because
#: the alternative the device ran before this program owned the mechanism was
#: `all`, and narrowing it is a decision worth finding.
CAPABILITY = 'CAP_NET_ADMIN'

#: How a container is stopped, and the same instruction restated on the inside.
#: s6 treats a shutdown signal as advisory: it returns from the signal with its
#: supervisors still running, they keep the machine's control group populated,
#: and the next start fails against a group that never emptied. `SIGKILL` from
#: the outside ends that, and a zero grace time keeps the supervision tree from
#: waiting on anything on its way down.
KILL_SIGNAL = 'SIGKILL'
S6_KILL_GRACETIME = 0

#: The one s6 variable a machine's environment may never carry, refused by
#: `nspawn_file` rather than left to be noticed. `machinectl reboot` is the verb
#: expected to bounce a machine on this device -- an expectation
#: physical/gateway.md §1 leaves to the first soak -- and it rests on
#: s6-linux-init handling `SIGINT` as a reboot. With a non-zero value here,
#: s6-overlay's `stage0` renames the `.s6-svscan` signal handlers aside --
#: `SIGINT` among them -- and installs its own forwarder instead, so the verb
#: would bounce nothing and nothing would fail to say so.
S6_CMD_RECEIVE_SIGNALS = 'S6_CMD_RECEIVE_SIGNALS'

#: What an image's own network setup reads out of PID 1's environment: the
#: address with its prefix, the router, and the token that fixes the interface
#: half of the v6 address so that a delegated prefix changing underneath does
#: not move the resolver every lease on the LAN points at. The names belong to
#: the images (the AdGuard `net-setup` service), not to this program.
ENV_IPV4_CIDR = 'IPV4_CIDR'
ENV_IPV4_GATEWAY = 'IPV4_GATEWAY'
ENV_IPV6_TOKEN = 'IPV6_TOKEN'


# ---------------------------------------------------------------------------
# What a service is made of
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True)
class Rootfs:
    """A root filesystem built by continuous integration, pinned by digest.

    The digest is the pin, and the repository and tag are only where those
    bytes were found: bumping any of them is a previewed, reviewable deployment
    event, which is the whole reason images are built elsewhere and referenced
    here. What the device receives is the reference and nothing else — it pulls
    the image itself and unpacks it into the directory `systemd-nspawn` boots
    (`device_files.provider`).
    """

    repository: str
    tag: str
    digest: str


@final
@dataclass(frozen=True)
class MountedFile:
    """A file written on the device and bind-mounted into the container.

    `target` is the path inside the container the image reads it at; the file
    itself sits in the machine's own directory, so the image stays the software
    and the configuration stays declarable. It is mounted read-only and it is
    this program's on every deployment, which is what separates it from an
    initial-state file.

    `content` does not print. With `secret` set it holds a credential the image
    reads (the ACME token caddy answers DNS-01 challenges with); the stack
    hands it over as an `Output`, which discloses nothing, but a test hands
    over the literal, and the field is hidden for what it holds rather than for
    how one of its types prints.
    """

    name: str
    target: str
    content: Annotated[pulumi.Input[str], Secret] = field(repr=False, compare=False)
    secret: bool = False


@final
@dataclass(frozen=True)
class InitialState:
    """A file placed into a service's own state, once, when it has none.

    The difference from a `MountedFile` is who owns the file afterwards. A
    mounted file is this program's, rewritten on every deployment; an
    initial-state file becomes the service's the moment it is placed, because
    the software behind it rewrites it — the overlay daemon writing what its
    controller sends into its network file, for instance. It is therefore
    delivered to the device, copied into the state directory only while that
    directory is empty, and left alone afterwards; it is not bind-mounted and
    it is not in the content stamp, because a change to it can never be a
    reason to restart something that has already made the file its own.

    One name and not two: what keeps an initial-state file from overwriting the
    live file is the directory it is delivered into
    (`nspawn.initial_state_path`), which holds nothing but initial state and is
    the shape the converger copies. So the name here is the path the file lands
    at inside the state directory, and the device needs to be told nothing else
    about it.

    `content` does not print, on the same rule as `MountedFile.content`: what
    an initial state holds is a service's own configuration, and the type
    carries no promise that it arrives as an `Output`.
    """

    #: Where it lands, relative to the service's state directory — and equally
    #: where it is delivered, relative to the machine's initial-state directory.
    into: str
    content: Annotated[pulumi.Input[str], Secret] = field(repr=False, compare=False)


@dataclass(frozen=True)
class ContainerDeclaration[S: conventions.gateway.ContainerService]:
    """One service, as the gateway declares it: its census entry and its image.

    The census entry is held rather than named, so the binding is a reference
    the type checker follows instead of a string looked up at runtime. What the
    image needs of its machine is stated by the subclass below that stands for
    it; the defaults here are what a service needs when it needs nothing special
    — no writable state, no device node, nothing injected, nothing mounted.
    """

    service: S
    pin: Rootfs

    @property
    def state(self) -> str | None:
        """The directory inside the container bind-mounted from the device."""
        return None

    @property
    def devices(self) -> tuple[str, ...]:
        """Device nodes the container needs, bound into the machine."""
        return ()

    @property
    def environment(self) -> Mapping[str, str]:
        """What the machine's settings put into the container's PID 1."""
        return {}

    @property
    def mounted_files(self) -> tuple[MountedFile, ...]:
        return ()

    @property
    def initial_state(self) -> InitialState | None:
        return None

    @property
    def bridge(self) -> str | None:
        """The device bridge the machine attaches the container to.

        `None` is the host's own network namespace, which is what nspawn does
        when it is given no bridge at all.
        """
        return None

    @property
    def state_bind(self) -> str | None:
        """The bind the machine keeps the service's writable state through."""
        return None if self.state is None else f'{nspawn.state_path(self.service.name)}:{self.state}'

    @property
    def unit_name(self) -> str:
        """The unit that runs this service, which is systemd's own template."""
        return nspawn.machine_unit(self.service.name)


@dataclass(frozen=True)
class BridgedDeclaration(ContainerDeclaration[conventions.gateway.BridgedService]):
    """A service on the container VLAN, holding an address there.

    Being built on a bridged census entry is the whole of it: that is where the
    address comes from, and it is why the machine can name a bridge at all.
    """

    @property
    def bridge(self) -> str:
        return CONTAINER_BRIDGE


@final
@dataclass(frozen=True)
class CaddyService(BridgedDeclaration):
    """The gateway's reverse proxy, and its own TLS issuer.

    `acme_token` is the zone-scoped credential it answers DNS-01 challenges
    with — a device secret, separate from the cluster's issuer on purpose, so
    that two issuers which must survive each other's outage do not share a
    credential (gateway.md §1). It does not print: the stack hands it over as
    an `Output`, which discloses nothing, but a test hands over the literal,
    and the field is hidden for what it holds rather than for how one of its
    types prints.

    Its address is injected, the same way the resolvers take theirs: the image
    carries the `net-setup` the AdGuard pair has, the proxy is ordered after it,
    and that oneshot exits non-zero when the addressing is not in its
    environment. So the address the census holds for caddy is the address it
    holds — the one a rewrite has to name — rather than one the design merely
    intends, and a machine that failed to deliver it stops the proxy instead of
    starting it somewhere else.

    Two directories come with it, and they are the image's names rather than
    paths chosen here: `XDG_CONFIG_HOME` is where it reads its `Caddyfile` and
    `XDG_DATA_HOME` where it keeps the certificates it must not lose.

    **It resolves through the gateway's own resolver**, delivered as a third
    mounted file. That is the one resolver that answers both halves of what the
    proxy asks — it is authoritative for the internal zone its upstreams are
    named in, and it forwards the rest, so the issuance calls to the registrar's
    API resolve as well — and it is not the resolver pair the proxy fronts, so
    the proxy still does not depend on what it serves. The address is the
    container VLAN's gateway, the same one the image's network setup is handed
    as its default route, so the two cannot disagree.

    **What it serves arrives with the declaration.** The proxy holds no roll of
    its own and looks nothing up: `vhosts` are the services it fronts, each
    holding the name it answers under and the address it answers at, and
    `legacy` is the census of names it still serves for applications that have
    not migrated. Both are the entries themselves rather than keys into a
    table, so the binding is one the type checker follows (rfc-002 §5.3), and
    the file this renders changes when the census does and at no other time.
    """

    acme_token: Annotated[pulumi.Input[str], Secret] = field(repr=False, compare=False)
    vhosts: tuple[conventions.gateway.BridgedService, ...]
    legacy: tuple[conventions.gateway.LegacyVhost, ...]

    @property
    def state(self) -> str:
        return CADDY_STATE

    @property
    def environment(self) -> Mapping[str, str]:
        return {
            'XDG_CONFIG_HOME': CADDY_CONFIG_HOME,
            'XDG_DATA_HOME': CADDY_STATE,
            'HOME': CADDY_STATE,
            **net_setup_environment(self.service.address),
        }

    @property
    def mounted_files(self) -> tuple[MountedFile, ...]:
        return (
            MountedFile(name='Caddyfile', target=CADDY_CONFIG, content=caddyfile(self)),
            MountedFile(name='cloudflare.token', target=CADDY_TOKEN_PATH, content=self.acme_token, secret=True),
            MountedFile(name='resolv.conf', target=CADDY_RESOLV_CONF, content=resolv_conf()),
        )


@final
@dataclass(frozen=True)
class ResolverService(BridgedDeclaration):
    """One AdGuard Home instance: half of the LAN's name service.

    Its address is delivered where the image reads it — the `net-setup` service
    configures the interface out of PID 1's environment before the resolver it
    guards is allowed to start, so a resolver never comes up on an address the
    LAN was not told about.

    **Its configuration is its own, and nothing here gives it one.** An
    instance whose working directory holds no configuration file starts in
    first run, and first run is the one state the `dns` stack's setup
    configures: it makes the account and binds the listen addresses through
    the instance's own wizard, and then writes the rest through the API
    (dns.md §3). So the machine is given no initial state. A configuration
    file placed there would be one with no account in it, which the setup
    refuses until the instance is reset, and its listen addresses would be
    this program's rather than the ones the `dns` stack declares.
    """

    @property
    def state(self) -> str:
        return ADGUARD_STATE

    @property
    def environment(self) -> Mapping[str, str]:
        return net_setup_environment(self.service.address)


@final
@dataclass(frozen=True)
class OverlayDaemon(ContainerDeclaration[conventions.gateway.HostNetworkService]):
    """The container that carries the overlay membership.

    **It runs in the host's own network namespace, and must.** The daemon
    creates an interface when it joins and the gateway routes through that
    interface; an interface created inside a private namespace is invisible to
    the router that has to use it, so the member would join and route nothing.
    Being built on the host-networked census entry is what states that: there is
    no address, so there is no bridge, so nspawn leaves it in the host's
    namespace (rfc-002 §4.1).

    Nothing is injected into its environment: the daemon is started with its
    state directory as its only argument, and it reaches its roots by literal
    address. What it needs of its machine is the tunnel device, the state
    bind that is its identity on the overlay, its own settings, and its
    settings on the network.

    **The network it joins is an initial state.** At start the daemon joins
    exactly the networks named by the files in `networks.d/` of that directory,
    one `<network id>.conf` each, and nothing else in the image joins one. So
    the overlay's network is declared there, as an empty file named for
    `conventions.overlay.NETWORK_ID` — the id the stack hands the overlay
    component to adopt the network by, so the daemon asks to join the network
    whose members that component authorizes. Empty is enough:
    the daemon writes the configuration its controller sends into that same
    file, and starts from a request for one when what it finds there does not
    parse. That rewrite is why the file is an initial state and not a mounted
    one: it is the daemon's from its first start, and the daemon deletes it if
    it ever leaves. Like every initial state it is placed only into a state
    directory that holds nothing, so a daemon that has already run keeps the
    identity and the memberships it has.

    **Its settings on that network are a mounted file**, the
    `<network id>.local.conf` beside the membership (`OVERLAY_SETTINGS`), and
    this program's on every deployment. The daemon reads the file when the
    network comes up and at no other time, so a change takes a restart, which
    the file's place in the machine's directory gives it: it is in the content
    stamp. The daemon writes the file in one place, when its control API is
    asked to change a setting of the network (`zerotier-cli set`, or a
    repeated `zerotier-cli join`), and deletes it when it leaves the network.
    Bound read-only, both fail and the daemon carries on: the write leaves
    the change in memory alone, and the next start reads the declaration
    again. So nothing on the device contends with the declaration, and a
    setting changed by hand lasts until the machine restarts.

    **Its own settings are a mounted file too**, `local.conf` at the root of
    its home (`OVERLAY_DAEMON_SETTINGS`), which pin it to one UDP port, the one
    the census's WAN-local allow admits, at start and after an outage alike. It
    binds its ports at start, so a change takes a restart, and the content
    stamp gives it one. It writes the file in one place, its control API's
    `POST /config`, which nothing on the device sends; bound read-only, that
    write fails, and the declaration holds.

    Each bind lands inside the state bind, and that holds because nspawn
    mounts a machine's binds in the order of their targets, a directory
    before what is under it (`custom_mount_prepare_all` in systemd 257, the
    device's). Mounting one leaves an empty file at its target in the state
    directory, after the initial state is placed, and the daemon reads an
    empty file as its defaults.
    """

    @property
    def state(self) -> str:
        return OVERLAY_STATE

    @property
    def devices(self) -> tuple[str, ...]:
        return (TUN_DEVICE,)

    @property
    def mounted_files(self) -> tuple[MountedFile, ...]:
        settings = f'{conventions.overlay.NETWORK_ID}.local.conf'
        return (
            MountedFile(
                name=settings, target=f'{OVERLAY_STATE}/{OVERLAY_NETWORKS}/{settings}', content=overlay_settings()
            ),
            MountedFile(
                name=OVERLAY_DAEMON_CONF, target=f'{OVERLAY_STATE}/{OVERLAY_DAEMON_CONF}', content=overlay_daemon_conf()
            ),
        )

    @property
    def initial_state(self) -> InitialState:
        # `into` is relative to the state directory, which is bind-mounted at
        # `OVERLAY_STATE` -- the directory the daemon is started against.
        return InitialState(into=f'{OVERLAY_NETWORKS}/{conventions.overlay.NETWORK_ID}.conf', content='')


#: One service's declaration, whichever image it is for. A fifth service is a
#: new type here and a new parameter on `Gateway`, not a key in a mapping a loop
#: may or may not look up (rfc-002 §5.3).
ServiceDeclaration = CaddyService | ResolverService | OverlayDaemon


# ---------------------------------------------------------------------------
# Where one service's files sit
# ---------------------------------------------------------------------------


def mounted_path(service: str, mounted: MountedFile) -> str:
    """Where one file the container mounts sits, inside the machine's directory.

    Secrecy is the file's mode and the resource's own marking, not a directory
    of its own: what a reader needs to find is the whole of a machine in one
    place.
    """
    return nspawn.machine_file(service, mounted.name)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True)
class _MachineParams:
    """What `machine.nspawn.j2` reads.

    Every conditional part is already decided here, as the thing itself or as
    `None`: `bridge` is the container VLAN's bridge unless the service runs in
    the host's namespace, and `state_bind` and `binds` are whole bind arguments
    rather than pairs the file has to assemble.
    """

    cluster: str
    name: str
    capability: str
    kill_signal: str
    kill_gracetime: int
    environment: Mapping[str, str]
    bridge: str | None
    host_network: bool
    state_bind: str | None
    devices: tuple[str, ...]
    binds: tuple[str, ...]


@final
class InterceptedSignals(Exception):
    """A machine's environment would take s6's signal handlers away from it.

    `machinectl reboot` is the verb expected to bounce a machine on this device
    — an expectation physical/gateway.md §1 leaves to the first soak — and it
    rests on s6 keeping its own `SIGINT` handler. Losing that handler is the one
    casualty that would not announce itself, so the variable is refused where
    every machine's environment is rendered rather than recorded in a comment
    (`S6_CMD_RECEIVE_SIGNALS`).
    """

    def __init__(self, machine: str) -> None:
        super().__init__(
            f'refusing to render {machine}: its environment carries {S6_CMD_RECEIVE_SIGNALS}, which '
            f"replaces s6's own SIGINT handler -- the one `machinectl reboot` is expected to bounce this machine through"
        )
        self.machine: str = machine


def nspawn_file(declaration: ServiceDeclaration) -> str:
    """The settings that decide what one machine is when systemd starts it.

    There is no unit to write: `systemd-nspawn@.service` is systemd's own
    template and it reads this file, so what a machine says about itself is
    said once and in the place `machinectl` and `systemctl` already look.

    `S6_KILL_GRACETIME` is the only s6 variable this program sets, and
    `S6_CMD_RECEIVE_SIGNALS` is the one it refuses: a declaration carrying it
    fails here rather than producing a machine the reboot verb no longer
    bounces.
    """
    service = declaration.service.name
    if S6_CMD_RECEIVE_SIGNALS in declaration.environment:
        raise InterceptedSignals(service)
    bridge = declaration.bridge
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/machine.nspawn.j2',
        _MachineParams(
            cluster=conventions.CLUSTER_NAME,
            name=service,
            capability=CAPABILITY,
            kill_signal=KILL_SIGNAL,
            kill_gracetime=S6_KILL_GRACETIME,
            environment=declaration.environment,
            bridge=bridge,
            host_network=bridge is None,
            state_bind=declaration.state_bind,
            devices=declaration.devices,
            binds=tuple(f'{mounted_path(service, mounted)}:{mounted.target}' for mounted in declaration.mounted_files),
        ),
    )


def net_setup_environment(address: IPv4Address) -> dict[str, str]:
    """The addressing an image's own network setup reads from PID 1's environment.

    The address is configured rather than learned because the resolvers are
    what hands out the leases' name servers: a resolver that waited for a lease
    to learn its own address would be waiting on itself. The gateway cannot
    reserve one for it either — its controller does not manage clients on the
    device's own bridge — so what would arrive is a lease, not this address.

    The v6 half is a token rather than an address: the interface identifier is
    fixed here and the prefix keeps arriving in router advertisements, so the
    resolver stays at the same v6 address across a delegated prefix changing
    underneath it. Deriving the token from the v4 address is what makes the two
    legible as one host.
    """
    return {
        ENV_IPV4_CIDR: f'{address}/{conventions.CONTAINER_VLAN.v4.prefixlen}',
        ENV_IPV4_GATEWAY: str(conventions.CONTAINER_VLAN.require_gateway()),
        ENV_IPV6_TOKEN: f'::{address}',
    }


@final
@dataclass(frozen=True)
class _CaddyParams:
    """What `Caddyfile.j2` reads.

    Both rolls arrive as the declaration was handed them, so the file names
    each service's own vhost and each legacy row's own upstream without a
    second list to keep in step.
    """

    acme_contact: str
    zone: str
    controller: str
    controller_upstream: conventions.gateway.SelfSignedUpstream
    token_path: str
    api_port: int
    vhosts: tuple[conventions.gateway.BridgedService, ...]
    legacy_zone: str
    legacy: tuple[conventions.gateway.LegacyVhost, ...]


def caddyfile(declaration: CaddyService) -> str:
    """The gateway's vhosts, under one wildcard certificate it issues itself.

    The certificate is obtained through a DNS-01 challenge with a token that
    lives on the device — separate from the cluster's issuer on purpose, so
    that two issuers which must survive each other's outage do not share a
    credential (gateway.md §1). Two properties of *which* certificate follow
    from that separation (rfc-002 §9.3):

    -   **One wildcard, not a certificate per name.** These names resolve
        nowhere publicly (dns.md §4), and every issued certificate is published
        in Certificate Transparency logs, so per-name issuance would republish
        exactly the census that resolving nowhere hides.
    -   **The wildcard alone, never the apex.** Let's Encrypt counts its
        duplicate-certificate limit by identifier set across accounts, so two
        issuers asking for the same set share one weekly window and a
        crash-looping renewal on either side locks the other out. The cluster
        issuer's certificate carries the apex and the wildcard together; the
        gateway serves none of the public names the apex answers for, so it
        asks for less and the two sets stay different.

    One site block therefore serves every name in that zone, matching each
    vhost inside it by host and refusing everything else — a name under the
    zone that nothing here serves gets the connection closed rather than an
    answer from whichever block happened to be first. A wildcard covers one
    label, so every name the gateway serves has to be one label under the zone;
    that is a property of the census and `test_conventions` holds it there.

    The controller console is reverse-proxied to the device's own leg on the
    container VLAN over a connection whose certificate cannot be verified,
    because the certificate it presents is the device's self-signed one. Caddy
    forwards the client's `Host` unchanged, and the block names it anyway: the
    console answers a WebSocket upgrade whose `Origin` does not match the
    request's `Host` with a 500, and the requirement is worth a line where a
    reader meets it.

    **No site block asks a resolver of its own about the challenge.** The proxy
    asks one resolver for everything it names — its `home.arpa` upstreams, the
    registrar's API, the ACME directory, and the zone and the name servers of
    the zone being challenged — and the challenge record itself is then verified
    against that zone's authoritative servers, so no forwarder's cache is in
    that path. Naming a resolver here is what would put one there: given one,
    the client stops asking the authoritative servers and asks that resolver's
    cache instead, which holds the answer it gave before the record was written
    for the zone's negative TTL.

    **Three global options open the file.** The contact
    (`conventions.gateway.ACME_CONTACT`) is what the account in the state
    directory is loaded by, and its value is not free to change. `debug` is
    permanent: the ACME path's record of which servers a propagation check asked
    exists at no other level, and what it costs is a small share of a journal
    the device caps anyway. `admin off` because nothing here reconfigures the proxy
    through the API — a content change restarts the machine — so the endpoint is
    a listener with no user, and the consequence is that runtime reconfiguration
    is not available.

    **A second site block serves the retiring LAN zone**, under a second
    wildcard, and it is shaped the same way: one certificate, the census
    matched inside it by host, everything else refused. It is here because the
    device serves those names today and this file replaces what serves them in
    one window, before any of the applications behind them has moved
    (gateway.md §1.5). The block empties row by row and then is not rendered at
    all — an empty census is a file with one site block in it, and the second
    wildcard, the second zone in the token's scope and the redirects go
    together with it.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/Caddyfile.j2',
        _CaddyParams(
            acme_contact=conventions.gateway.ACME_CONTACT,
            zone=conventions.ZONE_PRIMARY,
            controller=conventions.gateway.VHOST_CONTROLLER,
            controller_upstream=conventions.gateway.CONTROLLER_UPSTREAM,
            token_path=CADDY_TOKEN_PATH,
            api_port=conventions.gateway.ADGUARD_API_PORT,
            vhosts=declaration.vhosts,
            legacy_zone=conventions.gateway.ZONE_LEGACY,
            legacy=declaration.legacy,
        ),
    )


@final
@dataclass(frozen=True)
class _ResolvParams:
    """What `resolv.conf.j2` reads: the one resolver the file may name."""

    cluster: str
    resolver: str


def resolv_conf() -> str:
    """Which resolver a container asks, for a container that cannot use the image's.

    Exactly one entry, because the images' resolver library asks every listed
    server in parallel and takes the first answer: a public resolver beside the
    site's own would answer the internal names with NXDOMAIN and win the race.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/resolv.conf.j2',
        _ResolvParams(
            cluster=conventions.CLUSTER_NAME,
            resolver=str(conventions.CONTAINER_VLAN.require_gateway()),
        ),
    )


def overlay_settings() -> str:
    """The gateway's settings on the overlay network, as its daemon reads them.

    One `name=value` line per setting, a boolean as `1` or `0`: the format the
    daemon writes the file in itself and parses with its own dictionary type.
    That format has no comment syntax, so the file carries no header naming
    who manages it; `OverlayDaemon` says why nothing else writes it.
    """
    return ''.join(f'{name}={int(value)}\n' for name, value in OVERLAY_SETTINGS.items())


def overlay_daemon_conf() -> str:
    """The gateway's daemon settings, as its daemon reads them.

    A JSON object whose `settings` member holds them: the format of the file
    the daemon parses at start, and of the one its control API writes. A
    file that does not parse stops the daemon rather than starting it on its
    defaults. JSON has no comment syntax, so the file carries no header naming
    who manages it; `OverlayDaemon` says why nothing else writes it.
    """
    return json.dumps({'settings': dict(OVERLAY_DAEMON_SETTINGS)}, indent=4) + '\n'


class Container(Component, pulumi_type='kluster:gateway:Container'):
    """One container service on the device, and every file that defines it.

    It owns its root filesystem artifact, its settings file, the files it mounts
    and its initial state where it has one — all of them in the machine's own
    directory — and that directory is also the whole of what the device is
    told: the converger carries no roll of machines and no per-machine case, so
    declaring this component is what makes the machine exist and dropping it is
    what retires one (`nspawn.machines_script`). The one fact it exposes is the
    unit that runs the machine.

    **Every file of the machine runs the runtime's hook once it lands**, which
    converges the machine and then holds it to having come up (`nspawn`). It is
    stated on every file rather than once on this component because Pulumi does
    not push a component's own `depends_on` down to its children — and a file
    whose hook runs a script the device has not been given yet fails its own
    apply, which is why the runtime's convergers are depended on here too.

    **The pieces are ordered so that the machine starts once, with all of
    them.** The files the container reads and the drop-in its unit carries come
    first, the settings file after them because it names those files as binds,
    and the root filesystem last: the converger skips a machine that has no
    tree, so the tree's own delivery is the one that starts it, and by then
    everything the machine needs is on the device. Unordered siblings would
    instead have each configuration file arrive at a machine that cannot start
    yet.

    `after` is what this machine must be actuated behind. It is a parameter
    rather than a fact of this class because every such constraint has a reason
    that is invisible from inside one machine: the machine carrying the
    deployment's own session must move last (rfc-002 §4.4), and of the site's
    resolvers no two may restart at once, which is a fact about the set of
    them (`Gateway`).
    """

    def __init__(
        self,
        name: str,
        *,
        declaration: ServiceDeclaration,
        runtime: NspawnRuntime,
        connection: Connection,
        after: Sequence[pulumi.Resource] = (),
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        service = declaration.service.name
        owner = conventions.gateway.SSH_USER
        child = self.child_opts(depends_on=[*runtime.convergers, *after])

        self.unit_name: str = declaration.unit_name

        self.mounted_files = {
            mounted.name: DeviceFile(
                f'{name}-file-{mounted.name}',
                connection=connection,
                path=mounted_path(service, mounted),
                content=mounted.content,
                mode=SECRET_MODE if mounted.secret else CONFIG_MODE,
                owner=owner,
                hook=runtime.hook(service, mounted_path(service, mounted)),
                secret=mounted.secret,
                opts=child,
            )
            for mounted in declaration.mounted_files
        }
        initial = declaration.initial_state
        # Under the machine's initial-state directory rather than beside the
        # files it mounts: that is what keeps the initial state out of the
        # content stamp, which covers the machine's directory itself, and what
        # lets the converger place it without being told where it goes.
        self.initial_state: DeviceFile | None = (
            None
            if initial is None
            else DeviceFile(
                f'{name}-initial-state',
                connection=connection,
                path=nspawn.initial_state_path(service, initial.into),
                content=initial.content,
                mode=CONFIG_MODE,
                owner=owner,
                hook=runtime.hook(service, nspawn.initial_state_path(service, initial.into)),
                opts=child,
            )
        )
        # What the machine's unit says that its settings file cannot: how the
        # machine is kept running, and what its start is ordered behind — the
        # bridge is mandatory because the settings file names it, not because
        # the unit does. It is not in the stamped set, because a drop-in takes
        # effect on the reload its own delivery does rather than on a start —
        # what the stamp decides is when the machine is bounced, and this is not
        # a reason to bounce one.
        self.dropin: DeviceFile = runtime.dropin(service, bridge=declaration.bridge, opts=child)

        before_the_machine_starts: list[pulumi.Resource] = [
            *self.mounted_files.values(),
            *([] if self.initial_state is None else [self.initial_state]),
            self.dropin,
        ]

        settings = nspawn.nspawn_path(service)
        self.settings = DeviceFile(
            f'{name}-nspawn',
            connection=connection,
            path=settings,
            content=nspawn_file(declaration),
            mode=CONFIG_MODE,
            owner=owner,
            hook=runtime.hook(service, settings),
            opts=self.child_opts(depends_on=[*runtime.convergers, *after, *before_the_machine_starts]),
        )
        root = nspawn.rootfs_path(service)
        self.image = DeviceArtifact(
            f'{name}-image',
            connection=connection,
            repository=declaration.pin.repository,
            tag=declaration.pin.tag,
            digest=declaration.pin.digest,
            root=root,
            # The one hook that may roll back: the tree is the only piece of a
            # machine the device keeps a displaced copy of, and this is the
            # delivery that starts the machine.
            hook=runtime.hook(service, root, rollback=True),
            opts=self.child_opts(depends_on=[*runtime.convergers, *after, *before_the_machine_starts, self.settings]),
        )

        self.register_outputs({})
