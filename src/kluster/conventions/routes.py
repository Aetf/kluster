"""How each application is reachable: which name, in which zones, through which gateways.

Two stacks decide from this table and both declare from it: `apps` builds the
HTTPRoutes and the public records from a row, `dns` builds the split-horizon
rewrites from the same row (declarative/dns.md §3). One edit produces both,
which is why an application cannot forget its rewrite -- it never writes one.

The census is empty while `apps` is unwritten. It grows one row per
application as the migration proceeds, and each row's rewrite appears in a
`dns` preview the same day the app's route does.

**The first LAN-side row makes `adguardEndpoints` required.** The `dns` stack
reads that key only when there is a rewrite to write (`stacks/dns.py`), which
is what keeps the stack deployable while nothing is routed -- and it means a
missing key is invisible until the first row lands, rather than failing on the
apply that introduced it. So the row that first sets `exposure` to anything but
`Exposure.PUBLIC` ships together with `kluster-py:adguardEndpoints` in
`Pulumi.dns.yaml`: one base URL per AdGuard instance, addressing the
administration API each answers on -- the address and port the overlay's flow
rules admit a `dns` run to, and nothing else (dns.md §3). The two credentials
beside it (`adguardUsername` / `adguardPassword`) are already in that file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from kluster.conventions.dns import PUBLIC_ALL

__all__ = ('ROUTES', 'Exposure', 'Route')


class Exposure(Enum):
    """Which gateways serve a name, which is also what the rewrite says.

    The values are the §3.6 matrix in one field: an app is reachable from
    the internet, from the LAN, or from both, and the IoT VLAN is a
    LAN-side variant answered by the media VIP rather than the LAN one.

    One field rather than two flags, because two admit "no public record and
    no LAN answer" -- a route that publishes nothing anywhere, and there is no
    such thing. A combination this set cannot express is added as a value, not
    by splitting the field.
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
    zones: Sequence[str] = PUBLIC_ALL
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


#: Every application route in the estate. Empty until `apps` declares one.
ROUTES: tuple[Route, ...] = ()
