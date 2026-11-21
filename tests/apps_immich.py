"""What `test_apps_immich.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import sso_portal

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import immich as immich_app
from kluster.components.apps.immich import CONFIG, RECOVERY, REDIS, Immich
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import (
    REWRITE,
    Direct,
    Exposure,
    Held,
    Route,
)
from kluster.conventions.sso import Client, Factor, Grant, Issuer, Scope, TokenEndpointAuth
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

ROUTE = Route('im-photos', internet=Direct('uploads larger than the proxy accepts'), lan=REWRITE)
#: The route held, as it is while the legacy VPS keeps the name.
PHOTOS = Exposure(routes=(Held(ROUTE),))
#: The mobile app's own redirect, which immich hands the portal as it is.
MOBILE = 'app.immich:///oauth-callback'
CLIENT = Client(
    'im',
    'im-photos',
    ('/auth/login', '/user-settings'),
    policy=(Grant('im-users', Factor.TWO),),
    token_endpoint_auth=TokenEndpointAuth.BASIC,
    scopes=(Scope.OPENID, Scope.PROFILE),
    native=(MOBILE,),
)
ISSUER = Issuer('https://portal.example.invalid')

SERVER_IMAGE = ImagePin('registry.example.invalid/server', 'v9.9.9', f'sha256:{"1" * 64}')
ML_IMAGE = ImagePin('registry.example.invalid/ml', 'v9.9.9-openvino', f'sha256:{"2" * 64}')
REDIS_IMAGE = ImagePin('registry.example.invalid/redis', '9.9.9', f'sha256:{"3" * 64}')
EXPORTER_IMAGE = ImagePin('registry.example.invalid/exporter', 'v9.9.9', f'sha256:{"6" * 64}')
DATABASE_IMAGE = ImagePin('registry.example.invalid/vchord', '15.99', f'sha256:{"4" * 64}')
DRILL_IMAGE = ImagePin('registry.example.invalid/drill', '1.0.0', f'sha256:{"5" * 64}')

REDIS_CIPHERTEXT = {key: f'AgB-ported-{key}' for key in REDIS.keys}
CONFIG_CIPHERTEXT = {key: f'AgB-ported-{key}' for key in CONFIG.keys}
RECOVERY_CIPHERTEXT = {key: f'AgB-ported-{key}' for key in RECOVERY.keys}


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


def arguments(**changed: Any) -> dict[str, Any]:
    return {
        'exposure': PHOTOS,
        'client': CLIENT,
        'issuer': ISSUER,
        'server_image': SERVER_IMAGE,
        'ml_image': ML_IMAGE,
        'redis_image': REDIS_IMAGE,
        'exporter_image': EXPORTER_IMAGE,
        'database_image': DATABASE_IMAGE,
        'drill_image': DRILL_IMAGE,
        'redis': REDIS_CIPHERTEXT,
        'config': CONFIG_CIPHERTEXT,
        'recovery': RECOVERY_CIPHERTEXT,
        'writer_key': writer_key(immich_app.NAMESPACE),
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
    } | changed


def declared() -> Sequence[Component]:
    """immich, for the run that declares every application component (`test_apps_placement.py`).

    Handed the census's client, at its host and held as `PHOTOS` is, and the
    portal's issuer, so the run reads it as a client of the portal it declares.
    """
    photos = sso_portal.client('immich')
    exposure = Exposure(routes=(Held(replace(ROUTE, host=photos.host)),))
    return (Immich('immich', **arguments(exposure=exposure, client=photos, issuer=sso_portal.ISSUER)),)
