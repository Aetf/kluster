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
VolSync's. A run is cut off well inside the hour, so a hung one does not hold
the next back. The history store's job also refuses a store holding fewer
rows than its last backup recorded beside the backup (`Store.record`):
`shell/backup-check.sh` ahead of `vmbackup`, and `shell/backup-record.sh`
after it.

**Each store restores itself before it starts**: two init containers on its
vmsingle, `shell/restore-check.sh` and then `shell/restore.sh` around
`vmrestore`, restore the backup into an empty data directory, start an empty
store only where the bucket holds no backup, and fail where the backup cannot
be read, so a store never starts empty over a history that exists. vmagent's
persistent queue holds what is scraped meanwhile.

**The bucket is reached through B2's native API** (`shell/b2.sh`), for the two
things `vmbackup` and `vmrestore` do not do: whether a prefix holds a backup at
all, and the history's row count kept beside its backup. Those calls run in
curl's image, whose `curl` reads the request headers carrying the writer key
and B2's tokens from files in the pod's memory, so no credential is ever a
process's argument, which every process on the node that sees the PID can
read; `vmbackup` and `vmrestore` read the key from their environment, and
`vmbackup` the snapshot key too. The scripts are shell because the images
carry no other interpreter: busybox, and the one binary each. They are one
ConfigMap, which the jobs and the init containers mount.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.conventions.backup import metrics_repo_path

__all__ = (
    'BACKUP_JOB_PREFIX',
    'BACKUP_PERIOD',
    'DATA_DIR',
    'DATA_VOLUME',
    'HISTORY_CLASS',
    'HISTORY_RETENTION',
    'HISTORY_STORE',
    'RECENT_RETENTION',
    'SHELL',
    'SHELL_CONFIGMAP',
    'SHELL_DIR',
    'SHELL_MODE',
    'SHELL_VOLUME',
    'SNAPSHOT_KEY_ENV',
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
    'work_volume',
)

#: How long the recent store keeps a sample: a month of full detail, for
#: investigating what a reading of the history flagged (Aetf/kluster-ops#790,
#: the operator's ruling of 2026-11-17).
RECENT_RETENTION = dt.timedelta(days=31)

#: How long the history store keeps a sample, in VictoriaMetrics' own syntax:
#: for good, so that every period compares with every other.
HISTORY_RETENTION = '100y'

#: How often each store is backed up, the period its freshness is held to.
BACKUP_PERIOD = dt.timedelta(hours=1)

#: The name of the history store's vmsingle object.
HISTORY_STORE = 'history'

#: The prefix every backup CronJob's name starts with, which the freshness
#: rule selects the jobs by.
BACKUP_JOB_PREFIX = 'vmbackup-'

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

#: The variable `vmbackup` reads the store's snapshot key from.
SNAPSHOT_KEY_ENV = 'SNAPSHOT_AUTH_KEY'

#: The scripts, by file name, as the ConfigMap carries them.
SHELL: Mapping[str, str] = {
    path.name: path.read_text() for path in sorted((Path(__file__).parent / 'shell').glob('*.sh'))
}

#: Where each image keeps its one binary.
_VMBACKUP = '/vmbackup-prod'
_VMRESTORE = '/vmrestore-prod'


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
    """Its name in the bucket and in its backup's CronJob."""
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
        return f'vmsingle-{self.vmsingle}'

    @property
    def url(self) -> str:
        """The store's address in the cluster, on the port the operator serves it at."""
        return f'http://{self.workload}.{conventions.MONITORING_NAMESPACE}.svc:8428'

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
    def backup_job(self) -> str:
        return f'{BACKUP_JOB_PREFIX}{self.name}'

    @property
    def schedule(self) -> str:
        """The backup's cron line: every `BACKUP_PERIOD`, at `minute`."""
        return f'{self.minute} * * * *'


def stores(release: str, chart: str) -> tuple[Store, Store]:
    """The recent store, the chart's vmsingle at this release, and the history store, backed up half an hour apart."""
    return (
        Store('recent', stack_fullname(release, chart), minute=15, keeps_every_row=False),
        Store('history', HISTORY_STORE, minute=45, keeps_every_row=True),
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
#: series. A reading is a query over these, so a reading defined later is
#: computed over every period on record.
HISTORY_CLASS: tuple[Kept, ...] = (
    Kept(
        'node_cpu_seconds_total|node_pressure_.*|node_memory_(MemAvailable|MemTotal|Dirty|Writeback)_bytes'
        '|node_disk_.*|node_filesystem_(avail|size)_bytes'
        '|node_network_(receive|transmit)_(bytes|packets|drop|errs)_total|node_load.*'
        '|node_vmstat_(pgmajfault|oom_kill)'
    ),
    Kept('etcd_.*|process_cpu_seconds_total|process_resident_memory_bytes', job='etcd'),
    Kept(
        'container_cpu_usage_seconds_total|container_cpu_cfs_(periods|throttled_periods)_total'
        '|container_memory_working_set_bytes|container_pressure_.*|container_fs_(reads|writes)_bytes_total'
        '|container_blkio_device_usage_total|container_network_(receive|transmit)_bytes_total'
    ),
    Kept('kube_pod_container_resource_(requests|limits)|kube_node_status_allocatable|kube_pod_info'),
    Kept('apiserver_request_total|apiserver_current_inflight_requests'),
    Kept('kluster:.*|oci_blockstore_.*'),
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
    """The writer key's two halves, under the names `vmbackup`, `vmrestore` and `shell/b2.sh` read them by."""
    return [
        {'name': field, 'valueFrom': {'secretKeyRef': {'name': bucket.writer_secret, 'key': field}}}
        for field in sorted(conventions.WRITER_KEY_FIELDS)
    ]


def _bucket_url(store: Store) -> str:
    return f's3://{conventions.BUCKET_BACKUP}/{store.repository}'


def _mounts(*, data: bool, data_read_only: bool = True) -> list[dict[str, Any]]:
    """The scripts and the work directory, and the store's data where the container reads it, at the same paths."""
    return [
        *([{'name': DATA_VOLUME, 'mountPath': DATA_DIR, 'readOnly': data_read_only}] if data else []),
        {'name': SHELL_VOLUME, 'mountPath': SHELL_DIR, 'readOnly': True},
        {'name': WORK_VOLUME, 'mountPath': WORK_DIR},
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


def _typed_mounts(mounts: list[dict[str, Any]]) -> list[k8s.core.v1.VolumeMountArgs]:
    return [
        k8s.core.v1.VolumeMountArgs(name=it['name'], mount_path=it['mountPath'], read_only=it.get('readOnly'))
        for it in mounts
    ]


def backup_pod(
    store: Store, *, images: Images, bucket: Bucket, snapshot_key: tuple[str, str]
) -> k8s.core.v1.PodSpecArgs:
    """The pod of `store`'s backup job: `vmbackup` of the claim, read-only, and around it the refusal to shrink.

    `snapshot_key` is the Secret and the key in it that hold the store's
    snapshot key. `vmbackup` reads it from its environment through
    VictoriaMetrics' `%{ENV}` placeholder in its argument (`expandArgs`,
    `lib/envflag/envflag.go` at v1.150.0), so no argument carries it. A store
    that keeps every row is checked ahead of `vmbackup` and its rows recorded
    after it, by containers in the bucket client's image; the record's
    container runs only once the init containers, `vmbackup` among them, have
    succeeded.
    """
    key_secret, key = snapshot_key
    vmbackup = k8s.core.v1.ContainerArgs(
        name='vmbackup',
        image=images.backup,
        command=[
            _VMBACKUP,
            f'-storageDataPath={DATA_DIR}',
            f'-dst={_bucket_url(store)}',
            f'-customS3Endpoint={bucket.endpoint}',
            f'-snapshot.createURL={store.url}/snapshot/create?authKey=%{{{SNAPSHOT_KEY_ENV}}}',
        ],
        env=_typed_env(
            [
                {'name': SNAPSHOT_KEY_ENV, 'valueFrom': {'secretKeyRef': {'name': key_secret, 'key': key}}},
                {'name': 'AWS_REGION', 'value': bucket.region},
                *_writer_env(bucket),
            ]
        ),
        # The store's data alone: `vmbackup` runs no script.
        volume_mounts=_typed_mounts([{'name': DATA_VOLUME, 'mountPath': DATA_DIR, 'readOnly': True}]),
    )
    data = k8s.core.v1.VolumeArgs(
        name=DATA_VOLUME,
        persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
            claim_name=store.workload, read_only=True
        ),
    )
    record = store.record
    if record is None:
        return k8s.core.v1.PodSpecArgs(restart_policy='Never', containers=[vmbackup], volumes=[data])
    volumes = [
        data,
        k8s.core.v1.VolumeArgs(
            name=SHELL_VOLUME,
            config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=SHELL_CONFIGMAP, default_mode=SHELL_MODE),
        ),
        k8s.core.v1.VolumeArgs(
            name=WORK_VOLUME, empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(medium=_WORK_MEDIUM, size_limit=_WORK_SIZE)
        ),
    ]

    def bucket_step(name: str, script: str) -> k8s.core.v1.ContainerArgs:
        return k8s.core.v1.ContainerArgs(
            name=name,
            image=images.bucket,
            command=['/bin/sh', f'{SHELL_DIR}/{script}'],
            env=_typed_env(
                [
                    {'name': 'STORE', 'value': store.url},
                    {'name': 'RECORD', 'value': record},
                    {'name': 'WORK', 'value': WORK_DIR},
                    *_writer_env(bucket),
                ]
            ),
            # The bucket and the store's address are all it reads: no data.
            volume_mounts=_typed_mounts(_mounts(data=False)),
        )

    return k8s.core.v1.PodSpecArgs(
        restart_policy='Never',
        init_containers=[bucket_step('backup-check', 'backup-check.sh'), vmbackup],
        containers=[bucket_step('backup-record', 'backup-record.sh')],
        volumes=volumes,
    )
