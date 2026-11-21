"""What `test_apps_authelia.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

import sso_portal

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.authelia import (
    CONFIG_SECRETS,
    SECRETS,
    Authelia,
)
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import PROXIED, REWRITE, Exposure, Held, Route
from kluster.lib.versions import ImagePin
from putils import Component

#: The route row the portal is declared with, held as the land declares it.
EXPOSURE = Exposure(routes=(Held(Route('auth', internet=PROXIED, lan=REWRITE)),))

#: The pins it is declared with here: references that are no published
#: image, so a container carrying one carries that pin.
IMAGE = ImagePin('registry.example.invalid/authelia', 'v9.9.9', f'sha256:{"a" * 64}')
REDIS_IMAGE = ImagePin('registry.example.invalid/redis', 'v9.9.9', f'sha256:{"b" * 64}')
CANARY_IMAGE = ImagePin('registry.example.invalid/curl', 'v9.9.9', f'sha256:{"c" * 64}')
TARGET_IMAGE = ImagePin('registry.example.invalid/nginx', 'v9.9.9', f'sha256:{"d" * 64}')

#: The ciphertexts it is handed, each naming what it stands for.
SECRETS_CIPHERTEXT = {key: f'sealed-{key}' for key in SECRETS.keys}
CONFIG_CIPHERTEXT = {key: f'sealed-{key}' for key in CONFIG_SECRETS.keys}
STORAGE_KEY = {key: f'sealed-{key}' for key in conventions.sealed.AUTHELIA_STORAGE_KEY.keys}
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-key-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-key-secret'}

#: A namespace whose routes the gate guards.
GATED = 'some-application'

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


def declare(*, exposure: Exposure = EXPOSURE, receiving: bool = False) -> Authelia:
    return Authelia(
        'authelia',
        exposure=exposure,
        image=IMAGE,
        redis_image=REDIS_IMAGE,
        canary_image=CANARY_IMAGE,
        target_image=TARGET_IMAGE,
        secrets=SECRETS_CIPHERTEXT,
        config_secrets=CONFIG_CIPHERTEXT,
        storage_key=STORAGE_KEY,
        writer_key=WRITER_KEY,
        gated=(GATED,),
        fronts=FRONTS,
        zone_ids=ZONE_IDS,
        receiving=receiving,
    )


def declared() -> Sequence[Component]:
    """The portal at the row its clients' issuer derives from, for the run that declares every application component (`test_apps_placement.py`)."""
    return (declare(exposure=sso_portal.PORTAL),)
