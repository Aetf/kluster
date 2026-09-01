"""The dns program against a `physical` stack that has never been applied.

The anchors are where this stack reaches across a StackReference, and
`physical` is a placeholder until the cloud site is built: its stack exists and
publishes nothing. Declaring them must not depend on that -- a `dns` preview has
to show the same records either way, or the two stacks could never be brought
up in the order the migration prescribes. The ZeroTier host block is here for
the opposite reason: it reaches across nothing at all, and this is where that
is held.
"""

from typing import Any

import pulumi
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.dns.zones import zt_label

RECORD = 'cloudflare:index/dnsRecord:DnsRecord'


class EmptyPhysical(Recorder):
    """A `physical` whose stack exists and publishes nothing."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': {}}
        return {}


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> EmptyPhysical:
    from kluster.stacks import dns

    pulumi.runtime.set_all_config({f'kluster:{dns.CLOUDFLARE_API_TOKEN}': 'a-zones-token'})
    monitor = await run_with(EmptyPhysical(), stack='dns', preview=True)
    async with declaring():
        await dns.main()
    return monitor


def record_names(stack: EmptyPhysical) -> set[str]:
    return stack.names(RECORD)


def test_the_anchors_are_declared_without_the_addresses_being_known(stack: EmptyPhysical) -> None:
    """Both families, from a stack reference that has nothing to hand out.

    Awaiting the address here — to skip a record whose address is missing,
    say — would make the declaration depend on `physical` having been applied
    first, and there would be no preview to review before it is.
    """
    names = record_names(stack)

    for suffix in ('a', 'aaaa'):
        assert f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-{suffix}' in names
    assert f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_VIP1}-a' in names


def test_the_overlay_block_is_the_whole_roster_and_waits_on_no_other_stack(stack: EmptyPhysical) -> None:
    """The `*.zt` block is decided by code, names and addresses alike.

    The roster crosses as a module and carries both halves of every entry, so
    an unapplied `physical` costs this block nothing — neither its shape nor
    its contents. A program that read the members out of the stack reference
    instead could declare nothing at all here, and the first `up` of the pair
    would have no preview to review.
    """
    names = record_names(stack)

    for entry in conventions.overlay.ROSTER:
        assert f'{conventions.ZONE_PRIMARY}-{zt_label(entry.name)}.{conventions.ZT_LABEL}-a' in names, entry.name


def test_the_rest_of_the_estate_is_declared_too(stack: EmptyPhysical) -> None:
    # The estate is literals: nothing about it should have been held up by
    # the one part of the program that reads another stack.
    names = record_names(stack)

    assert all(any(name.startswith(f'{zone}-') for name in names) for zone in conventions.ALL_ZONES)
