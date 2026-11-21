"""What `test_apps_doors.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster.components.apps import Fronts
from kluster.components.apps.doors import doors
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import PROXIED, Apex, Exposure, Held, Route
from kluster.lib.versions import ImagePin
from putils import Component

#: The apex's zone, and the zone of the two other names.
APEX = 'doors-apex.example'
COHOST = 'doors-cohost.example'

#: The doors at their land: the apex, and `games` and `game` held while the
#: legacy VPS still serves them.
LAND = Exposure(
    routes=(
        Apex((APEX,)),
        Held(Route('games', internet=PROXIED, zones=(COHOST,))),
        Held(Route('game', internet=PROXIED, zones=(COHOST,))),
    )
)

SERVER = ImagePin('registry.example.invalid/static', 'v9.9.9', f'sha256:{"d" * 64}')

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
ZONE_IDS = {zone: f'{zone}-id' for zone in (APEX, COHOST)}


def declared() -> Sequence[Component]:
    """The doors as their land declares them, for the run that declares every application component (`test_apps_placement.py`)."""
    return (doors(exposure=LAND, image=SERVER, fronts=FRONTS, zone_ids=ZONE_IDS),)
