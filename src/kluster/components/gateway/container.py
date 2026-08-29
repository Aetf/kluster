"""One container service on the device: its root filesystem, unit, and files.

Everything a service is made of lives under `/data`, the one directory a
firmware update leaves alone (architecture.md §5.2): the root filesystem
archive and the tree unpacked from it, the unit, the configuration the image
reads, the per-service writable state, and — where the software behind it
rewrites its own configuration — one initial-state file.

**A service is declared by its own type.** What a service *is* — where it keeps
state, which device nodes it needs, which environment its image reads — is a
fact about its image, so it lives in that image's declaration type rather than
in a mapping every reader has to look up (rfc-002 §5.3). A declaration holds
the census entry it stands for rather than naming it, which is what makes a
resolver bound to a service with no address impossible to write.

**The images are Alpine with s6-overlay, not systemd.** They ship that init at
`/sbin/init` so that `systemd-nspawn --boot` finds it, and a unit here
therefore declares nothing that only a systemd guest would honour. Two
consequences run through this module. A container is told things through **its
PID 1's environment**, because that is what its own startup scripts read; a
drop-in written for a network manager the image does not run is a file nobody
opens. And a container is stopped with **`SIGKILL`**: s6 treats a gentler
signal as advisory, returns from it with its supervisors still running, and
they hold the unit's control group open until the next start fails on it.

**Host networking is the absence of a bridge**, not a switch: `systemd-nspawn`
shares the host's network namespace unless it is given one. Only a declaration
built on a bridged census entry can produce a bridge argument, so the overlay
daemon — which must be in the host's namespace for the gateway to route through
the interface it creates — cannot acquire one by accident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import final

import pulumi

from kluster import conventions
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceArtifact, DeviceFile
from putils import Component

__all__ = (
    'ADGUARD_INITIAL_STATE',
    'ADGUARD_INSTALL',
    'ADGUARD_STATE',
    'ADGUARD_UPSTREAMS',
    'CADDY_CONFIG',
    'CADDY_CONFIG_HOME',
    'CADDY_STATE',
    'CADDY_TOKEN_PATH',
    'CONFIG_DIR',
    'CONFIG_MODE',
    'CONTAINER_BRIDGE',
    'ENV_IPV4_CIDR',
    'ENV_IPV4_GATEWAY',
    'ENV_IPV6_TOKEN',
    'IMAGE_DIR',
    'IMAGE_MODE',
    'KILL_SIGNAL',
    'OVERLAY_STATE',
    'ROOT_DIR',
    'S6_KILL_GRACETIME',
    'SECRET_DIR',
    'SECRET_MODE',
    'SERVICES_ROOT',
    'STATE_DIR',
    'TEMPLATE_PACKAGE',
    'TUN_DEVICE',
    'UNIT_DIR',
    'UNIT_PREFIX',
    'BridgedDeclaration',
    'CaddyService',
    'Container',
    'ContainerDeclaration',
    'InitialState',
    'MountedFile',
    'OverlayDaemon',
    'ResolverService',
    'Rootfs',
    'ServiceDeclaration',
    'adguard_initial_state',
    'bridge_device_unit',
    'caddyfile',
    'config_path',
    'image_path',
    'mounted_path',
    'net_setup_environment',
    'root_path',
    'secret_path',
    'state_path',
    'unit_file',
)

#: The package `importlib.resources` resolves the `templates/` directory
#: against, so the rendered files travel with the code that renders them
#: (rfc-002 §9.1). It is the package both this module and `services` render
#: from, which is why it is stated once.
TEMPLATE_PACKAGE = 'kluster.components.gateway'

# ---------------------------------------------------------------------------
# Where things live on the device
# ---------------------------------------------------------------------------

#: The services' root. Everything below it survives a firmware update because
#: `/data` does; everything outside it is re-materialized from here at boot.
SERVICES_ROOT = f'{conventions.gateway.DATA_ROOT}/services'
#: The pinned artifacts as they are published: one root filesystem tarball per
#: service, with the digest marker the artifact resource keeps beside each.
IMAGE_DIR = f'{SERVICES_ROOT}/images'
#: The trees unpacked from those tarballs, which are what the units boot. A tree
#: is derived state the push owns: it is replaced whole when the pin moves and
#: nothing in it is worth keeping, which is why per-service writable state is
#: bind-mounted from `STATE_DIR` instead of living here.
ROOT_DIR = f'{SERVICES_ROOT}/roots'
UNIT_DIR = f'{SERVICES_ROOT}/units'
CONFIG_DIR = f'{SERVICES_ROOT}/config'
SECRET_DIR = f'{SERVICES_ROOT}/secrets'
#: Per-service writable state, bind-mounted in. Kept out of the image so a
#: digest bump replaces the software and keeps the identity — which for the
#: overlay daemon is the difference between a reboot and a new node address.
STATE_DIR = f'{SERVICES_ROOT}/state'

#: Unit names are prefixed so the recovery script can tell this program's units
#: from the device's own and retire only its own.
UNIT_PREFIX = 'kluster-'

CONFIG_MODE = '0644'
SECRET_MODE = '0600'
#: A root filesystem is not secret, but it is also nobody's to read. This is the
#: mode of the tarball; what the tree gets is whatever the archive carries, which
#: is the container's own idea of its permissions and not this program's.
IMAGE_MODE = '0600'

# ---------------------------------------------------------------------------
# What the images are
# ---------------------------------------------------------------------------

#: The resolvers both AdGuard instances forward to. Two providers on purpose:
#: the LAN's name service must not fail with any single one of them.
ADGUARD_UPSTREAMS = ('https://dns.quad9.net/dns-query', 'https://dns.cloudflare.com/dns-query')

#: The resolvers' own working directory, bind-mounted from the device so that a
#: digest bump replaces the software and keeps the configuration; and the name
#: the initial state is delivered under, which is deliberately not the name the
#: instance reads, so that delivering one can never overwrite the other.
#:
#: The working directory is the image's, not a path of this program's choosing:
#: the resolver is started with `-w /data/adguard -c /data/adguard/AdGuardHome.yaml`
#: and its installation lives at `ADGUARD_INSTALL`, which the tree carries and a
#: digest bump replaces. Splitting them that way is what makes the software
#: disposable and the configuration durable, so the bind lands on the state half.
ADGUARD_STATE = '/data/adguard'
ADGUARD_INSTALL = '/opt/AdGuardHome'
ADGUARD_INITIAL_STATE = 'AdGuardHome.initial.yaml'

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
CADDY_TOKEN_PATH = '/etc/caddy/cloudflare.token'  # noqa: S105 -- a path, not a credential

#: The device node the overlay daemon needs, and the state directory whose
#: contents are its identity on the overlay.
TUN_DEVICE = '/dev/net/tun'
OVERLAY_STATE = '/var/lib/zerotier-one'

#: The device bridge a service on the container VLAN attaches to. It is that
#: VLAN's bridge — the container services are what the VLAN exists for.
CONTAINER_BRIDGE = 'br5'

#: How a container is stopped, and the same instruction restated on the inside.
#: s6 treats a shutdown signal as advisory: it returns from the signal with its
#: supervisors still running, they keep the unit's control group populated, and
#: the next start fails against a group that never emptied. `SIGKILL` from the
#: outside ends that, and a zero grace time keeps the supervision tree from
#: waiting on anything on its way down.
KILL_SIGNAL = 'SIGKILL'
S6_KILL_GRACETIME = 0

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

    The digest is the pin and the URL is only where the bytes were found:
    bumping either is a previewed, reviewable deployment event, which is the
    whole reason images are built elsewhere and referenced here.
    """

    url: str
    sha256: str


@final
@dataclass(frozen=True)
class MountedFile:
    """A file written on the device and bind-mounted into the container.

    `target` is the path inside the container the image reads it at; the file
    itself sits beside the other desired state, so the image stays the software
    and the configuration stays declarable. It is mounted read-only and it is
    this program's on every deployment, which is what separates it from an
    initial-state file.
    """

    name: str
    target: str
    content: pulumi.Input[str]
    secret: bool = False


@final
@dataclass(frozen=True)
class InitialState:
    """A file placed into a service's own state, once, when it has none.

    The difference from a `MountedFile` is who owns the file afterwards. A
    mounted file is this program's, rewritten on every deployment; an
    initial-state file becomes the service's the moment it is placed, because
    the software behind it rewrites it — a resolver accepting a rewrite through
    its API, for instance. It is therefore delivered to the device, copied into
    the state directory only when that directory holds none, and left alone
    afterwards; it is not bind-mounted and it is not in the content stamp,
    because a change to it can never be a reason to restart something that has
    already made the file its own.
    """

    #: The name it is delivered under, deliberately not the name the software
    #: reads, so that delivering one can never overwrite the live file.
    name: str
    #: The name it is installed as, inside the service's state directory.
    into: str
    content: pulumi.Input[str]


@dataclass(frozen=True)
class ContainerDeclaration[S: conventions.gateway.ContainerService]:
    """One service, as the gateway declares it: its census entry and its image.

    The census entry is held rather than named, so the binding is a reference
    the type checker follows instead of a string looked up at runtime. What the
    image needs of its unit is stated by the subclass below that stands for it;
    the defaults here are what a service needs when it needs nothing special —
    no writable state, no device node, nothing injected, nothing mounted.
    """

    service: S
    pin: Rootfs

    @property
    def state(self) -> str | None:
        """The directory inside the container bind-mounted from the device."""
        return None

    @property
    def devices(self) -> tuple[str, ...]:
        """Device nodes the container needs, bound in and asserted by the unit."""
        return ()

    @property
    def environment(self) -> Mapping[str, str]:
        """What the unit puts into the container's PID 1."""
        return {}

    @property
    def mounted_files(self) -> tuple[MountedFile, ...]:
        return ()

    @property
    def initial_state(self) -> InitialState | None:
        return None

    @property
    def bridge(self) -> str | None:
        """The device bridge the unit attaches the container to.

        `None` is the host's own network namespace, which is what nspawn does
        when it is given no bridge at all.
        """
        return None

    @property
    def state_bind(self) -> str | None:
        """The `--bind` the unit keeps the service's writable state through."""
        return None if self.state is None else f'{state_path(self.service.name)}:{self.state}'

    @property
    def unit_name(self) -> str:
        return f'{UNIT_PREFIX}{self.service.name}.service'

    @property
    def stamped_set(self) -> tuple[str, ...]:
        """The paths the service's content stamp covers (rfc-002 §4.2).

        The unit, the digest marker of the root filesystem tree, and every file
        the container mounts: change one of them and the recovery script
        restarts the service, change nothing and it does not.

        The root filesystem is represented by the marker beside its *tree*
        rather than by the tree itself: walking a root filesystem to notice it
        is unchanged would cost more than the restart it saves. The tree's
        marker rather than the archive's, because the archive's is written
        after the hook has already run — a service that waited for it would
        learn of a new root filesystem one deployment late.
        """
        return (
            f'{UNIT_DIR}/{self.unit_name}',
            f'{root_path(self.service.name)}.digest',
            *(mounted_path(self.service.name, mounted) for mounted in self.mounted_files),
        )


@dataclass(frozen=True)
class BridgedDeclaration(ContainerDeclaration[conventions.gateway.BridgedService]):
    """A service on the container VLAN, holding an address there.

    Being built on a bridged census entry is the whole of it: that is where the
    address comes from, and it is why the unit can name a bridge at all.
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
    credential (gateway.md §1).

    Its address is *not* injected: this image asks for a lease
    (`/etc/network/interfaces`), and the lease is also where its resolver comes
    from, so an address injected the way the resolvers take theirs would be read
    by nothing and a static one imposed over that file would take the name
    service with it. The address the census holds for caddy is therefore the
    address the design intends — the one a rewrite has to name — and delivering
    it is work in the image, which needs the `net-setup` the AdGuard pair
    already has plus a resolver that does not depend on these services.
    """

    acme_token: pulumi.Input[str]

    @property
    def state(self) -> str:
        return CADDY_STATE

    @property
    def environment(self) -> Mapping[str, str]:
        return {
            'XDG_CONFIG_HOME': CADDY_CONFIG_HOME,
            'XDG_DATA_HOME': CADDY_STATE,
            'HOME': CADDY_STATE,
        }

    @property
    def mounted_files(self) -> tuple[MountedFile, ...]:
        return (
            MountedFile(name='Caddyfile', target=CADDY_CONFIG, content=caddyfile()),
            MountedFile(name='cloudflare.token', target=CADDY_TOKEN_PATH, content=self.acme_token, secret=True),
        )


@final
@dataclass(frozen=True)
class ResolverService(BridgedDeclaration):
    """One AdGuard Home instance: half of the LAN's name service.

    Its address is delivered where the image reads it — the `net-setup` service
    configures the interface out of PID 1's environment before the resolver it
    guards is allowed to start, so a resolver never comes up on an address the
    LAN was not told about.

    Its live configuration is its own: the instance rewrites the file whenever
    the `dns` stack adds a rewrite through its API (dns.md §3). So what is
    declared here is an initial state, placed once into a working directory
    that has none.
    """

    @property
    def state(self) -> str:
        return ADGUARD_STATE

    @property
    def environment(self) -> Mapping[str, str]:
        return net_setup_environment(self.service.address)

    @property
    def initial_state(self) -> InitialState:
        # `into` is relative to the state directory, which is bind-mounted at
        # `ADGUARD_STATE` -- so the file lands at exactly the path the instance
        # is started with.
        return InitialState(
            name=ADGUARD_INITIAL_STATE,
            into='AdGuardHome.yaml',
            content=adguard_initial_state(self.service.address),
        )


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

    Nothing is injected into it: the daemon is started with its state directory
    as its only argument, it takes the network it joins from what that directory
    holds, and it reaches its roots by literal address. What it needs of the
    unit is the tunnel device and the state bind that is its identity on the
    overlay.
    """

    @property
    def state(self) -> str:
        return OVERLAY_STATE

    @property
    def devices(self) -> tuple[str, ...]:
        return (TUN_DEVICE,)


#: One service's declaration, whichever image it is for. A fifth service is a
#: new type here and a new parameter on `DeviceServices`, not a key in a mapping
#: a loop may or may not look up (rfc-002 §5.3).
ServiceDeclaration = CaddyService | ResolverService | OverlayDaemon


# ---------------------------------------------------------------------------
# Where one service's files sit
# ---------------------------------------------------------------------------


def image_path(service: str) -> str:
    """Where the service's root filesystem tarball lands, as published."""
    return f'{IMAGE_DIR}/{service}.tar'


def root_path(service: str) -> str:
    """The tree unpacked from that tarball, which is what the unit boots."""
    return f'{ROOT_DIR}/{service}'


def state_path(service: str) -> str:
    return f'{STATE_DIR}/{service}'


def config_path(service: str, name: str) -> str:
    return f'{CONFIG_DIR}/{service}/{name}'


def secret_path(service: str, name: str) -> str:
    return f'{SECRET_DIR}/{service}/{name}'


def mounted_path(service: str, mounted: MountedFile) -> str:
    return secret_path(service, mounted.name) if mounted.secret else config_path(service, mounted.name)


def bridge_device_unit(bridge: str) -> str:
    """The device unit systemd gives a network interface.

    Network devices are tagged `systemd` in the `udev` database, so a bridge
    has a unit a service can bind to. Most device nodes do not, which is why
    `/dev/net/tun` is asserted rather than depended on (rfc-002 §4.3).
    """
    return f'sys-subsystem-net-devices-{bridge}.device'


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True)
class _UnitParams:
    """What `container.service.j2` reads.

    Every conditional part of the command line is already decided here, as the
    thing itself or as `None`: `bridge` is the container VLAN's bridge unless
    the service runs in the host's namespace, and `state_bind` and `binds` are
    whole `--bind` arguments rather than pairs the file has to assemble.
    """

    cluster: str
    name: str
    root: str
    kill_signal: str
    kill_gracetime: int
    environment: Mapping[str, str]
    bridge: str | None
    bridge_unit: str | None
    state_bind: str | None
    devices: tuple[str, ...]
    binds: tuple[str, ...]


def unit_file(declaration: ServiceDeclaration) -> str:
    """The unit that runs one container.

    It states its own requirements and the recovery script chooses no start
    order (rfc-002 §4.3): every service comes after the network is up, a
    service on the container VLAN binds to the bridge's device unit so that it
    cannot be started against a bridge that does not exist yet, and a service
    that needs a device node asserts the node rather than depending on a unit
    `udev` never activates.

    What the rest of the command line answers is in the template beside this
    module: every flag on it is something an s6 image does or does not do for
    itself.
    """
    service = declaration.service.name
    bridge = declaration.bridge
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/container.service.j2',
        _UnitParams(
            cluster=conventions.CLUSTER_NAME,
            name=service,
            root=root_path(service),
            kill_signal=KILL_SIGNAL,
            kill_gracetime=S6_KILL_GRACETIME,
            environment=declaration.environment,
            bridge=bridge,
            bridge_unit=None if bridge is None else bridge_device_unit(bridge),
            state_bind=declaration.state_bind,
            devices=declaration.devices,
            binds=tuple(f'{mounted_path(service, mounted)}:{mounted.target}' for mounted in declaration.mounted_files),
        ),
    )


def net_setup_environment(address: IPv4Address) -> dict[str, str]:
    """The addressing an image's own network setup reads from PID 1's environment.

    The address is configured rather than learned because the two resolvers are
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

    The resolvers arrive as the census has them, so the file names each one's
    vhost and address without a second list to keep in step.
    """

    controller: str
    token_path: str
    api_port: int
    resolvers: tuple[conventions.gateway.BridgedService, ...]


def caddyfile() -> str:
    """The gateway's own vhosts, with certificates it issues for itself.

    Each name is served over TLS the gateway obtains through a DNS-01 challenge
    with a token of its own — separate from the cluster's issuer on purpose, so
    that two issuers which must survive each other's outage do not share a
    credential (gateway.md §1).

    The controller console is reverse-proxied to the device's own port 443 over
    a connection whose certificate cannot be verified, because the certificate
    it presents is the device's self-signed one; the name that matters is the
    one the client asked for, which is forwarded unchanged.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/Caddyfile.j2',
        _CaddyParams(
            controller=conventions.gateway.VHOST_CONTROLLER,
            token_path=CADDY_TOKEN_PATH,
            api_port=conventions.gateway.ADGUARD_API_PORT,
            resolvers=conventions.gateway.RESOLVERS,
        ),
    )


@final
@dataclass(frozen=True)
class _AdguardInitialParams:
    """What `adguard-home.initial.yaml.j2` reads."""

    cluster: str
    address: IPv4Address
    api_port: int
    upstreams: tuple[str, ...]


def adguard_initial_state(address: IPv4Address) -> str:
    """One resolver's static configuration, as an initial state rather than live.

    A running instance rewrites this file whenever it accepts a change through
    its API, and the `dns` stack writes the split-horizon rewrites that way. So
    what is declared is what the instance needs in order to exist at all — where
    it listens, what it forwards to — and the recovery script installs it only
    where there is no configuration. That is the state of a service whose
    working directory the device has never held: a new instance, or a device
    rebuilt from nothing. Replacing the root filesystem is not such a moment,
    because the working directory is bind-mounted from the device and survives
    the tree that is thrown away with it.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/adguard-home.initial.yaml.j2',
        _AdguardInitialParams(
            cluster=conventions.CLUSTER_NAME,
            address=address,
            api_port=conventions.gateway.ADGUARD_API_PORT,
            upstreams=ADGUARD_UPSTREAMS,
        ),
    )


class Container(Component):
    """One container service on the device, and every file that defines it.

    It owns its root filesystem artifact, its unit, the files it mounts and its
    initial state, and it exposes the two facts its parent needs: the unit's
    name, and the stamped set the content stamp covers.

    `hook` and `after` are the same script from two sides: the recovery script
    is what every file of this service runs once it lands, and it is the only
    thing that installs, starts or restarts anything on the device
    (rfc-002 §4.2). It is stated on every file rather than once on this
    component because Pulumi does not push a component's own `depends_on` down
    to its children — and a file whose hook runs a script the device has not
    been given yet fails its own apply.
    """

    def __init__(
        self,
        name: str,
        *,
        declaration: ServiceDeclaration,
        connection: Connection,
        hook: str,
        after: Sequence[pulumi.Resource] = (),
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        service = declaration.service.name
        owner = conventions.gateway.SSH_USER
        child = self.child_opts(depends_on=list(after))

        self.unit_name: str = declaration.unit_name
        self.stamped_set: tuple[str, ...] = declaration.stamped_set

        self.image = DeviceArtifact(
            f'{name}-image',
            connection=connection,
            url=declaration.pin.url,
            sha256=declaration.pin.sha256,
            target=image_path(service),
            extract=root_path(service),
            mode=IMAGE_MODE,
            owner=owner,
            hook=hook,
            opts=child,
        )
        self.unit = DeviceFile(
            f'{name}-unit',
            connection=connection,
            path=f'{UNIT_DIR}/{self.unit_name}',
            content=unit_file(declaration),
            mode=CONFIG_MODE,
            owner=owner,
            hook=hook,
            opts=child,
        )
        self.mounted_files = {
            mounted.name: DeviceFile(
                f'{name}-file-{mounted.name}',
                connection=connection,
                path=mounted_path(service, mounted),
                content=mounted.content,
                mode=SECRET_MODE if mounted.secret else CONFIG_MODE,
                owner=owner,
                hook=hook,
                secret=mounted.secret,
                opts=child,
            )
            for mounted in declaration.mounted_files
        }
        initial = declaration.initial_state
        self.initial_state: DeviceFile | None = (
            None
            if initial is None
            else DeviceFile(
                f'{name}-initial-state',
                connection=connection,
                path=config_path(service, initial.name),
                content=initial.content,
                mode=CONFIG_MODE,
                owner=owner,
                hook=hook,
                opts=child,
            )
        )

        self.register_outputs({})
