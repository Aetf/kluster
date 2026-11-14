"""What a declaration puts in `__provider` for a provider that holds its login only in its process."""

from __future__ import annotations

import base64
import pickle

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster.providers.adguard import AdGuardSetup
from kluster.providers.adguard.setup import AdGuardSetupProvider

INSTANCE = 'adguard-test'


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Recorder:
    return await run_with(Recorder(), stack='dns')


@pytest.mark.asyncio
async def test_an_adguard_setup_carries_a_provider_that_pickles_to_an_empty_bag(monitor: Recorder) -> None:
    """The setup makes the instance's account from the login, and its `__provider` names a class and nothing else.

    Read back from the declaration, so what is asserted is what state holds:
    unpickled, it is the provider class with no attribute at all, so no login
    lands in state and a rotation changes no resource's pickle.
    """
    async with declaring():
        _ = AdGuardSetup(
            'setup',
            instance=INSTANCE,
            endpoint='http://adguard.test:80',
            setup_endpoint='http://adguard.test:3000',
            listen={'web': {'ip': '0.0.0.0', 'port': 80}, 'dns': {'ip': '0.0.0.0', 'port': 53}},
        )

    (declared,) = [each for each in monitor.declared if each.typ == 'pulumi-python:dynamic/adguard:AdGuardSetup']
    pickled = str(declared.inputs['__provider']['value'])
    revived = pickle.loads(base64.b64decode(pickled))

    assert type(revived) is AdGuardSetupProvider
    assert vars(revived) == {}
    assert len(pickled) < 256
