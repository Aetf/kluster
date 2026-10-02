"""How each application is reachable: which name, in which zones, through which gateways.

A route is an application's exposure, and two programs read a row: `apps`
builds the HTTPRoutes and the public records from it, `dns` builds the
split-horizon rewrites from the same row (declarative/dns.md §3). One edit
produces both, which is why an application cannot forget its rewrite -- it
never writes one. Two readers is also what places the table here rather than
in either stack's own area (declarative/README.md §2).

The census is empty while `apps` declares no application. It grows one row per
application as the migration proceeds, and each row's rewrite appears in a
`dns` preview the same day the application's route does.

Two things are derived from the census here rather than in either reader,
because two programs read each. The **served zones** (`served_zones`) are the
zones the cluster issues a certificate for: `k8s-base` declares one
certificate per zone, and the `credentials` command scopes the DNS-01 token to
the same set (rfc-007 §5.2). The **route label** (`ROUTE_LABEL`) is what a
namespace carries for a route declared in it to attach to a Gateway: the
Gateways' `allowedRoutes` select on it, and the component declaring a route
sets it on its namespace.

Read qualified -- `conventions.routes.ROUTES` -- because the names a row is
built from are common nouns that mean one particular thing only while the
census stands beside them: `Route`, `Extra`, `SELF`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from kluster.conventions.dns import PRIMARY_ONLY, ZONE_PRIMARY
from kluster.conventions.identity import LABEL_DOMAIN

__all__ = (
    'ROUTES',
    'ROUTE_LABEL',
    'SELF',
    'Exposure',
    'Extra',
    'Route',
    'SelfTarget',
    'Srv',
    'served_zones',
)


class SelfTarget(Enum):
    """A target that is the row's own published name in the zone at hand."""

    SELF = 'self'


#: The one value of `SelfTarget`, so a row writes `target=SELF`.
SELF = SelfTarget.SELF


@dataclass(frozen=True)
class Srv:
    """A service record an application publishes beside its own name."""

    label: str
    """The service and protocol, relative to each zone the row is published in."""
    priority: int
    weight: int
    port: int
    target: str | SelfTarget = SELF
    """The host that answers. A string is a fully qualified name elsewhere;
    `SELF` is the row's own name and is what an application's own service
    record wants."""


#: The record kinds a row may publish beside its name. A renderer over
#: `Route.extras` handles every member.
type Extra = Srv


class Exposure(Enum):
    """Which gateways serve a name, which is also what the rewrite says.

    The values are the matrix of cluster/architecture.md §3.6 in one field: an
    application is reachable from the internet, from the LAN, or from both,
    and the IoT VLAN is a LAN-side variant answered by the media VIP rather
    than the LAN one.

    One field rather than two flags, because two independent flags admit "no
    public record and no LAN answer" -- a route that publishes nothing
    anywhere, and there is no such thing. A combination this set cannot
    express is a fifth value and never a split into flags: `IOT` implies
    LAN-only, because the IoT VLAN reaches the media gateway alone
    (cluster-infra.md §2), so a public name answered by the media VIP is the
    value such an application would add.
    """

    PUBLIC = 'public'
    """Internet gateway only; LAN clients take the cloud path."""
    SPLIT = 'split'
    """Both gateways; LAN and ZeroTier clients are steered to the LAN VIP."""
    LAN_ONLY = 'lan-only'
    """LAN gateway only, and no public record at all -- the name resolves
    for LAN clients and NXDOMAINs everywhere else (dns.md §4)."""
    IOT = 'iot'
    """As LAN_ONLY, but answered by the media VIP so the IoT VLAN reaches
    it (cluster-infra.md §2)."""


@dataclass(frozen=True)
class Route:
    """One application hostname."""

    host: str
    """The label, relative to each zone it is published in."""
    exposure: Exposure = Exposure.PUBLIC
    zones: Sequence[str] = PRIMARY_ONLY
    """Which zones serve the application. The default is the primary alone: a
    name costs a certificate in every zone it is published in, so a row names
    another zone by saying so in the open -- because that zone's audience wants
    it there, or because a rewrite-only name is covered by that zone's
    wildcard."""
    proxied: bool = True
    """Cloudflare proxy on the public record. Off for non-HTTP ports and
    for uploads larger than the proxy's body limit."""
    extras: tuple[Extra, ...] = ()
    """What the application publishes beside its name, stated rather than
    derived by any helper. It is empty on all but the few rows whose
    application publishes more than a hostname, and it is here rather than
    beside the component so that what a zone carries stays readable from the
    tables and this census, without a fourth place to look."""

    @property
    def public(self) -> bool:
        """Whether a public record is published for this name."""
        return self.exposure in (Exposure.PUBLIC, Exposure.SPLIT)

    @property
    def lan_side(self) -> bool:
        """Whether LAN clients must be steered away from the public answer."""
        return self.exposure is not Exposure.PUBLIC


#: Every application route in the installation. Empty until `apps` declares
#: one.
ROUTES: tuple[Route, ...] = ()


#: The label a namespace carries for an `HTTPRoute` declared in it to attach
#: to a Gateway, as the one-entry mapping both sides use: the namespace's
#: labels, and the `matchLabels` of each Gateway's `allowedRoutes`. A route in
#: a namespace without it -- one a chart installs, say -- attaches to nothing.
ROUTE_LABEL: Mapping[str, str] = MappingProxyType({f'{LABEL_DOMAIN}/routes': 'attach'})


def served_zones(routes: Sequence[Route] | None = None) -> tuple[str, ...]:
    """The zones the cluster serves, which is where it issues a certificate: the primary, then every zone a row names.

    **The primary is served by definition**, not by a row: the cluster's own
    endpoints are names under it, and so is the first certificate the cluster
    issues, which comes before the first application's row does. Every other
    zone is served because a row publishes in it, so a row in a new zone is a
    new certificate in the same preview that shows the row, and a zone no row
    names costs no certificate at all.

    In that order, each zone once, so a reader that declares one resource per
    zone declares them in the same order on every run. `routes` is the census
    unless given, read when this is called rather than when the module was
    imported.
    """
    if routes is None:
        routes = ROUTES
    zones = [ZONE_PRIMARY]
    for route in routes:
        for zone in route.zones:
            if zone not in zones:
                zones.append(zone)
    return tuple(zones)
