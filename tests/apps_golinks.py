"""What `test_apps_golinks.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.golinks import Golinks
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import (
    BARE,
    Exposure,
    Route,
)
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

#: A home route with its bare label, as golinks is used.
LINKS = Exposure(routes=(Route('gl-links', lan=BARE),))

IMAGE = ImagePin('registry.example.invalid/golinks', 'v9.9.9', f'sha256:{"3" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'golinks-id', 'AWS_SECRET_ACCESS_KEY': 'golinks-secret'}


def declared() -> Sequence[Component]:
    """golinks on its bare home route, for the run that declares every application component (`test_apps_placement.py`)."""
    return (Golinks('golinks', exposure=LINKS, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS),)
