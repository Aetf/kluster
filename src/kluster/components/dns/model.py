"""The record model: what a DNS record is, as plain data.

A record is a frozen dataclass and nothing else -- no provider types, no
outputs, no side effects -- so the zone census is a module of literals a
reviewer can read top to bottom and a test can assert on without a Pulumi
runtime. Turning a row into a `cloudflare.DnsRecord` is `zone.py`'s job.

Two fields exist for the sake of the declaration rather than the DNS wire:

-   `key` is the record's identity in Pulumi's state. A zone holds several
    records with the same label and type (the apex TXT verifications, five
    MX), so the logical name cannot be derived from label and type alone, and
    deriving it from the content instead would rename -- and therefore
    replace -- a record whenever its value is rotated.
-   `comment` rides along to Cloudflare's per-record comment field, the only
    place the dashboard can explain why a record exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pulumi

__all__ = (
    'TTL_AUTO',
    'TTL_HOUR',
    'Record',
    'a',
    'aaaa',
    'caa',
    'cname',
    'mx',
    'srv',
    'txt',
)

#: Cloudflare spells "let the edge pick a TTL" as a TTL of 1. It is the right
#: default for everything a client should not be caching decisions about;
#: anchors override it (`conventions.ANCHOR_TTL`) because they are the records
#: a node rebuild moves.
TTL_AUTO = 1
TTL_HOUR = 3600


@dataclass(frozen=True)
class Record:
    """One record in one zone, named relative to that zone's apex."""

    label: str
    """The name relative to the apex; `@` is the apex itself."""
    type: str
    content: pulumi.Input[str] | None = None
    ttl: int = TTL_AUTO
    proxied: bool = False
    """Cloudflare's reverse proxy. Off means the record hands out the origin
    address, which is what non-HTTP ports and large uploads need."""
    priority: int | None = None
    data: Mapping[str, str | int] | None = None
    """Structured payload for the types Cloudflare does not take as a content
    string (SRV, CAA)."""
    comment: str = ''
    key: str = ''
    """State identity; defaults to label and type, which is unique for most."""

    @property
    def resource_key(self) -> str:
        return self.key or f'{self.label}-{self.type}'.lower()

    def fqdn(self, zone: str) -> str:
        return zone if self.label == '@' else f'{self.label}.{zone}'


def a(
    label: str, address: pulumi.Input[str], *, proxied: bool = False, ttl: int = TTL_AUTO, comment: str = ''
) -> Record:
    return Record(label=label, type='A', content=address, proxied=proxied, ttl=ttl, comment=comment)


def aaaa(
    label: str, address: pulumi.Input[str], *, proxied: bool = False, ttl: int = TTL_AUTO, comment: str = ''
) -> Record:
    return Record(label=label, type='AAAA', content=address, proxied=proxied, ttl=ttl, comment=comment)


def cname(label: str, target: str, *, proxied: bool = False, ttl: int = TTL_AUTO, comment: str = '') -> Record:
    return Record(label=label, type='CNAME', content=target, proxied=proxied, ttl=ttl, comment=comment)


def txt(label: str, value: str, *, ttl: int = TTL_AUTO, key: str = '', comment: str = '') -> Record:
    # Quoted, because an unquoted SPF or DKIM string is split on its spaces by
    # the API and comes back as several character-strings.
    return Record(label=label, type='TXT', content=f'"{value}"', ttl=ttl, key=key, comment=comment)


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
    return Record(
        label=label,
        type='SRV',
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
