"""The echo server, as the run of every application declares it (`test_apps_placement.py`).

What the server renders is held in `test_apps.py`, beside the `apps`
program that wires it; this module is where the run of every application
finds it.
"""

from __future__ import annotations

from collections.abc import Sequence

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.echo import Echo
from kluster.components.cilium import InternetPoolMembers
from kluster.lib.versions import ImagePin
from putils import Component

FRONTS = Fronts(
    internet=InternetPoolMembers(
        node_private_ips={'cp-1': '10.0.1.11'},
        node_guas={'cp-1': '2001:db8:1::11'},
        dedicated_vip='10.0.1.99',
        balancer_v4='203.0.113.10',
        balancer_v6='2001:db8::10',
    ),
    worker_gua='2001:db8:70::10',
)
ZONE_IDS = {conventions.ZONE_PRIMARY: 'the-primary-zones-id'}

#: The pin it is declared with here: a reference that is no published image.
IMAGE = ImagePin('registry.example.invalid/whoami', 'v9.9.9', f'sha256:{"e" * 64}')


def declared() -> Sequence[Component]:
    """The echo server from its row of the census, for the run that declares every application component."""
    return (Echo('echo', exposure=conventions.routes.EXPOSURES['echo'], image=IMAGE, fronts=FRONTS, zone_ids=ZONE_IDS),)
