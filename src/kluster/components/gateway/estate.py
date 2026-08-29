"""The gateway's desired state: routing, the container estate, the recovery script.

Everything the device must hold, expressed as files under `/data` — the one
directory a firmware update leaves alone (architecture.md §5.2). Four kinds of
file, and the relationships between them are the design:

-   **The routing configuration.** One rendered file naming the worker VM as a
    BGP neighbour, with the inbound prefix-list and the prefix cap that keep a
    compromised (or impersonated) peer from advertising the LAN out from under
    itself (cluster-infra.md §2). It carries the session's authentication
    password, so its content is a secret input and the file is declared secret.
-   **The container estate.** For each member: a root filesystem pinned by
    digest, a unit that runs it under `systemd-nspawn`, and the files the
    container reads. The root filesystems are built by another repository's
    continuous integration and travel as a URL and a digest — never as bytes in
    state — so a preview compares two hashes rather than megabytes. They are
    published as archives, so the pinned artifact lands as a tarball and the
    push unpacks it into a per-member tree the unit boots with `--directory=`;
    the tree is the push's to replace and never the container's to keep.
-   **The recovery script**, under `on_boot.d`. This is the piece that makes the
    estate survive a firmware update with nothing else present: it installs the
    units, retires the ones the estate no longer declares, and starts what is
    left, autonomously, with no expectation that this program is reachable at
    boot.
-   **Device secrets**: the gateway's own ACME credential, which buys it
    certificates that keep renewing while the cluster is down, and the routing
    session's password.

**The recovery script is also the apply hook.** Every other file's post-apply
hook runs that same script, so the path exercised after a firmware update is the
path exercised on every deployment — the recovery story cannot rot unnoticed,
because it is the only story. That is why the script is declared first and
everything else depends on it, and why it converges whatever it finds rather
than assuming a particular starting point.

**A container is restarted only when something it reads changed.** The script
stamps each unit with a checksum over the files that define it and compares
before acting, because the estate includes the ZeroTier member the deployment's
own session rides: restarting it unconditionally would sever the connection that
issued the restart, on every single apply. The one member whose restart *can*
sever the session is handled last, so everything else has converged before the
risk is taken; an apply that dies there fails its resource and the retry finds
the work already done.

**The images are Alpine with s6-overlay, not systemd.** They ship that init at
`/sbin/init` so that `systemd-nspawn --boot` finds it, and a unit here therefore
declares nothing that only a systemd guest would honour. Two consequences run
through this module. A member takes its addressing from its own startup, reading
what the unit injects into PID 1's environment, because a drop-in written for a
network manager the image does not run is a file nobody opens. And stopping a
member means `SIGKILL`: s6 treats a gentler signal as advisory and leaves its
supervisors running, and they hold the unit's control group open until the next
start fails on it.

**The name plane the containers serve is not declared here.** The AdGuard pair's
rewrites are the `dns` stack's, written through the running instances' API
(dns.md §3), and AdGuard rewrites its own configuration file as it accepts them.
So the static configuration this module declares is a **seed**: the recovery
script installs it when the instance has no configuration at all, which is the
situation after a wipe and never afterwards. Declaring the live file instead
would make every deployment delete the rewrites the other stack just wrote.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway import facts
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, GwArtifact, GwFile
from putils import Component

__all__ = (
    'ADGUARD_UPSTREAMS',
    'CADDY_CONFIG',
    'CADDY_CONFIG_HOME',
    'CADDY_STATE',
    'CONFIG_DIR',
    'CONFIG_MODE',
    'CONTAINER_BRIDGE',
    'ENV_IPV4_CIDR',
    'ENV_IPV4_GATEWAY',
    'ENV_IPV6_TOKEN',
    'ESTATE_ROOT',
    'FRR_APPLY',
    'FRR_CONFIG',
    'FRR_LIVE_CONFIG',
    'FRR_MODE',
    'IMAGE_DIR',
    'KILL_SIGNAL',
    'MAX_PREFIXES',
    'ON_BOOT_SCRIPT',
    'ROOT_DIR',
    'S6_KILL_GRACETIME',
    'SECRET_DIR',
    'STATE_DIR',
    'UNIT_DIR',
    'Container',
    'Dropin',
    'Estate',
    'Rootfs',
    'Seed',
    'census',
    'frr_config',
    'image_path',
    'net_setup_environment',
    'on_boot_script',
    'parse_rootfs',
    'resolvers',
    'root_path',
    'unit_file',
    'unit_name',
)

#: The package `importlib.resources` resolves this module's `templates/`
#: directory against, so the rendered files travel with the code that renders
#: them (rfc-002 §9.1).
_PACKAGE = 'kluster.components.gateway'

# ---------------------------------------------------------------------------
# Where things live on the device
# ---------------------------------------------------------------------------

#: The estate's root. Everything below it survives a firmware update because
#: `/data` does; everything outside it is re-materialized from here at boot.
ESTATE_ROOT = f'{conventions.GW_DATA_ROOT}/estate'
#: The pinned artifacts as they are published: one root filesystem tarball per
#: member, with the digest marker the artifact resource keeps beside each.
IMAGE_DIR = f'{ESTATE_ROOT}/images'
#: The trees unpacked from those tarballs, which are what the units boot. A tree
#: is derived state the push owns: it is replaced whole when the pin moves and
#: nothing in it is worth keeping, which is why per-container writable state is
#: bind-mounted from `STATE_DIR` instead of living here.
ROOT_DIR = f'{ESTATE_ROOT}/roots'
UNIT_DIR = f'{ESTATE_ROOT}/units'
CONFIG_DIR = f'{ESTATE_ROOT}/config'
SECRET_DIR = f'{ESTATE_ROOT}/secrets'
#: Per-container writable state, bind-mounted in. Kept out of the image so a
#: digest bump replaces the software and keeps the identity — which for the
#: ZeroTier member is the difference between a reboot and a new node address.
STATE_DIR = f'{ESTATE_ROOT}/state'

#: The routing configuration, as desired state and as the daemon reads it. The
#: daemon's own path is not under `/data`, so the recovery script copies the
#: first to the second — which is also what the apply hook does, since the two
#: are the same script.
FRR_CONFIG = f'{conventions.GW_DATA_ROOT}/frr/frr.conf'
FRR_LIVE_CONFIG = '/etc/frr/frr.conf'
#: The session password is in it, so it is not world-readable.
FRR_MODE = '0640'
FRR_APPLY = f'install -m {FRR_MODE} {FRR_CONFIG} {FRR_LIVE_CONFIG} && systemctl reload frr'

#: The boot-time recovery script, and the command that runs it. `20-` orders it
#: after whatever numbering the device's own scripts use and before nothing.
ON_BOOT_SCRIPT = f'{conventions.GW_ON_BOOT_D}/20-kluster-estate.sh'
ON_BOOT_HOOK = f'sh {ON_BOOT_SCRIPT}'

#: Unit names are prefixed so the recovery script can tell the estate's units
#: from the device's own and retire only its own.
UNIT_PREFIX = 'kluster-'

SCRIPT_MODE = '0755'
CONFIG_MODE = '0644'
SECRET_MODE = '0600'
#: A root filesystem is not secret, but it is also nobody's to read. This is the
#: mode of the tarball; what the tree gets is whatever the archive carries, which
#: is the container's own idea of its permissions and not the estate's.
IMAGE_MODE = '0600'

# ---------------------------------------------------------------------------
# What the estate is made of
# ---------------------------------------------------------------------------

#: The routing session's inbound cap. The prefix-list already confines what the
#: peer may announce to the pool; this bounds how many /32s out of it arrive, so
#: a peer that floods the table is dropped rather than believed.
MAX_PREFIXES = 64

#: The resolvers both AdGuard instances forward to. Two providers on purpose:
#: the LAN's name service must not fail with any single one of them.
ADGUARD_UPSTREAMS = ('https://dns.quad9.net/dns-query', 'https://dns.cloudflare.com/dns-query')

#: The resolvers' own working directory, bind-mounted from the device so that a
#: digest bump replaces the software and keeps the configuration; and the name
#: the estate delivers its seed under, which is deliberately not the name the
#: instance reads, so that delivering one can never overwrite the other.
#:
#: The working directory is the image's, not a path of this program's choosing:
#: the resolver is started with `-w /data/adguard -c /data/adguard/AdGuardHome.yaml`
#: and its installation lives at `ADGUARD_INSTALL`, which the tree carries and a
#: digest bump replaces. Splitting them that way is what makes the software
#: disposable and the configuration durable, so the bind lands on the state half.
ADGUARD_STATE = '/data/adguard'
ADGUARD_INSTALL = '/opt/AdGuardHome'
ADGUARD_SEED = 'AdGuardHome.seed.yaml'

#: Where caddy looks for its configuration and where it keeps what it must not
#: lose. Both are directories the image names through the environment rather than
#: paths this program picks: the server is started with
#: `--config $XDG_CONFIG_HOME/caddy/Caddyfile`, and it places the certificates
#: and account keys it buys under `XDG_DATA_HOME`. Naming those two is therefore
#: how the estate decides which file is read and which directory outlives a
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

#: The member that carries the overlay, which the census declares as the one
#: service in the host's own network namespace. It is named here as well
#: because the unit needs a device node and a state directory, and those are
#: properties of this daemon rather than of any service's placement.
HOST_NETWORK_MEMBER = 'zerotier'

#: The device node the ZeroTier daemon needs, and the state directory whose
#: contents are its identity on the network.
TUN_DEVICE = '/dev/net/tun'
ZEROTIER_STATE = '/var/lib/zerotier-one'

#: The device bridge a non-host-networked member attaches to. It is the
#: container VLAN's bridge — the estate is what that VLAN exists for.
CONTAINER_BRIDGE = 'br5'

#: How a member is stopped, and the same instruction restated on the inside. s6
#: treats a shutdown signal as advisory: it returns from the signal with its
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
class Dropin:
    """A file the estate writes for a container, and where the container reads it.

    `target` is a path inside the container; the file itself lives beside the
    estate's other desired state and is bind-mounted to that path, so the image
    stays the software and the configuration stays declarable.
    """

    name: str
    target: str
    content: pulumi.Input[str]
    secret: bool = False


@final
@dataclass(frozen=True)
class Seed:
    """A file placed into a container's own state, once, when it has none.

    The difference from a `Dropin` is who owns the file afterwards. A dropin is
    the estate's, rewritten on every deployment; a seed becomes the container's
    the moment it is placed, because the software behind it rewrites it — a
    resolver accepting a rewrite through its API, for instance. Seeding is
    therefore what happens after a wipe and at no other time.
    """

    source: str
    into: str


@final
@dataclass(frozen=True)
class Container:
    """One member of the nspawn estate.

    `address` is the member's static address on the container VLAN, and `None`
    means the member runs in the host's network namespace instead — the
    ZeroTier member does, because a routed interface is no use inside a
    namespace the router cannot see.

    `environment` is what the unit puts into the container's PID 1, and it is
    the channel through which an image is told anything at all: the images run
    s6, which reads that environment in its own startup scripts, so a member
    that configures its interface or finds its configuration does it from here.
    Which variables a member reads is a fact about its image, which is why the
    census sets them per member rather than this module inventing a set.
    """

    name: str
    rootfs: Rootfs
    address: IPv4Address | None = None
    devices: tuple[str, ...] = ()
    state: str | None = None
    files: tuple[Dropin, ...] = field(default_factory=tuple[Dropin, ...])
    seed: Seed | None = None
    environment: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def host_network(self) -> bool:
        return self.address is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def unit_name(container: str) -> str:
    return f'{UNIT_PREFIX}{container}.service'


def image_path(container: str) -> str:
    """Where the member's root filesystem tarball lands, as published."""
    return f'{IMAGE_DIR}/{container}.tar'


def root_path(container: str) -> str:
    """The tree unpacked from that tarball, which is what the unit boots."""
    return f'{ROOT_DIR}/{container}'


def state_path(container: str) -> str:
    return f'{STATE_DIR}/{container}'


def config_path(container: str, name: str) -> str:
    return f'{CONFIG_DIR}/{container}/{name}'


def secret_path(container: str, name: str) -> str:
    return f'{SECRET_DIR}/{container}/{name}'


def dropin_path(container: str, dropin: Dropin) -> str:
    return secret_path(container, dropin.name) if dropin.secret else config_path(container, dropin.name)


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
        _PACKAGE,
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
class _UnitParams:
    """What `container.service.j2` reads.

    Every conditional part of the command line is already decided here, as the
    thing itself or as `None`: `bridge` is the container VLAN's bridge unless
    the member runs in the host's namespace, and `state_bind` and `binds` are
    whole `--bind` arguments rather than pairs the file has to assemble.
    """

    cluster: str
    name: str
    root: str
    kill_signal: str
    kill_gracetime: int
    environment: Mapping[str, str]
    bridge: str | None
    state_bind: str | None
    devices: tuple[str, ...]
    binds: tuple[str, ...]


def unit_file(container: Container) -> str:
    """The unit that runs one container.

    `systemd-nspawn` shares the host's network namespace unless told otherwise,
    so host networking is the *absence* of a bridge argument rather than a flag
    — which is why the ZeroTier member is described by having no address rather
    than by a switch.

    What the rest of the command line answers is in the template beside this
    module: every flag on it is something an s6 image does or does not do for
    itself.
    """
    return templates.render(
        _PACKAGE,
        'templates/container.service.j2',
        _UnitParams(
            cluster=conventions.CLUSTER_NAME,
            name=container.name,
            root=root_path(container.name),
            kill_signal=KILL_SIGNAL,
            kill_gracetime=S6_KILL_GRACETIME,
            environment=container.environment,
            bridge=None if container.host_network else CONTAINER_BRIDGE,
            state_bind=f'{state_path(container.name)}:{container.state}' if container.state else None,
            devices=container.devices,
            binds=tuple(f'{dropin_path(container.name, dropin)}:{dropin.target}' for dropin in container.files),
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
    resolvers: tuple[conventions.BridgedService, ...]


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
        _PACKAGE,
        'templates/Caddyfile.j2',
        _CaddyParams(
            controller=conventions.VHOST_CONTROLLER,
            token_path=CADDY_TOKEN_PATH,
            api_port=conventions.ADGUARD_API_PORT,
            resolvers=resolvers(),
        ),
    )


@final
@dataclass(frozen=True)
class _AdguardSeedParams:
    """What `adguard-home.initial.yaml.j2` reads."""

    cluster: str
    address: IPv4Address
    api_port: int
    upstreams: tuple[str, ...]


def adguard_seed(address: IPv4Address) -> str:
    """One resolver's static configuration, as a seed rather than as live state.

    A running instance rewrites this file whenever it accepts a change through
    its API, and the `dns` stack writes the split-horizon rewrites that way. So
    the estate declares what the instance needs in order to exist at all —
    where it listens, what it forwards to — and the recovery script installs it
    only where there is no configuration. That is the state of a member whose
    working directory the device has never held: a new instance, or a device
    rebuilt from nothing. Replacing the root filesystem is not such a moment,
    because the working directory is bind-mounted from the device and survives
    the tree that is thrown away with it.
    """
    return templates.render(
        _PACKAGE,
        'templates/adguard-home.initial.yaml.j2',
        _AdguardSeedParams(
            cluster=conventions.CLUSTER_NAME,
            address=address,
            api_port=conventions.ADGUARD_API_PORT,
            upstreams=ADGUARD_UPSTREAMS,
        ),
    )


@final
@dataclass(frozen=True)
class _Seeding:
    """Where one member's own configuration is delivered from, and where it lands."""

    source: str
    destination: str


@final
@dataclass(frozen=True)
class _UnitState:
    """One member as the recovery script deals with it.

    Both paths are resolved here rather than in the file: where a dropin,
    a root filesystem tree or a state directory sits is this module's, and the
    script only has to compare and copy.
    """

    name: str
    inputs: tuple[str, ...]
    seed: _Seeding | None


@final
@dataclass(frozen=True)
class _RecoveryParams:
    """What `recover-services.sh.j2` reads, with the members in startup order."""

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


def on_boot_script(containers: Sequence[Container]) -> str:
    """The script that re-establishes the estate, at boot and at every apply.

    Written for the device's shell, which is BusyBox: no arrays, no
    `bash`-isms, and `cksum` rather than a digest tool, since all it has to do
    is notice a change.

    What it guarantees, in order:

    1.  every declared unit is installed and enabled;
    2.  a unit the estate no longer declares is stopped and removed — which is
        what keeps the device from accumulating the estate's history;
    3.  the routing configuration is copied where the daemon reads it;
    4.  a container that owns its own configuration is seeded, but only where
        it has none;
    5.  a container is (re)started only if the files that define it changed,
        compared against a stamp beside its state.

    The last point is why this can be the apply hook: without it, every
    deployment would restart the ZeroTier member that the deployment's own
    session is riding on.
    """
    return templates.render(
        _PACKAGE,
        'templates/recover-services.sh.j2',
        _RecoveryParams(
            cluster=conventions.CLUSTER_NAME,
            data_root=conventions.GW_DATA_ROOT,
            unit_dir=UNIT_DIR,
            state_dir=STATE_DIR,
            root_dir=ROOT_DIR,
            unit_prefix=UNIT_PREFIX,
            config_mode=CONFIG_MODE,
            frr_config=FRR_CONFIG,
            frr_mode=FRR_MODE,
            frr_live_config=FRR_LIVE_CONFIG,
            units=tuple(_unit_state(container) for container in _startup_order(containers)),
        ),
    )


def _unit_state(container: Container) -> _UnitState:
    """One member, named and located the way the recovery script needs it."""
    return _UnitState(
        name=unit_name(container.name),
        inputs=_stamp_inputs(container),
        seed=None
        if container.seed is None
        else _Seeding(
            source=config_path(container.name, container.seed.source),
            destination=f'{state_path(container.name)}/{container.seed.into}',
        ),
    )


def _stamp_inputs(container: Container) -> tuple[str, ...]:
    """The files whose contents decide whether a container must be restarted.

    The root filesystem is represented by the digest marker beside its *tree*
    rather than by the tree itself: the marker is a line of text the artifact
    resource writes once the tree is in place, and walking a root filesystem to
    notice it is unchanged would cost more than the restart. The tree's marker
    rather than the tarball's, because the tarball's is written after this
    script has already run — it is the claim that the push as a whole
    succeeded, and a container that waited for it would learn of a new root
    filesystem one deployment late.
    """
    return (
        f'{UNIT_DIR}/{unit_name(container.name)}',
        f'{root_path(container.name)}.digest',
        *(dropin_path(container.name, dropin) for dropin in container.files),
    )


def _startup_order(containers: Sequence[Container]) -> tuple[Container, ...]:
    """Every container, with the host-networked one last.

    Restarting the member that carries the management overlay drops the session
    the restart arrived on. It has to happen when its image changes, so the
    order puts it after everything that would otherwise be lost with it.
    """
    return (
        *(container for container in containers if not container.host_network),
        *(container for container in containers if container.host_network),
    )


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


def parse_rootfs(raw: object) -> dict[str, Rootfs]:
    """Read the image pins from stack configuration.

    One entry per estate member, each a URL and the digest that pins it. The
    digest is checked for shape here rather than only at apply time, so a
    truncated paste is a configuration error with a name on it instead of a
    push that reaches the device and refuses there.
    """
    pins: dict[str, Rootfs] = {}
    for member, value in facts.mapping(raw, 'the estate image configuration').items():
        what = f'the image pin for {member}'
        entry = facts.mapping(value, what)
        digest = facts.text(entry, 'sha256', what)
        if len(digest) != _SHA256_LENGTH:
            raise ValueError(f'{what} is not a hex sha256 digest')
        pins[member] = Rootfs(url=facts.text(entry, 'url', what), sha256=digest.lower())
    return pins


#: The length of a hex-encoded SHA-256 digest.
_SHA256_LENGTH = 64


def resolvers() -> tuple[conventions.BridgedService, ...]:
    """The estate's resolver instances, in name order.

    A bridged service that serves a public name is what an AdGuard instance is
    here: the name proxied to it is its administration interface, and the port
    behind that name is the resolver's API.
    """
    return tuple(
        sorted(
            (
                service
                for service in conventions.GW_SERVICES
                if isinstance(service, conventions.BridgedService) and service.vhost is not None
            ),
            key=lambda service: service.name,
        )
    )


def census(*, rootfs: Mapping[str, Rootfs], acme_token: pulumi.Input[str]) -> tuple[Container, ...]:
    """The estate as the design has it: the service census, made runnable.

    `conventions.GW_SERVICES` decides which members exist and where the bridged
    ones sit; what arrives here is `rootfs`, which pins each member's image. A
    digest is whatever the build produced, so it is a site fact rather than a
    convention, and it is checked against the census by name.
    """
    services = {service.name: service for service in conventions.GW_SERVICES}
    missing = [name for name in services if name not in rootfs]
    if missing:
        raise ValueError(f'the estate has no image pinned for {", ".join(missing)}')
    unknown = sorted(set(rootfs) - set(services))
    if unknown:
        raise ValueError(f'{", ".join(unknown)} is not a member of the estate')

    caddy = services['caddy']
    assert isinstance(caddy, conventions.BridgedService), 'the proxy serves from an address on the container VLAN'
    containers = [
        Container(
            name=caddy.name,
            rootfs=rootfs[caddy.name],
            address=caddy.address,
            state=CADDY_STATE,
            # Where the server looks, rather than where a reader might assume it
            # does. Its address is *not* here: this image asks for a lease
            # (`/etc/network/interfaces`), and the lease is also where its
            # resolver comes from, so an address injected the way the resolvers
            # take theirs would be read by nothing and a static one imposed over
            # that file would take the name service with it. The address the
            # census holds for caddy is therefore the address the design intends
            # — the one a rewrite has to name — and delivering it is work in the
            # image, which needs the `net-setup` the AdGuard pair already has
            # plus a resolver that does not depend on this estate.
            environment={
                'XDG_CONFIG_HOME': CADDY_CONFIG_HOME,
                'XDG_DATA_HOME': CADDY_STATE,
                'HOME': CADDY_STATE,
            },
            files=(
                Dropin(name='Caddyfile', target=CADDY_CONFIG, content=caddyfile()),
                Dropin(name='cloudflare.token', target=CADDY_TOKEN_PATH, content=acme_token, secret=True),
            ),
        )
    ]
    containers.extend(
        Container(
            name=instance.name,
            rootfs=rootfs[instance.name],
            address=instance.address,
            state=ADGUARD_STATE,
            # The address, delivered where the image reads it: its `net-setup`
            # service configures the interface out of PID 1's environment before
            # the resolver it guards is allowed to start, so a resolver never
            # comes up on an address the LAN was not told about.
            environment=net_setup_environment(instance.address),
            files=(
                # Read-only beside the installation rather than in the working
                # directory: the working directory is where the live
                # configuration goes, and a seed delivered into it would be a
                # second file the instance might one day decide to read.
                Dropin(
                    name=ADGUARD_SEED,
                    target=f'{ADGUARD_INSTALL}/{ADGUARD_SEED}',
                    content=adguard_seed(instance.address),
                ),
            ),
            # The live configuration is the instance's own: it rewrites the
            # file whenever the `dns` stack adds a rewrite through its API. So
            # the seed is placed once, into a work directory that has none.
            # `into` is relative to the state directory the estate keeps on the
            # device, which is bind-mounted at `ADGUARD_STATE` -- so the seed
            # lands at exactly the path the instance is started with.
            seed=Seed(source=ADGUARD_SEED, into='AdGuardHome.yaml'),
        )
        for instance in resolvers()
    )
    containers.append(
        # Nothing is injected into this one: the daemon is started with its
        # state directory as its only argument, it takes the network it joins
        # from what that directory holds, and it reaches its roots by literal
        # address. What it needs of the unit is the tunnel device, the state
        # bind that is its identity on the overlay, and the host's own network
        # namespace — which it gets by having no address of its own here.
        Container(
            name=HOST_NETWORK_MEMBER,
            rootfs=rootfs[HOST_NETWORK_MEMBER],
            address=None,
            devices=(TUN_DEVICE,),
            state=ZEROTIER_STATE,
        )
    )
    return tuple(containers)


class Estate(Component):
    """The device's desired state, as files and one script that applies them."""

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        containers: Sequence[Container],
        bgp_neighbour: IPv4Address,
        bgp_password: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        ordered = _startup_order(containers)

        # Declared first and depended on by everything else, because it is what
        # every other file's hook runs. A device that has only this file has an
        # estate that converges the moment the rest arrives.
        self.recovery = GwFile(
            f'{name}-on-boot',
            connection=connection,
            path=ON_BOOT_SCRIPT,
            content=on_boot_script(ordered),
            mode=SCRIPT_MODE,
            owner=conventions.GW_SSH_USER,
            hook=ON_BOOT_HOOK,
            opts=self.child_opts(),
        )
        child = self.child_opts(depends_on=[self.recovery])

        # The routing configuration answers to the daemon rather than to the
        # estate, so it applies itself instead of going through the script. It
        # carries the session password, which is why it is secret.
        self.frr = GwFile(
            f'{name}-frr',
            connection=connection,
            path=FRR_CONFIG,
            content=pulumi.Output.from_input(bgp_password).apply(
                lambda password: frr_config(neighbour=bgp_neighbour, password=password)
            ),
            mode=FRR_MODE,
            owner=conventions.GW_SSH_USER,
            hook=FRR_APPLY,
            secret=True,
            opts=child,
        )

        self.images: dict[str, GwArtifact] = {}
        self.units: dict[str, GwFile] = {}
        self.files: dict[str, GwFile] = {}
        for container in ordered:
            self.images[container.name] = GwArtifact(
                f'{name}-image-{container.name}',
                connection=connection,
                url=container.rootfs.url,
                sha256=container.rootfs.sha256,
                target=image_path(container.name),
                extract=root_path(container.name),
                mode=IMAGE_MODE,
                owner=conventions.GW_SSH_USER,
                hook=ON_BOOT_HOOK,
                opts=child,
            )
            self.units[container.name] = GwFile(
                f'{name}-unit-{container.name}',
                connection=connection,
                path=f'{UNIT_DIR}/{unit_name(container.name)}',
                content=unit_file(container),
                mode=CONFIG_MODE,
                owner=conventions.GW_SSH_USER,
                hook=ON_BOOT_HOOK,
                opts=child,
            )
            for dropin in container.files:
                self.files[f'{container.name}/{dropin.name}'] = GwFile(
                    f'{name}-file-{container.name}-{dropin.name}',
                    connection=connection,
                    path=dropin_path(container.name, dropin),
                    content=dropin.content,
                    mode=SECRET_MODE if dropin.secret else CONFIG_MODE,
                    owner=conventions.GW_SSH_USER,
                    hook=ON_BOOT_HOOK,
                    secret=dropin.secret,
                    opts=child,
                )

        self.register_outputs({})
