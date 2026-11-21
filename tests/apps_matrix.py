"""What `test_apps_matrix.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

import sso_portal

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.matrix import Matrix
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import PROXIED, Exposure, Held, Route
from kluster.lib.versions import ImagePin
from putils import Component

#: The census's client, which the program hands the homeserver.
(CLIENT,) = [client for client in conventions.sso.CLIENTS if client.name == 'matrix']

#: The route row the homeserver is declared with: its client's host, behind
#: the proxy, held as the land declares it.
EXPOSURE = Exposure(routes=(Held(Route(CLIENT.host, internet=PROXIED)),))

#: The pin it is declared with here: a reference that is no published image,
#: so a container carrying it carries the pin.
IMAGE = ImagePin('registry.example.invalid/continuwuity', 'v9.9.9', f'sha256:{"e" * 64}')

#: The ciphertexts it is handed, each naming what it stands for.
CIPHERTEXT = {'oidc_client_id': 'sealed-client-id', 'oidc_client_secret': 'sealed-client-secret'}
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-key-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-key-secret'}

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


def declared() -> Sequence[Component]:
    """The homeserver, for the run that declares every application component (`test_apps_placement.py`).

    Handed the portal's issuer, so the run reads it as a client of the portal it declares.
    """
    return (
        Matrix(
            'matrix',
            exposure=EXPOSURE,
            client=CLIENT,
            issuer=sso_portal.ISSUER,
            image=IMAGE,
            oidc=CIPHERTEXT,
            writer_key=WRITER_KEY,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
    )
