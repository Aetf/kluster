"""Monitoring's memory on the worker: what each container is given, and the budget the requests are held to.

The budget is the monitoring line of nodes.md §4.4, which rfc-008 §6.1
reserves on the worker as a request: 0.7 GiB (`BUDGET_MIB`). Every container
the component runs on the worker for as long as the stack runs, the charts'
and the history store's, is a row of `WORKER_CONTAINERS`, and both charts take
each row's request and limit from there (`chart_values`), so such a container
is one some line of that table counts.
Each row names its line (`Line`): the stack's containers are charged to
monitoring's, which the budget holds, metrics-server to the line for
everything else in kube-system, where nodes.md §4.4 counts it, and the
history store, the vmsingle the component declares itself, to a line of its
own, `HISTORY_BUDGET_MIB` beside monitoring's (nodes.md §4.4, as the operator
ruled on Aetf/kluster-ops#790).

**A request is what the container runs at**, its anonymous memory as the
kubelet reports it, rounded up (workloads.md §1), and for the two
components below that shape their own memory, what they run at once
shaped. The working set
`kubectl top` reports counts the page cache a container has read or
written besides, which the kernel reclaims ahead of anonymous memory;
with no limit only pressure on the whole node reclaims it, so a container
that has read its own binary or a data file can carry that file on its
working set for as long as it runs. **A limit is the peak the container
is allowed**, which also bounds that cache: the kernel reclaims it as a
container nears its limit. Every container has one but metrics-server,
whose pinned chart offers no value for it. The limits are not held to the
budget: a limit bounds a peak, the peaks do not fall together, and the
worker's own ceiling is rfc-008 §6.1's.

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
line charged with them would hold the budget for memory the worker gives them
only then. The backups declare no memory. The pull's and the checks' are
declared here (`OCI_PULL`, `STATE_BACKEND_DUMPS`, `STATE_BACKEND_CERTIFICATE`):
each request is what a run peaks at, so the scheduler places it where that
fits.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = (
    'BUDGET_MIB',
    'GRAFANA',
    'GRAFANA_GOGC',
    'HISTORY_BUDGET_MIB',
    'HISTORY_STORE',
    'METRICS_SERVER_CHART',
    'OCI_PULL',
    'STACK_CHART',
    'STATE_BACKEND_CERTIFICATE',
    'STATE_BACKEND_DUMPS',
    'WORKER_CONTAINERS',
    'Container',
    'Line',
    'Memory',
    'charged_to',
    'chart_values',
    'merged',
)

#: The memory nodes.md §4.4 budgets for monitoring on the worker, and rfc-008
#: §6.1 reserves there as a request: 0.7 GiB, in MiB.
BUDGET_MIB = 0.7 * 1024

#: The memory nodes.md §4.4 gives the history store on the worker, a line of
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
    """A container's memory in MiB: what the scheduler reserves for it, and where the kernel stops it.

    `limit` is `None` only where the pinned chart offers no value to carry one
    (`tests/test_chart_values.py` holds every value to the chart's record).
    """

    request: int
    limit: int | None

    def resources(self) -> dict[str, dict[str, str]]:
        """The `resources` map a chart or an operator object takes: the memory alone, so CPU keeps its defaults."""
        resources = {'requests': {'memory': f'{self.request}Mi'}}
        if self.limit is not None:
            resources['limits'] = {'memory': f'{self.limit}Mi'}
        return resources


@dataclass(frozen=True)
class Container:
    """One container the component runs on the worker: which chart runs it, where that chart reads its memory.

    `chart` is `None` for an object the component declares itself, whose spec
    reads its memory at `path`. `line` is the line of nodes.md §4.4 the
    container is charged to.
    """

    name: str
    chart: str | None
    path: tuple[str, ...]
    memory: Memory
    line: Line = Line.MONITORING


#: Grafana itself, its Go runtime collecting at `GRAFANA_GOGC`.
GRAFANA = Container('grafana', STACK_CHART, ('grafana', 'resources'), Memory(request=128, limit=384))

#: The history store's vmsingle, which the component declares itself, on its
#: own line. Its limit is the recent store's: the same process, whose caches
#: are sized from it, and which a query over a long period of the history
#: needs room for.
HISTORY_STORE = Container(
    'vmsingle-history', None, ('spec', 'resources'), Memory(HISTORY_BUDGET_MIB, 512), Line.HISTORY
)

#: The pull of OCI's block-volume series, a job's one container: the
#: interpreter with the `oci` SDK imported and a request made is about 55 MiB,
#: read in the image, and a run holds one day of one series at a time, which
#: for four volumes is a few MiB of the SDK's objects on a workstation.
OCI_PULL = Memory(request=96, limit=256)

#: The state backend's dump-age check, a job's one container: the interpreter
#: with `urllib` and `ssl` imported and a call to B2 made peaks at about 27 MiB
#: resident, read in the image, and a listing is the few dozen names the
#: retention window holds.
STATE_BACKEND_DUMPS = Memory(request=32, limit=128)

#: The state backend's certificate check, a job's one container: the
#: interpreter, about 25 MiB resident, and the `openssl` it waits on beside it,
#: about 11 MiB, read in the image.
STATE_BACKEND_CERTIFICATE = Memory(request=40, limit=128)

#: Every container the component runs on the worker for as long as the stack
#: runs. The node exporter runs on every node, and its row is the worker's
#: copy. The stack's operator also runs an init container beside each config
#: reloader, from the reloader's resources, which counts toward its pod only
#: while it runs.
WORKER_CONTAINERS: tuple[Container, ...] = (
    GRAFANA,
    # The dashboard sidecar, a Python process: the interpreter and the
    # Kubernetes client are its memory, whatever it watches.
    Container('grafana-sc-dashboard', STACK_CHART, ('grafana', 'sidecar', 'resources'), Memory(72, 128)),
    # Its caches, about half of its memory, are sized from its limit.
    Container('vmsingle', STACK_CHART, ('vmsingle', 'spec', 'resources'), Memory(256, 512)),
    Container('vmagent', STACK_CHART, ('vmagent', 'spec', 'resources'), Memory(64, 256)),
    Container('vmagent config-reloader', STACK_CHART, ('vmagent', 'spec', 'configReloaderResources'), Memory(12, 32)),
    Container('vmalert', STACK_CHART, ('vmalert', 'spec', 'resources'), Memory(12, 64)),
    Container('vmalert config-reloader', STACK_CHART, ('vmalert', 'spec', 'configReloaderResources'), Memory(12, 32)),
    # The alerting spine (cluster-infra.md §1 item 6), given room for a storm.
    Container('alertmanager', STACK_CHART, ('alertmanager', 'spec', 'resources'), Memory(16, 128)),
    Container(
        'alertmanager config-reloader', STACK_CHART, ('alertmanager', 'spec', 'configReloaderResources'), Memory(12, 32)
    ),
    Container('operator', STACK_CHART, ('victoria-metrics-operator', 'resources'), Memory(48, 128)),
    Container('kube-state-metrics', STACK_CHART, ('kube-state-metrics', 'resources'), Memory(24, 64)),
    Container('node-exporter', STACK_CHART, ('prometheus-node-exporter', 'resources'), Memory(12, 48)),
    # The pinned chart's values hold `resources.requests` and only a comment
    # for limits, so its record has no path for one.
    Container('metrics-server', METRICS_SERVER_CHART, ('resources',), Memory(32, None), Line.KUBE_SYSTEM),
    HISTORY_STORE,
)


def charged_to(line: Line) -> tuple[Container, ...]:
    """The containers on the worker charged to `line`."""
    return tuple(container for container in WORKER_CONTAINERS if container.line is line)


def chart_values(chart: str) -> dict[str, object]:
    """The memory of every container `chart` runs, as that chart's values: each row's `resources` at its path."""
    values: dict[str, object] = {}
    for container in WORKER_CONTAINERS:
        if container.chart != chart:
            continue
        *parents, leaf = container.path
        nested: dict[str, object] = {leaf: container.memory.resources()}
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
