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
from kluster.gateway import facts
from kluster.gateway.provider import Connection, GwArtifact, GwFile
from putils import Component

__all__ = (
    'ADGUARD_API_PORT',
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
    'parse_addresses',
    'parse_rootfs',
    'root_path',
    'unit_file',
    'unit_name',
)

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

#: AdGuard Home's administration and API port, which is the port the `dns`
#: stack's rewrite resources speak to and the port the ZeroTier flow rules admit
#: from a continuous-integration member.
ADGUARD_API_PORT = 3000

#: The resolvers both AdGuard instances forward to. Two providers on purpose:
#: the LAN's name service must not fail with any single one of them.
ADGUARD_UPSTREAMS = ('https://dns.quad9.net/dns-query', 'https://dns.cloudflare.com/dns-query')

#: The public names the gateway serves for itself, as labels in the primary
#: zone. They are the gateway's own vhosts — the controller console and both
#: resolver interfaces — and it issues their certificates itself, because its
#: TLS has to keep renewing while the cluster is down (gateway.md §1).
VHOST_CONTROLLER = f'unifi.{conventions.ZONE_PRIMARY}'
VHOST_ADGUARD = {'adguard-alice': f'alice.{conventions.ZONE_PRIMARY}', 'adguard-bob': f'bob.{conventions.ZONE_PRIMARY}'}

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

#: The one estate member that runs in the host's own network namespace: its
#: interface has to land there for the gateway to route through it
#: (architecture.md §5.3). Which member it is also decides the order the
#: recovery script works in, since restarting it can sever the session that
#: asked for the restart.
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


def frr_config(
    *,
    neighbour: IPv4Address,
    password: str,
    local_asn: int = conventions.UDM_ASN,
    peer_asn: int = conventions.CLUSTER_ASN,
    pool_v4: IPv4Network = conventions.LAN_POOL_V4,
    pool_v6: IPv6Network = conventions.LAN_POOL_V6,
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
    peer = str(neighbour)
    v4_list = f'{conventions.CLUSTER_NAME}-lan-pool-v4'
    v6_list = f'{conventions.CLUSTER_NAME}-lan-pool-v6'
    return '\n'.join(
        (
            f'! Managed by the {conventions.CLUSTER_NAME} physical stack. Local edits are overwritten.',
            'frr defaults traditional',
            '!',
            f'router bgp {local_asn}',
            ' no bgp ebgp-requires-policy',
            f' neighbor {peer} remote-as {peer_asn}',
            f' neighbor {peer} description {conventions.CLUSTER_NAME} {conventions.HOMELAB_NODE}',
            f' neighbor {peer} password {password}',
            ' !',
            ' address-family ipv4 unicast',
            f'  neighbor {peer} activate',
            f'  neighbor {peer} soft-reconfiguration inbound',
            f'  neighbor {peer} prefix-list {v4_list} in',
            f'  neighbor {peer} maximum-prefix {MAX_PREFIXES}',
            ' exit-address-family',
            ' !',
            ' address-family ipv6 unicast',
            f'  neighbor {peer} activate',
            f'  neighbor {peer} soft-reconfiguration inbound',
            f'  neighbor {peer} prefix-list {v6_list} in',
            f'  neighbor {peer} maximum-prefix {MAX_PREFIXES}',
            ' exit-address-family',
            '!',
            f'ip prefix-list {v4_list} seq 10 permit {pool_v4} le {pool_v4.max_prefixlen}',
            f'ip prefix-list {v4_list} seq 20 deny any',
            f'ipv6 prefix-list {v6_list} seq 10 permit {pool_v6} le {pool_v6.max_prefixlen}',
            f'ipv6 prefix-list {v6_list} seq 20 deny any',
            '!',
            '',
        )
    )


def unit_file(container: Container) -> str:
    """The unit that runs one container.

    `systemd-nspawn` shares the host's network namespace unless told otherwise,
    so host networking is the *absence* of a bridge argument rather than a flag
    — which is why the ZeroTier member is described by having no address rather
    than by a switch.

    The root is a directory rather than a filesystem image because that is the
    shape the pins arrive in: the artifacts are root filesystem tarballs, and
    `--image=` boots a partitioned or filesystem image, not an archive. Turning
    one into the other on the device would mean building a filesystem and
    loop-mounting it there; unpacking it is the same information with none of
    the machinery.

    The rest of the command line is what running an s6 image asks for, and each
    flag answers something the image does or does not do for itself.
    """
    command = [
        '/usr/bin/systemd-nspawn',
        '--quiet',
        '--keep-unit',
        '--boot',
        # `--boot` looks for an init and finds the one the image places at
        # `/sbin/init`, which is s6's. That init never sends a readiness
        # message, so nspawn answers for it: the unit is ready once the
        # container's PID 1 exists. `Type=notify` below is about nspawn's own
        # message, not the guest's — asked to wait for the guest instead, every
        # member would hang until its start timed out and it was killed.
        '--notify-ready=no',
        # A member's `/etc/resolv.conf` is the image's answer to a question it
        # was built with, and overwriting it would undo that answer: the member
        # carrying the overlay resolves through public servers on purpose,
        # because it has to come up while the LAN's resolvers — two other
        # members of this estate — are down.
        '--resolv-conf=off',
        # Every image configures its own interface from inside, and that takes
        # the one capability nspawn's default set does not keep.
        '--capability=CAP_NET_ADMIN',
        f'--kill-signal={KILL_SIGNAL}',
        f'--machine={container.name}',
        f'--directory={root_path(container.name)}',
        f'--setenv=S6_KILL_GRACETIME={S6_KILL_GRACETIME}',
    ]
    # What the member itself is told. The unit's own `Environment=` would not do:
    # that is the environment of the `systemd-nspawn` process on the device, and
    # what has to be set is the environment of PID 1 inside the container.
    command.extend(f'--setenv={name}={value}' for name, value in container.environment.items())
    if not container.host_network:
        command.append(f'--network-bridge={CONTAINER_BRIDGE}')
    if container.state:
        command.append(f'--bind={state_path(container.name)}:{container.state}')
    for device in container.devices:
        command.append(f'--bind={device}')
    for dropin in container.files:
        command.append(f'--bind-ro={dropin_path(container.name, dropin)}:{dropin.target}')

    lines = [
        '[Unit]',
        f'Description={conventions.CLUSTER_NAME} estate container {container.name}',
        'After=network-online.target',
        'Wants=network-online.target',
        '',
        '[Service]',
        'Type=notify',
        f'ExecStart={" ".join(command)}',
        'Restart=always',
        'RestartSec=5',
        'KillMode=mixed',
    ]
    if container.devices:
        # A bind alone does not grant access to a device node; the unit's own
        # control-group policy has to admit it as well, or the container sees
        # the file and cannot open it. Naming any device at all is what turns
        # that policy from open into a list, so the list also has to carry what
        # a container needs anyway: its console is a pseudo-terminal, and
        # pseudo-terminals are not in the small set every service gets.
        lines.extend(f'DeviceAllow={device} rw' for device in container.devices)
        lines.append('DeviceAllow=char-pts rw')
    lines.extend(('', '[Install]', 'WantedBy=multi-user.target', ''))
    return '\n'.join(lines)


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
        ENV_IPV4_CIDR: f'{address}/{conventions.VLAN_CONTAINER.prefixlen}',
        ENV_IPV4_GATEWAY: str(_container_gateway()),
        ENV_IPV6_TOKEN: f'::{address}',
    }


def caddyfile(*, adguard: Mapping[str, IPv4Address]) -> str:
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
    blocks = [
        '\n'.join(
            (
                f'{VHOST_CONTROLLER} {{',
                '\ttls {',
                f'\t\tdns cloudflare {{file.{CADDY_TOKEN_PATH}}}',
                '\t}',
                '\treverse_proxy https://127.0.0.1:443 {',
                '\t\ttransport http {',
                '\t\t\ttls_insecure_skip_verify',
                '\t\t}',
                '\t}',
                '}',
            )
        )
    ]
    blocks.extend(
        '\n'.join(
            (
                f'{VHOST_ADGUARD[instance]} {{',
                '\ttls {',
                f'\t\tdns cloudflare {{file.{CADDY_TOKEN_PATH}}}',
                '\t}',
                f'\treverse_proxy http://{address}:{ADGUARD_API_PORT}',
                '}',
            )
        )
        for instance, address in sorted(adguard.items())
    )
    return '\n\n'.join(blocks) + '\n'


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
    upstreams = '\n'.join(f'    - {upstream}' for upstream in ADGUARD_UPSTREAMS)
    return '\n'.join(
        (
            f'# Seed configuration written by the {conventions.CLUSTER_NAME} physical stack.',
            "# Installed only where the instance has none: rewrites are the dns stack's,",
            '# written through the API, and live in this same file once accepted.',
            'http:',
            f'  address: {address}:{ADGUARD_API_PORT}',
            'dns:',
            f'  bind_hosts:\n    - {address}',
            '  port: 53',
            '  upstream_dns:',
            upstreams,
            '  bootstrap_dns:',
            '    - 9.9.9.9',
            '    - 1.1.1.1',
            'schema_version: 29',
            '',
        )
    )


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
    ordered = _startup_order(containers)
    units = ' '.join(unit_name(container.name) for container in ordered)
    stamped = '\n'.join(
        '        {unit}) inputs="{inputs}" ;;'.format(
            unit=unit_name(container.name),
            inputs=' '.join(_stamp_inputs(container)),
        )
        for container in ordered
    )
    seeded = (
        '\n'.join(
            '        {unit}) source={source}; destination={destination} ;;'.format(
                unit=unit_name(container.name),
                source=config_path(container.name, container.seed.source),
                destination=f'{state_path(container.name)}/{container.seed.into}',
            )
            for container in ordered
            if container.seed is not None
        )
        or '        # no member of this estate owns its own configuration'
    )
    return f"""#!/bin/sh
# Managed by the {conventions.CLUSTER_NAME} physical stack. Local edits are overwritten.
#
# Re-establishes the container estate and the routing configuration from the
# desired state under {conventions.GW_DATA_ROOT}. Runs at boot, when nothing else is present, and
# again after every deployment, so the recovery path is never the untested one.
set -eu

UNITS={UNIT_DIR}
SYSTEMD=/etc/systemd/system
STATE={STATE_DIR}
DECLARED="{units}"

stamp_inputs() {{
    case "$1" in
{stamped}
        *) inputs="" ;;
    esac
    echo "$inputs"
}}

# Place a container's own configuration where nothing put one. Whatever is
# already there belongs to the software that wrote it -- the resolvers' rewrites
# arrive through their API and land in this very file -- so an existing file is
# never touched.
seed_state() {{
    source=""
    destination=""
    case "$1" in
{seeded}
    esac
    [ -n "$destination" ] || return 0
    [ -e "$source" ] || return 0
    if [ -e "$destination" ]; then
        return 0
    fi
    mkdir -p "$(dirname "$destination")"
    cp "$source" "$destination"
}}

install -d "$SYSTEMD" "$STATE"

# What is declared *and* has landed. A deployment writes this script before the
# files it describes, so a member whose unit or root filesystem tree is still
# missing is a member this run has nothing to do about -- not an error, and not
# a reason to leave the rest unconverged.
READY=""
for unit in $DECLARED; do
    [ -e "$UNITS/$unit" ] || continue
    machine=${{unit#{UNIT_PREFIX}}}
    [ -d "{ROOT_DIR}/${{machine%.service}}" ] || continue
    install -m {CONFIG_MODE} "$UNITS/$unit" "$SYSTEMD/$unit"
    READY="$READY $unit"
done

# Retire what is not declared at all. Only this estate's own units are
# candidates: the device has plenty of others and none of them are ours.
for live in "$SYSTEMD/{UNIT_PREFIX}"*.service; do
    [ -e "$live" ] || continue
    name=$(basename "$live")
    case " $DECLARED " in
        *" $name "*) continue ;;
    esac
    systemctl disable --now "$name" || true
    rm -f "$live"
    rm -f "$STATE/$name.stamp"
done

# The routing daemon reads outside {conventions.GW_DATA_ROOT}, so its configuration is copied
# rather than linked; a device that lost it in an update gets it back here.
if [ -e {FRR_CONFIG} ]; then
    install -m {FRR_MODE} {FRR_CONFIG} {FRR_LIVE_CONFIG}
    systemctl reload frr || systemctl restart frr || true
fi

systemctl daemon-reload

# Start what changed, in an order that leaves the member carrying this session
# for last.
for unit in $READY; do
    systemctl enable "$unit" >/dev/null 2>&1 || true
    seed_state "$unit"
    stamp="$STATE/$unit.stamp"
    want=$(cat $(stamp_inputs "$unit") 2>/dev/null | cksum)
    have=$(cat "$stamp" 2>/dev/null || echo none)
    if [ "$want" = "$have" ] && systemctl is-active --quiet "$unit"; then
        continue
    fi
    systemctl restart "$unit"
    echo "$want" > "$stamp"
done
"""


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


def _container_gateway() -> IPv4Address:
    """The container VLAN's own router, which is this device: the first address."""
    return next(conventions.VLAN_CONTAINER.hosts())


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


def parse_addresses(raw: object) -> dict[str, IPv4Address]:
    """Read the bridged members' addresses from stack configuration."""
    entries = facts.mapping(raw, 'the estate address configuration')
    return {member: IPv4Address(facts.text(entries, member, f'the address of {member}')) for member in entries}


#: The length of a hex-encoded SHA-256 digest.
_SHA256_LENGTH = 64


def census(
    *,
    rootfs: Mapping[str, Rootfs],
    addresses: Mapping[str, IPv4Address],
    acme_token: pulumi.Input[str],
) -> tuple[Container, ...]:
    """The estate as the design has it: four members, one of them host-networked.

    `rootfs` pins each member's image and `addresses` places the bridged ones on
    the container VLAN. Both are site facts rather than conventions — an image
    digest is whatever the build produced, and the resolvers' addresses are
    already written into every lease on the LAN — so they arrive as stack
    configuration and are checked here against the census the design names.
    """
    missing = [name for name in conventions.GW_ESTATE if name not in rootfs]
    if missing:
        raise ValueError(f'the estate has no image pinned for {", ".join(missing)}')
    unknown = sorted(set(rootfs) - set(conventions.GW_ESTATE))
    if unknown:
        raise ValueError(f'{", ".join(unknown)} is not a member of the estate')

    bridged = [name for name in conventions.GW_ESTATE if name != HOST_NETWORK_MEMBER]
    unplaced = [name for name in bridged if name not in addresses]
    if unplaced:
        raise ValueError(f'the estate has no address for {", ".join(unplaced)}')

    adguard = {name: addresses[name] for name in VHOST_ADGUARD}
    containers = [
        Container(
            name='caddy',
            rootfs=rootfs['caddy'],
            address=addresses['caddy'],
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
                Dropin(name='Caddyfile', target=CADDY_CONFIG, content=caddyfile(adguard=adguard)),
                Dropin(name='cloudflare.token', target=CADDY_TOKEN_PATH, content=acme_token, secret=True),
            ),
        )
    ]
    containers.extend(
        Container(
            name=instance,
            rootfs=rootfs[instance],
            address=addresses[instance],
            state=ADGUARD_STATE,
            # The address, delivered where the image reads it: its `net-setup`
            # service configures the interface out of PID 1's environment before
            # the resolver it guards is allowed to start, so a resolver never
            # comes up on an address the LAN was not told about.
            environment=net_setup_environment(addresses[instance]),
            files=(
                # Read-only beside the installation rather than in the working
                # directory: the working directory is where the live
                # configuration goes, and a seed delivered into it would be a
                # second file the instance might one day decide to read.
                Dropin(
                    name=ADGUARD_SEED,
                    target=f'{ADGUARD_INSTALL}/{ADGUARD_SEED}',
                    content=adguard_seed(addresses[instance]),
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
        for instance in sorted(VHOST_ADGUARD)
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
