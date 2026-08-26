"""Route declarations: one row an app writes, two stacks read.

A route says how an application is reachable -- which hostname, in which
zones, through which gateways. `apps` builds HTTPRoutes and public CNAMEs
from these rows; `dns` builds the AdGuard rewrites from the same rows
(dns.md §3). That is why they are plain data in a module both stacks
import rather than a helper on a component: an app cannot forget its
rewrite, because it never writes one.

The census is empty while `apps` is unwritten. It grows one row per app as
the migration proceeds, and each row's rewrite appears in a `dns` preview
the same day the app's route does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from kluster import conventions

__all__ = ('ROUTES', 'Exposure', 'Rewrite', 'Route', 'rewrites')


class Exposure(Enum):
    """Which gateways serve a name, which is also what the rewrite says.

    The values are the §3.6 matrix in one field: an app is reachable from
    the internet, from the LAN, or from both, and the IoT VLAN is a
    LAN-side variant answered by the media VIP rather than the LAN one.
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
    zones: Sequence[str] = conventions.PUBLIC_ALL
    proxied: bool = True
    """Cloudflare proxy on the public record. Off for non-HTTP ports and
    for uploads larger than the proxy's body limit."""

    @property
    def public(self) -> bool:
        """Whether a public record is published for this name."""
        return self.exposure in (Exposure.PUBLIC, Exposure.SPLIT)

    @property
    def lan_side(self) -> bool:
        """Whether LAN clients must be steered away from the public answer."""
        return self.exposure is not Exposure.PUBLIC


@dataclass(frozen=True)
class Rewrite:
    """One AdGuard rewrite: a name, and the address LAN clients get for it."""

    domain: str
    answer: str


def rewrites(routes: Iterable[Route] = ()) -> tuple[Rewrite, ...]:
    """The split-horizon rewrites the routes imply, one per name per family.

    A rewrite is emitted for every zone a LAN-side route is published in --
    including LAN-only names, which have no public record but still resolve
    for LAN clients. Both address families are emitted: AdGuard answers a
    rewrite only for the family its answer is in, and a LAN client that
    prefers IPv6 (RFC 6724) would otherwise fall through to the public
    answer and take the cloud path.
    """
    emitted: list[Rewrite] = []
    for route in routes:
        if not route.lan_side:
            continue
        v4, v6 = (
            (conventions.VIP_MEDIA_V4, conventions.VIP_MEDIA_V6)
            if route.exposure is Exposure.IOT
            else (conventions.VIP_LAN_V4, conventions.VIP_LAN_V6)
        )
        for zone in route.zones:
            domain = f'{route.host}.{zone}'
            emitted.extend((Rewrite(domain=domain, answer=str(v4)), Rewrite(domain=domain, answer=str(v6))))
    return tuple(emitted)


#: Every application route in the estate. Empty until `apps` declares one.
ROUTES: tuple[Route, ...] = ()
