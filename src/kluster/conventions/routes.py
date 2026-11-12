"""How each application is reached: its `Exposure`, and what the census of them derives.

An application's exposure answers the two questions cluster/architecture.md
§3.6 asks of every deployment: *which Gateway* and *how*. Which Gateway is
three peers, `internet`, `lan` and `media`, the same fields on every row where
they mean something. How is the row's type: a `Route` or a `Passthrough` goes
through a Gateway's proxy, by name, and a `Port` is raw, at a Gateway's
address. Setting a field chooses that Gateway, and the field's type carries
what only that Gateway and that way of reaching it mean, so a combination that
means nothing cannot be written (declarative/dns.md §5). What a type cannot
say is refused where the row is constructed, so a census that holds one does
not load.

`EXPOSURES` maps each application to its exposure, and it is the census: the
rows `dns`, `physical` and `k8s-base` read derive from it, and an application's
component receives its own (declarative/README.md §2). Three programs read it,
which is what places it here rather than in any one stack's area.

Derived from the census here rather than in a reader, because more than one
program reads each:

-   **The served zones** (`served_zones`), the zones the cluster issues a
    certificate for: `k8s-base` declares one certificate per zone, and the
    `credentials` command scopes the DNS-01 token to the same set (rfc-007
    §5.2).
-   **The public names** (`public_names`), each name the census publishes a
    record for and whether that record is proxied, which `apps` declares and
    the `dns` stack's records are held against.
-   **The raw ports** (`ports`), which `physical` reads for the balancer's
    listeners and the site's forward and pinhole, and `apps` for the
    Services.

The **route label** (`ROUTE_LABEL`) is what a namespace carries for a route
declared in it to attach to a Gateway: the Gateways' `allowedRoutes` select on
it, and the component declaring a route sets it on its namespace.

Read qualified -- `conventions.routes.EXPOSURES` -- because the names a row is
built from are common nouns that mean one particular thing only while the
census stands beside them: `Route`, `Port`, `Host`, `Direct`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address
from types import MappingProxyType

from kluster.conventions.cluster import Transports
from kluster.conventions.dns import PRIMARY_ONLY, ZONE_PRIMARY
from kluster.conventions.identity import LABEL_DOMAIN

__all__ = (
    'BARE',
    'EXPOSURES',
    'PROXIED',
    'RAW_PORT',
    'REWRITE',
    'ROUTE_LABEL',
    'Apex',
    'Balancer',
    'Bare',
    'Dedicated',
    'Device',
    'Direct',
    'Exposure',
    'Gate',
    'Heard',
    'Held',
    'Host',
    'Passthrough',
    'Port',
    'Proxied',
    'PublicName',
    'Rewrite',
    'Route',
    'RouteRow',
    'SiteForward',
    'Vip',
    'ports',
    'public_names',
    'served_zones',
)


def _refuse_name(host: str, *, what: str) -> None:
    """Refuse a name a row publishes that is no name at all.

    A row's name is a label under each zone it is published in, so an empty
    one publishes the zone's apex under a row that does not say so, and `@` is
    the apex spelled out: a website's apex is an `Apex`, whose record the
    `dns` stack holds.
    """
    if not host:
        raise ValueError(f'{what} names no host')
    if host == '@':
        raise ValueError(f"{what} names the apex as its host; a website's apex is an `Apex`")


#: The ports a socket can be bound to: 0 asks the kernel for any, and the
#: field is sixteen bits.
_PORTS = range(1, 65536)


def _refuse_port(port: int, *, what: str) -> None:
    if port not in _PORTS:
        raise ValueError(f'{what} is on port {port}, which no socket listens on')


class Gate(Enum):
    """What the authentication filter asks the portal for, on a name it gates."""

    SESSION = 'session'
    """The portal's session, which a browser follows a redirect to get."""
    BASIC = 'basic'
    """HTTP Basic, for a client that cannot follow a redirect."""


@dataclass(frozen=True)
class Proxied:
    """On `internet`: published behind the proxy, gated or not."""

    gate: Gate | None = None


@dataclass(frozen=True)
class Direct:
    """On `internet`: published past the proxy, and why (declarative/dns.md §2)."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError('a name published direct says why')


@dataclass(frozen=True)
class Rewrite:
    """On `lan` or `media`: the name is rewritten to that Gateway's VIP for a home client."""


@dataclass(frozen=True)
class Bare:
    """On `lan`: rewritten as `Rewrite` is, and the bare label too, which redirects to the name."""


#: The plain values: proxied with no gate, and the two home sides.
PROXIED = Proxied()
REWRITE = Rewrite()
BARE = Bare()


def _refuse_sides(lan: object, media: object, *, what: str) -> None:
    """Refuse a row on no Gateway, or on both home Gateways.

    A host has one rewrite, and the media VIP already answers every home
    network the LAN VIP does, so a row on `media` serves the whole home.
    """
    if lan is not None and media is not None:
        raise ValueError(
            f'{what} sets both `lan` and `media`; a name has one rewrite, and `media` answers the whole home'
        )


@dataclass(frozen=True)
class Route:
    """An application reached over HTTPS by name, on the Gateways it sets."""

    host: str
    """The label, relative to each zone it is published in."""
    internet: Proxied | Direct | None = None
    lan: Rewrite | Bare | None = None
    media: Rewrite | None = None
    zones: tuple[str, ...] = PRIMARY_ONLY
    """Which zones serve the application. The default is the primary alone: a
    name costs a certificate in every zone it is published in, so a row names
    another zone by saying so in the open."""

    def __post_init__(self) -> None:
        what = f'the route {self.host!r}'
        _refuse_name(self.host, what='a route')
        if self.internet is None and self.lan is None and self.media is None:
            raise ValueError(f'{what} sets no Gateway')
        _refuse_sides(self.lan, self.media, what=what)
        if not self.zones:
            raise ValueError(f'{what} is published in no zone')


@dataclass(frozen=True)
class Passthrough:
    """TLS the pod terminates, on 443, by name: the Gateways pass it on unread and hold no certificate for it."""

    host: str
    """The label, in the primary zone."""
    internet: Direct | None = None
    lan: Rewrite | None = None
    media: Rewrite | None = None

    def __post_init__(self) -> None:
        what = f'the passthrough {self.host!r}'
        _refuse_name(self.host, what='a passthrough')
        if self.internet is None and self.lan is None and self.media is None:
            raise ValueError(f'{what} sets no Gateway')
        _refuse_sides(self.lan, self.media, what=what)

    @property
    def zones(self) -> tuple[str, ...]:
        """The primary alone: a passthrough takes no certificate, so no other zone is served for it."""
        return PRIMARY_ONLY


@dataclass(frozen=True)
class Apex:
    """A website's apex, on `internet`; the `dns` stack holds its address, as the web origin."""

    zones: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.zones:
            raise ValueError('an apex is the apex of no zone')


@dataclass(frozen=True)
class Host:
    """A name a raw port is dialed at, in the primary zone, published direct: a raw port cannot cross the proxy."""

    label: str

    def __post_init__(self) -> None:
        _refuse_name(self.label, what='a raw port')


@dataclass(frozen=True)
class Held[T: (Route, Passthrough, Host)]:
    """A row whose name the legacy VPS still holds (rfc-008 §3.3).

    A held row's route attaches to its Gateways, and the row publishes no
    record and no rewrite: the name resolves where the legacy side publishes
    it until the cut takes the wrapper off. A row on `internet` is held
    exactly while a record of the `dns` stack points its name at the legacy
    VPS, which the census tests hold as a relation.

    The wrapper is a migration-period statement, and it leaves with the last
    such record: this class, the branch in each derivation that reads it, and
    that relation test go together, and no row changes shape.
    """

    row: T


@dataclass(frozen=True)
class Balancer:
    """`internet` at the balancer, with the names the port is dialed at."""

    hosts: tuple[Host | Held[Host], ...] = ()

    def __post_init__(self) -> None:
        labels = [host.row.label if isinstance(host, Held) else host.label for host in self.hosts]
        if len(labels) != len(set(labels)):
            raise ValueError(f'a balancer front names a host twice: {labels}')


@dataclass(frozen=True)
class Dedicated:
    """`internet` at the dedicated node's own address, which no listener of the balancer forwards to."""


@dataclass(frozen=True)
class SiteForward:
    """`internet` through the home site's forward and pinhole, to the worker's own address."""


@dataclass(frozen=True)
class Vip:
    """`lan` or `media` at that Gateway's VIP, with the name a home client dials it at."""

    host: str

    def __post_init__(self) -> None:
        _refuse_name(self.host, what="a raw port's VIP")


@dataclass(frozen=True)
class Heard:
    """`lan` or `media`: found by a packet that stays on its segment, which a relay on the host carries to the VIP."""

    group: IPv4Address | None = None
    """The multicast group the client sends to; `None` is the broadcast."""

    def __post_init__(self) -> None:
        # The relay joins the group, and an address that is no group carries
        # nothing to join, with no error anywhere.
        if self.group is not None and not self.group.is_multicast:
            raise ValueError(f'a heard side joins {self.group}, which is no multicast group')


@dataclass(frozen=True)
class Port:
    """A raw port, at the addresses of the Gateways it sets."""

    name: str
    """What the port serves, never its number: what is declared for it is
    named after it."""
    port: int
    transports: Transports
    internet: Balancer | Dedicated | SiteForward | None = None
    lan: Vip | Heard | None = None
    media: Vip | Heard | None = None

    def __post_init__(self) -> None:
        what = f'the port {self.name!r}'
        if not self.name:
            raise ValueError('a raw port has no name, and what is declared for it is named after it')
        _refuse_port(self.port, what=what)
        if self.internet is None and self.lan is None and self.media is None:
            raise ValueError(f'{what} sets no Gateway')
        _refuse_sides(self.lan, self.media, what=what)
        heard = isinstance(self.lan, Heard) or isinstance(self.media, Heard)
        if heard and self.transports is not Transports.UDP:
            raise ValueError(f'{what} is heard, and only a packet is heard: it is served on UDP alone')


@dataclass(frozen=True)
class Device:
    """A machine at home that answers in place of pods, through a relay on the worker."""

    address: IPv4Address
    port: int
    self_signed: bool = False
    """It serves TLS on a certificate of its own."""

    def __post_init__(self) -> None:
        _refuse_port(self.port, what=f'the device at {self.address}')


type RouteRow = Route | Passthrough | Apex | Held[Route] | Held[Passthrough]


@dataclass(frozen=True)
class Exposure:
    """How one application is reached."""

    routes: tuple[RouteRow, ...] = ()
    ports: tuple[Port, ...] = ()
    device: Device | None = None

    def __post_init__(self) -> None:
        if not self.routes and not self.ports:
            raise ValueError('an exposure states no route and no port')
        if self.device is not None and (
            self.ports or not self.routes or not all(isinstance(_unwrapped(row), Route) for row in self.routes)
        ):
            raise ValueError("a device's relay forwards to one port, so it backs `Route`s alone")
        named = [row for row in map(_unwrapped, self.routes) if isinstance(row, Route)]
        # A route sets some Gateway, so one with no home side is on `internet` alone.
        internet_alone = [row.host for row in named if row.lan is None and row.media is None]
        home_alone = [row.host for row in named if row.internet is None]
        if internet_alone and home_alone:
            raise ValueError(
                f'the routes {internet_alone[0]!r} and {home_alone[0]!r} are one service under two names: '
                'a service reached from home and from outside has one name, its home side a field of that row'
            )


def _unwrapped(row: RouteRow) -> Route | Passthrough | Apex:
    """The row itself, whether the legacy VPS holds it or not."""
    return row.row if isinstance(row, Held) else row


#: Every application's exposure, by application. A row whose listener or
#: forward `physical` declares ahead of its application stands here before the
#: application's component is wired, because `physical` reads it (rfc-008
#: §3.1); every other row arrives with its application's land.
EXPOSURES: Mapping[str, Exposure] = MappingProxyType(
    {
        # The peer port, at the balancer for a roaming device and at the LAN
        # VIP for one at home. `syncapi` is the name a device dials it at,
        # held while the legacy VPS serves it.
        'syncthing': Exposure(
            ports=(
                Port(
                    'syncthing',
                    22000,
                    Transports.TCP_AND_UDP,
                    internet=Balancer((Held(Host('syncapi')),)),
                    lan=Vip('syncapi'),
                ),
            )
        ),
        # The bulk-transfer peer port, through the site's forward and
        # pinhole: inbound peer traffic lands where its outbound traffic
        # leaves, the worker's own address.
        'qbittorrent': Exposure(
            ports=(Port('qbittorrent-peer', 53363, Transports.TCP_AND_UDP, internet=SiteForward()),)
        ),
        # hath leaves and arrives on one address of its own.
        'hath': Exposure(ports=(Port('hath', 60011, Transports.TCP, internet=Dedicated()),)),
    }
)


#: The label a namespace carries for an `HTTPRoute` declared in it to attach
#: to a Gateway, as the one-entry mapping both sides use: the namespace's
#: labels, and the `matchLabels` of each Gateway's `allowedRoutes`. A route in
#: a namespace without it -- one a chart installs, say -- attaches to nothing.
ROUTE_LABEL: Mapping[str, str] = MappingProxyType({f'{LABEL_DOMAIN}/routes': 'attach'})


def _census(exposures: Iterable[Exposure] | None) -> tuple[Exposure, ...]:
    """The exposures given, or the census's, read when called rather than when the module was imported."""
    return tuple(EXPOSURES.values() if exposures is None else exposures)


def served_zones(exposures: Iterable[Exposure] | None = None) -> tuple[str, ...]:
    """The zones the cluster serves, which is where it issues a certificate: the primary, then every zone a row takes one in.

    **The primary is served by definition**, not by a row: the cluster's own
    endpoints are names under it, and so is the first certificate the cluster
    issues, which comes before the first application's row does. Every other
    zone is served because a `Route` or an `Apex` publishes in it, held or
    not, since verifying a held row needs the zone's certificate. A
    `Passthrough` and a raw port's `Host` take none: the pod holds the one,
    and the other is no TLS of the cluster's.

    In that order, each zone once, so a reader that declares one resource per
    zone declares them in the same order on every run. `exposures` is the
    census unless given.
    """
    zones = [ZONE_PRIMARY]
    for exposure in _census(exposures):
        for row in map(_unwrapped, exposure.routes):
            if not isinstance(row, Route | Apex):
                continue
            for zone in row.zones:
                if zone not in zones:
                    zones.append(zone)
    return tuple(zones)


@dataclass(frozen=True)
class PublicName:
    """One name the census publishes a record for: a CNAME to the cluster anchor, in one zone."""

    host: str
    zone: str
    unproxied: str
    """Why the record is published past the proxy; empty where it is proxied."""


#: Why a raw port's name is published direct.
RAW_PORT = 'a raw port, which the proxy does not carry'


def _published(exposure: Exposure) -> Iterator[PublicName]:
    for row in exposure.routes:
        if isinstance(row, Held | Apex):
            continue
        if isinstance(row.internet, Proxied):
            yield from (PublicName(row.host, zone, '') for zone in row.zones)
        elif isinstance(row.internet, Direct):
            yield from (PublicName(row.host, zone, row.internet.reason) for zone in row.zones)
    for port in exposure.ports:
        if isinstance(port.internet, Balancer):
            for host in port.internet.hosts:
                if isinstance(host, Host):
                    yield PublicName(host.label, ZONE_PRIMARY, RAW_PORT)


def public_names(exposures: Iterable[Exposure] | None = None) -> tuple[PublicName, ...]:
    """Every name the census publishes, once each, in census order.

    A row on `internet` publishes its host in each of its zones, and a raw
    port's `Host` in the primary; a held row publishes nothing, and an `Apex`
    nothing either, since its address is the `dns` stack's web origin. Rows
    that share a host publish one record: the first row's, which the census
    invariants hold to be direct wherever a `Host` shares it (rfc-008 §4.1,
    "one host, one answer").
    """
    names: dict[tuple[str, str], PublicName] = {}
    for exposure in _census(exposures):
        for name in _published(exposure):
            _ = names.setdefault((name.host, name.zone), name)
    return tuple(names.values())


def ports(exposures: Iterable[Exposure] | None = None) -> tuple[Port, ...]:
    """Every raw port the census holds, in census order."""
    return tuple(port for exposure in _census(exposures) for port in exposure.ports)
