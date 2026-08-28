"""The dns program against a `physical` stack that has never been applied.

The anchors and the ZeroTier host block are where this stack reaches across a
StackReference, and `physical` is a placeholder until the cloud site is built:
its stack exists and publishes nothing. Declaring them must not depend on that
— a `dns` preview has to show the same records either way, or the two stacks
could never be brought up in the order the migration prescribes.
"""

import asyncio
from typing import Any, cast

import pulumi
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions
from kluster.dns.zones import zt_label

ACCOUNT_ID = 'cf-account'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'

declared: list[tuple[str, str, dict[str, Any]]] = []


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        declared.append((args.typ, args.name, outputs))
        if args.typ == 'pulumi:pulumi:StackReference':
            # An empty stack: every `get_output` answers with nothing.
            outputs['outputs'] = {}
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> None:
    pulumi.runtime.set_all_config({'kluster:cloudflareAccountId': ACCOUNT_ID})
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='dns', preview=True)
    from kluster.stacks import dns

    # The same drain as the applied case: only the registrations this program
    # scheduled may be awaited, because the task queue is process-global.
    before = asyncio.all_tasks()

    await dns.main()

    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def _record_names() -> set[str]:
    return {name for kind, name, _ in declared if kind == RECORD}


def test_the_anchors_are_declared_without_the_addresses_being_known() -> None:
    """Both families, from a stack reference that has nothing to hand out.

    Awaiting the address here — to skip a record whose address is missing,
    say — would make the declaration depend on `physical` having been applied
    first, and there would be no preview to review before it is.
    """
    names = _record_names()

    for suffix in ('a', 'aaaa'):
        assert f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-{suffix}' in names
    assert f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_VIP1}-a' in names


def test_the_overlay_block_is_the_whole_roster_before_any_address_is_known() -> None:
    """How many `*.zt` records exist is decided by code, not by the export.

    The roster crosses as a module and only the addresses cross as outputs, so
    an unapplied `physical` costs the block its contents and not its shape. A
    program that read the member names out of the stack reference instead
    could declare nothing at all here — and the first `up` of the pair would
    have no preview to review.
    """
    names = _record_names()

    for entry in conventions.ZT_ROSTER:
        assert f'{conventions.ZONE_PRIMARY}-{zt_label(entry.name)}.{conventions.ZT_LABEL}-a' in names, entry.name


def test_the_rest_of_the_estate_is_declared_too() -> None:
    # The estate is literals: nothing about it should have been held up by
    # the one part of the program that reads another stack.
    names = _record_names()

    assert all(any(name.startswith(f'{zone}-') for name in names) for zone in conventions.ALL_ZONES)
