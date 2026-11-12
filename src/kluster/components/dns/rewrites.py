"""The split-horizon rewrites: what the exposure census implies, and who writes it.

Three things, in the order a reader meets them: the row (`Rewrite`), the
derivation that turns the shared exposure census into rows (`rewrites`), and
the component that writes one instance's rows (`ResolverRewrites`).

The exposures themselves are a convention (`kluster.conventions.routes`)
because `apps`, `dns` and `physical` all read them. The derivation is not:
only `dns` turns a row into rewrites, so it lives beside the component that
declares them.

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


def rewrites(exposures: Iterable[conventions.routes.Exposure]) -> tuple[Rewrite, ...]:
    """The split-horizon rewrites the census implies, one per name per family.

    Each row's home side decides its answer: a row on `lan` is rewritten to
    the LAN VIP and one on `media` to the media VIP, a `Route` in every zone
    it is published in and a `Passthrough` in the primary; `Bare` adds the
    bare label, which the LAN Gateway redirects to the name. A raw port's
    `Vip` rewrites its host in the primary to its Gateway's VIP, and a heard
    side has no host to rewrite. A name published nowhere else is rewritten
    all the same, since a home-only name resolves for a home client alone.

    **A held row publishes no rewrite** (rfc-008 §3.3), and neither does a
    raw port's VIP host that a held row shares: the name resolves where the
    legacy VPS publishes it until the cut. Rows that share a host publish one
    rewrite, which the census invariants hold to one answer.

    Both address families are emitted: AdGuard answers a rewrite only for the
    family its answer is in, and a client that prefers IPv6 (RFC 6724) would
    otherwise fall through to the public answer and take the cloud path. The
    only answers this can produce are the two VIPs, so the addresses are the
    site's own and the gateway resolves them without help.
    """
    census = tuple(exposures)
    held = {(name, zone) for exposure in census for name, zone in _held_names(exposure)}
    answers: dict[str, conventions.Vip] = {}
    for exposure in census:
        for domain, vip in _answers(exposure):
            if domain not in held:
                _ = answers.setdefault(_fqdn(*domain), vip)
    return tuple(
        Rewrite(domain=domain, answer=answer) for domain, vip in answers.items() for answer in (vip.v4, vip.v6)
    )


def _fqdn(host: str, zone: str | None) -> str:
    """The name a rewrite answers for: the host in its zone, or the bare label alone."""
    return host if zone is None else f'{host}.{zone}'


def _held_names(exposure: conventions.routes.Exposure) -> Iterable[tuple[str, str]]:
    """Every name a held row of the exposure stands on, in each zone it names."""
    routes = conventions.routes
    for row in exposure.routes:
        if isinstance(row, routes.Held):
            yield from ((row.row.host, zone) for zone in row.row.zones)
    for port in exposure.ports:
        if isinstance(port.internet, routes.Balancer):
            for host in port.internet.hosts:
                if isinstance(host, routes.Held):
                    yield (host.row.label, conventions.ZONE_PRIMARY)


def _answers(exposure: conventions.routes.Exposure) -> Iterable[tuple[tuple[str, str | None], conventions.Vip]]:
    """Each name an exposure's unheld rows rewrite, as host and zone (`None` for a bare label), with its VIP."""
    routes = conventions.routes
    pool = conventions.LAN_POOL
    for row in exposure.routes:
        if isinstance(row, routes.Held | routes.Apex):
            continue
        vip = pool.default_vip if row.lan is not None else pool.media_vip if row.media is not None else None
        if vip is None:
            continue
        yield from (((row.host, zone), vip) for zone in row.zones)
        if isinstance(row.lan, routes.Bare):
            yield ((row.host, None), vip)
    for port in exposure.ports:
        for side, vip in ((port.lan, pool.default_vip), (port.media, pool.media_vip)):
            if isinstance(side, routes.Vip):
                yield ((side.host, conventions.ZONE_PRIMARY), vip)


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
    deriving them from `EXPOSURES` in here would be a component reaching for a
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
