"""The hourly etcd snapshot: Sidero's `talos-backup` as a CronJob in a namespace of its own, on the control planes.

**What a run does.** `talos-backup` opens the talosconfig Talos wrote into its
pod, streams an etcd snapshot from apid into its working directory, refuses a
stream without etcd's hash trailer, encrypts the snapshot to every escrowed
backup generation, uploads it to the backup bucket under `ETCD_SNAPSHOT_PREFIX`
and exits; every step's error exits non-zero (`cmd/talos-backup/main.go` and
`cmd/talos-backup/service/service.go` at v0.1.0, cluster/storage.md §5). So a run
that succeeded is an object uploaded, and the scheduled-job family's rules on
its row (`conventions.schedule.ETCD_SNAPSHOT`) are what notices the snapshots
stopping. The object is named `<prefix>/<cluster>-<RFC 3339 UTC>.snap.age`,
fixed-width, so name order is time order; the bucket hides it
`ETCD_SNAPSHOT_RETENTION_DAYS` after its upload (`etcd_scope`).

**Its Talos credential is Talos'.** The namespace is the one the control
planes' machine configuration lets ask for a certificate for the Talos API,
for the one role the snapshot reads with (`conventions.ETCD_SNAPSHOT_GRANT`,
declared by `components.talos.control_plane_patch`). A `talos.dev/v1alpha1`
`ServiceAccount` asks for that role, and Talos writes a talosconfig into a
Secret of the same name beside it and renews its certificate by itself; the
endpoint in it is the `talos` Service in `default`, whose endpoints Talos keeps
on the control planes. Nothing here mints, holds or refreshes it, which is why
the register records the grant rather than a row (credentials.md §1, rule 3).
The `ServiceAccount`'s kind is Talos' own, applied by Talos as a bootstrap
manifest once the grant is in the machine configuration, so no chart pin
carries its definition and it is declared untyped.

**Its bucket credential is the `etcd` scope's writer key**, which `physical`
mints with the bucket (`etcd_scope`) and `credentials derived b2-writer seal`
seals into `k8s-base` (`conventions.sealed.ETCD_WRITER_KEY`). The key lists,
reads and writes the prefix and cannot delete; an upload asks for nothing but
writes. Until its ciphertext is in the configuration, nothing is declared but
the namespace and the `ServiceAccount`: no CronJob, and so no series for the
family's rules to read (`kluster.lib.k8s.sealed_value`, physical/bring-up.md
§1).

**It runs on the control planes.** The snapshots then go on through an outage
of the home site, the bucket traffic leaves from the cloud, and the stream does
not cross the mesh to the worker. `talos-backup` writes the snapshot and its
ciphertext relative to its working directory, and the image's is `/`, which
the run's user cannot write, so the working directory is an empty directory of
the pod's own. It is held in memory, since a control plane's disk is etcd's own
(cluster/nodes.md §1.2), and bounded (`WORK_SIZE`), since a run holds two copies
of the database at once. The root filesystem is read-only: the run writes only
there and to the Talos client's own directory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.backup import s3_endpoint
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.schedule import ETCD_SNAPSHOT
from kluster.lib.k8s import namespace, sealed_value
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'SERVICE_ACCOUNT',
    'TALOS_SECRET_DIR',
    'TALOS_SERVICE_ACCOUNT',
    'TALOS_SERVICE_ACCOUNT_API_VERSION',
    'WORK_SIZE',
    'EtcdSnapshots',
    'cronjob_spec',
)

#: The group, version and kind of Talos' `ServiceAccount`, the object a pod
#: asks for a Talos API certificate through.
TALOS_SERVICE_ACCOUNT_API_VERSION = 'talos.dev/v1alpha1'
TALOS_SERVICE_ACCOUNT = 'ServiceAccount'

#: The `ServiceAccount`'s name, and so the name of the Secret Talos writes the
#: talosconfig into.
SERVICE_ACCOUNT = ETCD_SNAPSHOT.cronjob

#: Where `talos-backup` opens the talosconfig: the Talos client's default path
#: inside a pod, `config` under it being the key Talos writes.
TALOS_SECRET_DIR = '/var/run/secrets/talos.dev'

#: The minute of each hour it runs at: off the hour, and apart from the
#: monitoring stack's hourly jobs.
_MINUTE = 5

#: The user and group the image's sample runs it as. The image is `scratch` and
#: owns nothing to any user, so what the run writes is its own directories'.
_USER = 1000

#: Where the run writes the snapshot and its ciphertext, and the directory the
#: Talos client keeps its own state in, both empty directories of the pod.
_WORK_DIR = '/tmp'
_TALOS_HOME = '/.talos'

#: The size of the working directory, a tmpfs the kubelet mounts at this size.
#: It is half the 1 GiB nodes.md §1.1 keeps available on every node, so a run
#: at its bound leaves a node that sits at that floor half of it, for the
#: seconds the run lasts. The directory holds two copies of the database, the
#: snapshot and its ciphertext, so it fits a database of up to half of it,
#: many times the one etcd holds today. A database grown past that fails every
#: run with `no space left on device`, which the failed alert reports
#: (operations.md §7.1).
WORK_SIZE = '512Mi'


def cronjob_spec(*, image: ImagePin, recipients: Sequence[str]) -> k8s.batch.v1.CronJobSpecArgs:
    """The snapshot every hour, its row's period, one run at a time, cut off at its row's deadline.

    `recipients` are the age recipients the snapshot is encrypted to. A
    failed run is left to the next hour's: every snapshot is whole on its
    own, so a missed one costs its hour and nothing after it.
    """
    return k8s.batch.v1.CronJobSpecArgs(
        schedule=f'{_MINUTE} * * * *',
        concurrency_policy='Forbid',
        starting_deadline_seconds=(
            int(ETCD_SNAPSHOT.starting_deadline.total_seconds()) if ETCD_SNAPSHOT.starting_deadline else None
        ),
        job_template=k8s.batch.v1.JobTemplateSpecArgs(
            spec=k8s.batch.v1.JobSpecArgs(
                backoff_limit=0,
                active_deadline_seconds=int(ETCD_SNAPSHOT.deadline.total_seconds()),
                template=k8s.core.v1.PodTemplateSpecArgs(spec=_pod(image, recipients)),
            ),
        ),
    )


def _environment(recipients: Sequence[str]) -> list[k8s.core.v1.EnvVarArgs]:
    """`talos-backup`'s settings, which it reads from its environment, and the writer key's two halves from its Secret."""
    region = conventions.B2_ACCOUNT.region
    settings = {
        'CUSTOM_S3_ENDPOINT': s3_endpoint(region),
        'AWS_REGION': region,
        'BUCKET': conventions.BUCKET_BACKUP,
        # No trailing separator: the object's key is `<prefix>/<file>`.
        'S3_PREFIX': conventions.ETCD_SNAPSHOT_PREFIX,
        'CLUSTER_NAME': conventions.CLUSTER_NAME,
        'AGE_X25519_PUBLIC_KEY': ','.join(recipients),
    }
    return [
        *(k8s.core.v1.EnvVarArgs(name=name, value=value) for name, value in settings.items()),
        *(
            k8s.core.v1.EnvVarArgs(
                name=field,
                value_from=k8s.core.v1.EnvVarSourceArgs(
                    secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(
                        name=conventions.sealed.ETCD_WRITER_KEY.name, key=field
                    )
                ),
            )
            for field in conventions.sealed.ETCD_WRITER_KEY.keys
        ),
    ]


def _pod(image: ImagePin, recipients: Sequence[str]) -> k8s.core.v1.PodSpecArgs:
    return k8s.core.v1.PodSpecArgs(
        restart_policy='Never',
        # It speaks Talos' API and B2's, and never Kubernetes'.
        automount_service_account_token=False,
        affinity=k8s.core.v1.AffinityArgs(
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
        ),
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=_USER,
            run_as_group=_USER,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[
            k8s.core.v1.ContainerArgs(
                name='talos-backup',
                image=str(image),
                command=['/talos-backup'],
                working_dir=_WORK_DIR,
                env=_environment(recipients),
                security_context=k8s.core.v1.SecurityContextArgs(
                    allow_privilege_escalation=False,
                    read_only_root_filesystem=True,
                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                ),
                volume_mounts=[
                    k8s.core.v1.VolumeMountArgs(name='work', mount_path=_WORK_DIR),
                    k8s.core.v1.VolumeMountArgs(name='talos-home', mount_path=_TALOS_HOME),
                    k8s.core.v1.VolumeMountArgs(name='talos-config', mount_path=TALOS_SECRET_DIR, read_only=True),
                ],
            )
        ],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='work', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(medium='Memory', size_limit=WORK_SIZE)
            ),
            k8s.core.v1.VolumeArgs(name='talos-home', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs()),
            k8s.core.v1.VolumeArgs(
                name='talos-config', secret=k8s.core.v1.SecretVolumeSourceArgs(secret_name=SERVICE_ACCOUNT)
            ),
        ],
    )


class EtcdSnapshots(Component, pulumi_type='kluster:backup:EtcdSnapshots'):
    """The snapshot's namespace, its Talos `ServiceAccount`, its writer key and its CronJob.

    `writer_key` is the ciphertext the stack's configuration holds for
    `conventions.sealed.ETCD_WRITER_KEY`, `None` while it holds none; until it
    holds one, neither the key nor the CronJob is declared. The `None` is the
    bring-up's tolerance, and goes with it (physical/bring-up.md §1).
    `recipients` are the age recipients each snapshot is encrypted to, and
    `image` the pin the CronJob runs.

    `after` is what it is installed behind: Cilium, since nothing schedules
    before the CNI, and the sealing controller, whose chart defines the
    writer key's SealedSecret. It is stated on the children rather than left
    to the component's own `depends_on`, which Pulumi does not push down to a
    component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        image: ImagePin,
        writer_key: Mapping[str, str] | None,
        recipients: Sequence[str],
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        if not recipients:
            raise ValueError('an etcd snapshot is encrypted to at least one recipient, and none was given')
        super().__init__(name, opts=opts)

        self.namespace = namespace(
            f'{name}-namespace',
            name=ETCD_SNAPSHOT.namespace,
            opts=self.child_opts(depends_on=list(after)),
        )
        self.service_account = k8s.apiextensions.CustomResource(
            f'{name}-talos-service-account',
            api_version=TALOS_SERVICE_ACCOUNT_API_VERSION,
            kind=TALOS_SERVICE_ACCOUNT,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=SERVICE_ACCOUNT, namespace=self.namespace.metadata.name),
            spec={'roles': [conventions.ETCD_SNAPSHOT_GRANT.role]},
            opts=self.child_opts(depends_on=[self.namespace]),
        )
        # Behind the sealing controller, whose chart defines the kind, and the
        # namespace the key is sealed to.
        self.writer_key = sealed_value(
            f'{name}-writer-key',
            conventions.sealed.ETCD_WRITER_KEY,
            ciphertext=writer_key,
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )
        # Behind what its pod mounts and reads: the talosconfig's Secret, which
        # Talos writes for the `ServiceAccount`, and the key's.
        self.cronjob = (
            k8s.batch.v1.CronJob(
                f'{name}-{ETCD_SNAPSHOT.cronjob}',
                metadata=k8s.meta.v1.ObjectMetaArgs(name=ETCD_SNAPSHOT.cronjob, namespace=self.namespace.metadata.name),
                spec=cronjob_spec(image=image, recipients=recipients),
                opts=self.child_opts(depends_on=[self.service_account, self.writer_key]),
            )
            if self.writer_key is not None
            else None
        )

        self.register_outputs({})
