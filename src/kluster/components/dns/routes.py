"""The split-horizon rewrites a route census implies: one per name per family.

The rows themselves are a convention (`kluster.conventions.routes`) because
`apps` and `dns` both read them. The derivation below is not: only `dns` turns
a route into rewrites, so it lives beside the component that declares them
(`rewrites.py`).

A rewrite answers with an address and never with a name. That is the row's
type rather than a convention, so a rewrite that pointed at another name --
resolvable only if some sibling rewrite happened to exist, and silently
NXDOMAIN if it did not -- cannot be written here at all.

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

from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address

from kluster import conventions
from kluster.conventions.routes import Exposure, Route

__all__ = ('Rewrite', 'rewrites')


@dataclass(frozen=True)
class Rewrite:
    """One AdGuard rewrite: a name, and the address LAN clients get for it."""

    domain: str
    answer: IPv4Address | IPv6Address
    """The address the instance answers with; its family is a property of it."""


def rewrites(routes: Iterable[Route] = ()) -> tuple[Rewrite, ...]:
    """The split-horizon rewrites the routes imply, one per name per family.

    A rewrite is emitted for every zone a LAN-side route is published in --
    including LAN-only names, which have no public record but still resolve
    for LAN clients. Both address families are emitted: AdGuard answers a
    rewrite only for the family its answer is in, and a LAN client that
    prefers IPv6 (RFC 6724) would otherwise fall through to the public
    answer and take the cloud path.

    The only answers this can produce are the two LAN VIPs, so the addresses
    are the site's own and the gateway resolves them without help.
    """
    emitted: list[Rewrite] = []
    for route in routes:
        if not route.lan_side:
            continue
        vip = conventions.LAN_POOL.media_vip if route.exposure is Exposure.IOT else conventions.LAN_POOL.default_vip
        for zone in route.zones:
            domain = f'{route.host}.{zone}'
            emitted.extend((Rewrite(domain=domain, answer=vip.v4), Rewrite(domain=domain, answer=vip.v6)))
    return tuple(emitted)
