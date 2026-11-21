"""What `test_apps_splitpro.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

import sso_portal

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.splitpro import (
    SplitPro,
)
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import PROXIED, Exposure, Held, Route
from kluster.lib.versions import ImagePin
from putils import Component

#: The census's client, which the program hands the server.
(CLIENT,) = [client for client in conventions.sso.CLIENTS if client.name == 'splitpro']

#: The route row the server is declared with: its client's host, behind the
#: proxy, held as the land declares it.
EXPOSURE = Exposure(routes=(Held(Route(CLIENT.host, internet=PROXIED)),))

#: The pins it is declared with here: references that are no published image,
#: so a container carrying one carries that pin.
IMAGE = ImagePin('registry.example.invalid/splitpro', 'v9.9.9', f'sha256:{"a" * 64}')
DATABASE_IMAGE = ImagePin('registry.example.invalid/pgcron', '17.9', f'sha256:{"b" * 64}')
DRILL_IMAGE = ImagePin('registry.example.invalid/drill', '1.0', f'sha256:{"c" * 64}')

#: The ciphertexts it is handed, each naming what it stands for.
CIPHERTEXT = {
    'nextauth_secret': 'sealed-session-secret',
    'oidc_client_id': 'sealed-client-id',
    'oidc_client_secret': 'sealed-client-secret',
}
RECOVERY_CIPHERTEXT = {'gcs_credentials': 'sealed-service-account-key'}
DATABASE_WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-barman-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-barman-secret'}
RECEIPTS_WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-volsync-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-volsync-secret'}

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
    """The server, for the run that declares every application component (`test_apps_placement.py`).

    Handed the portal's issuer, so the run reads it as a client of the portal it declares.
    """
    return (
        SplitPro(
            'splitpro',
            exposure=EXPOSURE,
            client=CLIENT,
            issuer=sso_portal.ISSUER,
            image=IMAGE,
            database_image=DATABASE_IMAGE,
            drill_image=DRILL_IMAGE,
            auth=CIPHERTEXT,
            recovery_key=RECOVERY_CIPHERTEXT,
            database_writer_key=DATABASE_WRITER_KEY,
            receipts_writer_key=RECEIPTS_WRITER_KEY,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        ),
    )
