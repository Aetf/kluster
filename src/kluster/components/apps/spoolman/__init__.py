"""spoolman: the filament spools the printer draws from (rfc-008 §5.5).

Reached by its route row alone, `spool`: from outside behind the portal's
session, and at home through `media`, ungated, which is how the printer, on the
IoT VLAN, reaches it, its socket upgrade included. The Gateway terminates TLS
for the name and speaks plain HTTP to the pod.

**Its data is one SQLite file in the claim**, beside the backups and the log
spoolman writes there, backed up under `STANDARD`. One process holds the
database, so a rollout stops the old pod before it starts the new one. The pod
declares nothing that holds it to a site, so the scheduler places it wherever
it fits (`kluster.lib.k8s.UNCONSTRAINED`); its claim keeps it to the claim's
node once bound, and it writes in the claim's blockio class.

The image is `donkie/spoolman`, pinned as `versions:image-spoolman`. Its image
config names no user: its entrypoint starts as root, moves its `app` user to
`PUID` and `PGID`, and drops to it. Started as `app` itself, `USER`, with both
variables stating that number, it changes nothing and runs the server as that
user, which owns the data in its claim and which the claim's movers run as. Any
other number would have it move `app`, which only root can.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s

from kluster.components.apps import Backend, Exposed, Fronts, declared_constraints, expose
from kluster.components.apps.storage import Claim, backed, claim
from kluster.conventions.backup import STANDARD
from kluster.conventions.routes import Exposure
from kluster.lib.k8s import ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = ('DATA', 'LABELS', 'NAME', 'PORT', 'USER', 'Spoolman')

#: The application's name: its namespace, its Deployment and its Service. The
#: namespace is a backed one of `conventions.backup`, whose writer key is
#: sealed under this name.
NAME = 'spoolman'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the server listens on, which its Service forwards on unchanged.
#: Above 1024, so a user that is not root binds it with no capability.
PORT = 8000

#: The user and group the server runs as: the uid and gid of the image's `app`
#: user, which owns the server's own files in the image.
USER = 1000

#: Where the claim is mounted, which the server keeps its database in. It is
#: the directory the image's user would default to, so a copy of the legacy
#: pod's lands where that pod kept it.
DATA = '/home/app/.local/share/spoolman'

#: The claim of the data, in GiB: the database runs to a few megabytes, and
#: the backups beside it to several copies of it.
_DATA_SIZE = 1


class Spoolman(Component, pulumi_type='kluster:apps:Spoolman'):
    """spoolman's namespace, its claim, its Deployment and Service, and what its exposure derives.

    `exposure` is its row of the census; `image` is the pin it runs,
    `writer_key` the namespace's sealed writer key as the program read it, and
    `fronts` and `zone_ids` what the program read for `expose`. The providers
    arrive in `opts`: the Kubernetes provider for the cluster's objects and the
    Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    data: Claim
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
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace)
        # Read off the namespace, so each object in it waits for it; the claim
        # takes the plain name, and waits for the namespace through opts.
        namespace = self.namespace.namespace.metadata.name

        self.data = claim(
            f'{name}-data',
            'data',
            _DATA_SIZE,
            backed(STANDARD, writer_key),
            namespace=NAME,
            uid=USER,
            constraints=self.constraints,
            opts=self.child_opts(depends_on=[self.namespace.namespace]),
        )

        health = k8s.core.v1.HTTPGetActionArgs(path='/api/v1/health', port='http')
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # SQLite in one file, which a second server beside the first
                # would write under it.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels=dict(LABELS), annotations=dict(self.data.pod_annotations)
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        # It speaks HTTP to its clients alone, and never Kubernetes.
                        automount_service_account_token=False,
                        **self.data.constraints.pod_fields(),
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=USER,
                            run_as_group=USER,
                            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
                        ),
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name=NAME,
                                image=str(image),
                                env=[
                                    k8s.core.v1.EnvVarArgs(name=variable, value=value)
                                    for variable, value in (
                                        ('PUID', str(USER)),
                                        ('PGID', str(USER)),
                                        ('SPOOLMAN_PORT', str(PORT)),
                                        ('SPOOLMAN_DIR_DATA', DATA),
                                    )
                                ],
                                ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
                                readiness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=10),
                                liveness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=60),
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={'cpu': '10m', 'memory': '128Mi'},
                                    limits={'cpu': '500m', 'memory': '256Mi'},
                                ),
                                security_context=k8s.core.v1.SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    read_only_root_filesystem=True,
                                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                                ),
                                volume_mounts=[k8s.core.v1.VolumeMountArgs(name='data', mount_path=DATA)],
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name='data',
                                persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                                    claim_name=self.data.claim.metadata.name
                                ),
                            )
                        ],
                    ),
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
