"""The split-horizon rewrites a route census implies.

The rows themselves are `conventions.ROUTES`, because `apps` authors them and
both stacks read them. The derivation is here because `dns` is the only stack
that performs it: it is what `adguard.py` declares from (dns.md §3).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address

from kluster import conventions

__all__ = ('Rewrite', 'rewrites')


@dataclass(frozen=True)
class Rewrite:
    """One AdGuard rewrite: a name, and the address LAN clients get for it.

    The answer is an address and never a name. AdGuard hands a rewrite's CNAME
    target to the upstream without re-applying its own rules, so a rewrite
    aimed at a name that only a sibling rewrite answers resolves to NXDOMAIN;
    the type is what keeps such a target unwritable.
    """

    domain: str
    answer: IPv4Address | IPv6Address

    @property
    def family(self) -> str:
        """The record type the instance answers this rewrite for: `v4` or `v6`."""
        return 'v4' if isinstance(self.answer, IPv4Address) else 'v6'


def rewrites(routes: Iterable[conventions.Route] = ()) -> tuple[Rewrite, ...]:
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
        vip = (
            conventions.LAN_POOL.media_vip
            if route.exposure is conventions.Exposure.IOT
            else conventions.LAN_POOL.default_vip
        )
        for zone in route.zones:
            domain = f'{route.host}.{zone}'
            emitted.extend((Rewrite(domain=domain, answer=vip.v4), Rewrite(domain=domain, answer=vip.v6)))
    return tuple(emitted)
