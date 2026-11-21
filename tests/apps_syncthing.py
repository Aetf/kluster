"""What `test_apps_syncthing.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

import image_busybox

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import syncthing as syncthing_app
from kluster.components.apps.syncthing import Syncthing
from kluster.components.apps.syncthing import webdav as webdav_app
from kluster.components.apps.syncthing.webdav import WebDav
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Balancer, Exposure, Gate, Held, Host, Port, Proxied, Route, Vip
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

#: The peer port, dialed at one name through the balancer, held as a row
#: whose name the legacy VPS keeps, and at another at home.
PEER = Port(
    'st-peer',
    22001,
    Transports.TCP_AND_UDP,
    internet=Balancer((Held(Host('st-dial')),)),
    lan=Vip('st-home'),
)
SYNC = Exposure(routes=(Route('st-ui', internet=Proxied(Gate.SESSION)),), ports=(PEER,))

DISCOVERY = Port('st-find', 8444, Transports.TCP, internet=Balancer((Host('st-find'),)))
DAV = Exposure(routes=(Route('st-files', internet=Proxied(Gate.BASIC)),))

RETIRED = '3RC4PLI-37YMKAT-PKEXMJM-QKPPZ4Z-T7IG4QT-OM7E3O2-JZ6GWX5-RRKBJQN'
RETIRED_TOO = 'IDKJET4-TUZKHCH-PL2EQ5S-AA72AQT-MBXXBD2-DXDFWES-6DL6SM3-6B4WCA6'

SYNCTHING_IMAGE = ImagePin('registry.example.invalid/syncthing', 'v9.9.9', f'sha256:{"1" * 64}')
WEBDAV_IMAGE = ImagePin('registry.example.invalid/dufs', 'v9.9.9', f'sha256:{"2" * 64}')
#: The merge's pin, which the script runs under here.
MERGE_IMAGE = image_busybox.pinned('syncthing-config')


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


def declared() -> Sequence[Component]:
    """The instance, retiring two devices, and the WebDAV server, for the run that declares every application component (`test_apps_placement.py`)."""
    return (
        Syncthing(
            'syncthing',
            exposure=SYNC,
            discovery=DISCOVERY,
            retired_devices=(RETIRED, RETIRED_TOO),
            image=SYNCTHING_IMAGE,
            config_image=MERGE_IMAGE,
            writer_key=writer_key(syncthing_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
        WebDav(
            'webdav',
            exposure=DAV,
            image=WEBDAV_IMAGE,
            writer_key=writer_key(webdav_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
    )
