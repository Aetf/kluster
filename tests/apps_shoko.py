"""What `test_apps_shoko.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.shoko import Shoko
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import (
    REWRITE,
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

#: A home route, as shoko is reached.
LIBRARY = Exposure(routes=(Route('sk-library', lan=REWRITE),))

IMAGE = ImagePin('registry.example.invalid/shoko', 'v9.9.9', f'sha256:{"5" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'shoko-id', 'AWS_SECRET_ACCESS_KEY': 'shoko-secret'}


def declared() -> Sequence[Component]:
    """shoko on its home route, for the run that declares every application component (`test_apps_placement.py`)."""
    return (Shoko('shoko', exposure=LIBRARY, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS),)
