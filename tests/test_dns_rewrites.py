"""Rewrites as declared resources: one per instance, and no credential on any of them."""

import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.dns.adguard import declare_rewrites
from kluster.components.dns.routes import Exposure, Route, rewrites
from kluster.providers import configured

ENDPOINTS = ('http://alice.lan:3000', 'http://bob.lan:3000')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> Recorder:
    """One split-horizon route, declared once against the pair of instances."""
    monitor = await run_with(Recorder(), stack='dns')
    async with declaring():
        _ = declare_rewrites(
            rewrites([Route(host='photos', exposure=Exposure.SPLIT, zones=('ucw.phd',))]),
            endpoints=ENDPOINTS,
        )
    return monitor


def test_both_instances_are_written_to_directly(stack: Recorder) -> None:
    """Dual-writing is what retires adguardhome-sync.

    A synchronizer would overwrite whichever instance Pulumi wrote second,
    so the pair is two resources rather than one plus a copier.
    """
    names = {declaration.name for declaration in stack.declared}

    assert names == {
        'alice-lan-photos.ucw.phd-v4',
        'alice-lan-photos.ucw.phd-v6',
        'bob-lan-photos.ucw.phd-v4',
        'bob-lan-photos.ucw.phd-v6',
    }


def test_a_rewrite_carries_the_vip_its_route_implies(stack: Recorder) -> None:
    inputs = stack.inputs_of('alice-lan-photos.ucw.phd-v4')

    assert inputs['domain'] == 'photos.ucw.phd'
    assert inputs['answer'] == str(conventions.LAN_POOL.default_vip.v4)
    assert inputs['endpoint'] == ENDPOINTS[0]


def test_a_rewrite_declares_no_credential_and_no_stamp(stack: Recorder) -> None:
    """What the caller declares, and no more.

    The login is the provider's, read in `configure`; the two stamps are added
    by `check` in the plugin's process. A rewrite that carried either would put
    it in state on every row, on both instances, for every name.
    """
    inputs = stack.inputs_of('alice-lan-photos.ucw.phd-v4')

    assert 'username' not in inputs
    assert 'password' not in inputs
    assert configured.SESSION not in inputs
    assert configured.PROVIDER_VERSION not in inputs
