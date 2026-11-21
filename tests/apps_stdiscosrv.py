"""What `test_apps_stdiscosrv.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.stdiscosrv import Stdiscosrv
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    REWRITE,
    Balancer,
    Direct,
    Exposure,
    Held,
    Host,
    Port,
    PortRedirect,
    Vip,
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

#: The discovery port: dialed at two names through the balancer, the first
#: held as a row whose name the legacy VPS keeps, and at a third at home.
FIND = Port(
    'st-find',
    8444,
    Transports.TCP,
    internet=Balancer((Held(Host('st-find')), Host('st-also'))),
    lan=Vip('st-home'),
)
#: The redirect a device that names the server with no port is answered with,
#: held as the name it shares is.
REDIRECT = PortRedirect('st-home', FIND.port, internet=Direct('a raw port'), lan=REWRITE)
EXPOSURE = Exposure(routes=(Held(REDIRECT),), ports=(FIND,))

IMAGE = ImagePin('registry.example.invalid/discosrv', 'v9.9.9', f'sha256:{"3" * 64}')


def declared() -> Sequence[Component]:
    """The discovery server, for the run that declares every application component (`test_apps_placement.py`)."""
    return (Stdiscosrv('stdiscosrv', exposure=EXPOSURE, image=IMAGE, fronts=FRONTS, zone_ids=ZONE_IDS),)
