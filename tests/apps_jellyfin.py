"""What `test_apps_jellyfin.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import sso_portal

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import jellyfin as jellyfin_app
from kluster.components.apps.jellyfin import Jellyfin
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    PROXIED,
    REWRITE,
    Exposure,
    Heard,
    Held,
    Port,
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

ROUTE = Route('jf-tube', internet=PROXIED, media=REWRITE)
DISCOVERY = Port('jf-find', 7359, Transports.UDP, media=Heard())

IMAGE = ImagePin('registry.example.invalid/jellyfin', 'v9.9.9', f'sha256:{"1" * 64}')


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


def declared() -> Sequence[Component]:
    """The server, for the run that declares every application component (`test_apps_placement.py`).

    Handed the census's client, at its host and held as `TUBE` is, and the
    portal's issuer, so the run reads it as a client of the portal it declares.
    """
    tube = sso_portal.client('jellyfin')
    return (
        Jellyfin(
            'jellyfin',
            exposure=Exposure(routes=(Held(replace(ROUTE, host=tube.host)),), ports=(DISCOVERY,)),
            client=tube,
            issuer=sso_portal.ISSUER,
            image=IMAGE,
            writer_key=writer_key(jellyfin_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
    )
