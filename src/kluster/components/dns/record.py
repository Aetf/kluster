"""The record model: what a DNS record is, as plain data.

A record is a frozen dataclass and nothing else -- no provider types, no
outputs, no side effects -- so the zone census is a module of literals a
reviewer can read top to bottom and a test can assert on without a Pulumi
runtime. Turning a row into a `cloudflare.DnsRecord` is `zone.py`'s job.

Some fields exist for the sake of the declaration rather than the DNS wire:

-   `key` is the record's identity in Pulumi's state. A zone holds several
    records with the same label and type (the apex TXT verifications, five
    MX), so the logical name cannot be derived from label and type alone, and
    deriving it from the content instead would rename -- and therefore
    replace -- a record whenever its value is rotated.
-   `comment` rides along to Cloudflare's per-record comment field, the only
    place the dashboard can explain why a record exists.
-   `unproxied` is why Cloudflare's proxy is off on a record of a type it
    can front. It is the census's and nobody else's: `zone.py` sends no
    field for it, so a reason's wording changes nothing a zone carries, and
    only whether a record has one does, by way of `proxied`.

`Block` is the unit the census is written in, and it is here rather than in
`base.py` because `legacy.py` is written in it too and `base.py` reads
`legacy.py` rather than the other way round.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pulumi

__all__ = (
    'PROXIABLE',
    'TTL_AUTO',
    'TTL_HOUR',
    'TXT_STRING',
    'Block',
    'Record',
    'a',
    'aaaa',
    'caa',
    'cname',
    'mx',
    'srv',
    'txt',
    'zone_records',
)

#: Cloudflare spells "let the edge pick a TTL" as a TTL of 1. It is the right
#: default for everything a client should not be caching decisions about;
#: anchors override it (`conventions.ANCHOR_TTL`) because they are the records
#: a node rebuild moves.
TTL_AUTO = 1
TTL_HOUR = 3600

#: The longest character-string a TXT record holds; `txt` splits a longer
#: value into strings of this length.
TXT_STRING = 255

#: The record types Cloudflare's proxy can front: "Only records used for IP
#: address resolution -- A, AAAA, and CNAME records -- can be proxied"
#: (developers.cloudflare.com/dns/proxy-status/).
PROXIABLE = frozenset({'A', 'AAAA', 'CNAME'})


@dataclass(frozen=True)
class Record:
    """One record in one zone, named relative to that zone's apex."""

    label: str
    """The name relative to the apex; `@` is the apex itself."""
    type: str
    content: pulumi.Input[str] | None = None
    ttl: int = TTL_AUTO
    priority: int | None = None
    data: Mapping[str, str | int] | None = None
    """Structured payload for the types Cloudflare does not take as a content
    string (SRV, CAA)."""
    comment: str = ''
    key: str = ''
    """State identity; defaults to label and type, which is unique for most."""
    unproxied: str = ''
    """Why the proxy is off, on a record of a type it can front (`PROXIABLE`).
    Such a record is proxied unless it says why not -- a port or protocol
    the proxy does not carry, a body larger than the proxy accepts, an
    address the edge cannot reach -- so proxy-off is never an oversight, and a reason is all it takes
    to turn the proxy off."""

    @property
    def proxied(self) -> bool:
        """Cloudflare's reverse proxy. Off means the record hands out the
        origin address. Derived, so a record cannot be proxied with a reason
        not to be, or direct without one."""
        return self.type in PROXIABLE and not self.unproxied

    @property
    def resource_key(self) -> str:
        return self.key or f'{self.label}-{self.type}'.lower()

    def fqdn(self, zone: str) -> str:
        return zone if self.label == '@' else f'{self.label}.{zone}'


@dataclass(frozen=True)
class Block:
    """Records that appear together, in every zone of one set."""

    zones: tuple[str, ...]
    records: tuple[Record, ...]


def zone_records(zone: str, blocks: Iterable[Block]) -> tuple[Record, ...]:
    """What one zone carries: the provider's per-zone view, derived."""
    return tuple(record for block in blocks if zone in block.zones for record in block.records)


def a(label: str, address: pulumi.Input[str], *, unproxied: str = '', ttl: int = TTL_AUTO, comment: str = '') -> Record:
    return Record(label=label, type='A', content=address, unproxied=unproxied, ttl=ttl, comment=comment)


def aaaa(
    label: str, address: pulumi.Input[str], *, unproxied: str = '', ttl: int = TTL_AUTO, comment: str = ''
) -> Record:
    return Record(label=label, type='AAAA', content=address, unproxied=unproxied, ttl=ttl, comment=comment)


def cname(label: str, target: str, *, unproxied: str = '', ttl: int = TTL_AUTO, comment: str = '') -> Record:
    return Record(label=label, type='CNAME', content=target, unproxied=unproxied, ttl=ttl, comment=comment)


def txt(label: str, value: str, *, ttl: int = TTL_AUTO, key: str = '', comment: str = '') -> Record:
    # The content is the form Cloudflare stores and reads back, so a refresh
    # finds what the program declares. A TXT record's data is a sequence of
    # character-strings of at most 255 octets (RFC 1035 §3.3.14), and Cloudflare
    # holds a longer value as consecutive strings of 255 characters, each
    # quoted, joined by one space: `"first part" "second part"`
    # (developers.cloudflare.com/dns/faq/, "Why does my TXT record show a double
    # quote in the middle of the value?"). Declared as one string instead, such
    # a value differs from the read-back on every refresh. A reader of SPF or
    # DKIM concatenates the strings, so either form publishes the same value.
    # Every string is quoted, because an unquoted SPF or DKIM value is split on
    # its spaces by the API instead.
    strings = (value[start : start + TXT_STRING] for start in range(0, max(len(value), 1), TXT_STRING))
    content = ' '.join(f'"{string}"' for string in strings)
    return Record(label=label, type='TXT', content=content, ttl=ttl, key=key, comment=comment)


def mx(label: str, target: str, priority: int, *, ttl: int = TTL_AUTO, comment: str = '') -> Record:
    # The mail host is the key: five MX rows differ only by host and
    # preference, and the preference is the field an edit changes.
    return Record(
        label=label,
        type='MX',
        content=target,
        priority=priority,
        ttl=ttl,
        comment=comment,
        key=f'mx-{target.rstrip(".").split(".")[0].lower()}',
    )


def srv(label: str, *, priority: int, weight: int, port: int, target: str, comment: str = '') -> Record:
    # The priority goes at the top level as well as in `data`: Cloudflare reads
    # an SRV record's priority back in both places (the SRV example response in
    # developers.cloudflare.com/dns/manage-dns-records/reference/dns-record-types/),
    # and the provider's top-level `priority` is "Required for MX, SRV and URI
    # records". Declared in `data` alone, the top-level field differs from the
    # read-back on every refresh.
    return Record(
        label=label,
        type='SRV',
        priority=priority,
        data={'priority': priority, 'weight': weight, 'port': port, 'target': target},
        comment=comment,
    )


def caa(label: str, *, tag: str, value: str, flags: int = 0, comment: str = '') -> Record:
    # The key names the certificate authority alone, not the whole value: the
    # `;`-separated parameters (`cansignhttpexchanges`) are modifiers on the
    # same authorization, so keying on them would replace the record whenever
    # one is added or dropped.
    authority = value.split(';')[0].strip()
    return Record(
        label=label,
        type='CAA',
        data={'flags': flags, 'tag': tag, 'value': value},
        comment=comment,
        key=f'caa-{tag}-{authority}'.replace('.', '-').lower(),
    )
