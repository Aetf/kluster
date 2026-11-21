"""What `test_apps_blog.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

from collections.abc import Sequence

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.blog import blog
from kluster.components.apps.static_site import StaticSite
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import PROXIED, Apex, Exposure, Route
from kluster.conventions.sso import Client, Factor, Grant, Scope, TokenEndpointAuth
from kluster.lib.versions import ImagePin
from putils import Component

#: The blog's zones: the primary, whose apex is the Matrix server name, and
#: one of the test's own.
ZONES = (conventions.ZONE_PRIMARY, 'blog-cohost.example')

#: The blog's rows: the apex and `www` of each zone.
EXPOSURE = Exposure(routes=(Apex(ZONES), Route('www', internet=PROXIED, zones=ZONES)))

#: A homeserver's client of the test's own, at a host no census row carries.
CLIENT = Client(
    'mx-test',
    'mx-home',
    ('/callback',),
    policy=(Grant('mx-test-users', Factor.ONE),),
    token_endpoint_auth=TokenEndpointAuth.BASIC,
    scopes=(Scope.OPENID,),
)

#: The pins the blog is declared with here: references that are no published
#: image, so a container carrying one carries the pin.
SERVER = ImagePin('registry.example.invalid/static', 'v9.9.9', f'sha256:{"a" * 64}')
SYNC = ImagePin('registry.example.invalid/git-sync', 'v9.9.9', f'sha256:{"b" * 64}')

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
ZONE_IDS = {zone: f'{zone}-id' for zone in ZONES}


def declare(exposure: Exposure = EXPOSURE) -> StaticSite:
    return blog(
        exposure=exposure,
        matrix=CLIENT,
        image=SERVER,
        sync=SYNC,
        fronts=FRONTS,
        zone_ids=ZONE_IDS,
    )


def declared() -> Sequence[Component]:
    """The blog with its rows its own, for the run that declares every application component (`test_apps_placement.py`)."""
    return (declare(),)
