"""The scrape jobs: the `job` label each gives its series, which the rules and the readings select a series's source by, and the interval every one is scraped at.

A selector that names its job reads that job alone, so a reading is never
summed with a same-named series of another exporter -- the API server's and
etcd's `process_*` among them -- and the scrape-target family watches exactly
the jobs a rule names (`components.monitoring.rules.inputs`).
"""

from __future__ import annotations

import datetime as dt

__all__ = ('APISERVER_JOB', 'ETCD_JOB', 'KUBELET_JOB', 'KUBE_STATE_METRICS_JOB', 'NODE_EXPORTER_JOB', 'SCRAPE_INTERVAL')

#: How often vmagent scrapes every job, the chart's kubelet scrapes and
#: etcd's included: one interval, so that a rate over a window reads the same
#: number of samples whichever source it is computed from. `perf import`
#: judges where the cluster scraped a node by it.
SCRAPE_INTERVAL = dt.timedelta(seconds=30)

#: The `job` etcd's series carry. Stated, because the operator's default is the
#: scrape object's namespace and name, and the object's name is autonamed.
ETCD_JOB = 'etcd'

#: The `job` of every one of the chart's kubelet scrapes, the kubelet's own
#: metrics and cAdvisor's alike, which the monitoring component's relabeling
#: writes (`components.monitoring.KUBELET_RELABELING`) as the chart's own does.
KUBELET_JOB = 'kubelet'

#: The `job` the node exporter's series carry: the chart's scrape of it takes
#: the job from its Service's `jobLabel` label (`values.yaml` L2201 at stack
#: chart 0.91.2), which the chart's values set to this (L2190).
NODE_EXPORTER_JOB = 'node-exporter'

#: The `job` the API server's series carry: the chart's scrape of it takes the
#: job from the `component` label of the `kubernetes` Service it selects by
#: that label's value (`values.yaml` L2298-2304 at 0.91.2).
APISERVER_JOB = 'apiserver'

#: The `job` kube-state-metrics' series carry: the chart's scrape of it takes
#: the job from its Service's `app.kubernetes.io/name` (`jobLabel`,
#: `values.yaml` L2228 at stack chart 0.91.2), which the kube-state-metrics
#: chart sets to its own name (`kube-state-metrics.selectorLabels` at 7.5.3).
#: `components.monitoring.KUBE_STATE_METRICS_ENDPOINT` replaces the scrape's
#: endpoints, relabels no `job`, and leaves the `jobLabel` in place. Its
#: `honorLabels` bears on the series kube-state-metrics publishes, not on
#: `up`, which vmagent writes with the target's own labels
#: (`docs/victoriametrics/vmagent.md`, "Automatically generated metrics", at
#: VictoriaMetrics v1.150.0).
KUBE_STATE_METRICS_JOB = 'kube-state-metrics'
