"""The doors: three small static sites, served from the NAS by the `StaticSite` component (rfc-008 §5.5).

The doors are frozen, low-traffic pages whose content does not belong in a
public repository, so they are the static site's NAS instance
(declarative/workloads.md §4): one nginx reading `CONTENT` under the host's
export through a read-only `nas_claim`, at the home site, which alone reaches
the export. They are edited by dropping files on the NAS, and the cluster
deploys nothing for a change.

**Each of its names reads the directory the legacy server read it from**, so
the content is the legacy webroot's three directories, copied once onto the NAS
with their names: the apex reads `APEX_DIRECTORY`, and each route row reads
the directory `LABEL_DIRECTORIES` names for its label, in every zone it serves.
A route row for which the doors hold no directory is refused, rather than left
to read the content's root, which holds the three sites side by side.

The rows themselves are the census's, handed in: an apex, whose zone holds no
other row, and `game` and `games` in the website co-host's zone. The labels
that alias the apex in DNS have no row, so they reach the Gateway with no
route, as they reach the legacy ingress.

The server is the static site's own (`nginx-unprivileged`, pinned as
`versions:image-static-site`), which runs as its image's numeric user. The doors
port no Secret, so their namespace is named after them
(declarative/workloads.md §5).
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi

from kluster.components.apps import Fronts
from kluster.components.apps.static_site import NasSource, StaticSite
from kluster.conventions.routes import Apex, Exposure, Route, unwrapped
from kluster.lib.versions import ImagePin

__all__ = ('APEX_DIRECTORY', 'CONTENT', 'LABEL_DIRECTORIES', 'NAME', 'directories', 'doors')

#: The application's name: its namespace's, its Deployment's and its Service's.
NAME = 'doors'

#: Where the content is, under the host's export: the legacy webroot's three
#: directories, copied there at the head of the wave.
CONTENT = 'Doors'

#: The directory the apex reads, under the content: the legacy webroot's name
#: for it.
APEX_DIRECTORY = 'door-jiahui'

#: The directory each route row reads, under the content, by the row's label:
#: the legacy webroot's names for them.
LABEL_DIRECTORIES: Mapping[str, str] = {'games': 'door-shiyu', 'game': 'door'}


def directories(exposure: Exposure) -> dict[str, str]:
    """The directory each name the exposure serves reads, held rows included; a row the doors hold no directory for is refused."""
    found: dict[str, str] = {}
    for held in exposure.routes:
        match unwrapped(held):
            case Apex() as row:
                found |= dict.fromkeys(row.zones, APEX_DIRECTORY)
            case Route() as row if row.host in LABEL_DIRECTORIES:
                found |= {f'{row.host}.{zone}': LABEL_DIRECTORIES[row.host] for zone in row.zones}
            case Route() as row:
                raise ValueError(
                    f'{NAME}: the row {row.host!r} names no directory the doors hold '
                    f'(the apex, and {", ".join(sorted(LABEL_DIRECTORIES))}), and would read every site at once'
                )
            case _:
                # A passthrough or a redirect serves no file of the content:
                # `StaticSite` holds what each means for a static site.
                pass
    return found


def doors(
    *,
    exposure: Exposure,
    image: ImagePin,
    fronts: Fronts,
    zone_ids: pulumi.Input[Mapping[str, str]],
    opts: pulumi.ResourceOptions | None = None,
) -> StaticSite:
    """The doors' static site: `CONTENT` from the NAS, each name reading its own directory, and no documents.

    `exposure` is the doors' row of the census, route rows alone; `image` is the
    server's pin, and `fronts` and `zone_ids` are what the program read for
    `expose`. The providers arrive in `opts`: the Kubernetes provider for the
    cluster's objects and the Cloudflare one for its records.
    """
    return StaticSite(
        NAME,
        exposure=exposure,
        source=NasSource(CONTENT),
        image=image,
        fronts=fronts,
        zone_ids=zone_ids,
        directories=directories(exposure),
        documents={},
        opts=opts,
    )
