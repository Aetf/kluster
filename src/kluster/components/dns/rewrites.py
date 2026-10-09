"""The rule list both resolvers answer from: its rows, the derivations that produce them, and the one rendering.

Three things, in the order a reader meets them: the rows (`Rewrite`, a name and
its answer; `Blocklist`, a client and the hosts it may not resolve), the
derivations that turn a census into rows, and `user_rules`, which renders
blocks of rows into the list one `AdGuardUserRules` resource per instance owns
whole (`kluster.providers.adguard`). The list is the instances' only source of
rules, so it is generated and never typed: a line in it that this module did
not render was typed by hand, and a refresh reports it as drift.

The censuses the derivations read are conventions -- the routes, the overlay
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
