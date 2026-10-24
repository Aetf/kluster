"""The gateway's overlay daemon joins the network the overlay component adopts.

The two halves are declared by two components that never see each other
(rfc-002 §6): the overlay adopts the network and authorizes its members at
Central, and the gateway delivers the daemon that has to ask to join it. The
id reaches them by two routes: the overlay is handed it by the stack program,
and the gateway reads it from `conventions` for itself, so the two can
disagree. The case runs the program whole and reads both halves out of what it
registered.

What "joins" means is the daemon's own rule, stated here from its source rather
than from this program's: at start, ZeroTierOne joins one network for every
file directly in `networks.d/` of its state directory whose name is the
network's sixteen hex digits followed by `.conf` (`service/OneService.cpp`,
1.16.2), and nothing in the image joins one any other way.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from device_places import DEVICE_FILE
from mock_monitor import declaring
from physical_installation import Installation, install

from kluster import conventions
from kluster.components.gateway import nspawn
from kluster.stacks import physical

#: The daemon's join rule, as a path relative to its state directory.
JOINED_AT_START = re.compile(r'networks\.d/([0-9a-f]{16})\.conf')

#: The type the overlay network is registered under.
NETWORK = 'zerotier:index/network:Network'


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def recorded(tmp_path_factory: pytest.TempPathFactory) -> Installation:
    """The whole program, run once under the configuration as committed."""
    with pytest.MonkeyPatch.context() as patch:
        installation = await install(patch, tmp_path_factory.mktemp('checkout'))
        async with declaring():
            await physical.main()
    return installation


def joined_at_first_start(recorded: Installation) -> set[str]:
    """The networks the overlay daemon joins the first time it starts.

    A first start is a state directory that has never held anything, so what
    the daemon finds in it is its initial state, delivered under the machine's
    initial-state directory in the layout it is copied into (`nspawn`).
    """
    initial = nspawn.initial_state_path(conventions.gateway.OVERLAY.name, '')
    placed = [
        str(declaration.inputs['path']).removeprefix(initial)
        for declaration in recorded.of_type(DEVICE_FILE)
        if str(declaration.inputs.get('path', '')).startswith(initial)
    ]
    return {joined[1] for path in placed if (joined := JOINED_AT_START.fullmatch(path))}


def test_the_overlay_daemon_joins_the_network_the_overlay_adopts_at_its_first_start(recorded: Installation) -> None:
    """A daemon that joined nothing would mint a node id and never ask to be a member.

    The ceremony reads that node id and authorizes it at Central
    (physical/gateway.md §2.5), and authorization admits a member only once
    the member has asked: without a network named in its state, the device
    stays outside the overlay and every dial over it fails. The network asked
    for has to be the one adopted, by its id, or the request goes to a network
    whose members nobody here authorizes.
    """
    (adopted,) = [request.importId for request in recorded.requested if request.type == NETWORK]

    assert joined_at_first_start(recorded) == {adopted}
