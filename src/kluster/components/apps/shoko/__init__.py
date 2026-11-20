"""shoko: the anime library's metadata server, over the NAS's `Media` (rfc-008 §5.6).

Shoko Server reads the files under `Media`, identifies them against the anime
database, AniDB, and serves what it learns through its web interface and its
API. It is reached by its route row alone, which the Gateway terminates TLS for,
speaking plain HTTP to the pod. **Its own sign-in is all that guards its
interface**, so a row whose `internet` side asks the portal for no gate is
refused: from outside, only a signed-in user reaches it.

**The library is read-only.** `Media` is mounted through `nas_claim`, read-only,
at the path the legacy pod mounted it at, `/media`, because the import folders
in the server's database name their files by that path. The pod runs on the
worker, the one node that reaches the export, through `WORKER_POD_AFFINITY`.

**The configuration is a claim, backed up under `STANDARD`**: the server's
settings, its SQLite database and its logs, which the land copies from the
legacy claim through the NAS. Under its home, the server keeps them in
`.shoko/Shoko.CLI`, and the claim is mounted at `.shoko`, as the legacy claim
was, so the copy is the legacy claim's contents whole. One process holds the
database, so a rollout stops the old pod before it starts the new one.

**It ports no Secret.** The pinned server reads its settings from
`settings-server.json` alone (`Shoko.Server/Settings/SettingsProvider.cs`), its
AniDB AVDump key among them, so everything it keeps is in the copied
configuration, and its namespace is named after it (declarative/workloads.md
§5).

The image is `shokoanime/server`, pinned as `versions:image-shoko`. Its
entrypoint creates the image's user and switches to it, and both need root, so
the container runs the server's binary itself, as `USER` and `GROUP`: the
image's `PUID` and `PGID`, which own every file the legacy server wrote in its
claim. The claim's movers run as `USER` too, so they read what the server
writes. A user the image's password file does not name has no home of its own,
so the container states `HOME`, which is where the server finds its
configuration.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s

from kluster.components.apps import WORKER_POD_AFFINITY, Backend, Exposed, Fronts, expose
from kluster.components.apps.storage import Claim, NasClaim, claim, nas_claim
from kluster.conventions.backup import STANDARD
from kluster.conventions.routes import Exposure, Proxied, Route
from kluster.lib.k8s import ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CONFIGURATION',
    'GROUP',
    'HOME',
    'LABELS',
    'LIBRARY',
    'LIBRARY_MOUNT',
    'NAME',
    'PORT',
    'USER',
    'Shoko',
)

#: The application's name: its namespace, its Deployment and its Service. The
#: namespace is a backed one of `conventions.backup`, whose writer key is
#: sealed under this name.
NAME = 'shoko'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the server listens on: it binds its settings' `Web.Port`, whose
#: default this is and which the legacy Service forwarded to, and the Service
#: forwards on it unchanged. Above 1024, so a user that is not root binds it
#: with no capability.
PORT = 8111

#: The user and group the server runs as: the image's `PUID` and `PGID`.
USER = 1000
GROUP = 100

#: The server's home, which it reads from the environment; its configuration
#: is `.shoko` under it.
HOME = '/home/shoko'

#: Where the configuration's claim is mounted.
CONFIGURATION = f'{HOME}/.shoko'

#: The path under the export that holds the library, and where it is mounted.
LIBRARY = 'Media'
LIBRARY_MOUNT = '/media'

#: The server's own binary, which the image's entrypoint runs once it has
#: switched to the image's user.
_SERVER = '/usr/src/app/build/Shoko.CLI'

#: The time zone the server writes its logs and schedules in.
_TIME_ZONE = 'America/Los_Angeles'

#: The claim of the configuration, in GiB: it holds about 3.3 GiB.
_CONFIGURATION_SIZE = 8

#: The runtime's temporary directory, on a read-only root. ASP.NET Core's JSON
#: input formatter, which the server's API reads its requests with, buffers a
#: request body past 30 KiB to a file there (`FileBufferingReadStream`), and the
#: runtime writes its diagnostic pipes there.
_TMP = '/tmp'

#: What answers once the web host listens, without a key: the server's state.
_STATUS = '/api/v3/Init/Status'


def _route(exposure: Exposure) -> Route:
    """The exposure's one row, a route, refused where its `internet` side would publish the interface ungated.

    A held row is refused with any other kind: the name is new, so no legacy
    record holds it (rfc-008 §5.6).
    """
    if exposure.ports or len(exposure.routes) != 1:
        raise ValueError(
            f'shoko is reached by one route row alone, and its exposure holds {len(exposure.routes)} route rows '
            f'and {len(exposure.ports)} raw ports'
        )
    (row,) = exposure.routes
    if not isinstance(row, Route):
        raise ValueError(f'shoko is reached by a route row, and its exposure holds a {type(row).__name__}')
    if row.internet is not None and not (isinstance(row.internet, Proxied) and row.internet.gate is not None):
        raise ValueError(
            f'the route {row.host!r} reaches shoko from outside with no gate, and its own sign-in is all that guards '
            'its interface'
        )
    return row


class Shoko(Component, pulumi_type='kluster:apps:Shoko'):
    """shoko's namespace, its claims, its Deployment and Service, and what its exposure derives.

    `exposure` is its row of the census, one route; `image` is the pin it runs;
    `writer_key` the namespace's sealed writer key as the program read it; and
    `fronts` and `zone_ids` what the program read for `expose`. The
    providers arrive in `opts`: the Kubernetes provider for the cluster's
    objects and the Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    configuration: Claim
    library: NasClaim
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        image: ImagePin,
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        _ = _route(exposure)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        # Read off the namespace, so each object in it waits for it; the claims
        # name it plainly, and wait for it through opts.
        namespace = self.namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[self.namespace.namespace])

        self.configuration = claim(
            f'{name}-configuration',
            'configuration',
            _CONFIGURATION_SIZE,
            STANDARD,
            namespace=NAME,
            uid=USER,
            writer_key=writer_key,
            opts=in_namespace,
        )
        self.library = nas_claim(
            f'{name}-library', 'library', LIBRARY, namespace=NAME, read_only=True, opts=in_namespace
        )

        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # The SQLite database is one process's.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(LABELS)),
                    spec=self._pod(image),
                ),
            ),
            opts=self.child_opts(),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='http', port=PORT, target_port='http', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=Backend(service=self.service.metadata.name, port=PORT),
            selector=None,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    def _pod(self, image: ImagePin) -> k8s.core.v1.PodSpecArgs:
        status = k8s.core.v1.HTTPGetActionArgs(path=_STATUS, port='http')
        return k8s.core.v1.PodSpecArgs(
            # It speaks HTTP to its clients and to AniDB alone, and never to
            # Kubernetes.
            automount_service_account_token=False,
            affinity=WORKER_POD_AFFINITY,
            security_context=k8s.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=USER,
                run_as_group=GROUP,
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
            ),
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=NAME,
                    image=str(image),
                    command=[_SERVER],
                    env=[
                        k8s.core.v1.EnvVarArgs(name='HOME', value=HOME),
                        k8s.core.v1.EnvVarArgs(name='TZ', value=_TIME_ZONE),
                    ],
                    ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
                    readiness_probe=k8s.core.v1.ProbeArgs(http_get=status, period_seconds=10),
                    liveness_probe=k8s.core.v1.ProbeArgs(http_get=status, period_seconds=60, timeout_seconds=10),
                    # The memory is rfc-008 §6.1's, from the legacy server's
                    # running size and its 30-day peak; the CPU the legacy pod's.
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '200m', 'memory': '300Mi'},
                        limits={'cpu': '2', 'memory': '1Gi'},
                    ),
                    security_context=k8s.core.v1.SecurityContextArgs(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                    ),
                    volume_mounts=[
                        k8s.core.v1.VolumeMountArgs(name='configuration', mount_path=CONFIGURATION),
                        k8s.core.v1.VolumeMountArgs(name='library', mount_path=LIBRARY_MOUNT, read_only=True),
                        k8s.core.v1.VolumeMountArgs(name='tmp', mount_path=_TMP),
                    ],
                )
            ],
            volumes=[
                k8s.core.v1.VolumeArgs(
                    name='configuration',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.configuration.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='library',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.library.claim.metadata.name, read_only=True
                    ),
                ),
                k8s.core.v1.VolumeArgs(name='tmp', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(size_limit='64Mi')),
            ],
        )
