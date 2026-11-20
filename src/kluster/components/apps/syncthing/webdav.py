"""The WebDAV server beside syncthing: its own files, and syncthing's `Stuff` folder under them (rfc-008 §5.3).

A WebDAV server over two trees in one: its own files, which are in no
syncthing folder and live in a claim backed up under `STANDARD`, and, at
`STUFF_MOUNT` under them, the `Stuff` folder syncthing keeps on the NAS, which
the server writes to as a peer of the instance would. The folder is the
export's `Sync/Stuff`, so the server runs on the worker beside the instance
(`nas_claim`), which its pod's affinity states and its claim's mover follows
(`claim`), and what it writes there the instance finds at its next scan.

**The gate is all that guards it.** The server allows every operation to every
client and checks no credential: its route's `internet` side asks the portal
for HTTP Basic before a request reaches it, and nothing else reaches the pod,
since the namespace's default-deny admits what the Gateways forward and the
namespace's own pods. It serves plain HTTP to the Gateway, which terminates
TLS for the name.

The image is `sigoden/dufs`, pinned as `versions:image-webdav`. Its image
config names no user, so the pod states a numeric one, `USER`, which owns what
the server writes in its claim and which the claim's movers run as; what it
writes on the NAS is the export's anonymous user's (`nas_claim`).
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s

from kluster.components.apps import Backend, Exposed, Fronts, expose
from kluster.components.apps.storage import Claim, NasClaim, claim, nas_claim
from kluster.components.apps.syncthing import SYNC
from kluster.conventions.backup import STANDARD
from kluster.conventions.routes import Exposure
from kluster.lib.k8s import WORKER_POD_AFFINITY, ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = ('FILES', 'LABELS', 'NAME', 'PORT', 'STUFF_MOUNT', 'USER', 'WebDav')

#: The application's name: its namespace, its Deployment and its Service. The
#: namespace is a backed one of `conventions.backup`, whose writer key is
#: sealed under this name.
NAME = 'webdav'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the server listens on, which its Service forwards on unchanged.
#: Above 1024, so a user that is not root binds it with no capability.
PORT = 5000

#: The user and group the server runs as, which owns the server's files in its
#: claim. The server reads and writes nothing of its image's, so any user that
#: is not root serves.
USER = 1000

#: The root the server serves, where its claim is mounted.
FILES = '/files'

#: Where syncthing's `Stuff` folder appears under the root.
STUFF_MOUNT = f'{FILES}/stuff'

#: The claim of the server's own files, in GiB: they run to about 1.1 GiB.
_FILES_SIZE = 5


class WebDav(Component, pulumi_type='kluster:apps:WebDav'):
    """The WebDAV server's namespace, its claims, its Deployment and Service, and what its exposure derives.

    `exposure` is its row of the census, `image` the pin it runs,
    `writer_key` the namespace's sealed writer key as the program read it,
    and `fronts` and `zone_ids` what the program read for `expose`. The
    providers arrive in `opts`: the Kubernetes provider for the cluster's
    objects and the Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    files: Claim
    stuff: NasClaim
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
        # Read off the namespace, so each object in it waits for it; the
        # claims take the plain name, and wait for the namespace through opts.
        namespace = self.namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[self.namespace.namespace])

        self.files = claim(
            f'{name}-files',
            'files',
            _FILES_SIZE,
            STANDARD,
            namespace=NAME,
            uid=USER,
            writer_key=writer_key,
            placement=WORKER_POD_AFFINITY,
            opts=in_namespace,
        )
        self.stuff = nas_claim(
            f'{name}-stuff', 'stuff', f'{SYNC}/Stuff', namespace=NAME, read_only=False, opts=in_namespace
        )

        # Probed at dufs's own health path, which answers before any file is read.
        health = k8s.core.v1.HTTPGetActionArgs(path='/__dufs__/health', port='http')
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(LABELS)),
                    spec=k8s.core.v1.PodSpecArgs(
                        # It speaks HTTP to its clients alone, and never Kubernetes.
                        automount_service_account_token=False,
                        affinity=self.files.placement,
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
                                args=[f'--port={PORT}', '--allow-all', FILES],
                                ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
                                readiness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=10),
                                liveness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=60),
                                # Serving a file fills the page cache, which
                                # the memory limit counts.
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={'cpu': '10m', 'memory': '32Mi'},
                                    limits={'cpu': '500m', 'memory': '128Mi'},
                                ),
                                security_context=k8s.core.v1.SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                                ),
                                volume_mounts=[
                                    k8s.core.v1.VolumeMountArgs(name='files', mount_path=FILES),
                                    k8s.core.v1.VolumeMountArgs(name='stuff', mount_path=STUFF_MOUNT),
                                ],
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name='files',
                                persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                                    claim_name=self.files.claim.metadata.name
                                ),
                            ),
                            k8s.core.v1.VolumeArgs(
                                name='stuff',
                                persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                                    claim_name=self.stuff.claim.metadata.name
                                ),
                            ),
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
