"""The etcd snapshots: Sidero's `talos-backup` hourly, and their daily restore-verify.

**What a run does.** `talos-backup` opens the talosconfig Talos wrote into its
pod, streams an etcd snapshot from apid into its working directory, refuses a
stream without etcd's hash trailer, encrypts the snapshot to every recipient
it is handed (the escrowed backup generations, and the restore-verify's own),
uploads it to the backup bucket under `ETCD_SNAPSHOT_PREFIX` and exits; every
step's error exits non-zero (`cmd/talos-backup/main.go` and
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
seals into `k8s-base` (`conventions.sealed.ETCD_WRITER_KEY`). The key writes
under the prefix and does nothing else: it cannot list, read or delete, and an
upload asks for nothing more.

**The scheduler places it** (`kluster.lib.k8s.UNCONSTRAINED`): nothing about
a node holds it (declarative/workloads.md §1). The snapshots go on through an
outage of the home site without a constraint, since the scheduler places no
new pod on a node that is not ready. `talos-backup` writes the snapshot and its
ciphertext relative to its working directory, and the image's is `/`, which
the run's user cannot write, so the working directory is an empty directory of
the pod's own. It is held in memory, since a control plane's disk is etcd's own
(cluster/nodes.md §1.2), and bounded (`WORK_SIZE`), since a run holds two copies
of the database at once. The root filesystem is read-only: the run writes only
there and to the Talos client's own directory. The pod carries CPU and memory
limits, as any pod the scheduler may place on a cloud node does (declarative/workloads.md §1):
its memory is the directory's bound and the processes' share above it
(`MEMORY`), since the directory is charged to the pod, and its CPU a quarter of
a core, which on a control plane leaves the rest of its cores to etcd.

**The restore-verify reads the newest snapshot back daily**
(`EtcdRestoreVerify`, its row `conventions.schedule.ETCD_RESTORE_VERIFY`). A
recovery hands the newest snapshot to Talos' etcd, which restores it and
starts a member on it, so a snapshot passes when the same steps succeed on it:
`shell/etcd-restore-verify.sh` lists the prefix for the newest snapshot inside
the snapshot job's `max_age`, downloads it, decrypts it, restores it with
`etcdutl` and starts a scratch `etcd` on it, on the pod's loopback, which has
to answer healthy and hold `VERIFY_KEY`. It lists and downloads with the
snapshots' read-only key (`conventions.sealed.ETCD_READER_KEY`), which writes
nothing, so nothing the job runs can plant the name a recovery takes for the
newest; it decrypts with an age identity of its own
(`conventions.sealed.ETCD_DRILL_IDENTITY`), whose recipient every snapshot is
encrypted to beside the escrowed backup generations
(`kluster.lib.etcd_backup.drill_recipient`), so the identity in the cluster
opens nothing an escrowed generation does not. It reaches B2 through the
backup bucket's shell client, `shell/b2.sh`, which the metrics stores' scripts
source too, since its image carries no interpreter but busybox; the image is
`etcd-age`, built by this repository (`docker/etcd-age.conf`).

**The scheduler places it too** (`kluster.lib.k8s.UNCONSTRAINED`): nothing
about a node holds it, and on a control plane its scratch etcd draws on the
allocatable the control plane's reservation leaves, as any pod's memory does
(nodes.md §1.1). Its working directory
is in memory, so the plaintext keyspace never reaches a disk, and is bounded
(`VERIFY_WORK_SIZE`); the pod's memory is bounded above that by the processes'
own share (`VERIFY_MEMORY`), and its CPU at half a core, which holds the
scratch etcd's start well inside its bound (`VERIFY_CPU`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.backup import s3_endpoint
from kluster.conventions.backup import max_age
from kluster.conventions.schedule import ETCD_RESTORE_VERIFY, ETCD_SNAPSHOT
from kluster.lib import templates
from kluster.lib.k8s import UNCONSTRAINED, namespace, sealed_value
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CPU',
    'CPU_REQUEST',
    'MEMORY',
    'MEMORY_REQUEST',
    'PROCESS_MEMORY_MIB',
    'SERVICE_ACCOUNT',
    'TALOS_SECRET_DIR',
    'TALOS_SERVICE_ACCOUNT',
    'TALOS_SERVICE_ACCOUNT_API_VERSION',
    'VERIFY_CLIENT_PORT',
    'VERIFY_CPU',
    'VERIFY_CPU_REQUEST',
    'VERIFY_IDENTITY_DIR',
    'VERIFY_KEY',
    'VERIFY_MEMORY',
    'VERIFY_MEMORY_REQUEST',
    'VERIFY_PEER_PORT',
    'VERIFY_PROCESS_MEMORY_MIB',
    'VERIFY_SHELL',
    'VERIFY_SHELL_CONFIGMAP',
    'VERIFY_SHELL_DIR',
    'VERIFY_SHELL_MODE',
    'VERIFY_START_BOUND',
    'VERIFY_WORK_DIR',
    'VERIFY_WORK_SIZE',
    'WORK_SIZE',
    'EtcdRestoreVerify',
    'EtcdSnapshots',
    'cronjob_spec',
    'verify_cronjob_spec',
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
_WORK_SIZE_MIB = 512
WORK_SIZE = f'{_WORK_SIZE_MIB}Mi'

#: The processes' memory beside the working directory, in MiB. The snapshot
#: and the encryption stream from file to file (`pkg/talos`, `pkg/encryption`
#: at v0.1.0). The upload buffers its parts: up to 16 MiB read ahead, then six
#: parts of 8 MiB (`feature/s3/transfermanager` v0.5.2, `UploadObject`), so
#: the processes' memory is bounded whatever the database's size. The
#: container's working set peaked at 112.6 MiB over the seven days to
#: 2026-11-19, and that peak counts the directory's two copies as well, since
#: the directory is charged to the container that writes it; the term holds
#: the buffers and the runtime with room above them.
PROCESS_MEMORY_MIB = 128

#: The pod's memory. The request is the working set's peak with room above
#: it. The limit is the working directory's bound and the processes' term, so
#: a database that outgrows the directory fails on the directory, with an
#: error that names it, rather than on the limit.
MEMORY_REQUEST = '128Mi'
MEMORY = f'{_WORK_SIZE_MIB + PROCESS_MEMORY_MIB}Mi'

#: The pod's CPU. A run is shorter than the metrics' scrape interval, so no
#: rate of it has been read; the most CPU time a scrape saw a run hold is
#: 0.59 s, a lower bound on a run's. What it does is copy a database of tens
#: of MB three times, through TLS, `age` and TLS again. At the limit, a quarter
#: of a core, ten times that CPU time takes under half a minute against the
#: run's deadline of 15 minutes (`conventions.schedule.ETCD_SNAPSHOT`), and on
#: a control plane the run leaves the rest of its cores to etcd.
CPU_REQUEST = '50m'
CPU = '250m'


#: The restore-verify's scripts, by file name, as its ConfigMap carries them:
#: the run and the bucket client it sources.
VERIFY_SHELL: Mapping[str, str] = templates.render_tree('kluster.components.backup', 'shell')
#: The ConfigMap holding them, where its pod mounts it, and the mode the
#: scripts are mounted with: readable and executable.
VERIFY_SHELL_CONFIGMAP = f'{ETCD_RESTORE_VERIFY.cronjob}-shell'
VERIFY_SHELL_DIR = '/opt/kluster/etcd-restore-verify'
VERIFY_SHELL_MODE = 0o555
#: The script the container runs, one of `VERIFY_SHELL`.
_VERIFY_SCRIPT = 'etcd-restore-verify.sh'

#: Where the identity's Secret is mounted, read-only; the run reads the
#: identity from the file of the row's one key under it.
VERIFY_IDENTITY_DIR = '/run/etcd-drill'

#: The run's working directory: the ciphertext, the snapshot, the restored
#: member and the scratch etcd's log, in memory.
VERIFY_WORK_DIR = '/work'

#: The key whose presence makes the restored keyspace a Kubernetes cluster's:
#: the namespace every cluster holds from its first start.
VERIFY_KEY = '/registry/namespaces/kube-system'

#: The scratch etcd's client and peer ports. It listens on the pod's own
#: loopback alone, so etcd's own defaults clash with nothing.
VERIFY_CLIENT_PORT = 2379
VERIFY_PEER_PORT = 2380

#: How long the scratch etcd may take to answer healthy once started. It
#: serves a restored member of one, which elects itself at once; a bound
#: rather than a delay, so a run that passes waits only as long as etcd takes.
VERIFY_START_BOUND = dt.timedelta(minutes=2)

#: The size of the working directory, a tmpfs the kubelet mounts at this size.
#: Each step deletes its input once its output exists, so the directory holds
#: at most the plaintext snapshot and the restored member, and the restored
#: member is its database and two 64 MiB write-ahead-log segments etcd
#: allocates ahead of writing them. So the bound fits a database of about
#: 220 MB, several times the one etcd holds today; a database grown past it
#: fails the run with `no space left on device` (operations.md §7.2).
_VERIFY_WORK_SIZE_MIB = 512
VERIFY_WORK_SIZE = f'{_VERIFY_WORK_SIZE_MIB}Mi'

#: The processes' memory beside the working directory, in MiB: the scratch
#: etcd's own, which is the one that grows with the database. Serving a 41 MB
#: snapshot in the pinned image, measured under podman, the processes held
#: 22 MiB beside the directory's 161 MiB; the term is ten times that, for a
#: database up to the largest the directory fits.
VERIFY_PROCESS_MEMORY_MIB = 256

#: The pod's memory. The tmpfs is charged to the pod, so its peak is the
#: working directory's plus the processes'. The request is the peak at the
#: database etcd holds today, a 30 MB snapshot and about 200 MiB in all, with
#: room above it. On a 41 MB snapshot, larger than today's, the steps after
#: the download peaked at 208 to 257 MiB in the pinned image, measured under
#: podman, the upper end page cache that the kernel reclaims ahead of the
#: limit. The limit is the working directory's bound and the processes'
#: term, so a database that outgrows the bound fails on the directory first,
#: with an error that names it, rather than on the limit.
VERIFY_MEMORY_REQUEST = '256Mi'
VERIFY_MEMORY = f'{_VERIFY_WORK_SIZE_MIB + VERIFY_PROCESS_MEMORY_MIB}Mi'

#: The pod's CPU. The steps after the download, the decrypt, the restore and
#: the scratch etcd's start and read, took 0.3 to 0.6 s of CPU time on a 41 MB
#: snapshot in the pinned image on x86-64, measured under podman with its CPU
#: limited to between 0.1 and 1 core, and 7 s of wall time at 0.1 core. At the
#: limit, half a core, five times that CPU time, for about the largest
#: database the working directory fits, takes 6 s against the two minutes of
#: `VERIFY_START_BOUND`.
VERIFY_CPU_REQUEST = '50m'
VERIFY_CPU = '500m'

#: The user and group it runs as: the base image's unprivileged one. Nothing in
#: the image is written; the run writes only its working directory.
_VERIFY_USER = 65534

#: When it runs: daily at 04:40. The snapshot of 04:05 has ended by then, so
#: the newest snapshot is about half an hour old; the hour is apart from the
#: volume backups' retention classes, which run at 03:00 and 04:00.
_VERIFY_MINUTE = 40
_VERIFY_HOUR = 4


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
        **UNCONSTRAINED.pod_fields(),
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
                resources=k8s.core.v1.ResourceRequirementsArgs(
                    requests={'cpu': CPU_REQUEST, 'memory': MEMORY_REQUEST},
                    limits={'cpu': CPU, 'memory': MEMORY},
                ),
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
    `conventions.sealed.ETCD_WRITER_KEY` (`kluster.lib.k8s.sealed_ciphertext`).
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
        writer_key: Mapping[str, str],
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
        self.cronjob = k8s.batch.v1.CronJob(
            f'{name}-{ETCD_SNAPSHOT.cronjob}',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=ETCD_SNAPSHOT.cronjob, namespace=self.namespace.metadata.name),
            spec=cronjob_spec(image=image, recipients=recipients),
            opts=self.child_opts(depends_on=[self.service_account, self.writer_key]),
        )

        self.register_outputs({})


def verify_cronjob_spec(*, image: ImagePin) -> k8s.batch.v1.CronJobSpecArgs:
    """The restore-verify daily, its row's period, one run at a time, cut off at its row's deadline.

    A failed run is left to the next day's: what it found holds for every
    snapshot after it until it is repaired, and the failed alert has
    reported it.
    """
    return k8s.batch.v1.CronJobSpecArgs(
        schedule=f'{_VERIFY_MINUTE} {_VERIFY_HOUR} * * *',
        concurrency_policy='Forbid',
        starting_deadline_seconds=(
            int(ETCD_RESTORE_VERIFY.starting_deadline.total_seconds())
            if ETCD_RESTORE_VERIFY.starting_deadline
            else None
        ),
        job_template=k8s.batch.v1.JobTemplateSpecArgs(
            spec=k8s.batch.v1.JobSpecArgs(
                backoff_limit=0,
                active_deadline_seconds=int(ETCD_RESTORE_VERIFY.deadline.total_seconds()),
                template=k8s.core.v1.PodTemplateSpecArgs(spec=_verify_pod(image)),
            ),
        ),
    )


def _verify_environment() -> list[k8s.core.v1.EnvVarArgs]:
    """What the script reads, every value from a convention or a constant here, and the reader key's two halves from its Secret."""
    (identity_key,) = conventions.sealed.ETCD_DRILL_IDENTITY.keys
    settings = {
        'PREFIX': conventions.ETCD_SNAPSHOT_PREFIX,
        'CLUSTER': conventions.CLUSTER_NAME,
        # The snapshot job's own `max_age`, which its stale alert holds the
        # last success to: a newest snapshot older than that is stale.
        'MAX_AGE': str(int(max_age(ETCD_SNAPSHOT.period).total_seconds())),
        'KEY': VERIFY_KEY,
        'CLIENT_PORT': str(VERIFY_CLIENT_PORT),
        'PEER_PORT': str(VERIFY_PEER_PORT),
        'START_BOUND': str(int(VERIFY_START_BOUND.total_seconds())),
        'IDENTITY': f'{VERIFY_IDENTITY_DIR}/{identity_key}',
        'WORK': VERIFY_WORK_DIR,
    }
    return [
        *(k8s.core.v1.EnvVarArgs(name=name, value=value) for name, value in settings.items()),
        *(
            k8s.core.v1.EnvVarArgs(
                name=field,
                value_from=k8s.core.v1.EnvVarSourceArgs(
                    secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(
                        name=conventions.sealed.ETCD_READER_KEY.name, key=field
                    )
                ),
            )
            for field in conventions.sealed.ETCD_READER_KEY.keys
        ),
    ]


def _verify_pod(image: ImagePin) -> k8s.core.v1.PodSpecArgs:
    return k8s.core.v1.PodSpecArgs(
        restart_policy='Never',
        # It speaks B2's API and its own loopback, and never Kubernetes'.
        automount_service_account_token=False,
        **UNCONSTRAINED.pod_fields(),
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=_VERIFY_USER,
            run_as_group=_VERIFY_USER,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[
            k8s.core.v1.ContainerArgs(
                name=ETCD_RESTORE_VERIFY.cronjob,
                image=str(image),
                command=['/bin/sh', f'{VERIFY_SHELL_DIR}/{_VERIFY_SCRIPT}'],
                env=_verify_environment(),
                resources=k8s.core.v1.ResourceRequirementsArgs(
                    requests={'cpu': VERIFY_CPU_REQUEST, 'memory': VERIFY_MEMORY_REQUEST},
                    limits={'cpu': VERIFY_CPU, 'memory': VERIFY_MEMORY},
                ),
                security_context=k8s.core.v1.SecurityContextArgs(
                    allow_privilege_escalation=False,
                    read_only_root_filesystem=True,
                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                ),
                volume_mounts=[
                    k8s.core.v1.VolumeMountArgs(name='shell', mount_path=VERIFY_SHELL_DIR, read_only=True),
                    k8s.core.v1.VolumeMountArgs(name='identity', mount_path=VERIFY_IDENTITY_DIR, read_only=True),
                    k8s.core.v1.VolumeMountArgs(name='work', mount_path=VERIFY_WORK_DIR),
                ],
            )
        ],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='shell',
                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(
                    name=VERIFY_SHELL_CONFIGMAP, default_mode=VERIFY_SHELL_MODE
                ),
            ),
            k8s.core.v1.VolumeArgs(
                name='identity',
                secret=k8s.core.v1.SecretVolumeSourceArgs(secret_name=conventions.sealed.ETCD_DRILL_IDENTITY.name),
            ),
            k8s.core.v1.VolumeArgs(
                name='work',
                empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(medium='Memory', size_limit=VERIFY_WORK_SIZE),
            ),
        ],
    )


class EtcdRestoreVerify(Component, pulumi_type='kluster:backup:EtcdRestoreVerify'):
    """The restore-verify's two sealed values, its scripts and its CronJob, in the etcd snapshots' namespace.

    `reader_key` and `identity` are the ciphertexts the stack's configuration
    holds for `conventions.sealed.ETCD_READER_KEY` and
    `conventions.sealed.ETCD_DRILL_IDENTITY`
    (`kluster.lib.k8s.sealed_ciphertext`). `namespace` is the snapshots'
    (`EtcdSnapshots.namespace`), and `image` the pin the CronJob runs.

    `after` is what it is installed behind: Cilium, since nothing schedules
    before the CNI, and the sealing controller, whose chart defines the
    SealedSecrets. It is stated on the children, as `EtcdSnapshots` states
    it.
    """

    def __init__(
        self,
        name: str,
        *,
        namespace: k8s.core.v1.Namespace,
        image: ImagePin,
        reader_key: Mapping[str, str],
        identity: Mapping[str, str],
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        # Behind the sealing controller, whose chart defines the kind, and the
        # namespace each value is sealed to.
        self.reader_key = sealed_value(
            f'{name}-reader-key',
            conventions.sealed.ETCD_READER_KEY,
            ciphertext=reader_key,
            opts=self.child_opts(depends_on=[*after, namespace]),
        )
        self.identity = sealed_value(
            f'{name}-identity',
            conventions.sealed.ETCD_DRILL_IDENTITY,
            ciphertext=identity,
            opts=self.child_opts(depends_on=[*after, namespace]),
        )
        self.shell = k8s.core.v1.ConfigMap(
            f'{name}-shell',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=VERIFY_SHELL_CONFIGMAP, namespace=namespace.metadata.name),
            data=dict(VERIFY_SHELL),
            opts=self.child_opts(depends_on=[namespace]),
        )
        # Behind what its pod mounts and reads.
        self.cronjob = k8s.batch.v1.CronJob(
            f'{name}-{ETCD_RESTORE_VERIFY.cronjob}',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=ETCD_RESTORE_VERIFY.cronjob, namespace=namespace.metadata.name),
            spec=verify_cronjob_spec(image=image),
            opts=self.child_opts(depends_on=[*after, self.shell, self.reader_key, self.identity]),
        )

        self.register_outputs({})
