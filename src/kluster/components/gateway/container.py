"""One container service on the device: the machine it is, and the files it reads.

A workload on the nspawn runtime (`nspawn`), and everything it is made of lives
in that machine's own directory under the custom root: the root filesystem
unpacked from its pin, its settings file, the configuration the image reads,
the writable state bind-mounted into it, and — where the software behind it
rewrites its own configuration — one initial-state file.

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
declare nothing that only a systemd guest would honour. Two consequences run
through this module. A container is told things through **its PID 1's
environment**, because that is what its own startup scripts read; a drop-in
written for a network manager the image does not run is a file nobody opens.
And a container is stopped with **`SIGKILL`**: s6 treats a gentler signal as
advisory, returns from it with its supervisors still running, and they hold the
machine's control group open until the next start fails on it.

**Host networking is the absence of a bridge.** Only a declaration built on a
bridged census entry can produce a bridge, so the overlay daemon — which must
be in the host's namespace for the gateway to route through the interface it
creates — cannot acquire one by accident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway import nspawn
from kluster.components.gateway.nspawn import NspawnRuntime
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceArtifact, DeviceFile, marker_path
from putils import Component

__all__ = (
    'ADGUARD_INITIAL_STATE',
    'ADGUARD_INSTALL',
    'ADGUARD_STATE',
    'ADGUARD_UPSTREAMS',
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
    'OVERLAY_STATE',
    'S6_KILL_GRACETIME',
    'SECRET_MODE',
    'TEMPLATE_PACKAGE',
    'TUN_DEVICE',
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
    'caddyfile',
    'machine',
    'mounted_path',
    'net_setup_environment',
    'nspawn_file',
    'resolv_conf',
)

#: The package `importlib.resources` resolves the `templates/` directory
#: against, so the rendered files travel with the code that renders them
#: (rfc-002 §9.1).
TEMPLATE_PACKAGE = 'kluster.components.gateway'

CONFIG_MODE = '0644'
SECRET_MODE = '0600'

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

#: Where the image's resolver library reads which resolver to ask. Which one an
#: estate offers is a site fact rather than an image fact, so it is delivered
#: rather than baked: the image's own default is a public resolver, which
#: answers none of the internal names this proxy's upstreams have.
CADDY_RESOLV_CONF = '/etc/resolv.conf'

#: The device node the overlay daemon needs, and the state directory whose
#: contents are its identity on the overlay.
TUN_DEVICE = '/dev/net/tun'
OVERLAY_STATE = '/var/lib/zerotier-one'

#: The device bridge a service on the container VLAN attaches to. It is that
#: VLAN's bridge — the container services are what the VLAN exists for.
CONTAINER_BRIDGE = 'br5'

#: The one capability every image here needs beyond nspawn's default set: each
#: configures its own interface from inside. Stated as a constant because the
#: settings template is what a reviewer reads it in, and the alternative the
#: device ran before this program owned the mechanism was `all`.
CAPABILITY = 'CAP_NET_ADMIN'

#: How a container is stopped, and the same instruction restated on the inside.
#: s6 treats a shutdown signal as advisory: it returns from the signal with its
#: supervisors still running, they keep the machine's control group populated,
#: and the next start fails against a group that never emptied. `SIGKILL` from
#: the outside ends that, and a zero grace time keeps the supervision tree from
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
    the state directory only while that directory is empty, and left alone
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

    @property
    def stamped_set(self) -> tuple[str, ...]:
        """The paths the machine's content stamp covers (rfc-002 §4.2).

        The settings file, the digest marker of the root filesystem tree, and
        every file the container mounts: change one of them and the converger
        restarts the machine, change nothing and it does not.

        The root filesystem is represented by the marker beside it rather than
        by the tree itself: walking a root filesystem to notice it is unchanged
        would cost more than the restart it saves. The artifact resource writes
        that marker before it runs the converger, which is what makes it a
        change the converger can see.
        """
        return (
            nspawn.nspawn_path(self.service.name),
            marker_path(nspawn.rootfs_path(self.service.name)),
            *(mounted_path(self.service.name, mounted) for mounted in self.mounted_files),
        )


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
    credential (gateway.md §1).

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
            **net_setup_environment(self.service.address),
        }

    @property
    def mounted_files(self) -> tuple[MountedFile, ...]:
        return (
            MountedFile(name='Caddyfile', target=CADDY_CONFIG, content=caddyfile()),
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
    holds, and it reaches its roots by literal address. What it needs of its
    machine is the tunnel device and the state bind that is its identity on the
    overlay.
    """

    @property
    def state(self) -> str:
        return OVERLAY_STATE

    @property
    def devices(self) -> tuple[str, ...]:
        return (TUN_DEVICE,)


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


def machine(declaration: ServiceDeclaration) -> nspawn.Machine:
    """One service as the runtime converges it, from what the gateway declared.

    The runtime is handed this rather than the component, which is what keeps
    the two sides acyclic while leaving one source for both: the same
    declaration produces the machine the converger acts on and the files the
    component declares, so a stamped set cannot name a file no resource
    declares.
    """
    service = declaration.service.name
    initial = declaration.initial_state
    return nspawn.Machine(
        name=service,
        stamped=declaration.stamped_set,
        initial_state=None
        if initial is None
        else nspawn.Placement(
            source=nspawn.machine_file(service, initial.name),
            destination=f'{nspawn.state_path(service)}/{initial.into}',
        ),
    )


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


def nspawn_file(declaration: ServiceDeclaration) -> str:
    """The settings that decide what one machine is when systemd starts it.

    There is no unit to write: `systemd-nspawn@.service` is systemd's own
    template and it reads this file, so what a machine says about itself is
    said once and in the place `machinectl` and `systemctl` already look.
    """
    service = declaration.service.name
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

    zone: str
    controller: str
    token_path: str
    api_port: int
    resolvers: tuple[conventions.gateway.BridgedService, ...]


def caddyfile() -> str:
    """The gateway's vhosts, under one wildcard certificate it issues itself.

    The certificate is obtained through a DNS-01 challenge with a token that
    lives on the device — separate from the cluster's issuer on purpose, so
    that two issuers which must survive each other's outage do not share a
    credential (gateway.md §1). Two properties of *which* certificate follow
    from that separation (rfc-002 §9.3):

    -   **One wildcard, not three per-name certificates.** These names resolve
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

    One site block therefore serves every name, matching the three vhosts
    inside it by host and refusing everything else — a name under the zone that
    nothing here serves gets the connection closed rather than an answer from
    whichever block happened to be first. A wildcard covers one label, so every
    name the gateway serves has to be one label under the zone; that is a
    property of the census and `test_conventions` holds it there.

    The controller console is reverse-proxied to the device's own port 443 over
    a connection whose certificate cannot be verified, because the certificate
    it presents is the device's self-signed one; the name that matters is the
    one the client asked for, which is forwarded unchanged.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        'templates/Caddyfile.j2',
        _CaddyParams(
            zone=conventions.ZONE_PRIMARY,
            controller=conventions.gateway.VHOST_CONTROLLER,
            token_path=CADDY_TOKEN_PATH,
            api_port=conventions.gateway.ADGUARD_API_PORT,
            resolvers=conventions.gateway.RESOLVERS,
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
    it listens, what it forwards to — and the converger installs it only while
    the state directory is empty. That is the state of a service whose working
    directory the device has never held: a new instance, or a device rebuilt
    from nothing. Replacing the root filesystem is not such a moment, because
    the working directory is bind-mounted from the device and survives the tree
    that is thrown away with it.
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

    It owns its root filesystem artifact, its settings file, the files it mounts
    and its initial state — all of them in the machine's own directory — and it
    exposes the two facts a reader needs of it: the unit that runs the machine,
    and the stamped set its content stamp covers.

    **Every file of the machine runs the runtime's hook once it lands**, which
    converges the machine and then holds it to having come up (`nspawn`). It is
    stated on every file rather than once on this component because Pulumi does
    not push a component's own `depends_on` down to its children — and a file
    whose hook runs a script the device has not been given yet fails its own
    apply, which is why the runtime's convergers are depended on here too.

    `after` is what this machine must be actuated behind. It is a parameter
    rather than a fact of this class because the only service that has such a
    constraint has it for a reason that is invisible from inside: the machine
    carrying the deployment's own session must move last (rfc-002 §4.4).
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
        self.stamped_set: tuple[str, ...] = declaration.stamped_set

        root = nspawn.rootfs_path(service)
        self.image = DeviceArtifact(
            f'{name}-image',
            connection=connection,
            repository=declaration.pin.repository,
            tag=declaration.pin.tag,
            digest=declaration.pin.digest,
            root=root,
            hook=runtime.hook(service, root),
            opts=child,
        )
        settings = nspawn.nspawn_path(service)
        self.settings = DeviceFile(
            f'{name}-nspawn',
            connection=connection,
            path=settings,
            content=nspawn_file(declaration),
            mode=CONFIG_MODE,
            owner=owner,
            hook=runtime.hook(service, settings),
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
                hook=runtime.hook(service, mounted_path(service, mounted)),
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
                path=nspawn.machine_file(service, initial.name),
                content=initial.content,
                mode=CONFIG_MODE,
                owner=owner,
                hook=runtime.hook(service, nspawn.machine_file(service, initial.name)),
                opts=child,
            )
        )

        self.register_outputs({})
