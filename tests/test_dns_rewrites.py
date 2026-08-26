"""Rewrites as declared resources: one per instance, credentials kept secret."""

import asyncio
from typing import Any, cast

import pulumi
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions
from kluster.dns.adguard import declare_rewrites
from kluster.dns.routes import Exposure, Route, rewrites

ENDPOINTS = ('http://alice.lan:3000', 'http://bob.lan:3000')

declared: list[tuple[str, str, dict[str, Any]]] = []


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        declared.append((args.typ, args.name, outputs))
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> None:
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='dns', preview=False)

    # Same drain as the stack test: a declaration schedules a registration
    # task, and only the tasks this module added may be awaited.
    before = asyncio.all_tasks()
    _ = declare_rewrites(
        rewrites([Route(host='photos', exposure=Exposure.SPLIT, zones=('ucw.phd',))]),
        endpoints=ENDPOINTS,
        username='admin',
        password='secret',
    )
    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def test_both_instances_are_written_to_directly() -> None:
    """Dual-writing is what retires adguardhome-sync.

    A synchronizer would overwrite whichever instance Pulumi wrote second,
    so the pair is two resources rather than one plus a copier.
    """
    names = {name for _, name, _ in declared}

    assert names == {
        'alice-lan-photos.ucw.phd-v4',
        'alice-lan-photos.ucw.phd-v6',
        'bob-lan-photos.ucw.phd-v4',
        'bob-lan-photos.ucw.phd-v6',
    }


def test_a_rewrite_carries_the_vip_its_route_implies() -> None:
    inputs = next(inputs for _, name, inputs in declared if name == 'alice-lan-photos.ucw.phd-v4')

    assert inputs['domain'] == 'photos.ucw.phd'
    assert inputs['answer'] == str(conventions.VIP_LAN_V4)
    assert inputs['endpoint'] == ENDPOINTS[0]
