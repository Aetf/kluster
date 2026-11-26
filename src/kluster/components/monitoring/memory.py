"""Monitoring's resources: what each container is given in memory and CPU, and the memory budget the requests are held to.

The budget is the monitoring line of nodes.md §4.4, which rfc-008 §6.1
reserves as a request, planned at the home site: 0.7 GiB (`BUDGET_MIB`).
Every container the component runs for as long as the stack runs, the
charts' and the history store's, is a row of `CONTAINERS`, and both charts
take each row's requests and limits from there (`chart_values`), so such a
container is one some line of that table counts.
Each row names its line (`Line`): the stack's containers are charged to
monitoring's, which the budget holds, metrics-server to the line for
everything else in kube-system, where nodes.md §4.4 counts it, and the
history store, the vmsingle the component declares itself, to a line of its
own, `HISTORY_BUDGET_MIB` beside monitoring's (nodes.md §4.4, as the operator
ruled on Aetf/kluster-ops#790).

**A memory request is what the container runs at**, its anonymous memory as
the kubelet reports it, rounded up (workloads.md §1), and for the two
components below that shape their own memory, what they run at once
shaped. The working set
`kubectl top` reports counts the page cache a container has read or
written besides, which the kernel reclaims ahead of anonymous memory;
with no limit only pressure on the whole node reclaims it, so a container
that has read its own binary or a data file can carry that file on its
working set for as long as it runs. **A memory limit is the peak the container
is allowed**, which also bounds that cache: the kernel reclaims it as a
container nears its limit. Every container has one. The limits are not
held to the budget: a limit bounds a peak, the peaks do not fall together, and the
home site's own ceiling is rfc-008 §6.1's.

**Every container carries CPU too** (`Cpu`), since the scheduler may place
any of these pods on a cloud node, where etcd shares the cores
(workloads.md §1). Each row's figures come from its container's five-minute
CPU rate over 24 hours, read on the cluster on 2026-11-20, the highest of
the container's pods: the request is its p99, rounded up to a multiple of
5m and at no less than 10m, a figure chosen rather than read; the limit is above its peak, so
the peak is not throttled, and `Cpu` refuses a limit below the peak it
records. A row with no reading says so, and its figures are estimates.

Two of the stack's components shape their own memory. The
VictoriaMetrics components size their caches to 60 % of their limit (the
`-memory.allowedPercent` default), so vmsingle's request is met through
its limit: its caches, about half of its memory, follow the limit down
from the operator's default of 1500 MiB.
Grafana's Go runtime collects garbage once its heap has grown by half its
live size (`GRAFANA_GOGC`) rather than doubled, the runtime's default; the
chart sets its `GOMEMLIMIT` to the container's limit whenever one is set
(grafana chart 12.7.3, `templates/_pod.tpl` L1482-1488), so under a query
that needs more the runtime collects harder before the kernel stops it.

**A job's container is no row.** The stores' backups, the pull of OCI's
series and the state backend's checks run for minutes of each hour, and a
line charged with them would hold the budget for memory the home site gives
them only then. Their resources are declared here all the same (`OCI_PULL`,
`STATE_BACKEND_DUMPS`, `STATE_BACKEND_CERTIFICATE`, `VMBACKUP`,
`BUCKET_STEP`, and `VMRESTORE` for the restore that runs ahead of each
store): each memory request is what a run peaks at, so the scheduler places
it where that fits.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = (
    'BUCKET_STEP',
    'BUDGET_MIB',
    'CONTAINERS',
    'GRAFANA',
    'GRAFANA_GOGC',
    'HISTORY_BUDGET_MIB',
    'HISTORY_STORE',
    'METRICS_SERVER_CHART',
    'OCI_PULL',
    'STACK_CHART',
    'STATE_BACKEND_CERTIFICATE',
    'STATE_BACKEND_DUMPS',
    'VMBACKUP',
    'VMRESTORE',
    'Container',
    'Cpu',
    'Line',
    'Memory',
    'Resources',
    'charged_to',
    'chart_values',
    'merged',
)

#: The memory nodes.md §4.4 budgets for monitoring, planned at the home site,
#: and rfc-008 §6.1 reserves there as a request: 0.7 GiB, in MiB.
BUDGET_MIB = 0.7 * 1024

#: The memory nodes.md §4.4 gives the history store, a line of
#: its own beside `BUDGET_MIB`, in MiB. Derived rather than measured: the
#: recent store ran at 225 MiB holding 171,229 series (Aetf/kluster-ops#790),
#: and the history holds 5,000 to 6,000 of them, so what it takes is the
#: process's and its caches' floor rather than its series'.
HISTORY_BUDGET_MIB = 150

#: The names of the two charts the component installs, as their pins name
#: them (`versions.chart`).
STACK_CHART = 'victoria-metrics-k8s-stack'
METRICS_SERVER_CHART = 'metrics-server'

#: Grafana's `GOGC`: the percentage of its live heap its heap grows by before
#: the runtime collects. Grafana idles between the queries of whoever has it
#: open, and the garbage between two collections is memory it holds for
#: nothing; at 50 its heap peaks at one and a half times its live heap rather
#: than twice it, which keeps it near its request.
GRAFANA_GOGC = 50


class Line(enum.Enum):
    """The line of nodes.md §4.4's table of infrastructure memory that a container is charged to."""

    #: Monitoring, whose budget is `BUDGET_MIB`.
    MONITORING = 'monitoring'
    #: Everything else in kube-system, metrics-server among it.
    KUBE_SYSTEM = 'kube-system'
    #: The history store, whose budget is `HISTORY_BUDGET_MIB`.
    HISTORY = 'monitoring history'


@dataclass(frozen=True)
class Memory:
    """A container's memory in MiB: what the scheduler reserves for it, and where the kernel stops it."""

    request: int
    limit: int

    def __post_init__(self) -> None:
        if self.request > self.limit:
            raise ValueError(f'a memory request of {self.request} MiB is above its limit of {self.limit} MiB')


@dataclass(frozen=True)
class Cpu:
    """A container's CPU in millicores: what the scheduler reserves for it, where the kernel throttles it, and its read peak.

    `peak` is the 24-hour peak of the container's five-minute rate, or `None`
    where no reading was taken; a limit below a peak it records is refused.
    """

    request: int
    limit: int
    peak: int | None

    def __post_init__(self) -> None:
        if self.request > self.limit:
            raise ValueError(f'a CPU request of {self.request}m is above its limit of {self.limit}m')
        if self.peak is not None and self.limit <= self.peak:
            raise ValueError(f'a CPU limit of {self.limit}m would throttle the {self.peak}m peak it was read against')


@dataclass(frozen=True)
class Resources:
    """A container's memory and CPU."""

    memory: Memory
    cpu: Cpu

    def rendered(self) -> dict[str, dict[str, str]]:
        """The `resources` map a chart, an operator object or a container takes: each request and each limit."""
        return {
            'requests': {'cpu': f'{self.cpu.request}m', 'memory': f'{self.memory.request}Mi'},
            'limits': {'cpu': f'{self.cpu.limit}m', 'memory': f'{self.memory.limit}Mi'},
        }


@dataclass(frozen=True)
class Container:
    """One container the component runs for as long as the stack does: which chart runs it, where that chart reads its resources.

    `chart` is `None` for an object the component declares itself, whose spec
    reads its resources at `path`. `line` is the line of nodes.md §4.4 the
    container's memory is charged to.
    """

    name: str
    chart: str | None
    path: tuple[str, ...]
    resources: Resources
    line: Line = Line.MONITORING


#: Grafana itself, its Go runtime collecting at `GRAFANA_GOGC`. Its CPU peak
#: is a burst of queries while a dashboard is open; between them it idles.
GRAFANA = Container(
    'grafana',
    STACK_CHART,
    ('grafana', 'resources'),
    Resources(Memory(request=128, limit=384), Cpu(request=80, limit=750, peak=611)),
)

#: The CPU of each vmsingle: the readings are the highest of the two stores'
#: pods, so both stores take them. The limit leaves a merge or a long query
#: room above the peak.
_STORE_CPU = Cpu(request=115, limit=500, peak=137)

#: The history store's vmsingle, which the component declares itself, on its
#: own line. Its memory limit is the recent store's: the same process, whose
#: caches are sized from it, and which a query over a long period of the
#: history needs room for.
HISTORY_STORE = Container(
    'vmsingle-history',
    None,
    ('spec', 'resources'),
    Resources(Memory(HISTORY_BUDGET_MIB, 512), _STORE_CPU),
    Line.HISTORY,
)

#: The CPU of a job's container that no reading covers: a run is shorter than
#: the five-minute rate a scrape reads. It is the state table's upkeep's,
#: another interpreter calling an API (`components.state_upkeep.CPU`), an
#: estimate.
_JOB_CPU = Cpu(request=50, limit=250, peak=None)

#: The pull of OCI's block-volume series, a job's one container: the
#: interpreter with the `oci` SDK imported and a request made is about 55 MiB,
#: read in the image, and a run holds one day of one series at a time, which
#: for four volumes is a few MiB of the SDK's objects on a workstation.
OCI_PULL = Resources(Memory(request=96, limit=256), _JOB_CPU)

#: The state backend's dump-age check, a job's one container: the interpreter
#: with `urllib` and `ssl` imported and a call to B2 made peaks at about 27 MiB
#: resident, read in the image, and a listing is the few dozen names the
#: retention window holds.
STATE_BACKEND_DUMPS = Resources(Memory(request=32, limit=128), _JOB_CPU)

#: The state backend's certificate check, a job's one container: the
#: interpreter, about 25 MiB resident, and the `openssl` it waits on beside it,
#: about 11 MiB, read in the image.
STATE_BACKEND_CERTIFICATE = Resources(Memory(request=40, limit=128), _JOB_CPU)

#: `vmbackup`, in a store's backup job: it streams the snapshot's parts to the
#: bucket over TLS. Its memory is unread, an estimate: the request is
#: VictoriaMetrics' tools' floor and the limit leaves its upload buffers room.
VMBACKUP = Resources(Memory(request=64, limit=512), Cpu(request=15, limit=250, peak=11))

#: A step in the bucket client's image, busybox's shell and one `curl` at a
#: time: every step of a store's backup job but `vmbackup`, and the check
#: ahead of a store's restore. Neither its CPU nor its
#: memory is read, a run being shorter than a scrape's rate: both are a
#: shell's and a client's few MiB with room, estimates.
BUCKET_STEP = Resources(Memory(request=16, limit=64), Cpu(request=10, limit=100, peak=None))

#: `vmrestore`, ahead of each store: it runs before the store, on the node the
#: store is then given, so its limit is the store's own, which whatever node
#: can hold the store at its peak can hold, and which can fail no start the
#: store itself would pass. Its request is `VMBACKUP`'s, the other
#: VictoriaMetrics tool. Unread, estimates.
VMRESTORE = Resources(Memory(request=64, limit=512), Cpu(request=25, limit=_STORE_CPU.limit, peak=None))

#: Every container the component runs for as long as the stack runs. The
#: node exporter runs on every node, and its row is each copy's. The stack's
#: operator also runs an init container beside each config reloader, from the
#: reloader's resources, which counts toward its pod only while it runs.
#: Every config reloader's CPU is the highest of theirs.
_RELOADER = Resources(Memory(12, 32), Cpu(request=10, limit=50, peak=3))

CONTAINERS: tuple[Container, ...] = (
    GRAFANA,
    # The dashboard sidecar, a Python process: the interpreter and the
    # Kubernetes client are its memory, whatever it watches.
    Container(
        'grafana-sc-dashboard',
        STACK_CHART,
        ('grafana', 'sidecar', 'resources'),
        Resources(Memory(72, 128), Cpu(request=10, limit=100, peak=5)),
    ),
    # Its caches, about half of its memory, are sized from its limit.
    Container('vmsingle', STACK_CHART, ('vmsingle', 'spec', 'resources'), Resources(Memory(256, 512), _STORE_CPU)),
    Container(
        'vmagent',
        STACK_CHART,
        ('vmagent', 'spec', 'resources'),
        Resources(Memory(64, 256), Cpu(request=55, limit=200, peak=54)),
    ),
    Container('vmagent config-reloader', STACK_CHART, ('vmagent', 'spec', 'configReloaderResources'), _RELOADER),
    Container(
        'vmalert',
        STACK_CHART,
        ('vmalert', 'spec', 'resources'),
        Resources(Memory(12, 64), Cpu(request=10, limit=200, peak=7)),
    ),
    Container('vmalert config-reloader', STACK_CHART, ('vmalert', 'spec', 'configReloaderResources'), _RELOADER),
    # The alerting spine (cluster-infra.md §1 item 6), given room for a storm.
    Container(
        'alertmanager',
        STACK_CHART,
        ('alertmanager', 'spec', 'resources'),
        Resources(Memory(16, 128), Cpu(request=10, limit=100, peak=2)),
    ),
    Container(
        'alertmanager config-reloader', STACK_CHART, ('alertmanager', 'spec', 'configReloaderResources'), _RELOADER
    ),
    Container(
        'operator',
        STACK_CHART,
        ('victoria-metrics-operator', 'resources'),
        Resources(Memory(48, 128), Cpu(request=20, limit=100, peak=30)),
    ),
    # Its CPU is a relist of every object it reports on, near its peak at p99.
    Container(
        'kube-state-metrics',
        STACK_CHART,
        ('kube-state-metrics', 'resources'),
        Resources(Memory(24, 64), Cpu(request=270, limit=400, peak=284)),
    ),
    Container(
        'node-exporter',
        STACK_CHART,
        ('prometheus-node-exporter', 'resources'),
        Resources(Memory(12, 48), Cpu(request=10, limit=50, peak=5)),
    ),
    # Its memory is unread; the limit is twice its request, an estimate.
    Container(
        'metrics-server',
        METRICS_SERVER_CHART,
        ('resources',),
        Resources(Memory(32, 64), Cpu(request=10, limit=50, peak=7)),
        Line.KUBE_SYSTEM,
    ),
    HISTORY_STORE,
)


def charged_to(line: Line) -> tuple[Container, ...]:
    """The containers charged to `line`."""
    return tuple(container for container in CONTAINERS if container.line is line)


def chart_values(chart: str) -> dict[str, object]:
    """The resources of every container `chart` runs, as that chart's values: each row's `resources` at its path."""
    values: dict[str, object] = {}
    for container in CONTAINERS:
        if container.chart != chart:
            continue
        *parents, leaf = container.path
        nested: dict[str, object] = {leaf: container.resources.rendered()}
        for key in reversed(parents):
            nested = {key: nested}
        values = merged(values, nested)
    return values


def merged(base: Mapping[str, object], overlay: Mapping[str, object]) -> dict[str, object]:
    """`base` with `overlay` laid over it: maps merged key by key, anything else replaced."""
    result = dict(base)
    for key, value in overlay.items():
        below = result.get(key)
        if isinstance(below, Mapping) and isinstance(value, Mapping):
            result[key] = merged(below, value)  # pyright: ignore[reportUnknownArgumentType] -- values maps of str keys
        else:
            result[key] = value
    return result
