"""The rule list both resolvers answer from: its rows, the derivations that produce them, and the one rendering.

Three things, in the order a reader meets them: the rows (`Rewrite`, a name and
its answer; `Blocklist`, a client and the hosts it may not resolve), the
derivations that turn a census into rows, and `user_rules`, which renders
blocks of rows into the list one `AdGuardUserRules` resource per instance owns
whole (`kluster.providers.adguard`). The list is the instances' only source of
rules, so it is generated and never typed: a line in it that this module did
not render was typed by hand, and a refresh reports it as drift.

The censuses the derivations read are conventions -- the exposures, the overlay
roster, the gateway's own names, the legacy vhosts -- because more than one
program reads each. The derivations are not: only `dns` turns a census into
rules, so they live beside the component that declares them, and each takes
its census as a parameter rather than reaching for it.

**The rendering rules are the function's, not a row's** (`user_rules`):

-   **Every rewrite takes the exact anchor, `|name^`.** It matches the name and
    nothing under it, so a bare label answers for itself and not for a
    top-level domain of the same spelling.
-   **The record type is the answer's**: A for an IPv4 address, AAAA for an
    IPv6 one, CNAME for a name, in the long form
    `$dnsrewrite=NOERROR;<type>;<value>`.
-   **No rule narrows the query type.** A rewrite then answers every type for
    its name, the declared one with its record and every other with `NOERROR`
    and no record, so no query for a rewritten name leaves the instance: an
    HTTPS query never fetches the public record's address hints, and no
    upstream NXDOMAIN reaches a stub that would cache it for the whole name.
-   **A blocking rule keeps the subtree form, `||host^`**, and names the one
    client it applies to.

**A rewrite answers with an address, or with a name on the device plane.** That
is `Rewrite.answer`'s type and its construction check, not a convention anyone
keeps. An instance resolves a CNAME's target upstream without applying its own
rules to it again, so the target must be a name the gateway's resolver answers
(dns.md §4 item 1), never one a sibling rule answers: a target outside
`home.arpa` cannot be built, and `test_dns_stack` holds the rest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Literal

from kluster import conventions
from kluster.components.dns.base import overlay_label
from kluster.providers.adguard import Client

__all__ = (
    'DEVICE_PLANE_ZONE',
    'HEADER',
    'Blocklist',
    'Rewrite',
    'RuleBlock',
    'UnusableRow',
    'blocklist_clients',
    'gateway_rewrites',
    'legacy_vhost_rewrites',
    'overlay_rewrites',
    'rewrites',
    'user_rules',
)

#: The zone the gateway's resolver answers from its leases (dns.md §4 item 1),
#: the only one a rewrite may name as its answer.
DEVICE_PLANE_ZONE = 'home.arpa'

#: The first line of every list, so whoever opens the instance's UI reads why
#: an edit there does not last.
HEADER = "! Declared by the dns stack: an edit made here is drift, and the stack's next write of this list replaces it."

#: A name as a rule can carry it: dot-separated hostname labels, one of them at
#: least, lowercase. Nothing that would end the rule's pattern early -- `^`,
#: `$`, `|`, whitespace -- can appear in one.
_NAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*')

#: A client name `$client=` takes unquoted.
_CLIENT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')


class UnusableRow(ValueError):
    """A row that would render as a rule matching something else, or nothing at all."""


@dataclass(frozen=True)
class Rewrite:
    """One name, and what a client resolving it through an instance is answered with."""

    domain: str
    answer: IPv4Address | IPv6Address | str
    """An address, whose family is the record type, or a name under `DEVICE_PLANE_ZONE`, answered as a CNAME."""

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.domain):
            raise UnusableRow(f'{self.domain!r} is not a name a rule can match')
        if isinstance(self.answer, str) and not (
            _NAME.fullmatch(self.answer) and self.answer.endswith(f'.{DEVICE_PLANE_ZONE}')
        ):
            raise UnusableRow(
                f'{self.domain} would answer with {self.answer!r}, which is not a name under {DEVICE_PLANE_ZONE}: '
                "an instance resolves a CNAME's target upstream, and only the gateway's resolver answers the "
                'device plane'
            )

    @property
    def record_type(self) -> Literal['A', 'AAAA', 'CNAME']:
        if isinstance(self.answer, str):
            return 'CNAME'
        return 'A' if self.answer.version == 4 else 'AAAA'


@dataclass(frozen=True)
class Blocklist:
    """Hosts one client may not resolve, and the client itself, which the instance must hold for the rules to match.

    One structure, so a rule can name no client the instance lacks: the client
    set the instances hold is drawn from these rows (`blocklist_clients`).
    """

    client: Client
    hosts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _CLIENT_NAME.fullmatch(self.client['name']):
            raise UnusableRow(f'client name {self.client["name"]!r} is not a bare token `$client=` takes unquoted')
        if not self.hosts:
            raise UnusableRow(f'the blocklist for {self.client["name"]} holds no host')
        for host in self.hosts:
            if not _NAME.fullmatch(host):
                raise UnusableRow(f'{host!r} is not a name a rule can match')


@dataclass(frozen=True)
class RuleBlock:
    """Rows that come from one source, rendered together under a comment naming it."""

    source: str
    rows: tuple[Rewrite | Blocklist, ...]

    def __post_init__(self) -> None:
        if '\n' in self.source or '\r' in self.source:
            raise UnusableRow(f'the source {self.source!r} would render as more than one line')


def user_rules(blocks: Iterable[RuleBlock]) -> tuple[str, ...]:
    """The whole list, in a fixed order: the header, then each block under a `!` comment naming its source.

    Every rendering rule in the module docstring is applied here and only here,
    so the same rows render the same list on every run.
    """
    rendered = [HEADER]
    for block in blocks:
        rendered.append(f'! {block.source}')
        for row in block.rows:
            if isinstance(row, Rewrite):
                rendered.append(f'|{row.domain}^$dnsrewrite=NOERROR;{row.record_type};{row.answer}')
            else:
                rendered.extend(f'||{host}^$client={row.client["name"]}' for host in row.hosts)
    return tuple(rendered)


def blocklist_clients(blocks: Iterable[RuleBlock]) -> tuple[Client, ...]:
    """The clients the blocklist rows name, each once, in the order the rows name them."""
    clients: dict[str, Client] = {}
    for block in blocks:
        for row in block.rows:
            if isinstance(row, Blocklist):
                _ = clients.setdefault(row.client['name'], row.client)
    return tuple(clients.values())


def rewrites(exposures: Iterable[conventions.routes.Exposure]) -> tuple[Rewrite, ...]:
    """The split-horizon rewrites the census implies, one per name per family.

    Each row's home side decides its answer: a row on `lan` is rewritten to
    the LAN VIP and one on `media` to the media VIP, a `Route` in every zone
    it is published in and a `Passthrough` or a `PortRedirect` in the primary;
    `Bare` adds the bare label, which the LAN Gateway redirects to the name.
    A raw port's `Vip` rewrites its host in the primary to its Gateway's
    VIP, and a heard side has no host to rewrite. A name published nowhere
    else is rewritten all the same, since a home-only name resolves for a
    home client alone.

    **A held row publishes no rewrite** (rfc-008 §3.3), and neither does a
    raw port's VIP host that a held row shares: the name resolves where the
    legacy VPS publishes it until the cut. Rows that share a host publish one
    rewrite, which the census invariants hold to one answer.

    Both address families are emitted. An instance answers the other family
    of a rewritten name with an empty response, not with the public one, so a
    v4-only row would leave the name with no IPv6 address for a home client
    at all; the v6 row gives it the VIP's ULA, which a client may dial first
    (architecture.md §1.3). The only answers this can produce are the two
    VIPs, so the addresses are the site's own and the gateway resolves them
    without help.
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


def overlay_rewrites(roster: Iterable[conventions.overlay.RosterEntry]) -> tuple[Rewrite, ...]:
    """One rewrite per overlay member: its name in the overlay host block, its overlay address.

    The name and the address are the pair the overlay host block publishes for
    the same entry (`base.overlay_records`), so a client gets one answer for an
    overlay name whichever path it takes: alice and bob answer it from here, and
    every other resolver from the public record. The name is the block's, the
    member's label under `conventions.OVERLAY_LABEL` in the primary, the one
    zone the block is published in; a device that holds the overlay's tunnel
    and applies its resolver asks alice and bob for it as a home client does
    (physical/gateway.md §2.7).

    One family, where `rewrites` emits two: the overlay is IPv4-only, a roster
    address is an `IPv4Address` by type, and an A rewrite answers AAAA with an
    empty `NOERROR` -- which is what the A-only public record produces too.
    The answer is the member's own address and never a LAN VIP: these names
    are hosts, not routes.
    """
    return tuple(
        Rewrite(
            domain=f'{overlay_label(entry.name)}.{conventions.OVERLAY_LABEL}.{conventions.ZONE_PRIMARY}',
            answer=entry.address,
        )
        for entry in roster
    )


def gateway_rewrites(
    controller: str,
    resolvers: Iterable[conventions.gateway.BridgedService],
    proxy: conventions.gateway.BridgedService,
) -> tuple[Rewrite, ...]:
    """The names the gateway's proxy serves of its own: the controller console's, and each resolver's interface.

    They are names in the primary zone that no public resolver answers (dns.md
    §4 item 4), so this is what makes them resolve on the LAN at all, and the
    answer is the proxy's address, which serves them.
    """
    names = (controller, *(resolver.vhost for resolver in resolvers if resolver.vhost is not None))
    return tuple(Rewrite(domain=name, answer=proxy.address) for name in names)


def legacy_vhost_rewrites(
    legacy: Iterable[conventions.gateway.LegacyVhost], proxy: conventions.gateway.BridgedService
) -> tuple[Rewrite, ...]:
    """The names the proxy still serves under the retiring zone, each answered at the proxy's address.

    One rule per row, for its name under the zone. A row the proxy also
    answers bare gets two more: the label alone, for a client that asks for
    it, and the label under `home.arpa`, for one that appends the device
    plane's search domain first. Either way the client sends the bare label as
    its `Host`, which the proxy's redirect answers. A rule leaves with its row,
    in the change that migrates the row's application (dns.md §4 item 3).
    """
    emitted: list[Rewrite] = []
    for row in legacy:
        names = (row.host, row.label, f'{row.label}.{DEVICE_PLANE_ZONE}') if row.bare_name else (row.host,)
        emitted.extend(Rewrite(domain=name, answer=proxy.address) for name in names)
    return tuple(emitted)
