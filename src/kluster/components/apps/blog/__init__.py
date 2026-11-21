"""The blog: the static site whose content is the blog repository's built branch, serving Matrix's delegation at its apex (rfc-008 §5.3).

The blog is one instance of `kluster.components.apps.static_site.StaticSite`,
and `blog` declares it: every name its route rows serve reads the content's
root, so the apex and `www` of each zone its rows name are one site. A path
the content does not hold is answered with the content's own `/404.html`.

**The content is a built branch, pulled** (declarative/workloads.md §4): the
blog repository's CI pushes the built site to `BRANCH` of `REPOSITORY`
(publishing it is Wave A2's prerequisite, docs/cluster/migration.md), a public
repository the cluster clones over HTTPS with no credential of its own, and
the site's git-sync checks the branch out. The branch is the one hexo's git
deployer pushes to when its configuration names none, which for a repository
named after its owner's GitHub Pages site is `master` (`lib/parse_config.js`
in `hexo-deployer-git`). A push is live
within the sync's period, and a revert on the branch is the rollback. The site
keeps no state, so it claims no volume, and it declares nothing that holds it
to a site: its replicas run wherever the scheduler places them, spread over
the nodes (`kluster.components.apps.static_site.GIT_REPLICAS`).

**The apex serves Matrix's delegation**: the documents at
`/.well-known/matrix/server` and `/.well-known/matrix/client` are what
`kluster.components.apps.matrix.delegation` renders for the homeserver's
client, which the program hands in from the single sign-on census, so the
homeserver's name is stated once, in the census. A Matrix server or client
reads them at the server name, `kluster.components.apps.matrix.SERVER_NAME`,
so an exposure with no apex there is refused: the delegation would be served
where nothing looks for it.

The server and git-sync run as their images' numeric users
(`kluster.components.apps.static_site.SERVER_USER`, `SYNC_USER`), so the
namespace's `restricted` level admits the pod as the site declares it.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.matrix import SERVER_NAME, delegation
from kluster.components.apps.static_site import GitSource, StaticSite
from kluster.conventions.routes import Apex, Exposure, unwrapped
from kluster.lib.versions import ImagePin

__all__ = ('BRANCH', 'NAME', 'REPOSITORY', 'blog')

#: The application's name: its namespace's, its Deployment's and its Service's.
NAME = 'blog'

#: The blog repository, public, which git-sync clones with no credential.
REPOSITORY = 'https://github.com/Aetf/Aetf.github.io.git'

#: The branch git-sync checks out, which the blog repository's CI is to push
#: the built site to (docs/cluster/migration.md, Wave A2's prerequisite):
#: hexo's git deployer's choice where its configuration names none, for a
#: repository named after its owner's GitHub Pages site.
BRANCH = 'master'


def _refuse_exposure(exposure: Exposure) -> None:
    """Refuse an exposure with no apex at the Matrix server name, where the delegation is read."""
    zones = {zone for row in exposure.routes if isinstance(apex := unwrapped(row), Apex) for zone in apex.zones}
    if SERVER_NAME not in zones:
        raise ValueError(
            f"{NAME}: Matrix's delegation is read at the apex of its server name, {SERVER_NAME}, "
            'and the exposure holds no apex row there'
        )


def blog(
    *,
    exposure: Exposure,
    matrix: conventions.sso.Client,
    image: ImagePin,
    sync: ImagePin,
    fronts: Fronts,
    zone_ids: pulumi.Input[Mapping[str, str]],
    opts: pulumi.ResourceOptions | None = None,
) -> StaticSite:
    """Declare the blog: its static site, pulling `BRANCH` of `REPOSITORY`, with Matrix's delegation at its apex.

    The site is named `NAME`. `exposure` is its row
    of the route census, route rows alone, with an apex at the Matrix server
    name; `matrix` is the homeserver's row of the single sign-on census, which
    the delegation names. `image` is the server's pin,
    `versions:image-static-site`, and `sync` git-sync's,
    `versions:image-static-site-git-sync`. `fronts` and `zone_ids` are what
    the program read for `expose`. The providers arrive in `opts`: the
    Kubernetes provider for the cluster's objects and the Cloudflare one for
    its records.
    """
    _refuse_exposure(exposure)
    return StaticSite(
        NAME,
        exposure=exposure,
        source=GitSource(REPOSITORY, BRANCH, sync),
        image=image,
        fronts=fronts,
        zone_ids=zone_ids,
        directories={},
        documents=delegation(matrix),
        opts=opts,
    )
