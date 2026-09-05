"""The split-horizon rewrites: what a route census implies, and who writes it.

Three things, in the order a reader meets them: the row (`Rewrite`), the
derivation that turns the shared route census into rows (`rewrites`), and the
component that writes one instance's rows (`ResolverRewrites`).

The routes themselves are a convention (`kluster.conventions.routes`) because
`apps` and `dns` both read them. The derivation is not: only `dns` turns a
route into rewrites, so it lives beside the component that declares them.

**A rewrite answers with an address and never with a name.** That is
`Rewrite.answer`'s type rather than a convention anyone has to keep, so a
rewrite pointing at another name -- resolvable only if some sibling rewrite
happened to exist, and silently NXDOMAIN if it did not -- cannot be built at
all, here or by any caller.

The AdGuard pair (alice, bob) is written to directly rather than synchronized:
one component per instance, so an instance that is down fails its own resources
and leaves the other's converged (dns.md §3). Nothing else in the stack depends
on a rewrite, which is what makes an unreachable UDM cost only these resources
rather than the whole up (ci.md §2).

The resource and the provider behind it are
`kluster.providers.adguard_rewrites`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address

import pulumi

from kluster import conventions
from kluster.conventions.routes import Exposure, Route
from kluster.providers.adguard_rewrites import AdGuardRewrite
from putils import Component

__all__ = ('ResolverRewrites', 'Rewrite', 'rewrites')


@dataclass(frozen=True)
class Rewrite:
    """One AdGuard rewrite: a name, and the address LAN clients get for it."""

    domain: str
    answer: IPv4Address | IPv6Address
    """The address the instance answers with; its family is a property of it."""

    @property
    def family(self) -> str:
        """`v4` or `v6`, as a resource name spells the answer's family."""
        return f'v{self.answer.version}'


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


class ResolverRewrites(Component):
    """Every rewrite one AdGuard instance answers, written directly to it.

    One component per instance rather than one over the pair: their
    independence is the design (dns.md §3), and as two sibling components that
    is what the resource tree says rather than something a reader derives from
    the resource names. Dual-writing is also what retires adguardhome-sync -- a
    synchronizer would overwrite whichever instance Pulumi wrote second.

    The instance is taken as its census entry rather than as a URL, which is
    what leaves `conventions.gateway.resolver_api_url` the only spelling of the
    address. The rows are taken as a parameter for the opposite reason:
    deriving them from `ROUTES` in here would be a component reaching for a
    census instead of receiving one.

    The instances' login is not a parameter either. It is the provider's own,
    read in `configure` out of the stack's configuration, so nothing on this
    side of the boundary holds it.
    """

    def __init__(
        self,
        name: str,
        *,
        resolver: conventions.gateway.BridgedService,
        entries: Sequence[Rewrite],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        # The instance is named by its census entry and addressed separately:
        # the name is what identifies a row, and the address is where this run
        # happens to reach the instance. Moving the instance is then an update
        # rather than a delete and a create of every row on it.
        endpoint = conventions.gateway.resolver_api_url(resolver)

        self.rewrites: tuple[AdGuardRewrite, ...] = tuple(
            AdGuardRewrite(
                f'{name}-{entry.domain}-{entry.family}',
                instance=resolver.name,
                endpoint=endpoint,
                domain=entry.domain,
                # The wire takes a string: the address is spelled here and
                # nowhere earlier.
                answer=str(entry.answer),
                opts=self.child_opts(),
            )
            for entry in entries
        )

        self.register_outputs({})
