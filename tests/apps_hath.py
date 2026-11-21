"""What `test_apps_hath.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import hath as hath_app
from kluster.components.apps.hath import Hath, Stage
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Dedicated, Exposure, Port
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

#: The raw port, at the dedicated node's own address.
SERVE = Port('hh-serve', 60123, Transports.TCP, internet=Dedicated())
CLIENT = Exposure(ports=(SERVE,))

FINAL = Stage('a-final-pass', final=True)

IMAGE = ImagePin('registry.example.invalid/hath', 'v9.9.9', f'sha256:{"5" * 64}')
#: Ciphertext of each key of the ported Secret, which names the key.
CIPHERTEXT = {key: f'sealed-{key}' for key in hath_app.PORTED.keys}


def declared() -> Sequence[Component]:
    """The client at its final stage, for the run that declares every application component (`test_apps_placement.py`)."""
    return (
        Hath('hath', exposure=CLIENT, image=IMAGE, login=CIPHERTEXT, stage=FINAL, fronts=FRONTS, zone_ids=ZONE_IDS),
    )
