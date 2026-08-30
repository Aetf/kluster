"""Which names are rewritten, and on which instances (dns.md §3).

The AdGuard pair (alice, bob) is written to directly rather than
synchronized: one `AdGuardRewrite` per instance per name, so an instance that
is down fails its own resources and leaves the other instance's converged.
Nothing else in the stack depends on a rewrite, which is what makes an
unreachable UDM cost only these resources rather than the whole up (ci.md §2).

The resource and the provider behind it are
`kluster.providers.adguard_rewrites`; what is here is the census that decides
how many of them there are.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

import pulumi

from kluster.components.dns.routes import Rewrite
from kluster.providers.adguard_rewrites import AdGuardRewrite

__all__ = ('declare_rewrites', 'instance_label')


def instance_label(endpoint: str) -> str:
    """A resource-name fragment for an instance, from its base URL."""
    host = urlsplit(endpoint).hostname or endpoint
    return host.replace('.', '-')


def declare_rewrites(
    entries: Iterable[Rewrite],
    *,
    endpoints: Iterable[str],
    opts: pulumi.ResourceOptions | None = None,
) -> dict[str, AdGuardRewrite]:
    """Every rewrite on every instance: the cross product, written directly.

    Dual-writing is what retires adguardhome-sync -- a synchronizer would
    overwrite whichever instance Pulumi wrote second (dns.md §3).

    The instances' login is not a parameter: it is the provider's own, read in
    `configure` out of the stack's configuration, so nothing on this side of
    the boundary holds it.
    """
    declared: dict[str, AdGuardRewrite] = {}
    for endpoint in endpoints:
        instance = instance_label(endpoint)
        for entry in entries:
            family = 'v6' if ':' in entry.answer else 'v4'
            name = f'{instance}-{entry.domain}-{family}'
            declared[name] = AdGuardRewrite(
                name,
                endpoint=endpoint,
                domain=entry.domain,
                answer=entry.answer,
                opts=opts,
            )
    return declared
