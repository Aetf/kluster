"""The `dns` stack: zones, the base records that belong to no app, anchors.

Per-app records live beside their apps in `apps` (docs/declarative/dns.md);
what lands here is what has no app to co-locate with — mail, the overlay host
block, verifications, the family and parked zones — plus the anchors every app
record points at, plus the split-horizon rewrites for every app: they are
read from the same plain-data route declaration `apps` builds its routes
from, and they are the reason this stack joins ZeroTier.

The records themselves are data, written as blocks — the records that appear
together, in every zone of one set (`kluster.components.dns.base`,
`kluster.components.dns.legacy`). This program is only the wiring: which zones
exist, and which addresses the anchors carry. What each zone carries is derived
from the blocks by `zone_records`, so no zone set is spelled out here, and
which instances the rewrites are written to is the gateway census's answer
(`conventions.gateway.RESOLVERS`), so no instance is spelled out here either.

The anchors, `kluster.hosts` and `vip1.hosts`, are the one thing whose
contents are not written down: the addresses in them are machine facts the
`physical` stack hands out, so they come across the StackReference, and they
are the only thing that does. Their shape — labels, families, TTLs and
comments — is census data and lives with the rest of it; what this program
supplies is three addresses.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Literal

import pulumi
import pulumi_cloudflare as cloudflare
from pulumi.output import Unknown

from kluster import conventions
from kluster.components.dns import base
from kluster.components.dns.legacy import LEGACY
from kluster.components.dns.record import zone_records
from kluster.components.dns.rewrites import ResolverRewrites, rewrites
from kluster.components.dns.zone import ManagedZone

#: Where the zones token is read: at the line that builds the provider it
#: configures, and nowhere else (rfc-002 §8.1). The key is this project's, not
#: the provider package's, because a `cloudflare:` entry in a committed stack
#: file is indistinguishable from the ambient configuration this repository has
#: removed everywhere else, and reads as one to anybody who does not also check
#: that default providers are disabled for that package. Every other provider
#: credential here is a `kluster-py:` key read at the line that builds its
#: provider, and this one is no different.
CLOUDFLARE_API_TOKEN = 'cloudflareApiToken'


class UnusableAnchorAddress(ValueError):
    """An output of the `physical` stack that an anchor reads and that is not an address of the anchor's family."""


async def main() -> None:
    config = pulumi.Config()

    # One provider for every zone: the token is scoped to the installation's
    # zones as a set, so a provider built inside one zone's component would be
    # reached into by the rest (rfc-002 §8.1).
    zone_provider = cloudflare.Provider(
        f'{conventions.CLUSTER_NAME}-cloudflare',
        api_token=config.require_secret(CLOUDFLARE_API_TOKEN),
    )
    on_cloudflare = pulumi.ResourceOptions(providers=[zone_provider])

    physical = pulumi.StackReference(
        f'{pulumi.get_organization()}/{pulumi.get_project()}/{conventions.STACK_NAMES.physical}'
    )

    # The whole declaration, as blocks: what belongs to no application, and
    # what the legacy VPS still serves until each application migrates. Which
    # zones a block appears in is the block's own first column, so this loop
    # has no zone in it but the one it is building.
    blocks = (*base.blocks(anchors=_anchor_addresses(physical)), *LEGACY)
    zones = {
        zone: ManagedZone(
            zone,
            zone=zone,
            account_id=conventions.CLOUDFLARE_ACCOUNT.account_id,
            records=zone_records(zone, blocks),
            opts=on_cloudflare,
        )
        for zone in conventions.ALL_ZONES
    }

    # The rewrites the routes imply, one component per AdGuard instance and
    # unconditionally: with an empty route census each declares nothing, no
    # dynamic resource exists, the provider process never starts and the login
    # is never read. Nothing here reads it in any case -- it opens the rewrite
    # provider and nothing else, so the provider reads it in `configure`
    # (framework/pulumi.md §5.2), and where each instance is reached is the census's answer
    # rather than a key this stack carries.
    entries = rewrites(conventions.routes.ROUTES)
    for resolver in conventions.gateway.RESOLVERS:
        _ = ResolverRewrites(f'rewrites-{resolver.name}', resolver=resolver, entries=entries)

    # Machine facts: `apps` needs the zone ids to declare its own records.
    pulumi.export('zone_ids', {zone: managed.zone.id for zone, managed in zones.items()})


def _anchor_addresses(physical: pulumi.StackReference) -> base.AnchorAddresses:
    """The three addresses the anchors carry, out of the physical stack.

    Reading them is a job the census cannot do for itself, and this is the one
    place in the stack that reaches across a StackReference. The names asked
    for are `conventions.PHYSICAL_OUTPUTS`, the same structure `physical`
    exports under, so an output renamed there is renamed here in the same
    edit.
    """
    outputs = conventions.PHYSICAL_OUTPUTS
    return base.AnchorAddresses(
        cluster_v4=_address(physical, outputs.cluster_endpoint, 4),
        cluster_v6=_address(physical, outputs.cluster_endpoint_v6, 6),
        vip1_v4=_address(physical, outputs.vip1, 4),
    )


def _address(physical: pulumi.StackReference, output: str, version: Literal[4, 6]) -> pulumi.Output[str]:
    """One address output of the physical stack, as a record's content, refused unless it is one.

    The record's content is whatever this hands on, so the read is where a
    value that is not an address has to stop: carried on, an absent output is
    `None` and becomes the record content `"None"`. What a StackReference
    reads back in place of an address, measured at the pinned SDK
    (framework/pulumi.md §1.4, §3.1):

    -   `None`, for an output `physical` has not published -- the state of a
        stack that has never been applied -- and, in an update, for one a
        targeted apply of it wrote as Pulumi's unknown sentinel;
    -   an unknown, in a preview, for every output of a `physical` that holds
        that sentinel in any of them;
    -   `{}`, for a secret this stack cannot decrypt.

    So the value is checked unknowns included (`run_with_unknowns`), in a
    preview as in an update, and anything but an address of the record's
    family stops the run naming the output and what it found.
    """
    return physical.get_output(output).apply(
        lambda value: _usable_address(output, version, value), run_with_unknowns=True
    )


def _usable_address(output: str, version: Literal[4, 6], value: object) -> str:
    """`value` if it is an IPv`version` address, else a refusal saying what it is -- never the value itself."""
    if isinstance(value, str):
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            found = 'a string that is not an address' if value else 'an empty string'
        else:
            if parsed.version == version:
                return value
            found = f'an IPv{parsed.version} address'
    elif value is None:
        found = (
            'absent: `physical` has not published it, or a targeted apply of `physical` wrote it as '
            "Pulumi's unknown sentinel, which an update reads back as nothing (framework/pulumi.md §1.4), "
            'and the rest of `physical` has to be applied first'
        )
    elif isinstance(value, Unknown):
        found = (
            "unknown, which is how a preview reads every output of a `physical` holding Pulumi's unknown "
            'sentinel in any of them: a targeted apply of `physical` wrote it (framework/pulumi.md §1.4), '
            'and the rest of `physical` has to be applied first'
        )
    elif isinstance(value, Mapping):
        found = (
            'a mapping, which is how a StackReference reads back a secret it could not decrypt: this '
            "stack cannot open `physical`'s secrets"
        )
    else:
        found = f'a {type(value).__name__}'
    raise UnusableAnchorAddress(
        f"the physical stack's {output!r} output is {found}; an anchor takes an IPv{version} address from it, "
        'and no record is declared with what it holds'
    )
