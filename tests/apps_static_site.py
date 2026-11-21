"""What `test_apps_static_site.py` declares the application with, and `declared()`, which the run of every application calls (`test_apps_placement.py`)."""

from __future__ import annotations

import json
from collections.abc import Sequence

from kluster.components.apps import Fronts
from kluster.components.apps.static_site import GitSource, StaticSite
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.routes import PROXIED, Apex, Exposure, Route
from kluster.lib.versions import ImagePin
from putils import Component

#: The blog's apex documents: two JSON bodies of the test's own, at paths
#: without an extension, as a delegation's are.
DOCUMENTS = {
    '/.well-known/example/server': json.dumps({'server': 'home.blog.example:443'}),
    '/.well-known/example/client': json.dumps({'base_url': 'https://home.blog.example'}),
}

#: The zones of the test's sites: the blog's two, and the doors' apex and the
#: zone of its two other names.
BLOG_ZONES = ('blog.example', 'blog-cohost.example')
DOORS_APEX = 'doors.example'
DOORS_ZONE = 'games.example'

#: The blog: its apex and `www` in each zone.
BLOG = Exposure(routes=(Apex(BLOG_ZONES), Route('www', internet=PROXIED, zones=BLOG_ZONES)))

#: The repository and branch the blog syncs: a repository the sync's case lays
#: into the container's home before git-sync starts, whose default branch is
#: not the one synced.
REPOSITORY = 'file:///tmp/upstream'
BRANCH = 'built'

#: The pins the sites are declared with here: references that are no
#: published image, so a container carrying one carries the pin.
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
ZONE_IDS = {zone: f'{zone}-id' for zone in (*BLOG_ZONES, DOORS_APEX, DOORS_ZONE)}


def declared() -> Sequence[Component]:
    """A site pulled from a branch, for the run that declares every application component (`test_apps_placement.py`).

    Named apart from the blog and the doors, which `test_apps_blog.py` and
    `test_apps_doors.py` declare there.
    """
    return (
        StaticSite(
            'pages',
            exposure=BLOG,
            source=GitSource(REPOSITORY, BRANCH, SYNC),
            image=SERVER,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
            directories={},
            documents=DOCUMENTS,
        ),
    )
