"""What `test_apps_emailproxy.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import emailproxy as emailproxy_app
from kluster.components.apps.emailproxy import EmailProxy
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Exposure, Port, Vip
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

#: The raw port, dialed at one name at the LAN VIP.
MAIL = Port('ep-mail', 1994, Transports.TCP, lan=Vip('ep-mail'))
PROXY = Exposure(ports=(MAIL,))

IMAGE = ImagePin('registry.example.invalid/emailproxy', 'v9.9.9', f'sha256:{"4" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'emailproxy-id', 'AWS_SECRET_ACCESS_KEY': 'emailproxy-secret'}
#: Ciphertext of each key of the ported Secret, which names the key.
CIPHERTEXT = {key: f'sealed-{key}' for key in emailproxy_app.PORTED.keys}


def declared() -> Sequence[Component]:
    """The proxy, for the run that declares every application component (`test_apps_placement.py`)."""
    return (
        EmailProxy(
            'emailproxy',
            exposure=PROXY,
            image=IMAGE,
            config=CIPHERTEXT,
            writer_key=WRITER_KEY,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
    )
