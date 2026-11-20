"""The readings: what the control planes are measured by, each defined once, as data (Aetf/kluster-ops#790 §5).

**A reading is a name, a window, an expression, a unit and its budget.** The
expression is PromQL over the history store's series
(`components.monitoring.stores.HISTORY_CLASS`): the history keeps a reading's
inputs rather than its result, so a reading whose definition changes is
computed again, the same way, over every period on record, and two periods
always compare under one definition. The budget is the one the canon gives the
reading (cluster/nodes.md §1.1 and §1.2, and the per-wave gate of
cluster/migration.md §0 rule 6), with the phrase that states it and where.
Everything else reads this table:

- **the recording rules** (`components.monitoring.rules`): one per reading,
  under `record`, `kluster:<name>:<window>`, which vmalert writes through
  vmagent to both stores, and which the history keeps by its prefix;
- **the SLO alerts** (`components.monitoring.rules`): each compares a
  reading's recorded series with the reading's budget, so an alert's
  threshold is its budget by construction;
- **the dashboard** (`components.monitoring.dashboard`): a row per
  `Resource`, a panel per reading, each computed from the reading's
  expression over the history, so it draws every period the history holds,
  imported ones among them, under one definition;
- **the per-wave gate**, which evaluates the expressions over a window of the
  history (Aetf/kluster-ops#785, slice C2).

**Every selector names its scrape job and selects this cluster** (`series`):
the job, so that a reading reads one exporter's series of a name and the
scrape-target family watches exactly what it reads; and `cluster=""`, since
the history also holds the legacy cluster's series under `cluster="legacy"`
(cluster/migration.md §2.1), which a sum by node, pod or namespace would
otherwise add to this cluster's.

**A reading of a node groups by `node`**, which every source here labels its
series with: the operator's node scrapes, the kubelet's and etcd's, and the
node exporter's scrape (`components.monitoring.NODE_EXPORTER_ENDPOINT`). An
etcd member is its node. A reading is recorded for every node its series come from; a budget
binds the control planes alone (`CONTROL_PLANE`), and the dashboard shows
them.

**The load classes are nodes.md §1.1's**, each read where that
table reads it: the control-plane slice is etcd's own series and the API
server's, the controller manager's and the scheduler's pods by name;
the pods are Envoy's pods and every pod outside the namespaces the
platform's components run in (`PLATFORM_NAMESPACES`); the platform is the
node's total less the other two, which is everything Talos, the kubelet,
containerd and the platform's pods take. The pods' limits are summed over
those outside the platform's namespaces, so Envoy, which runs in Cilium's
namespace with no limit, is left out of the sum. The classes carry no
budget: a control plane is budgeted by its allocatable, which the scheduler
holds every pod's requests to, and a plan checks the pods against it by
their requests (nodes.md §1.1), so the classes and the pods' limits are
read, not bound.
etcd's own series give its CPU and its memory whole, and of its I/O its
WAL's bytes alone: no count of its backend's bytes exists, so its I/O is a
lower bound and the platform's, the rest of the boot volume's writes, takes
the difference. The same sources bound the boot volume's writes other than
etcd's from above (nodes.md §1.2 rule 2), which is the side a budget can be
held on: every WAL fsync and backend commit is at least one write of etcd's.

**KubeSpan's paths are read through what crosses them**, since nothing
exports a peer's state: between control planes, each etcd member's
connected peers; between the worker and each control plane, vmagent's
scrapes of etcd and the node exporter there. A member or a target that is
down reads as its path down, which is the side a gate errs on.

**Two readings' inputs the history does not keep** (`inputs_kept`): the API
server's latency, whose duration histogram is the largest family the store
holds, and the worker's scrapes of the control planes, which read `up`. The
history keeps their recorded results alone, so a change to either's
definition applies from the change on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from kluster import conventions
from kluster.conventions.backup import duration, max_age
from kluster.conventions.cluster import STATE_UPKEEP_NAMESPACE
from kluster.conventions.providers import OCI_BLOCKSTORE_METRICS
from kluster.conventions.schedule import OCI_METRICS_PULL
from kluster.lib.monitoring.jobs import (
    APISERVER_JOB,
    ETCD_JOB,
    KUBE_STATE_METRICS_JOB,
    KUBELET_JOB,
    NODE_EXPORTER_JOB,
)

__all__ = (
    'BOND',
    'BOOT_DEVICE',
    'CLASS_LABEL',
    'CLUSTER_LABEL',
    'CONTROL_PLANE',
    'CONTROL_PLANE_PODS',
    'ENVOY_PODS',
    'ETHERNET',
    'KUBESPAN',
    'OCI_WINDOW',
    'PLATFORM_NAMESPACES',
    'POD_CGROUP',
    'READINGS',
    'RECORD_PREFIX',
    'THIS_CLUSTER',
    'WAL_FSYNC_SECOND',
    'Bound',
    'Budget',
    'Citation',
    'LoadClass',
    'Reading',
    'Resource',
    'series',
)

#: The prefix every recorded series carries, which the history keeps whole.
RECORD_PREFIX = 'kluster'

#: The label the history's series of another cluster carry, and this
#: cluster's selection by it: its own series carry no such label.
CLUSTER_LABEL = 'cluster'
THIS_CLUSTER = f'{CLUSTER_LABEL}=""'

#: The control planes, as a matcher of the `node` label: the cloud nodes,
#: which are the control plane (nodes.md §4.2).
CONTROL_PLANE = f'node=~"{"|".join(conventions.CLOUD_NODES)}"'

#: The control-plane slice's pods, by name: Talos runs each as a static pod
#: named after its component and its node.
CONTROL_PLANE_PODS = 'kube-apiserver-.+|kube-controller-manager-.+|kube-scheduler-.+'

#: Envoy's pods, which answer `internet-gw`: the pods of Cilium's `cilium-envoy`
#: DaemonSet, in Cilium's namespace.
ENVOY_PODS = 'cilium-envoy-.+'

#: The namespaces `k8s-base` places its objects in: every pod there is the
#: platform's, Envoy's apart, and every pod elsewhere is a workload's. A test
#: holds the set to the namespaces the `k8s-base` program declares objects in,
#: so a namespace it adds is never read as a workload's.
PLATFORM_NAMESPACES: tuple[str, ...] = (
    conventions.CILIUM_NAMESPACE,
    conventions.CERT_MANAGER_NAMESPACE,
    conventions.ETCD_BACKUP_NAMESPACE,
    conventions.GATEWAY_NAMESPACE,
    conventions.MONITORING_NAMESPACE,
    conventions.INTEL_DEVICE_PLUGINS_NAMESPACE,
    conventions.NFD_NAMESPACE,
    conventions.LOCAL_PATH_NAMESPACE,
    conventions.CNPG_NAMESPACE,
    conventions.RELOADER_NAMESPACE,
    STATE_UPKEEP_NAMESPACE,
    conventions.VOLSYNC_NAMESPACE,
)

#: The label a reading by load class carries its class under.
CLASS_LABEL = 'class'


class LoadClass(StrEnum):
    """A load class of nodes.md §1.1, as the `class` label carries it.

    `WORKLOADS` is the table's pods class; its value is the label the
    history already records it under.
    """

    CONTROL_PLANE = 'control-plane'
    PLATFORM = 'platform'
    WORKLOADS = 'workloads'


class Resource(StrEnum):
    """What a reading measures: the dashboard's row, and the rule group its recording rule is in."""

    ETCD = 'etcd'
    CPU = 'cpu'
    MEMORY = 'memory'
    DISK = 'disk'
    NETWORK = 'network'
    LOAD_CLASSES = 'load-classes'
    API_SERVER = 'apiserver'


class Bound(StrEnum):
    """Which side of its limit a reading stays on, as the comparison that breaches it."""

    AT_MOST = '>'
    AT_LEAST = '<'


@dataclass(frozen=True)
class Citation:
    """A numbered section of a document of this repository: where the canon states something."""

    document: str
    """The document, from the repository's root."""
    section: str


@dataclass(frozen=True, kw_only=True)
class Budget:
    """The bound the canon gives a reading: its limit, the phrase stating it and where, and the series it binds."""

    limit: float
    bound: Bound
    stated: str
    """The canon's own phrase, in the section `source` names, with the window
    it states the budget over where it states one beside the number."""
    source: Citation
    scope: str = CONTROL_PLANE
    """A matcher selecting the reading's series the budget binds."""

    def breach(self, series: str) -> str:
        """The expression that holds wherever `series`, recorded for this budget's reading, is outside it."""
        return f'{series}{{{self.scope}}} {self.bound} {_number(self.limit)}'


@dataclass(frozen=True, kw_only=True)
class Reading:
    """One reading: what it is called, over what window, how it is computed, and what bounds it."""

    name: str
    window: dt.timedelta
    expr: str
    unit: str
    """Grafana's identifier of the unit the value is in."""
    title: str
    """What the reading is, in a few words: its panel's title."""
    resource: Resource
    per: tuple[str, ...]
    """The labels that tell the reading's series apart."""
    budget: Budget | None = None
    inputs_kept: bool = True
    """Whether the history keeps the series the expression reads."""
    note: str = ''
    """What a reader of the reading's panel needs beside its title: how it is
    biased, where it is."""

    @property
    def record(self) -> str:
        """The name of the reading's recorded series."""
        return f'{RECORD_PREFIX}:{self.name}:{duration(self.window)}'


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(value)


def series(metric: str, *matchers: str, job: str | None) -> str:
    """A selector of this cluster's series of `metric`, from `job` where the series has one, under `matchers`."""
    named = (f'job="{job}"',) if job is not None else ()
    return f'{metric}{{{", ".join((*named, THIS_CLUSTER, *matchers))}}}'


def _quantile(metric: str, by: str, window: dt.timedelta, *matchers: str, job: str) -> str:
    """The 99th percentile of a histogram's observations over the window, per `by`."""
    buckets = series(f'{metric}_bucket', *matchers, job=job)
    return f'histogram_quantile(0.99, sum by ({by}, le) (rate({buckets}[{duration(window)}])))'


def _increase(metric: str, by: str, window: dt.timedelta, *matchers: str, job: str | None) -> str:
    return f'sum by ({by}) (increase({series(metric, *matchers, job=job)}[{duration(window)}]))'


def _changes(metric: str, window: dt.timedelta, *, job: str) -> str:
    """How much a slow counter grew over the window, per node, where the counter was read at the window's start.

    `increase` reads a counter's first sample in a window that holds no
    sample before it as growth from 0, so a counter that sits above 0 and is
    read again after a scrape gap would count its whole value: every member's
    leader changes after the worker's vmagent comes back, say. Holding the
    window to a counter read at its start drops that window rather than
    counting it. What that gives up is a change inside a gap longer than the
    window: every window after such a gap starts either inside it, which is
    dropped, or past the change. A reading of a whole soak (`perf gate`,
    Aetf/kluster-ops#785 slice C2) takes the counter's increase over the
    whole of it instead.
    """
    selected = series(metric, job=job)
    return f'sum by (node) (increase({selected}[{duration(window)}]) and {selected} offset {duration(window)})'


def _rate(metric: str, by: str, window: dt.timedelta, *matchers: str, job: str | None) -> str:
    return f'sum by ({by}) (rate({series(metric, *matchers, job=job)}[{duration(window)}]))'


_FIVE = dt.timedelta(minutes=5)
_TEN = dt.timedelta(minutes=10)
_HOUR = dt.timedelta(hours=1)

#: Where nodes.md and migration.md state the budgets.
_NODES_LOAD = Citation('docs/cluster/nodes.md', '1.1')
_NODES_DISK = Citation('docs/cluster/nodes.md', '1.2')
_GATE = Citation('docs/cluster/migration.md', '0')

# -- etcd ---------------------------------------------------------------------------

#: The upper edge of etcd's WAL fsync bucket nearest one second: its buckets
#: double from a millisecond (`ExponentialBuckets(0.001, 2, 14)`,
#: `server/storage/wal/metrics.go` at etcd v3.6), so the bucket ending at
#: 1.024 s holds every fsync up to it, and the count less that bucket is the
#: fsyncs over it, which is what etcd's own `slow fdatasync` warning reports.
WAL_FSYNC_SECOND = '1.024'

_WAL_FSYNC = 'etcd_disk_wal_fsync_duration_seconds'
_BACKEND_COMMIT = 'etcd_disk_backend_commit_duration_seconds'

#: The bytes etcd writes to its WAL, a gauge by type that only grows: the one
#: count of etcd's own I/O its listener serves.
_WAL_BYTES = 'etcd_disk_wal_write_bytes_total'


def _etcd() -> tuple[Reading, ...]:
    member = ('node',)
    over_a_second = (
        f'{_increase(f"{_WAL_FSYNC}_count", "node", _FIVE, job=ETCD_JOB)}'
        f' - {_increase(f"{_WAL_FSYNC}_bucket", "node", _FIVE, f'le="{WAL_FSYNC_SECOND}"', job=ETCD_JOB)}'
    )
    apply = 'etcd_server_apply_duration_seconds'
    return (
        Reading(
            name='etcd_wal_fsync_p99',
            window=_FIVE,
            expr=_quantile(_WAL_FSYNC, 'node', _FIVE, job=ETCD_JOB),
            unit='s',
            title='WAL fsync, p99',
            resource=Resource.ETCD,
            per=member,
            budget=Budget(
                limit=0.010, bound=Bound.AT_MOST, stated='WAL fsync p99 under 10 ms in every 5 minutes', source=_GATE
            ),
        ),
        Reading(
            name='etcd_wal_fsyncs_over_1s',
            window=_FIVE,
            expr=over_a_second,
            unit='short',
            title='WAL fsyncs over 1 s',
            resource=Resource.ETCD,
            per=member,
            budget=Budget(limit=0, bound=Bound.AT_MOST, stated='no WAL fsync over 1 s', source=_NODES_DISK),
        ),
        Reading(
            name='etcd_backend_commit_p99',
            window=_FIVE,
            expr=_quantile(_BACKEND_COMMIT, 'node', _FIVE, job=ETCD_JOB),
            unit='s',
            title='Backend commit, p99',
            resource=Resource.ETCD,
            per=member,
            budget=Budget(
                limit=0.025,
                bound=Bound.AT_MOST,
                stated='backend commit p99 under 25 ms over every 5 minutes',
                source=_NODES_DISK,
            ),
        ),
        Reading(
            name='etcd_apply_mean',
            window=_FIVE,
            expr=(
                f'{_rate(f"{apply}_sum", "node", _FIVE, job=ETCD_JOB)}'
                f' / {_rate(f"{apply}_count", "node", _FIVE, job=ETCD_JOB)}'
            ),
            unit='s',
            title='Apply, mean',
            resource=Resource.ETCD,
            per=member,
            budget=Budget(limit=0.1, bound=Bound.AT_MOST, stated='mean apply at most 100 ms', source=_GATE),
        ),
        Reading(
            name='etcd_apply_p99',
            window=_FIVE,
            expr=_quantile(apply, 'node', _FIVE, job=ETCD_JOB),
            unit='s',
            title='Apply, p99',
            resource=Resource.ETCD,
            per=member,
        ),
        Reading(
            name='etcd_leader_changes',
            window=dt.timedelta(minutes=15),
            expr=_changes('etcd_server_leader_changes_seen_total', dt.timedelta(minutes=15), job=ETCD_JOB),
            unit='short',
            title='Leader changes',
            resource=Resource.ETCD,
            per=member,
            budget=Budget(limit=0, bound=Bound.AT_MOST, stated='no leader change', source=_GATE),
        ),
        Reading(
            name='etcd_heartbeat_send_failures',
            window=_FIVE,
            expr=_changes('etcd_server_heartbeat_send_failures_total', _FIVE, job=ETCD_JOB),
            unit='short',
            title='Heartbeat send failures',
            resource=Resource.ETCD,
            per=member,
            budget=Budget(limit=0, bound=Bound.AT_MOST, stated='heartbeat send failures not increasing', source=_GATE),
        ),
        Reading(
            name='etcd_proposals_failed',
            window=_FIVE,
            expr=_changes('etcd_server_proposals_failed_total', _FIVE, job=ETCD_JOB),
            unit='short',
            title='Failed proposals',
            resource=Resource.ETCD,
            per=member,
        ),
        Reading(
            name='etcd_peer_round_trip_p99',
            window=_FIVE,
            expr=_quantile('etcd_network_peer_round_trip_time_seconds', 'node, To', _FIVE, job=ETCD_JOB),
            unit='s',
            title='Peer round trip, p99',
            resource=Resource.ETCD,
            per=('node', 'To'),
        ),
    )


# -- The node: CPU, memory, disk, network ----------------------------------------------

#: The modes in which a CPU does no work: `iowait` is idle time spent waiting
#: on I/O, which the I/O pressure readings count.
_IDLE = 'mode!~"idle|iowait"'


def _node_cpu(window: dt.timedelta, *matchers: str) -> str:
    """The cores a node's CPUs spent working over the window."""
    return _rate('node_cpu_seconds_total', 'node', window, _IDLE, *matchers, job=NODE_EXPORTER_JOB)


def _cpu() -> tuple[Reading, ...]:
    node = ('node',)

    def pressure(window: dt.timedelta, budget: Budget | None) -> Reading:
        return Reading(
            name='node_cpu_pressure_some',
            window=window,
            expr=_rate('node_pressure_cpu_waiting_seconds_total', 'node', window, job=NODE_EXPORTER_JOB),
            unit='percentunit',
            title=f'CPU pressure (some), {duration(window)}',
            resource=Resource.CPU,
            per=node,
            budget=budget,
        )

    return (
        Reading(
            name='node_cpu_used',
            window=_FIVE,
            expr=_node_cpu(_FIVE),
            unit='short',
            title='CPU used (cores)',
            resource=Resource.CPU,
            per=node,
        ),
        pressure(dt.timedelta(minutes=1), None),
        pressure(
            _TEN,
            Budget(
                limit=0.2,
                bound=Bound.AT_MOST,
                stated='CPU pressure (some) at most 20 % in every 10 minutes',
                source=_GATE,
            ),
        ),
    )


def _memory() -> tuple[Reading, ...]:
    node = ('node',)
    available = series('node_memory_MemAvailable_bytes', job=NODE_EXPORTER_JOB)
    return (
        Reading(
            name='node_memory_available',
            window=_FIVE,
            expr=f'min by (node) (min_over_time({available}[{duration(_FIVE)}]))',
            unit='bytes',
            title='Memory available, lowest',
            resource=Resource.MEMORY,
            per=node,
            budget=Budget(limit=2**30, bound=Bound.AT_LEAST, stated='never falls under 1 GiB', source=_NODES_LOAD),
        ),
        Reading(
            name='node_memory_pressure_full',
            window=_FIVE,
            expr=_rate('node_pressure_memory_stalled_seconds_total', 'node', _FIVE, job=NODE_EXPORTER_JOB),
            unit='percentunit',
            title='Memory pressure (full)',
            resource=Resource.MEMORY,
            per=node,
            budget=Budget(limit=0, bound=Bound.AT_MOST, stated='no memory pressure (full)', source=_GATE),
        ),
    )


#: The control planes' boot volume as the kernel names it: every control
#: plane's boot volume is attached first, as `sda` (Aetf/kluster-ops#790 §1).
BOOT_DEVICE = 'device="sda"'

#: How far back a reading of OCI's series looks: as far as the pull may lag,
#: which is the one rule for stale over its hourly period, the age past which
#: `OciMetricsPullStale` reports it. A sample OCI's pull writes is stamped
#: with its own minute, up to a period after that minute, and a window this
#: long reads it for at least half a period after it lands.
OCI_WINDOW = max_age(OCI_METRICS_PULL.period)


def _oci(metric: str, over: str) -> str:
    """A per-volume reading of one of OCI's series, over `OCI_WINDOW`."""
    selected = series(f'{OCI_BLOCKSTORE_METRICS}_{metric}', job=None)
    return f'{over} by (node, volume) ({over}_over_time({selected}[{duration(OCI_WINDOW)}]))'


def _disk() -> tuple[Reading, ...]:
    node = ('node',)
    volume = ('node', 'volume')
    writes = _rate('node_disk_writes_completed_total', 'node', _FIVE, BOOT_DEVICE, job=NODE_EXPORTER_JOB)
    write_time = _rate('node_disk_write_time_seconds_total', 'node', _FIVE, BOOT_DEVICE, job=NODE_EXPORTER_JOB)

    def io_pressure(kind: str, metric: str, budget: Budget | None) -> Reading:
        return Reading(
            name=f'node_io_pressure_{kind}',
            window=_TEN,
            expr=_rate(metric, 'node', _TEN, job=NODE_EXPORTER_JOB),
            unit='percentunit',
            title=f'I/O pressure ({kind})',
            resource=Resource.DISK,
            per=node,
            budget=budget,
        )

    return (
        Reading(
            name='boot_volume_write_iops',
            window=_FIVE,
            expr=writes,
            unit='iops',
            title='Boot volume, writes',
            resource=Resource.DISK,
            per=node,
        ),
        Reading(
            name='boot_volume_write_bytes',
            window=_FIVE,
            expr=_rate('node_disk_written_bytes_total', 'node', _FIVE, BOOT_DEVICE, job=NODE_EXPORTER_JOB),
            unit='Bps',
            title='Boot volume, bytes written',
            resource=Resource.DISK,
            per=node,
        ),
        Reading(
            name='boot_volume_write_latency',
            window=_FIVE,
            expr=f'{write_time} / {writes}',
            unit='s',
            title='Boot volume, mean write latency',
            resource=Resource.DISK,
            per=node,
        ),
        Reading(
            # The time-weighted count of requests in flight, over the window:
            # the mean queue the volume held.
            name='boot_volume_io_in_flight',
            window=_FIVE,
            expr=_rate('node_disk_io_time_weighted_seconds_total', 'node', _FIVE, BOOT_DEVICE, job=NODE_EXPORTER_JOB),
            unit='short',
            title='Boot volume, I/O in flight',
            resource=Resource.DISK,
            per=node,
        ),
        Reading(
            # An upper bound: every WAL fsync and every backend commit asks
            # the volume for at least one write, so the rest is at most this.
            name='boot_volume_other_write_iops',
            window=_HOUR,
            expr=(
                f'{_rate("node_disk_writes_completed_total", "node", _HOUR, BOOT_DEVICE, job=NODE_EXPORTER_JOB)}'
                f' - {_rate(f"{_WAL_FSYNC}_count", "node", _HOUR, job=ETCD_JOB)}'
                f' - {_rate(f"{_BACKEND_COMMIT}_count", "node", _HOUR, job=ETCD_JOB)}'
            ),
            unit='iops',
            title="Boot volume, writes other than etcd's (at most)",
            resource=Resource.DISK,
            per=node,
            budget=Budget(
                limit=300,
                bound=Bound.AT_MOST,
                stated='300 IOPS and 2.4 MB/s, averaged over an hour',
                source=_NODES_DISK,
            ),
            note="An upper bound: every WAL fsync and backend commit is counted as one write of etcd's, the least it takes.",
        ),
        Reading(
            # An upper bound: the WAL's bytes are at most the bytes they
            # cost the volume, and the backend's bytes, which etcd counts
            # nowhere, stay on this side.
            name='boot_volume_other_write_bytes',
            window=_HOUR,
            expr=(
                f'{_rate("node_disk_written_bytes_total", "node", _HOUR, BOOT_DEVICE, job=NODE_EXPORTER_JOB)}'
                f' - {_rate(_WAL_BYTES, "node", _HOUR, job=ETCD_JOB)}'
            ),
            unit='Bps',
            title="Boot volume, bytes other than etcd's (at most)",
            resource=Resource.DISK,
            per=node,
            budget=Budget(
                limit=2.4e6, bound=Bound.AT_MOST, stated='2.4 MB/s, averaged over an hour', source=_NODES_DISK
            ),
            note="An upper bound: only the WAL's bytes are etcd's here; its backend's, which etcd counts nowhere, are not.",
        ),
        io_pressure(
            'some',
            'node_pressure_io_waiting_seconds_total',
            Budget(
                limit=0.1,
                bound=Bound.AT_MOST,
                stated='I/O pressure (some) at most 10 % in every 10 minutes',
                source=_GATE,
            ),
        ),
        io_pressure('full', 'node_pressure_io_stalled_seconds_total', None),
        Reading(
            name='volume_throttled_ios',
            window=OCI_WINDOW,
            expr=_oci('VolumeThrottledIOs', 'sum'),
            unit='short',
            title='Throttled I/O (OCI)',
            resource=Resource.DISK,
            per=volume,
            budget=Budget(
                limit=0,
                bound=Bound.AT_MOST,
                stated="OCI's throttled I/O is 0 on every boot volume",
                source=_GATE,
                scope='volume="boot"',
            ),
        ),
        Reading(
            name='volume_guaranteed_iops',
            window=OCI_WINDOW,
            expr=_oci('VolumeGuaranteedIOPS', 'max'),
            unit='iops',
            title='Guaranteed IOPS (OCI)',
            resource=Resource.DISK,
            per=volume,
        ),
        Reading(
            name='volume_guaranteed_throughput',
            window=OCI_WINDOW,
            expr=_oci('VolumeGuaranteedThroughput', 'max'),
            unit='MBs',
            title='Guaranteed throughput (OCI)',
            resource=Resource.DISK,
            per=volume,
        ),
    )


#: A node's uplink, the link its traffic leaves by, as the kernel names it: a
#: bond where the node has one, `bond0` on a control plane, and otherwise its
#: physical Ethernet link, `eth<n>` or, under predictable naming, `en…`. A
#: bond's member links carry what it does, so a node with a bond is read by
#: the bond alone. The names are the platform's to give, and the program
#: addresses the link by an alias of its own (`components.talos.UPLINK`);
#: `dummy0` and the CNI's links are none of these. On a control plane the
#: uplink carries the public traffic, and KubeSpan's too, encrypted.
BOND = 'bond[0-9]+'
ETHERNET = 'en.+|eth[0-9]+'

#: KubeSpan's WireGuard link, which Talos names `kubespan`.
KUBESPAN = 'kubespan'


def _both_ways(kind: str, link: str, by: str, function: str) -> str:
    """Received and transmitted together, over the links `link` matches, per `by`."""
    received, transmitted = (
        series(f'node_network_{direction}_{kind}', f'device=~"{link}"', job=NODE_EXPORTER_JOB)
        for direction in ('receive', 'transmit')
    )
    window = duration(_FIVE)
    return f'sum by ({by}) ({function}({received}[{window}]) + {function}({transmitted}[{window}]))'


def _network() -> tuple[Reading, ...]:
    link = ('node', 'device')
    return (
        Reading(
            # The uplink alone: KubeSpan's traffic crosses it encrypted,
            # so it holds the public traffic and KubeSpan's together, a request
            # to a home backend twice, as nodes.md §1.1 counts it.
            name='node_network_bytes',
            window=_FIVE,
            expr=(
                f'{_both_ways("bytes_total", BOND, "node", "rate")}'
                f' or {_both_ways("bytes_total", ETHERNET, "node", "rate")}'
            ),
            unit='Bps',
            title='Network, public link and KubeSpan',
            resource=Resource.NETWORK,
            per=('node',),
            budget=Budget(limit=62.5e6, bound=Bound.AT_MOST, stated='62.5 MB/s', source=_NODES_LOAD),
        ),
        Reading(
            name='node_link_bytes',
            window=_FIVE,
            expr=_both_ways('bytes_total', f'{BOND}|{ETHERNET}|{KUBESPAN}', 'node, device', 'rate'),
            unit='Bps',
            title='Bytes, by link',
            resource=Resource.NETWORK,
            per=link,
        ),
        Reading(
            # etcd's members reach one another over KubeSpan, so a member
            # that keeps its peers connected has a path to each; one that
            # loses a peer reads it here, a member down as a path down. etcd
            # keeps a removed member's series at its last value for as long
            # as the process runs (`Transport.removePeer` deletes no label,
            # `rafthttp` at etcd v3.6.12), so a peer counts only while its own
            # listener reports its ID: a replaced member drops out with its
            # instance, and a member that is down drops out five minutes
            # later, when `control_plane_scrapes_up` reads its path down.
            name='etcd_active_peers',
            window=_FIVE,
            expr=(
                f'min by (node, Remote) (min_over_time({series("etcd_network_active_peers", job=ETCD_JOB)}'
                f'[{duration(_FIVE)}]))'
                f' and on (Remote) label_replace(max by (server_id) (max_over_time('
                f'{series("etcd_server_id", job=ETCD_JOB)}[{duration(_FIVE)}])), "Remote", "$1", "server_id", "(.+)")'
            ),
            unit='short',
            title='etcd peers connected, between control planes',
            resource=Resource.NETWORK,
            per=('node', 'Remote'),
            note='Stands for KubeSpan between control planes: a member down reads as its path down.',
        ),
        Reading(
            # vmagent on the worker scrapes etcd and the node exporter on each
            # control plane over KubeSpan, so the lowest `up` of the two is the
            # worker's path to the node. The history keeps no `up`, so this
            # reading's record is all it keeps of it.
            name='control_plane_scrapes_up',
            window=_FIVE,
            expr=(
                f'min by (node) (min_over_time('
                f'{series("up", f'job=~"{ETCD_JOB}|{NODE_EXPORTER_JOB}"', CONTROL_PLANE, job=None)}'
                f'[{duration(_FIVE)}]))'
            ),
            unit='short',
            title="The worker's scrapes of each control plane",
            resource=Resource.NETWORK,
            per=('node',),
            inputs_kept=False,
            note='Stands for KubeSpan between the worker and the control plane: a target down reads as its path down.',
        ),
        Reading(
            name='node_link_drops',
            window=_FIVE,
            expr=_both_ways('drop_total', f'{BOND}|{ETHERNET}|{KUBESPAN}', 'node, device', 'increase'),
            unit='short',
            title='Drops, by link',
            resource=Resource.NETWORK,
            per=link,
        ),
    )


# -- The load classes -----------------------------------------------------------------


#: The selection of a pod's own control group among `cAdvisor`'s series: no
#: container, and no image. Under containerd `cAdvisor` reports the pod's
#: sandbox, the pause container, with no container too, under the pause
#: image, and that series is a child of the pod's own, which counts it.
POD_CGROUP = ('container=""', 'image=""', 'pod!=""')


def _pods(metric: str, function: str, *matchers: str) -> str:
    """The pods' own control groups, as `cAdvisor` reports each pod on a control plane, over five minutes."""
    pods = series(metric, *POD_CGROUP, CONTROL_PLANE, *matchers, job=KUBELET_JOB)
    return f'{function}({pods}[{duration(_FIVE)}])'


def _by_class(metric: str, function: str, etcd: str, node: str) -> str:
    """A resource by load class on each control plane, from `cAdvisor`'s `metric` and etcd's and the node's totals.

    The control-plane slice is etcd's own series and its three pods; the
    pods are Envoy's pods and every pod outside the platform's namespaces,
    nothing where none runs; the platform is the node's total less the two.
    """
    platform = '|'.join(PLATFORM_NAMESPACES)
    control_plane = (
        f'sum by (node) ({function}({series(etcd, CONTROL_PLANE, job=ETCD_JOB)}[{duration(_FIVE)}]))'
        f' + sum by (node) ({_pods(metric, function, 'namespace="kube-system"', f'pod=~"{CONTROL_PLANE_PODS}"')})'
    )
    workloads = (
        f'(sum by (node) ({_pods(metric, function, f'namespace!~"{platform}"')}'
        f' or {_pods(metric, function, f'namespace="{conventions.CILIUM_NAMESPACE}"', f'pod=~"{ENVOY_PODS}"')})'
        f' or 0 * {node})'
    )
    classes = {
        LoadClass.CONTROL_PLANE: control_plane,
        LoadClass.WORKLOADS: workloads,
        LoadClass.PLATFORM: f'{node} - ({control_plane}) - {workloads}',
    }
    return ' or '.join(f'label_replace({expr}, "{CLASS_LABEL}", "{name}", "", "")' for name, expr in classes.items())


def _limits(resource: str) -> str:
    """The limits of the pods class on each control plane, of the pods that hold them.

    kube-state-metrics publishes each container's limits whatever its pod's
    phase, a finished Job's pod among them, which the scheduler no longer
    reserves anything for; so a pod counts while it is pending or running.
    A pod a rollout replaced counts until its series ends, not for a window
    after.
    """
    platform = '|'.join(PLATFORM_NAMESPACES)
    limits = series(
        'kube_pod_container_resource_limits',
        f'resource="{resource}"',
        CONTROL_PLANE,
        f'namespace!~"{platform}"',
        job=KUBE_STATE_METRICS_JOB,
    )
    holding = series('kube_pod_status_phase', 'phase=~"Pending|Running"', job=KUBE_STATE_METRICS_JOB)
    return (
        f'sum by (node) (last_over_time({limits}[{duration(_FIVE)}])'
        f' * on (namespace, pod) group_left () max by (namespace, pod) ({holding} == 1))'
    )


def _load_classes() -> tuple[Reading, ...]:
    by_class = ('node', CLASS_LABEL)
    memory_total = series('node_memory_MemTotal_bytes', CONTROL_PLANE, job=NODE_EXPORTER_JOB)
    memory_available = series('node_memory_MemAvailable_bytes', CONTROL_PLANE, job=NODE_EXPORTER_JOB)
    window = duration(_FIVE)
    memory_used = (
        f'sum by (node) (avg_over_time({memory_total}[{window}]) - avg_over_time({memory_available}[{window}]))'
    )
    return (
        Reading(
            name='load_class_cpu',
            window=_FIVE,
            expr=_by_class(
                'container_cpu_usage_seconds_total',
                'rate',
                'process_cpu_seconds_total',
                _node_cpu(_FIVE, CONTROL_PLANE),
            ),
            unit='short',
            title='CPU used by class (cores)',
            resource=Resource.LOAD_CLASSES,
            per=by_class,
        ),
        Reading(
            name='load_class_memory',
            window=_FIVE,
            expr=_by_class(
                'container_memory_working_set_bytes', 'avg_over_time', 'process_resident_memory_bytes', memory_used
            ),
            unit='bytes',
            title='Memory used by class',
            resource=Resource.LOAD_CLASSES,
            per=by_class,
        ),
        Reading(
            name='load_class_io',
            window=_FIVE,
            expr=_by_class(
                'container_fs_writes_bytes_total',
                'rate',
                _WAL_BYTES,
                _rate(
                    'node_disk_written_bytes_total', 'node', _FIVE, BOOT_DEVICE, CONTROL_PLANE, job=NODE_EXPORTER_JOB
                ),
            ),
            unit='Bps',
            title='Bytes written by class',
            resource=Resource.LOAD_CLASSES,
            per=by_class,
            note=(
                "etcd's share is its WAL's bytes alone, a lower bound; its backend's bytes, and containerd's"
                " unpacks and logs, are the platform's, which is the boot volume's writes less the other two."
                " A pod's writes to a volume of its own are subtracted from the boot volume's all the same."
            ),
        ),
        Reading(
            name='workloads_cpu_limits',
            window=_FIVE,
            expr=_limits('cpu'),
            unit='short',
            title='CPU limits of the pods (cores)',
            resource=Resource.LOAD_CLASSES,
            per=('node',),
        ),
        Reading(
            name='workloads_memory_limits',
            window=_FIVE,
            expr=_limits('memory'),
            unit='bytes',
            title='Memory limits of the pods',
            resource=Resource.LOAD_CLASSES,
            per=('node',),
        ),
    )


# -- The API server -------------------------------------------------------------------


def _api_server() -> tuple[Reading, ...]:
    return (
        Reading(
            # A watch or a connection lasts as long as its client holds it,
            # so its duration says nothing of the server's.
            name='apiserver_request_p99',
            window=_FIVE,
            expr=_quantile(
                'apiserver_request_duration_seconds', 'verb', _FIVE, 'verb!~"WATCH|CONNECT"', job=APISERVER_JOB
            ),
            unit='s',
            title='Request duration, p99, by verb',
            resource=Resource.API_SERVER,
            per=('verb',),
            inputs_kept=False,
        ),
    )


#: Every reading, by resource in the dashboard's order.
READINGS: Sequence[Reading] = (*_etcd(), *_cpu(), *_memory(), *_disk(), *_network(), *_load_classes(), *_api_server())
