"""syncthing's discovery server, stdiscosrv (rfc-008 §5.3).

A device announces its addresses to the server and looks up the addresses of
the devices it shares folders with, over HTTPS. The server reads each device's
ID from the TLS client certificate the device presents, so it terminates TLS
itself: no proxy may stand between it and the device.

**It is reached at a raw port** ([Aetf/kluster-ops#723](https://github.com/Aetf/kluster-ops/issues/723)):
its exposure is one `Port` on TCP, which `expose` serves at the fronts the row
sets with the allow that admits the world to it. A route row is refused here,
since a Gateway would end the TLS that carries a device's ID, and a
`Passthrough` is no row the cluster can carry
(`kluster.lib.k8s.refuse_passthrough`). The server listens on the row's port,
which its Services forward to unchanged, so a device dials the name and the
port the row states, `https://<name>:<port>/`, which is what syncthing's
component configures its instance to announce to.

**Its certificate is the cluster issuer's, for each name the row is dialed at**
(`kluster.components.apps.dialed_names`, whose first is the name syncthing's
component announces to): the names its balancer front publishes, held or not,
and the name its home side is rewritten for, each under the primary zone. A
balancer front that publishes none is dialed at its home side's name, which
another row publishes: the discovery server's front is reached at `syncapi`,
the peer port's name. A device has no ID of the server's to pin, so it checks
the certificate against the public roots, as it would any HTTPS server's. The
server reads the certificate at its start alone, so Reloader rolls the pod when
a renewal rewrites the Secret.

**It runs on the cloud pool**, so devices away from home find each other with
the homelab down. Behind a raw port's `Cluster` policy it sees the client's
address only when the balancer hands the connection to the node it runs on,
the forwarding node's address otherwise, and at the LAN VIP the worker's. So no
address it saw can be relied on, and a device announces names of its own
instead, which is syncthing's side.

**It keeps nothing worth keeping**: its database is an `emptyDir`, which the
devices' announcements rebuild within minutes (rfc-008 §4.2).

The image is `syncthing/discosrv`, pinned as `versions:image-stdiscosrv`. Its
image config names no user, and its entrypoint drops to `PUID` only when it
starts as root, so the pod states that user's number, as the namespace's
`restricted` level requires, and the entrypoint then runs the server as it.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster.components.apps import Exposed, Fronts, dialed_names, expose
from kluster.components.certificates import ISSUER
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Exposure, Port
from kluster.lib.k8s import ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = ('DATABASE', 'LABELS', 'NAME', 'TLS_MOUNT', 'TLS_SECRET', 'USER', 'Stdiscosrv')

#: The application's name: its namespace, its Deployment and its certificate.
NAME = 'stdiscosrv'

#: The labels its pods carry, which its raw port's Services and allow select.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The user and group the server runs as: the image's `PUID` and `PGID`.
USER = 1000

#: The Secret the certificate is issued into, and where the pod mounts it.
TLS_SECRET = f'{NAME}-tls'
TLS_MOUNT = '/tls'

#: The server's database directory, the image's working directory, where an
#: `emptyDir` is mounted: the root filesystem is read-only.
DATABASE = '/var/stdiscosrv'


def _discovery(exposure: Exposure) -> Port:
    """The exposure's one raw port, refused unless the server can serve it."""
    if exposure.routes:
        raise ValueError(
            "the discovery server reads each device's ID from the device's TLS client certificate, so it is reached "
            'at a raw port and at no route, where a Gateway would end that TLS (Aetf/kluster-ops#723)'
        )
    if len(exposure.ports) != 1:
        raise ValueError(f'the discovery server listens on one port, and its exposure holds {len(exposure.ports)}')
    (row,) = exposure.ports
    if row.transports is not Transports.TCP:
        raise ValueError(
            f'the discovery server speaks HTTPS over TCP alone, and its port {row.name!r} is on {row.transports.value}'
        )
    if not dialed_names(row):
        raise ValueError(
            f'the discovery port {row.name!r} names no host a device dials it at, and its certificate is for those names'
        )
    return row


#: What places the pod on the cloud pool: a control plane node, which every
#: cloud node is and the worker is not (nodes.md §4.2). The control planes take
#: workloads, so no toleration goes with it.
_CLOUD_POOL = k8s.core.v1.AffinityArgs(
    node_affinity=k8s.core.v1.NodeAffinityArgs(
        required_during_scheduling_ignored_during_execution=k8s.core.v1.NodeSelectorArgs(
            node_selector_terms=[
                k8s.core.v1.NodeSelectorTermArgs(
                    match_expressions=[
                        k8s.core.v1.NodeSelectorRequirementArgs(key=CONTROL_PLANE_LABEL, operator='Exists')
                    ]
                )
            ]
        )
    )
)


def _container(image: ImagePin, row: Port) -> k8s.core.v1.ContainerArgs:
    # `/ping` answers 204 once the server serves. The kubelet does not verify
    # the certificate of an HTTPS probe.
    probe = k8s.core.v1.ProbeArgs(
        http_get=k8s.core.v1.HTTPGetActionArgs(path='/ping', port='https', scheme='HTTPS'), period_seconds=30
    )
    return k8s.core.v1.ContainerArgs(
        name=NAME,
        image=str(image),
        args=[
            f'--listen=:{row.port}',
            f'--cert={TLS_MOUNT}/tls.crt',
            f'--key={TLS_MOUNT}/tls.key',
            f'--db-dir={DATABASE}',
        ],
        ports=[k8s.core.v1.ContainerPortArgs(name='https', container_port=row.port, protocol='TCP')],
        readiness_probe=probe,
        liveness_probe=probe,
        # The legacy server's 30-day peak is 0.003 core and 0.03 GiB of working
        # set (nodes.md §1.1); the cloud pool asks a limit of each.
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '10m', 'memory': '32Mi'},
            limits={'cpu': '20m', 'memory': '64Mi'},
        ),
        security_context=k8s.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
        ),
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name='tls', mount_path=TLS_MOUNT, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='database', mount_path=DATABASE),
        ],
    )


class Stdiscosrv(Component, pulumi_type='kluster:apps:Stdiscosrv'):
    """The discovery server's namespace, its certificate, its Deployment, and what its exposure derives.

    `exposure` is its row of the census, one raw port on TCP dialed at a
    name; `image` is the pin it runs, and `fronts` and `zone_ids` what the
    program read for `expose`. The providers arrive in `opts`: the Kubernetes
    provider for the cluster's objects and the Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    certificate: crds.cert_manager.v1.Certificate
    deployment: k8s.apps.v1.Deployment
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        image: ImagePin,
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        row = _discovery(exposure)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        # Read off the namespace, so each object in it waits for it.
        namespace = self.namespace.namespace.metadata.name

        self.certificate = crds.cert_manager.v1.Certificate(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=crds.cert_manager.v1.CertificateSpecArgs(
                dns_names=list(dialed_names(row)),
                secret_name=TLS_SECRET,
                issuer_ref=crds.cert_manager.v1.CertificateSpecIssuerRefArgs(
                    name=ISSUER, kind='ClusterIssuer', group='cert-manager.io'
                ),
            ),
            opts=self.child_opts(),
        )
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=NAME,
                namespace=namespace,
                # Reloader rolls the pod when the certificate's Secret changes:
                # the server reads it at its start alone.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(LABELS)),
                    spec=k8s.core.v1.PodSpecArgs(
                        # It speaks HTTPS to devices alone, and never Kubernetes.
                        automount_service_account_token=False,
                        affinity=_CLOUD_POOL,
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=USER,
                            run_as_group=USER,
                            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
                        ),
                        containers=[_container(image, row)],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name='tls', secret=k8s.core.v1.SecretVolumeSourceArgs(secret_name=TLS_SECRET)
                            ),
                            # The database runs to a few megabytes.
                            k8s.core.v1.VolumeArgs(
                                name='database', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(size_limit='64Mi')
                            ),
                        ],
                    ),
                ),
            ),
            opts=self.child_opts(),
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=None,
            selector=LABELS,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})
