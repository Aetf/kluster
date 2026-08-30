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
from ipaddress import IPv4Address

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

#: The gateway's desired-state root. `/data` is the one directory that survives
#: a firmware update, which is why everything this program puts on the device
#: lives under it (architecture.md §5.2); `on_boot.d` holds the scripts that
#: re-establish the services after one, with no expectation that Pulumi is
#: reachable at boot.
DATA_ROOT = '/data'
ON_BOOT_D = f'{DATA_ROOT}/on_boot.d'

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
    #: assigns it: the resolvers take it from the environment their unit
    #: injects, and the LAN's leases already name them.
    address: IPv4Address
    #: The build the service runs, as the release publishes it (`rootfs_url`).
    #: Distinct from the name because two services may be two instances of one
    #: build: the resolvers are.
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
    #: The build the service runs, as the release publishes it (`rootfs_url`).
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

#: Where the container root filesystems are published, and what a release calls
#: each asset. A pin says which release and which digest and nothing more
#: (`versions:rootfs-…`), because who publishes the artifacts and how they are
#: named is a decision rather than a value an operator maintains four copies of
#: (rfc-002 §11.1): moving publication elsewhere is an edit here and a review of
#: it. The device is the site's UDM and there is one of it, so one architecture
#: is the whole of the matrix.
ROOTFS_RELEASES = 'https://github.com/Aetf/homelab-containers/releases/download'
ROOTFS_ARCH = 'arm64'


def rootfs_url(*, release: str, artifact: str) -> str:
    """Where the archive one service boots from is published."""
    return f'{ROOTFS_RELEASES}/{release}/{artifact}-{ROOTFS_ARCH}.tar.zst'


def rootfs_pin(service: ContainerService) -> str:
    """The name one service's root filesystem is pinned under, in the `rootfs` kind.

    One key per service and not one per build: the resolvers run the same
    archive, and separate keys are what let one of them move first
    (`versions.rootfs`, rfc-002 §11.1). The device is in the name because the
    `versions:` namespace is the whole repository's.
    """
    return f'gateway-{service.name}'
