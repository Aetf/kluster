"""The `dns` stack: zones, the base records that belong to no app, anchors, and the resolvers' configuration.

Per-app records live beside their apps in `apps` (docs/declarative/dns.md);
what lands here is what has no app to co-locate with — mail, the overlay host
block, verifications, the family and parked zones — plus the anchors every app
record points at, plus the whole configuration of the AdGuard pair the LAN
resolves through: the split-horizon rewrites for every app, read from the same
plain-data route declaration `apps` builds its routes from, the overlay names,
the gateway's own and legacy names, and every setting the instances hold. That
configuration is the reason this stack joins ZeroTier.

The records themselves are data, written as blocks — the records that appear
together, in every zone of one set (`kluster.components.dns.base`,
`kluster.components.dns.legacy`). This program is only the wiring: which zones
exist, which addresses the anchors carry, and which censuses the resolvers'
rule list is derived from. What each zone carries is derived from the blocks
by `zone_records`, so no zone set is spelled out here, and which instances are
configured is the gateway census's answer (`conventions.gateway.RESOLVERS`), so
no instance is spelled out here either.

The anchors, `kluster.hosts` and `vip1.hosts`, are the one thing whose
contents are not written down: the addresses in them are machine facts the
`physical` stack hands out, so they come across the StackReference, and they
are the only thing that does. Their shape — labels, families, TTLs and
comments — is census data and lives with the rest of it; what this program
supplies is three addresses.
"""

from __future__ import annotations

import pulumi
import pulumi_cloudflare as cloudflare

from kluster import conventions
from kluster.components.dns import base, resolver_settings
from kluster.components.dns.aliases import ALIASES
from kluster.components.dns.blocklists import PS4_UPDATES, PS4_UPDATES_SOURCE
from kluster.components.dns.legacy import LEGACY
from kluster.components.dns.record import zone_records
from kluster.components.dns.resolver import AdGuardConfiguration, ResolverConfiguration
from kluster.components.dns.rewrites import (
    RuleBlock,
    gateway_rewrites,
    legacy_vhost_rewrites,
    overlay_rewrites,
    rewrites,
)
from kluster.components.dns.zone import ManagedZone
from kluster.components.gateway.container import ADGUARD_GATEWAY_ZONES
from kluster.lib import stack_addresses

#: Where the zones token is read: at the line that builds the provider it
#: configures, and nowhere else (rfc-002 §8.1). The key is this project's, not
#: the provider package's, because a `cloudflare:` entry in a committed stack
#: file is indistinguishable from the ambient configuration this repository has
#: removed everywhere else, and reads as one to anybody who does not also check
#: that default providers are disabled for that package. Every other provider
#: credential here is a `kluster-py:` key read at the line that builds its
#: provider, and this one is no different.
CLOUDFLARE_API_TOKEN = 'cloudflareApiToken'


#: What an anchor's read raises when the `physical` output it reads is not an
#: address of the anchor's family: the refusal every address read across a
#: StackReference raises, which names the output, the family read from it, and
#: what it held instead, and never quotes the value.
UnusableAnchorAddress = stack_addresses.UnusableAddressOutput


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

    # One configuration, handed whole to one component per AdGuard instance:
    # the instances differ in no setting. Nothing here reads the instances'
    # login -- the provider reads it in `configure` (framework/pulumi.md
    # §5.2) -- and where each instance is reached is the census's answer
    # rather than a key this stack carries. No two instances restart their
    # DNS listeners at once: each one's restarting writes go behind the
    # previous one's, in the census's order (`ResolverConfiguration`).
    configuration = resolver_configuration()
    previous: tuple[pulumi.Resource, ...] = ()
    for resolver in conventions.gateway.RESOLVERS:
        component = ResolverConfiguration(resolver.name, resolver=resolver, configuration=configuration, after=previous)
        previous = component.restarting

    # Machine facts: `apps` needs the zone ids to declare its own records.
    pulumi.export('zone_ids', {zone: managed.zone.id for zone, managed in zones.items()})


def resolver_configuration() -> AdGuardConfiguration:
    """The one configuration both resolvers are handed.

    The rule list is derived from the censuses it answers for, in the order it
    renders, and the client set from its blocklist rows. A function of its own
    so that a rehearsal against a throwaway instance declares exactly what
    this program does.
    """
    return resolver_settings.configuration(
        blocks=(
            RuleBlock(
                'split-horizon exposures (conventions.routes.EXPOSURES)',
                rewrites(conventions.routes.EXPOSURES.values()),
            ),
            RuleBlock('overlay members (conventions.overlay.ROSTER)', overlay_rewrites(conventions.overlay.ROSTER)),
            RuleBlock(
                "the gateway's own names, at its proxy (conventions.gateway)",
                gateway_rewrites(
                    conventions.gateway.VHOST_CONTROLLER, conventions.gateway.RESOLVERS, conventions.gateway.CADDY
                ),
            ),
            RuleBlock(
                'legacy names until their application migrates (conventions.gateway.LEGACY_VHOSTS)',
                legacy_vhost_rewrites(conventions.gateway.LEGACY_VHOSTS, conventions.gateway.CADDY),
            ),
            RuleBlock('aliases to device-plane names', ALIASES),
            RuleBlock(PS4_UPDATES_SOURCE, (PS4_UPDATES,)),
        ),
        # The device plane and the site's reverse zones go to the gateway's
        # own resolver, the one `physical` renders into the initial state too.
        forwarded_zones=ADGUARD_GATEWAY_ZONES,
        gateway=conventions.CONTAINER_VLAN.require_gateway(),
    )


def _anchor_addresses(physical: pulumi.StackReference) -> base.AnchorAddresses:
    """The three addresses the anchors carry, out of the physical stack, each refused unless it is one.

    Reading them is a job the census cannot do for itself, and this is the one
    place in the stack that reaches across a StackReference. The names asked
    for are `conventions.PHYSICAL_OUTPUTS`, the same structure `physical`
    exports under, so an output renamed there is renamed here in the same
    edit. A record's content is whatever this hands on, so the read is where a
    value that is not an address has to stop: each goes through
    `kluster.lib.stack_addresses`, which refuses anything but an address of
    the record's family by name, in a preview as in an update
    (`UnusableAnchorAddress`).
    """
    outputs = conventions.PHYSICAL_OUTPUTS
    return base.AnchorAddresses(
        cluster_v4=stack_addresses.address(physical, outputs.cluster_endpoint, 4),
        cluster_v6=stack_addresses.address(physical, outputs.cluster_endpoint_v6, 6),
        vip1_v4=stack_addresses.address(physical, outputs.vip1, 4),
    )
