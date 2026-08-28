"""The `dns` stack: zones, the estate records that belong to no app, anchors.

Per-app records live beside their apps in `apps` (docs/declarative/dns.md);
what lands here is what has no app to co-locate with — mail, the ZeroTier host
block, verifications, the family and alias zones — plus the anchors every app
record points at, plus the split-horizon rewrites for every app: they are
read from the same plain-data route declaration `apps` builds its routes
from, and they are the reason this is the one stack that joins ZeroTier.

The records themselves are data (`kluster.dns.zones`, `kluster.dns.legacy`),
so this program is only the wiring: which zones exist, which records go in
them, which addresses the anchors carry, and which instances the rewrites are
written to.

Two blocks are built here rather than written down, and both for the same
reason — the `physical` stack decides what is in them, so they come across the
StackReference as machine facts. The anchors, `kluster.hosts` and `vip1.hosts`,
name addresses that stack hands out; the ZeroTier host block names one record
per member of the overlay roster it admits, at the address ZeroTier Central
assigned. Anchors are also the one place an IP literal is allowed, the overlay
block excepted: private addresses under `*.zt` are an existing deliberate
practice (dns.md §2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import pulumi

from kluster import conventions
from kluster.dns.adguard import declare_rewrites
from kluster.dns.legacy import LEGACY
from kluster.dns.model import Record, a, aaaa
from kluster.dns.routes import ROUTES, rewrites
from kluster.dns.zone import ManagedZone
from kluster.dns.zones import ESTATE, zt_records

#: The `physical` outputs the anchors are made of: the load balancer's two
#: public addresses, and the reserved IPv4 behind the dedicated VIP.
OUTPUT_CLUSTER_V4 = 'cluster_endpoint'
OUTPUT_CLUSTER_V6 = 'cluster_endpoint_v6'
OUTPUT_VIP1_V4 = 'vip1'

#: The `physical` output the ZeroTier host block is filled from: member name →
#: the overlay address Central assigned it, for the members whose address is a
#: fact rather than one of this repository's conventions.
OUTPUT_ZT_ADDRESSES = 'zerotier_addresses'

#: Both families of the cluster anchor say the same thing to a reader of the
#: Cloudflare dashboard, so they say it in the same words.
ANCHOR_CLUSTER_COMMENT = 'cluster ingress; every app record is a CNAME here'


async def main() -> None:
    config = pulumi.Config()
    account_id = config.require('cloudflareAccountId')

    physical = pulumi.StackReference(f'{pulumi.get_organization()}/{pulumi.get_project()}/physical')
    anchors = _anchors(physical)
    overlay = _zt_block(physical)

    zones = {
        zone: ManagedZone(
            zone,
            zone=zone,
            account_id=account_id,
            records=[
                *ESTATE[zone],
                *LEGACY.get(zone, ()),
                # The overlay block belongs to the mirrored estate, so it
                # reaches exactly the zones that carry the rest of it.
                *(overlay if zone in conventions.PUBLIC_ALL else ()),
                # Only the primary carries the cluster anchors: every app
                # record in every zone is a CNAME to the one in the primary,
                # so a rebuild moves one record, not one per zone.
                *(anchors if zone == conventions.ZONE_PRIMARY else ()),
            ],
        )
        for zone in conventions.ALL_ZONES
    }

    # The rewrites the routes imply, on both AdGuard instances. Reading the
    # credential only when there is something to write keeps the stack
    # deployable before the AdGuard secrets exist, which is the state it is
    # in until the first app declares a LAN-side route.
    entries = rewrites(ROUTES)
    if entries:
        _ = declare_rewrites(
            entries,
            endpoints=config.require_object('adguardEndpoints'),
            username=config.require_secret('adguardUsername'),
            password=config.require_secret('adguardPassword'),
        )

    # Machine facts: `apps` needs the zone ids to declare its own records.
    pulumi.export('zone_ids', {zone: managed.zone.id for zone, managed in zones.items()})


def _anchors(physical: pulumi.StackReference) -> Sequence[Record]:
    """The anchor namespace, from the physical stack's addresses.

    `kluster.hosts` is the cluster's front door — the network load balancer,
    which is dual-stack (architecture.md §3.2), so the anchor carries both an
    A and an AAAA and an app CNAME to it inherits both families. `vip1.hosts`
    is the dedicated VIP, which nothing resolves in anger: it is there so an
    operator can name the address without looking it up. It is IPv4 only by
    construction — the VIP is a reserved public IPv4 that OCI 1:1-NATs onto a
    secondary private address, and that mechanism has no IPv6 counterpart.
    The state backend deliberately has no anchor: its clients pin its IP, and
    its hot path must not depend on this stack.

    These are also the only anchors in the estate that are not literals, and
    nothing here awaits them. An address the `physical` stack has not
    published yet travels into the record as an unresolved output rather than
    raising, so this program declares the same records whether or not
    `physical` has been applied — which is the state it is in today.
    """
    return (
        a(
            conventions.ANCHOR_CLUSTER,
            _address(physical, OUTPUT_CLUSTER_V4),
            ttl=conventions.ANCHOR_TTL,
            comment=ANCHOR_CLUSTER_COMMENT,
        ),
        aaaa(
            conventions.ANCHOR_CLUSTER,
            _address(physical, OUTPUT_CLUSTER_V6),
            ttl=conventions.ANCHOR_TTL,
            comment=ANCHOR_CLUSTER_COMMENT,
        ),
        a(
            conventions.ANCHOR_VIP1,
            _address(physical, OUTPUT_VIP1_V4),
            ttl=conventions.ANCHOR_TTL,
            comment='dedicated VIP, operator convenience; IPv4 only by construction',
        ),
    )


def _zt_block(physical: pulumi.StackReference) -> Sequence[Record]:
    """The ZeroTier host block, from the roster the `physical` stack declares.

    The split across the reference is the point. *Which* records exist is code
    — the roster is that stack's own census and is shared as a module, like
    every other convention (framework/pulumi.md §3.1) — and only the contents
    are machine facts. So this block is declared without awaiting anything, and
    a member's address arriving late reaches the record unresolved exactly as
    an anchor's does; `dns` previews the same names before and after
    `physical` is applied.

    The lookup is written not to raise, for the same reason: before the first
    apply there is no map to look in, and a preview that failed there would be
    a preview nobody could review. What it cannot do is quietly publish
    something wrong afterwards — `physical` exports an entry for every member
    whose address is configured, and those are precisely the members asked for
    here (the rest are addresses this repository decides, which `zt_records`
    reads off the roster entry without asking at all), so a miss can only mean
    an address that does not exist yet, and the record it would write is one
    Cloudflare refuses rather than one that resolves somewhere unintended.
    """
    published = physical.get_output(OUTPUT_ZT_ADDRESSES)

    def address(member: str) -> pulumi.Output[str]:
        return published.apply(lambda addresses: _member_address(addresses, member))

    return zt_records(address)


def _member_address(published: object, member: str) -> str:
    """One member's overlay address out of the map `physical` exports."""
    addresses = cast('Mapping[str, object]', published or {})
    return str(addresses.get(member))


def _address(physical: pulumi.StackReference, output: str) -> pulumi.Output[str]:
    """One address output of the physical stack, as a record's content."""
    return physical.get_output(output).apply(str)
