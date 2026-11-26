"""The monitoring stack's two VictoriaMetrics stores, what each keeps, and how each is backed up and restored.

**Two stores** (declarative/cluster-infra.md §1 item 6). The chart's vmsingle
is the *recent* store: every series vmagent scrapes, kept `RECENT_RETENTION`.
The *history* store is a second vmsingle the component declares, kept
`HISTORY_RETENTION`, which vmagent writes only the series of `HISTORY_CLASS`
to, through a remote write of its own (`history_remote_write`).
VictoriaMetrics' open-source release keeps one retention per instance and
downsamples nothing, so a subset kept for good needs an instance of its own.

**Each store is copied hourly to the backup bucket by `vmbackup`**, from an
instant snapshot of hard links to the store's immutable parts, under
`conventions.backup.metrics_repo_path`: one CronJob per store, the two at
different minutes, mounting the store's claim read-only, so the job's pod
lands on the store's node with it. A copy taken file by file while the store
merges and deletes its parts is not consistent, which is why it is not
VolSync's. The snapshot is taken and deleted around `vmbackup` by steps of
the job's own (`_snapshotted`), because `vmbackup` logs the address it takes
one at, the key in it. Each job is a row of the scheduled-job family
(`conventions.schedule.METRICS_BACKUPS`, `Store.backup`), which gives its name,
its period and the deadline a run is cut off at. The history store's job also refuses a store holding fewer
rows than its last backup recorded beside the backup (`Store.record`):
`shell/backup-check.sh` ahead of `vmbackup`, and `shell/backup-record.sh`
after it.

**Each store restores itself before it starts**: two init containers on its
vmsingle, `shell/restore-check.sh` and then `shell/restore.sh` around
`vmrestore`, restore the backup into an empty data directory, start an empty
store only where the bucket holds no backup, and fail where the backup cannot
be read, so a store never starts empty over a history that exists. vmagent's
persistent queue holds what is scraped meanwhile.

**The bucket is reached through B2's native API** (`b2.sh`, the backup
bucket's shell client, which `components.backup` keeps beside its own
scripts), for the two things `vmbackup` and `vmrestore` do not do: whether a prefix holds a backup at
all, and the history's row count kept beside its backup. Those calls run in
curl's image, whose `curl` reads the request headers carrying the writer key
and B2's tokens from files in the pod's memory, so no credential is ever a
process's argument, which every process on the node that sees the PID can
read; `vmbackup` and `vmrestore` read the writer key from their environment,
and the snapshot steps read the store's snapshot key from its Secret, mounted
as a file. The scripts are shell because the images
carry no other interpreter: busybox, and the one binary each. They are one
ConfigMap, the scripts of `shell/` and `b2.sh` beside them, which the jobs and
the init containers mount.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.monitoring import memory
from kluster.conventions.backup import metrics_repo_path
from kluster.conventions.providers import OCI_BLOCKSTORE_METRICS
from kluster.conventions.schedule import METRICS_BACKUPS, ScheduledJob
from kluster.lib import templates
from kluster.lib.monitoring.jobs import ETCD_JOB
from kluster.lib.monitoring.vmsingle import VMSINGLE_PORT, vmsingle_workload

__all__ = (
    'DATA_DIR',
    'DATA_VOLUME',
    'HISTORY_CLASS',
    'HISTORY_RETENTION',
    'RECENT_RETENTION',
    'SHELL',
    'SHELL_CONFIGMAP',
    'SHELL_DIR',
    'SHELL_MODE',
    'SHELL_VOLUME',
    'SNAPSHOT_KEY_DIR',
    'SNAPSHOT_KEY_VOLUME',
    'VMAGENT_PORT',
    'WORK_DIR',
    'WORK_VOLUME',
    'Bucket',
    'Images',
    'Kept',
    'Store',
    'backup_pod',
    'history_relabeling',
    'history_remote_write',
    'restore_containers',
    'restore_volumes',
    'shell_volume',
    'stack_fullname',
    'stores',
    'vmagent_service',
    'vmagent_url',
    'work_volume',
)

#: How long the recent store keeps a sample: a month of full detail, for
#: investigating what a reading of the history flagged (Aetf/kluster-ops#790,
#: the operator's ruling of 2026-11-17).
RECENT_RETENTION = dt.timedelta(days=31)

#: How long the history store keeps a sample, in VictoriaMetrics' own syntax:
#: for good, so that every period compares with every other.
HISTORY_RETENTION = '100y'

#: Where the operator mounts a vmsingle's data volume, and that volume's name
#: in its pod (`internal/controller/operator/factory/vmsingle/vmsingle.go` L37-38
#: and `factory/build/container.go` L21 at operator v0.74.0), which the init
#: container mounts too and each backup job mounts its claim at.
DATA_DIR = '/victoria-metrics-data'
DATA_VOLUME = 'data'

#: The ConfigMap holding the scripts, the volume it is mounted as, and where.
SHELL_CONFIGMAP = 'metrics-backup-shell'
SHELL_VOLUME = 'backup-shell'
SHELL_DIR = '/opt/kluster/metrics-backup'
#: The mode the scripts are mounted with: readable and executable.
SHELL_MODE = 0o555

#: The volume one container of a pod leaves what it read to the next in, and
#: where it is mounted.
WORK_VOLUME = 'work'
WORK_DIR = '/work'

#: The volume a backup pod mounts the store's snapshot key from, the one key
#: of its Secret, and where. A Secret's volume is held in memory.
SNAPSHOT_KEY_VOLUME = 'snapshot-key'
SNAPSHOT_KEY_DIR = '/opt/kluster/snapshot-key'

#: The scripts, by file name, as the ConfigMap carries them: this package's
#: own, and the bucket client they source.
SHELL: Mapping[str, str] = {
    **templates.render_tree('kluster.components.monitoring', 'shell'),
    'b2.sh': templates.load('kluster.components.backup', 'shell/b2.sh'),
}

#: Where each image keeps its one binary.
_VMBACKUP = '/vmbackup-prod'
_VMRESTORE = '/vmrestore-prod'


#: The port vmagent serves its HTTP API at, the operator's default for a
#: VMAgent that sets none (`AsURL`, `api/operator/v1beta1/vmagent_types.go`
#: L615-619 at operator v0.74.0).
VMAGENT_PORT = 8429


def vmagent_service(release: str, chart: str) -> str:
    """The name of the stack's vmagent Service.

    The stack names its VMAgent object `vm.cr.fullname`, as it names the recent
    store's vmsingle (`stack_fullname`; `templates/victoria-metrics-operator/vmagent/vmagent.yaml`
    at stack chart 0.91.2), and the operator names its Service `vmagent-<object>`
    (`PrefixedName`, `vmagent_types.go` L567-572 at operator v0.74.0).
    """
    return f'vmagent-{stack_fullname(release, chart)}'


def vmagent_url(release: str, chart: str) -> str:
    """The stack's vmagent in the cluster, which a series written to it reaches both stores through."""
    return f'http://{vmagent_service(release, chart)}.{conventions.MONITORING_NAMESPACE}.svc:{VMAGENT_PORT}'


def stack_fullname(release: str, chart: str) -> str:
    """The name the stack chart gives its objects at this release: `vm.fullname`, with no override set.

    victoria-metrics-common's `_helpers.tpl` L51-73 at stack chart 0.91.2: the
    release's name where it already holds the chart's, the two joined
    otherwise, cut to 63 characters with a trailing hyphen dropped. The
    recent store's vmsingle carries it.
    """
    fullname = release if chart in release else f'{release}-{chart}'
    return fullname[:63].removesuffix('-')


@dataclass(frozen=True)
class Store:
    """One VictoriaMetrics store: its object, its place in the bucket, and its backup's minute."""

    name: str
    """Its name in the bucket, and its backup's in `conventions.schedule.METRICS_BACKUPS`."""
    vmsingle: str
    """The name of its vmsingle object."""
    minute: int
    """The minute of each hour its backup runs at."""
    keeps_every_row: bool
    """Whether its retention keeps every row it is written, so that its backup
    may refuse to shrink."""

    @property
    def workload(self) -> str:
        """The name of the store's Deployment, Service and claim: the operator's `PrefixedName`."""
        return vmsingle_workload(self.vmsingle)

    @property
    def url(self) -> str:
        """The store's address in the cluster, on the port the operator serves it at."""
        return f'http://{self.workload}.{conventions.MONITORING_NAMESPACE}.svc:{VMSINGLE_PORT}'

    @property
    def repository(self) -> str:
        """Where its backup is in the bucket."""
        return metrics_repo_path(conventions.MONITORING_NAMESPACE, self.name)

    @property
    def record(self) -> str | None:
        """Beside the backup, outside the directory `vmbackup` keeps, the row count its last backup recorded.

        `None` for a store whose rows age out, whose count falls with every
        retention cut.
        """
        return f'{self.repository}.rows' if self.keeps_every_row else None

    @property
    def backup(self) -> ScheduledJob:
        """Its backup's row of the scheduled-job family."""
        return METRICS_BACKUPS[self.name]

    @property
    def schedule(self) -> str:
        """The backup's cron line: every hour, its row's period, at `minute`."""
        return f'{self.minute} * * * *'


def stores(release: str, chart: str) -> tuple[Store, Store]:
    """The recent store, the chart's vmsingle at this release, and the history store, backed up half an hour apart."""
    return (
        Store('recent', stack_fullname(release, chart), minute=15, keeps_every_row=False),
        Store('history', conventions.HISTORY_STORE, minute=45, keeps_every_row=True),
    )


@dataclass(frozen=True)
class Kept:
    """Series the history store keeps: names matching `names`, from the scrape job matching `job`."""

    names: str
    job: str = '.*'


#: The class of series the history store keeps: what a node, etcd or a
#: container consumes -- CPU, memory, disk, network, pressure -- and how long an
#: operation of etcd or of the API server takes, beside every series made for
#: comparison: the recording rules under `kluster:`, and OCI's block-volume
#: series -- and what a reading attributes them by: a pod's node, its requests
#: and limits, and its phase. A reading is a query over these, so a reading
#: defined later is computed over every period on record.
HISTORY_CLASS: tuple[Kept, ...] = (
    Kept(
        'node_cpu_seconds_total|node_pressure_.*|node_memory_(MemAvailable|MemTotal|Dirty|Writeback)_bytes'
        '|node_disk_.*|node_filesystem_(avail|size)_bytes'
        '|node_network_(receive|transmit)_(bytes|packets|drop|errs)_total|node_load.*'
        '|node_vmstat_(pgmajfault|oom_kill)'
    ),
    Kept('etcd_.*|process_cpu_seconds_total|process_resident_memory_bytes', job=ETCD_JOB),
    Kept(
        'container_cpu_usage_seconds_total|container_cpu_cfs_(periods|throttled_periods)_total'
        '|container_memory_working_set_bytes|container_pressure_.*|container_fs_(reads|writes)_bytes_total'
        '|container_blkio_device_usage_total|container_network_(receive|transmit)_bytes_total'
    ),
    Kept(
        'kube_pod_container_resource_(requests|limits)|kube_pod_status_phase|kube_node_status_allocatable|kube_pod_info'
    ),
    Kept('apiserver_request_total|apiserver_current_inflight_requests'),
    Kept(f'kluster:.*|{OCI_BLOCKSTORE_METRICS}_.*'),
)

#: Families of the class's patterns it keeps none of: etcd's debugging series,
#: which etcd itself names unstable.
_HISTORY_EXCLUDED = 'etcd_debugging_.*'


def history_relabeling() -> list[dict[str, Any]]:
    """The remote write's relabeling to the history store: the class kept, everything else dropped."""
    return [
        {
            'action': 'keep',
            'sourceLabels': ['job', '__name__'],
            'regex': '|'.join(f'({kept.job});({kept.names})' for kept in HISTORY_CLASS),
        },
        {'action': 'drop', 'sourceLabels': ['__name__'], 'regex': _HISTORY_EXCLUDED},
    ]


def history_remote_write(history: Store) -> dict[str, Any]:
    """vmagent's remote write to the history store, which vmalert's recording rules reach through vmagent too."""
    return {'url': f'{history.url}/api/v1/write', 'inlineUrlRelabelConfig': history_relabeling()}


def shell_volume() -> dict[str, Any]:
    """The scripts' ConfigMap as a pod's volume, each script executable."""
    return {'name': SHELL_VOLUME, 'configMap': {'name': SHELL_CONFIGMAP, 'defaultMode': SHELL_MODE}}


#: The work volume's source: memory, so the bucket's header files never reach
#: a disk, and a few files' worth of it.
_WORK_MEDIUM = 'Memory'
_WORK_SIZE = '1Mi'


def work_volume() -> dict[str, Any]:
    """Where one container of a pod leaves what the next reads, and the bucket's header files are written: memory."""
    return {'name': WORK_VOLUME, 'emptyDir': {'medium': _WORK_MEDIUM, 'sizeLimit': _WORK_SIZE}}


@dataclass(frozen=True)
class Images:
    """The images a store's backups and restores run: `vmbackup`'s, `vmrestore`'s, and the bucket's client's."""

    backup: str
    restore: str
    bucket: str
    """curl's, which reads the request headers carrying the bucket's
    credentials from a file rather than from its arguments."""


@dataclass(frozen=True)
class Bucket:
    """Where the stores are backed up, and the Secret the writer key opens into."""

    writer_secret: str
    endpoint: str
    """B2's S3-compatible endpoint for the account's region."""
    region: str


def _writer_env(bucket: Bucket) -> list[dict[str, Any]]:
    """The writer key's two halves, under the names `vmbackup`, `vmrestore` and `b2.sh` read them by."""
    return [
        {'name': field, 'valueFrom': {'secretKeyRef': {'name': bucket.writer_secret, 'key': field}}}
        for field in sorted(conventions.WRITER_KEY_FIELDS)
    ]


def _bucket_url(store: Store) -> str:
    return f's3://{conventions.BUCKET_BACKUP}/{store.repository}'


def _mounts(*, data: bool, data_read_only: bool = True, snapshot_key: bool = False) -> list[dict[str, Any]]:
    """The scripts and the work directory, and the store's data and its snapshot key where the container reads them."""
    return [
        *([{'name': DATA_VOLUME, 'mountPath': DATA_DIR, 'readOnly': data_read_only}] if data else []),
        {'name': SHELL_VOLUME, 'mountPath': SHELL_DIR, 'readOnly': True},
        {'name': WORK_VOLUME, 'mountPath': WORK_DIR},
        *([{'name': SNAPSHOT_KEY_VOLUME, 'mountPath': SNAPSHOT_KEY_DIR, 'readOnly': True}] if snapshot_key else []),
    ]


def restore_containers(store: Store, *, images: Images, bucket: Bucket) -> list[dict[str, Any]]:
    """The two init containers that decide whether `store` is restored before it starts, and restore it.

    The first, in the bucket client's image, reads the data directory and the
    bucket and leaves its decision in the work directory; the second, in
    `vmrestore`'s, restores where it was decided to. Until the Secret the
    writer key opens into exists, the kubelet starts neither, so the store
    waits for its key rather than starting empty.
    """
    return [
        {
            'name': 'restore-check',
            'image': images.bucket,
            'command': ['/bin/sh', f'{SHELL_DIR}/restore-check.sh'],
            'env': [
                {'name': 'DATA', 'value': DATA_DIR},
                {'name': 'PREFIX', 'value': f'{store.repository}/'},
                {'name': 'WORK', 'value': WORK_DIR},
                *_writer_env(bucket),
            ],
            'volumeMounts': _mounts(data=True),
            'resources': memory.BUCKET_STEP.rendered(),
        },
        {
            'name': 'restore',
            'image': images.restore,
            'command': [
                '/bin/sh',
                f'{SHELL_DIR}/restore.sh',
                _VMRESTORE,
                f'-src={_bucket_url(store)}',
                f'-storageDataPath={DATA_DIR}',
                f'-customS3Endpoint={bucket.endpoint}',
            ],
            'env': [
                {'name': 'WORK', 'value': WORK_DIR},
                {'name': 'AWS_REGION', 'value': bucket.region},
                *_writer_env(bucket),
            ],
            'volumeMounts': _mounts(data=True, data_read_only=False),
            'resources': memory.VMRESTORE.rendered(),
        },
    ]


def restore_volumes() -> list[dict[str, Any]]:
    """What a store's restore mounts beside the store's own data volume."""
    return [shell_volume(), work_volume()]


def _typed_env(env: list[dict[str, Any]]) -> list[k8s.core.v1.EnvVarArgs]:
    return [
        k8s.core.v1.EnvVarArgs(name=it['name'], value=it['value'])
        if 'value' in it
        else k8s.core.v1.EnvVarArgs(
            name=it['name'],
            value_from=k8s.core.v1.EnvVarSourceArgs(
                secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(**it['valueFrom']['secretKeyRef'])
            ),
        )
        for it in env
    ]


def _typed_container(container: Mapping[str, Any]) -> k8s.core.v1.ContainerArgs:
    """A container declared as a pod's object spells it, as the provider's type."""
    resources = container['resources']
    return k8s.core.v1.ContainerArgs(
        name=container['name'],
        image=container['image'],
        command=container['command'],
        env=_typed_env(container['env']),
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name=it['name'], mount_path=it['mountPath'], read_only=it.get('readOnly'))
            for it in container['volumeMounts']
        ],
        resources=k8s.core.v1.ResourceRequirementsArgs(requests=resources['requests'], limits=resources['limits']),
    )


def backup_pod(
    store: Store, *, images: Images, bucket: Bucket, snapshot_key: tuple[str, str]
) -> k8s.core.v1.PodSpecArgs:
    """The pod of `store`'s backup job: `vmbackup` of a snapshot of the claim, read-only, and around it the refusal to shrink.

    `snapshot_key` is the Secret and the key in it that hold the store's
    snapshot key, which the pod mounts as a file, that key alone. A store
    that keeps every row is checked ahead of the backup and its rows recorded
    after it, by containers in the bucket client's image. The containers run
    one after another, the last as the pod's container and the rest as its
    init containers, so each runs only once every one before it has
    succeeded.
    """
    key_secret, key = snapshot_key
    copy = [
        _VMBACKUP,
        f'-storageDataPath={DATA_DIR}',
        f'-dst={_bucket_url(store)}',
        f'-customS3Endpoint={bucket.endpoint}',
    ]
    copy_env = [{'name': 'AWS_REGION', 'value': bucket.region}, *_writer_env(bucket)]

    def bucket_step(name: str, script: str, record: str) -> dict[str, Any]:
        return {
            'name': name,
            'image': images.bucket,
            'command': ['/bin/sh', f'{SHELL_DIR}/{script}'],
            'env': [
                {'name': 'STORE', 'value': store.url},
                {'name': 'RECORD', 'value': record},
                {'name': 'WORK', 'value': WORK_DIR},
                *_writer_env(bucket),
            ],
            # The bucket and the store's address are all it reads: no data.
            'volumeMounts': _mounts(data=False),
            'resources': memory.BUCKET_STEP.rendered(),
        }

    record = store.record
    steps = [
        *([bucket_step('backup-check', 'backup-check.sh', record)] if record is not None else []),
        *_snapshotted(store, copy, copy_env, images=images, key_file=f'{SNAPSHOT_KEY_DIR}/{key}'),
        *([bucket_step('backup-record', 'backup-record.sh', record)] if record is not None else []),
    ]
    volumes = {
        DATA_VOLUME: k8s.core.v1.VolumeArgs(
            name=DATA_VOLUME,
            persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                claim_name=store.workload, read_only=True
            ),
        ),
        SHELL_VOLUME: k8s.core.v1.VolumeArgs(
            name=SHELL_VOLUME,
            config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=SHELL_CONFIGMAP, default_mode=SHELL_MODE),
        ),
        WORK_VOLUME: k8s.core.v1.VolumeArgs(
            name=WORK_VOLUME, empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(medium=_WORK_MEDIUM, size_limit=_WORK_SIZE)
        ),
        SNAPSHOT_KEY_VOLUME: k8s.core.v1.VolumeArgs(
            name=SNAPSHOT_KEY_VOLUME,
            secret=k8s.core.v1.SecretVolumeSourceArgs(
                secret_name=key_secret, items=[k8s.core.v1.KeyToPathArgs(key=key, path=key)]
            ),
        ),
    }
    mounted = {mount['name'] for step in steps for mount in step['volumeMounts']}
    return k8s.core.v1.PodSpecArgs(
        restart_policy='Never',
        init_containers=[_typed_container(it) for it in steps[:-1]],
        containers=[_typed_container(it) for it in steps[-1:]],
        volumes=[volume for name, volume in volumes.items() if name in mounted],
    )


# -- The snapshot layer --------------------------------------------------------------
#
# A workaround for an upstream defect, a layer of its own (docs/style/README.md,
# "A workaround for an upstream defect is a layer of its own").
#
# **The defect.** `vmbackup` takes its snapshot at `-snapshot.createURL`, and
# the store takes its snapshot key only as the form value `authKey`
# (`CheckAuthFlag`, `lib/httpserver/httpserver.go` at v1.150.0), which that
# client can send only in the URL's query. `vmbackup` logs that URL and the
# delete URL made from it (`app/vmbackup/main.go`, "Snapshot create url"),
# through Go's `URL.Redacted`, which hides a password in the URL's user
# information and nothing else; a failed call logs it again, in its error
# (`lib/snapshot/snapshot.go`, and the `url.Error` of Go's client, which
# strips the same password alone). Its client has no flag that carries a key
# by any other way. VictoriaMetrics#5973 asks for one.
#
# **The boundary.** `_snapshotted` below, and the three scripts it runs, each
# naming this layer: `shell/snapshot-create.sh` and `shell/snapshot-delete.sh`,
# in the curl image, which send the key read from its mounted file as the
# url-encoded body of a POST, and `shell/backup.sh`, in `vmbackup`'s, which
# runs `vmbackup` on the snapshot by name (`-snapshotName`). `backup_pod`
# hands the layer what it would hand `vmbackup` with no defect: the copy's
# command, its environment, and the path of the mounted key. In
# `tests/test_monitoring.py`, what reads the snapshot steps goes with the
# layer, or moves to the one-container form: the fake store
# `FAKE_STORE_CURL`, which `Pod` installs in front of the fake bucket, and
# the snapshot check of the fake `vmbackup` in `FAKE_BINARY`;
# `_copied_one_snapshot` and its callers,
# `test_the_history_store_is_backed_up_and_its_rows_recorded_where_it_holds_no_fewer`
# and `test_the_recent_store_is_backed_up_whatever_it_holds`, whose assertion
# on the paths `curl` called goes too;
# `test_a_failed_backup_deletes_its_snapshot_fails_and_records_nothing`; the
# `authKey@` assertion of
# `test_no_credential_is_an_argument_or_a_line_of_output_of_any_process_the_pods_start`;
# the comparison of the containers that mount the key with those that call
# the store in
# `test_the_snapshot_key_reaches_only_the_steps_that_call_the_store_and_only_as_a_file`;
# and the section of the layer's tripwire.
#
# **The removal condition.** `vmbackup` takes the snapshot key through a
# password-type flag, one that reads `file://` (`flagutil.NewPassword`), such
# as the `-snapshot.authKey` VictoriaMetrics#6475 proposes, and the store, at
# the release that ships it, takes the key the way that flag sends it. Then
# `_snapshotted` returns one container, `vmbackup` with `-snapshot.createURL`
# at the store and that flag at `key_file`, mounting the key and the data, and
# the three scripts go.
#
# **The tripwire.** `tests/test_monitoring.py` runs the pinned `vmbackup` and
# the pinned store. Its case of the removal condition,
# `test_the_pinned_vmbackup_has_no_password_flag_for_the_snapshot_key`, turns
# red on the `vmbackup` whose help lists a password-type `-snapshot.*` flag.
# It watches for that remedy rather than for the defect itself: an upstream
# that stops logging the key some other way, by redacting it from what
# `vmbackup` logs, leaves it green, and the layer is then judged by the
# removal condition read again. Its case of the detour,
# `test_the_pinned_store_takes_its_snapshot_key_from_a_posts_body`, on the
# store that stops taking the key that way.


def _snapshotted(
    store: Store,
    copy: list[str],
    copy_env: list[dict[str, Any]],
    *,
    images: Images,
    key_file: str,
) -> list[dict[str, Any]]:
    """`vmbackup` running `copy` with `copy_env`, of a snapshot of `store` taken at the store with the key at `key_file`.

    Three steps: the snapshot taken, `vmbackup` of it, and the snapshot
    deleted, whether or not `vmbackup` succeeded, which also fails the run
    where `vmbackup` failed. A run that never reaches the deletion -- one cut
    off at its deadline, one whose `vmbackup` container never starts, one
    whose store does not answer the deletion -- leaves the snapshot to the
    store's `-snapshotsMaxAge`, at most one a run.
    """

    def snapshot_step(name: str, script: str) -> dict[str, Any]:
        return {
            'name': name,
            'image': images.bucket,
            'command': ['/bin/sh', f'{SHELL_DIR}/{script}'],
            'env': [
                {'name': 'STORE', 'value': store.url},
                {'name': 'KEY', 'value': key_file},
                {'name': 'WORK', 'value': WORK_DIR},
            ],
            # The store's address and its snapshot key are all it reads.
            'volumeMounts': _mounts(data=False, snapshot_key=True),
            'resources': memory.BUCKET_STEP.rendered(),
        }

    vmbackup = {
        'name': 'vmbackup',
        'image': images.backup,
        'command': ['/bin/sh', f'{SHELL_DIR}/backup.sh', *copy],
        'env': [{'name': 'WORK', 'value': WORK_DIR}, *copy_env],
        'volumeMounts': _mounts(data=True),
        'resources': memory.VMBACKUP.rendered(),
    }
    return [
        snapshot_step('snapshot-create', 'snapshot-create.sh'),
        vmbackup,
        snapshot_step('snapshot-delete', 'snapshot-delete.sh'),
    ]
