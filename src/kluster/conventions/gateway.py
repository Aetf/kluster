"""The gateway device: where its files live, what it runs, and under which names.

The device is the home site's router, and the one machine this program
configures through three doors at once (physical/gateway.md). What is here is
the half that is a decision of this repository: the paths the desired state
occupies, the account it is delivered as, the services the device runs and the
names they answer to.

Read qualified — `conventions.gateway.DATA_ROOT` — because the names in here
are short enough to be ambiguous on their own, and the module is what says
which machine they belong to (rfc-002 §3.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address
from typing import ClassVar

from kluster.conventions.dns import ZONE_PRIMARY
from kluster.conventions.identity import CLUSTER_NAME

#: The account the gateway is configured as. It has no other.
SSH_USER = 'root'

#: The key the device must present, pinned. Code rather than configuration for
#: the reasons the homelab host's pin already carries
#: (`conventions.HOMELAB_HOST_KEY`): a public key is not a secret, and a pin
#: typed in beside the client credential could be replaced by whoever could
#: already replace the credential. Being code is also what puts it in a
#: preview, which is where a reviewer checks the pin a session will be held to
#: — a secret-typed configuration value is redacted there (rfc-002 §11).
#: Stored in `authorized_keys` form — the bare `ssh-ed25519 AAAA…` blob, with
#: no host name in front of it — so it matches the device at whichever address
#: the session dials.
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINrKKu2hnEHPUrWm4TEN40YFQVI3JEPfQQDUebNj0R4k'

#: The other half of the same handshake: the public key of the credential this
#: program opens the session with, which the device must hold for any of the
#: rest to be deliverable. Here for the same reasons `HOST_KEY` is — a public
#: key is not a secret, and code is what a preview shows — and the private half
#: is the provider's own configuration, read in its `configure` and nowhere
#: else (rfc-002 §7.4).
#:
#: Stored as the whole `authorized_keys` line, comment included, because that
#: is what lands on the device and what the converger compares against: a line
#: differing only in its comment is a second key as far as the file is
#: concerned.
CLIENT_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHbSmpDYOHlgjIfrVs9WZ7BAl7kgwpFqquLcqtJuK9iy kluster-physical@gw'

#: The gateway's desired-state root. `/data` is the one directory that survives
#: a firmware update, which is why everything this program puts on the device
#: lives under it (architecture.md §5.2); `on_boot.d` holds the scripts that
#: re-establish the services after one, with no expectation that Pulumi is
#: reachable at boot, and its path is fixed by the vendored `udm-boot.service`
#: rather than chosen here.
#:
#: `custom` is the single root everything else occupies: the executables, the
#: unit sources, the offline package cache, and whatever directory a layer of
#: the gateway asks for. Two roots rather than one because they answer to two
#: different owners — the boot chain's directory is upstream's, and everything
#: below `custom` is this program's.
DATA_ROOT = '/data'
ON_BOOT_D = f'{DATA_ROOT}/on_boot.d'
CUSTOM_ROOT = f'{DATA_ROOT}/custom'

#: AdGuard Home's administration and API port. Three declarations meet on it:
#: the caddy vhost that proxies each instance's interface, the initial state
#: that tells the instance where to listen, and the overlay flow rule that
#: admits a continuous-integration member to exactly that port so the `dns`
#: stack can write its rewrites. It is a convention rather than a constant each
#: of them keeps, because the three are free to disagree and the failure is a
#: resolver that answers nothing anyone asked it.
ADGUARD_API_PORT = 3000

#: The controller console, served by the gateway's own reverse proxy rather
#: than by any service of the census below: what answers behind the name is the
#: device's own web server.
VHOST_CONTROLLER = f'unifi.{ZONE_PRIMARY}'

#: The controller site the UniFi resources are declared in. `default` is the
#: internal name whatever the site is labelled in the interface.
UNIFI_SITE = 'default'

#: The cluster VLAN as controller-side objects: the network the gateway serves
#: it as, and the firewall zone that network is alone in. The contrast with the
#: `lan` pool is the whole point — the VLAN is a network object *so that* it
#: can be named, and the pool is not one so that it cannot fight the host
#: routes.
UNIFI_NETWORK_CLUSTER = CLUSTER_NAME
UNIFI_ZONE_CLUSTER = CLUSTER_NAME


@dataclass(frozen=True)
class BridgedService:
    """A service on the container VLAN: it has an address, and may serve a name."""

    name: str
    #: Its static address on the container VLAN. Nothing outside this program
    #: assigns it: every bridged service takes it from the environment its own
    #: unit injects, and where the LAN needs to reach one, its leases already
    #: name it.
    address: IPv4Address
    #: The build the service runs, named as its registry repository names it
    #: (`image_repository`). Distinct from the service's own name because two
    #: services may be two instances of one build: the resolvers are.
    artifact: str
    #: The public name the gateway serves the service under, where it serves
    #: one. It is a name in a public zone that public resolvers do not answer
    #: for — the split-horizon rewrite steers LAN clients to it and no
    #: Cloudflare record is published (dns.md §4).
    vhost: str | None = None


@dataclass(frozen=True)
class HostNetworkService:
    """A service in the host's own network namespace: no address of its own.

    Its interface has to land in the main network namespace for the gateway to
    route through it (architecture.md §5.3), which also means there is nothing
    for the gateway to proxy a name to.
    """

    name: str
    #: The build the service runs, named as its registry repository names it
    #: (`image_repository`).
    artifact: str


ContainerService = BridgedService | HostNetworkService

#: The reverse proxy: the one service that answers for the device's own names.
CADDY = BridgedService(name='caddy', address=IPv4Address('10.0.5.180'), artifact='caddy')

#: The LAN's name service, in the order the design lists it. Two instances
#: because every lease on the LAN names both, and a resolver each is what makes
#: one of them replaceable. One build behind both, and a pin each: what the two
#: keys buy is proving a new resolver build on one instance before the other
#: (rfc-002 §11.1).
RESOLVERS: tuple[BridgedService, ...] = (
    BridgedService(
        name='adguard-alice',
        address=IPv4Address('10.0.5.3'),
        artifact='adguard',
        vhost=f'alice.{ZONE_PRIMARY}',
    ),
    BridgedService(
        name='adguard-bob',
        address=IPv4Address('10.0.5.4'),
        artifact='adguard',
        vhost=f'bob.{ZONE_PRIMARY}',
    ),
)

#: The overlay daemon: the one service in the host's own network namespace,
#: because the interface it creates has to be visible to the router that uses
#: it. It keeps the daemon's own name — the software is ZeroTier, and the
#: device's unit and image are named after it.
OVERLAY = HostNetworkService(name='zerotier', artifact='zerotier')

#: What the device runs. Each entry is named above as well, because a
#: declaration binds to the entry itself rather than looking one up by name
#: (rfc-002 §5.3); this tuple is for the readers that want the whole set —
#: the roster of unit names, the configuration completeness check.
SERVICES: tuple[ContainerService, ...] = (CADDY, *RESOLVERS, OVERLAY)

#: The retiring LAN zone (dns.md §4.3), and the second wildcard the proxy holds
#: a certificate for. It is a name inside `ucw.phd` rather than a zone of its
#: own at the registrar, so the DNS-01 challenge for `*.lan.ucw.phd` is written
#: into that zone: the device's ACME token has to be scoped to `ucw.phd` for as
#: long as the census below has a row in it.
ZONE_LEGACY = 'lan.ucw.phd'

#: Which resolver the legacy block's issuance checks propagation against, and
#: the one directive the declared block has no use for. The LAN's own resolvers
#: answer this zone from a rewrite that points every name in it at the proxy,
#: so a check that asked them would read the site's answer rather than the
#: record the registrar published.
LEGACY_ACME_RESOLVER = '1.1.1.1'

#: The homelab host as the device plane names it: a DHCP-derived name the
#: device's own resolver answers and no public one does (dns.md §4.1), which is
#: why the proxy resolves through that resolver. Most of the census below
#: proxies to a port on this one host, and it is spelled once so that no two
#: rows can disagree about it.
LEGACY_UPSTREAM_HOST = 'aetf-arch-homelab.home.arpa'

#: The port the resolvers' interfaces answer the legacy names on, which is not
#: `ADGUARD_API_PORT`. The cutover carries each instance's live configuration
#: into its machine directory, and that configuration binds the interface to
#: port 80; the API port is what an instance that started with no state would
#: bind. The three rows below name the instances the device is running, so they
#: move to the API port when those instances do.
LEGACY_RESOLVER_PORT = 80


class Wave(Enum):
    """A wave of the migration plan, as `cluster/migration.md` §2 numbers them.

    Only the three waves that retire a legacy vhost are spelled: a row cannot
    be scheduled for a wave that lands after the zone it is in has retired.
    """

    B = 'B'
    C = 'C'
    D = 'D'


@dataclass(frozen=True)
class PlainUpstream:
    """A legacy vhost's upstream, reached over plain HTTP.

    There is nothing to configure beyond where it is: no transport, no
    certificate to trust, and no header to correct — which is why the two kinds
    of upstream are two types rather than one type with flags that only apply
    to half the rows.
    """

    host: str
    port: int

    #: What the render branches on: this kind has no transport to configure.
    self_signed: ClassVar[bool] = False

    @property
    def dial(self) -> str:
        """What `reverse_proxy` is given, with the scheme and port explicit."""
        return f'http://{self.host}:{self.port}'


@dataclass(frozen=True)
class SelfSignedUpstream:
    """A legacy vhost's upstream, reached over HTTPS on a certificate of its own.

    That certificate is not verified: the upstream is an appliance whose
    certificate is its own to choose and not this program's to replace, and
    what the client asked for is terminated at the proxy.
    """

    host: str
    #: Whether the client's `Host` header is forwarded rather than replaced by
    #: the upstream's own name. It belongs to this kind alone, because Caddy
    #: rewrites the header for a scheme-qualified upstream and a plain row is
    #: never one.
    pass_host_header: bool = False

    #: What the render branches on: this kind configures a transport.
    self_signed: ClassVar[bool] = True

    @property
    def dial(self) -> str:
        """What `reverse_proxy` is given. The scheme carries the port."""
        return f'https://{self.host}'


@dataclass(frozen=True)
class LegacyVhost:
    """One name under the retiring LAN zone that the proxy answers for meanwhile.

    A row is here because what answers behind the name has not moved yet. The
    declaration replaces the device's live configuration whole in one window,
    weeks before the first application migrates, so a name that is not in this
    census is a name that stops being served on the day the device is taken
    over.

    Every row carries the wave that deletes it, and the census is empty from
    Wave D on — the same statement as the zone retiring (dns.md §4.3,
    migration.md §2). A row whose wave has landed is deleted here in the same
    change that gives its application a public name.
    """

    #: The one label under `ZONE_LEGACY`; a wildcard certificate covers one.
    label: str
    upstream: PlainUpstream | SelfSignedUpstream
    wave: Wave
    #: Whether the proxy also answers the bare label and redirects it here.
    #: Carried by the names that are typed rather than followed from a link.
    bare_name: bool = False

    @property
    def host(self) -> str:
        """The name clients ask for."""
        return f'{self.label}.{ZONE_LEGACY}'


#: What the proxy still answers for under the retiring zone, in the order the
#: device's own configuration lists it. The wave against each row is the wave
#: that deletes the row: for the six that are applications, the wave that
#: migrates the application, and for the five whose upstream is not migrating
#: at all, the wave by which the name has to be gone anyway.
LEGACY_VHOSTS: tuple[LegacyVhost, ...] = (
    # Jellyfin, on the homelab host.
    LegacyVhost(
        label='tube',
        upstream=PlainUpstream(host=LEGACY_UPSTREAM_HOST, port=8096),
        wave=Wave.C,
        bare_name=True,
    ),
    # qBittorrent's interface, host-native and onboarded with seedwatch.
    LegacyVhost(
        label='bt',
        upstream=PlainUpstream(host=LEGACY_UPSTREAM_HOST, port=9876),
        wave=Wave.D,
        bare_name=True,
    ),
    # The three resolver names. Nothing migrates them — the instances stay on
    # this device (migration.md §2) — and what retires them is the naming move
    # onto the public names the declared block already serves, which the `dns`
    # stack's first up publishes rewrites for. They are carried across the
    # window so that what is bookmarked keeps working, and `dns` stays pointed
    # at alice, which is the pair's sync origin.
    LegacyVhost(
        label='dns',
        upstream=PlainUpstream(host=str(RESOLVERS[0].address), port=LEGACY_RESOLVER_PORT),
        wave=Wave.B,
        bare_name=True,
    ),
    LegacyVhost(
        label='dns-alice',
        upstream=PlainUpstream(host=str(RESOLVERS[0].address), port=LEGACY_RESOLVER_PORT),
        wave=Wave.B,
    ),
    LegacyVhost(
        label='dns-bob',
        upstream=PlainUpstream(host=str(RESOLVERS[1].address), port=LEGACY_RESOLVER_PORT),
        wave=Wave.B,
    ),
    # The controller console, which `VHOST_CONTROLLER` also serves. Reached at
    # the device's own name on the LAN rather than at a loopback address, which
    # from the proxy's network namespace is the proxy. The console needs its
    # client's `Host`: it answers a WebSocket upgrade whose `Origin` does not
    # match the request's `Host` with a 500, which blanks its own interface.
    LegacyVhost(
        label='gw',
        upstream=SelfSignedUpstream(host='dmse.home.arpa', pass_host_header=True),
        wave=Wave.B,
        bare_name=True,
    ),
    # Spoolman and thread-dashboard, both on the homelab host today.
    LegacyVhost(label='spool', upstream=PlainUpstream(host=LEGACY_UPSTREAM_HOST, port=8000), wave=Wave.B),
    LegacyVhost(label='thread', upstream=PlainUpstream(host=LEGACY_UPSTREAM_HOST, port=8480), wave=Wave.B),
    # seedwatch, onboarded with the qBittorrent it reconciles.
    LegacyVhost(label='seedwatch', upstream=PlainUpstream(host=LEGACY_UPSTREAM_HOST, port=8490), wave=Wave.D),
    # golinks, already in the cluster and reached here at its LoadBalancer.
    LegacyVhost(
        label='go',
        upstream=PlainUpstream(host=LEGACY_UPSTREAM_HOST, port=8067),
        wave=Wave.B,
        bare_name=True,
    ),
    # The UPS network management card: a LAN appliance, and the one row no wave
    # moves. Its interface only accepts certificates in the card's own
    # proprietary format and its TLS stack is stuck on TLSv1.2, so a real
    # certificate is terminated at the proxy and the hop to the card is not
    # verified. Retiring it means serving it under a public name instead, which
    # is what the last wave the zone survives has to have done.
    LegacyVhost(label='ups', upstream=SelfSignedUpstream(host='ups.home.arpa'), wave=Wave.D),
)

#: Where the container root filesystems are published: one registry repository
#: per build, under a namespace of this estate's own. A pin carries its whole
#: reference (`versions:image-gateway-…`), and this is what that reference is
#: *checked against* rather than what it is assembled from — which is what
#: keeps two services that must run one build from pointing at two repositories
#: (rfc-002 §11.1). Moving publication elsewhere is then an edit here and to
#: the pins, reviewed together.
#:
#: The device is the site's UDM and there is one of it, and each repository
#: holds that one architecture, so a pin names no platform.
IMAGE_NAMESPACE = 'ghcr.io/aetf/homelab-containers'


def image_repository(artifact: str) -> str:
    """The registry repository one service's build is published to."""
    return f'{IMAGE_NAMESPACE}/{artifact}'


def image_pin(service: ContainerService) -> str:
    """The name one service's root filesystem is pinned under, in the `image` kind.

    One key per service and not one per build: the resolvers run the same
    image, and separate keys are what let one of them move first
    (`versions.image`, rfc-002 §11.1). The device is in the name because the
    `versions:` namespace is the whole repository's.
    """
    return f'gateway-{service.name}'
