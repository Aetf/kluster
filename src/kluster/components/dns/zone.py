"""A zone and everything declared in it.

The component is the only place that knows Cloudflare's resource shapes:
everywhere else a record is a `record.Record`, so adding a record is a data
change and a provider upgrade is one file's problem.

Two per-zone hygiene resources come with every zone rather than being
opted into: DNSSEC, and the CAA records the census carries. Both are cheap,
both are invisible until they are needed, and a zone that quietly lacks
them is exactly the zone nobody notices.
"""

from __future__ import annotations

from collections.abc import Iterable

import pulumi
import pulumi_cloudflare as cloudflare

from kluster.components.dns.record import Record
from putils import Component

__all__ = ('ManagedZone',)


class ManagedZone(Component):
    """One Cloudflare zone, its DNSSEC state, and its records."""

    def __init__(
        self,
        name: str,
        *,
        zone: str,
        account_id: pulumi.Input[str],
        records: Iterable[Record],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.zone_name = zone

        self.zone = cloudflare.Zone(
            name,
            account=cloudflare.ZoneAccountArgs(id=account_id),
            name=zone,
            type='full',
            opts=self.child_opts(
                # The zone is the registrar-facing object: destroying it
                # would take the delegation with it, and every record below.
                protect=True,
            ),
        )

        self.dnssec = cloudflare.ZoneDnssec(
            f'{name}-dnssec',
            zone_id=self.zone.id,
            status='active',
            opts=self.child_opts(),
        )

        self.records: dict[str, cloudflare.DnsRecord] = {}
        for record in records:
            key = record.resource_key
            if key in self.records:
                raise ValueError(f'{zone}: two records share the state key {key!r}; give one an explicit key')
            self.records[key] = cloudflare.DnsRecord(
                f'{name}-{key}',
                zone_id=self.zone.id,
                name=record.fqdn(zone),
                type=record.type,
                content=record.content,
                data=_data(record),
                ttl=record.ttl,
                proxied=record.proxied,
                priority=record.priority,
                comment=record.comment or None,
                opts=self.child_opts(),
            )

        self.register_outputs({'zone_id': self.zone.id})


def _data(record: Record) -> cloudflare.DnsRecordDataArgs | None:
    """The structured payload, for the two record types that need one."""
    if record.data is None:
        return None
    fields = record.data
    match record.type:
        case 'SRV':
            return cloudflare.DnsRecordDataArgs(
                priority=int(fields['priority']),
                weight=int(fields['weight']),
                port=int(fields['port']),
                target=str(fields['target']),
            )
        case 'CAA':
            return cloudflare.DnsRecordDataArgs(
                flags=int(fields['flags']),
                tag=str(fields['tag']),
                value=str(fields['value']),
            )
        case _:
            raise ValueError(f'{record.type} records carry content, not data')
