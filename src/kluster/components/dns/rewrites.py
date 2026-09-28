"""The AdGuard rewrites: what the route census and the overlay roster imply, and who writes them.

Four things, in the order a reader meets them: the row (`Rewrite`), the two
derivations that turn a shared census into rows -- the split-horizon rewrites
of the routes (`rewrites`) and the overlay names of the roster
(`overlay_rewrites`) -- and the component that writes one instance's rows
(`ResolverRewrites`), which takes rows and does not care which derivation they
came from.

The routes and the roster are conventions (`kluster.conventions.routes`,
`kluster.conventions.overlay`) because more than one stack reads each. The
derivations are not: only `dns` turns a route or a member into rewrites, so
they live beside the component that declares them.

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
from kluster.components.dns.base import overlay_label
from kluster.providers.adguard_rewrites import AdGuardRewrite
from putils import Component

__all__ = ('ResolverRewrites', 'Rewrite', 'overlay_rewrites', 'rewrites')


@dataclass(frozen=True)
class Rewrite:
    """One AdGuard rewrite: a name, and the address a client resolving through the instance gets for it."""

    domain: str
    answer: IPv4Address | IPv6Address
    """The address the instance answers with; its family is a property of it."""

    @property
    def family(self) -> str:
        """`v4` or `v6`, as a resource name spells the answer's family."""
        return f'v{self.answer.version}'


def rewrites(routes: Iterable[conventions.routes.Route]) -> tuple[Rewrite, ...]:
    """The split-horizon rewrites the routes imply, one per name per family.

    A rewrite is emitted for every zone a LAN-side route is published in --
    including LAN-only names, which have no public record but still resolve
    for LAN clients. Both address families are emitted. AdGuard answers the
    other family of a rewritten name with an empty response, not with the
    public one, so a v4-only row would leave the name with no IPv6 address
    on the LAN at all; the v6 row gives it the VIP's ULA (architecture.md
    §1.3 on how rarely a client picks it).

    The only answers this can produce are the two LAN VIPs, so the addresses
    are the site's own and the gateway resolves them without help.
    """
    emitted: list[Rewrite] = []
    for route in routes:
        if not route.lan_side:
            continue
        vip = (
            conventions.LAN_POOL.media_vip
            if route.exposure is conventions.routes.Exposure.IOT
            else conventions.LAN_POOL.default_vip
        )
        for zone in route.zones:
            domain = f'{route.host}.{zone}'
            emitted.extend((Rewrite(domain=domain, answer=vip.v4), Rewrite(domain=domain, answer=vip.v6)))
    return tuple(emitted)


def overlay_rewrites(roster: Iterable[conventions.overlay.RosterEntry]) -> tuple[Rewrite, ...]:
    """One rewrite per overlay member: its name under the overlay domain, its overlay address.

    The name and the address are the pair the overlay host block publishes for
    the same entry (`base.overlay_records`), so a client gets one answer for an
    overlay name whichever path it takes: alice and bob answer it from here, and
    every other resolver from the public record. The domain is
    `conventions.OVERLAY_DOMAIN`, the one the network pushes to its members as
    the scope of their managed DNS, so a member that has opted in is answered
    from here for exactly these names and for no application name.

    One family, where `rewrites` emits two: the overlay is IPv4-only, a roster
    address is an `IPv4Address` by type, and an A rewrite answers AAAA with an
    empty `NOERROR` -- which is what the A-only public record produces too.
    The answer is the member's own address and never a LAN VIP: these names
    are hosts, not routes.
    """
    return tuple(
        Rewrite(domain=f'{overlay_label(entry.name)}.{conventions.OVERLAY_DOMAIN}', answer=entry.address)
        for entry in roster
    )


class ResolverRewrites(Component, pulumi_type='kluster:dns:ResolverRewrites'):
    """Every rewrite one AdGuard instance answers, written directly to it.

    One component per instance rather than one over the pair: their
    independence is the design (dns.md §3), and as two sibling components that
    is what the resource tree says rather than something a reader derives from
    the resource names. Dual-writing is also what retires adguardhome-sync -- a
    synchronizer would overwrite whichever instance Pulumi wrote second.

    The instance is taken as its census entry rather than as a URL, which is
    what leaves `conventions.gateway.resolver_api_url` the only spelling of the
    address. The rows are taken as a parameter for the opposite reason:
    deriving them from `ROUTES` or `ROSTER` in here would be a component
    reaching for a census instead of receiving one.

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
