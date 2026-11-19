"""Monitoring: the VictoriaMetrics stack and metrics-server, on the homelab worker, and the in-cluster alert leg.

Item 6 of declarative/cluster-infra.md §1, with metrics-server from item 8
(rfc-007 §7, §8). The VictoriaMetrics stack chart brings the operator, the
database (vmsingle), the scraper (vmagent), the rule evaluator (vmalert),
alertmanager, Grafana, kube-state-metrics and the node exporter.

**It runs on the worker** (`WORKER_AFFINITY`), the node exporter apart, which
runs on every node it reports on. Alertmanager has to: it posts to Home
Assistant at Home Assistant's own address, which only the worker has a route
to (rfc-008 §4.1, operations.md §4). The rest goes with it because the worker
has the memory and the local disk, and the cloud nodes share their cores with
etcd. Grafana is reached by a port-forward until its name moves (rfc-007
§7.4).

**Two stores keep what is scraped** (`stores`): the chart's vmsingle, the
recent store, every series for a month; and the history store, a vmsingle the
component declares, the series that measure what the cluster consumes and how
long its control plane takes, for good, so that every period compares with
every other. vmagent writes the history's class to it through a second
remote write, and vmalert writes its recording rules through vmagent, so they
reach both stores the same way. Grafana reads the history as a datasource of
its own. Each store's volume is `local-path`, on the worker. **Each store is
backed up hourly to the backup bucket and restores itself before it starts**
(`stores`), under the monitoring namespace's writer key on the bucket
(`conventions.sealed.writer_key`), which `k8s-base` holds sealed. While the
stack's configuration holds no ciphertext for it, no SealedSecret is declared
for it, and neither the backup jobs nor the restore are declared: the stores
then start empty and are copied nowhere (physical/bring-up.md §1). Each
backup is a row of the scheduled-job family
(`conventions.schedule.METRICS_BACKUPS`): one older than its period allows
raises `MetricsBackupStale`, and one whose last run failed
`MetricsBackupFailed` (`rules`).

**OCI's block-volume series are pulled into the stores every hour**
(`oci_metrics`): a CronJob runs `oci_pull`, a program of this repository
rendered into a ConfigMap, in the `oci-sdk` image, under the metrics reader's
key, which `k8s-base` holds sealed (`conventions.sealed.OCI_METRICS`). Its
targets are the volumes the census declares, by the OCIDs `physical`
publishes for them. While the stack's configuration holds no ciphertext for
the key, neither the SealedSecret nor the pull is declared. The pull is a row
of the scheduled-job family (`conventions.schedule.OCI_METRICS_PULL`): one older
than its period allows raises `OciMetricsPullStale`, and one whose last run
failed `OciMetricsPullFailed` (`rules`).

**The state backend is checked from here every hour** (`state_backend`): two
CronJobs run `state_backend_check`, a program of this repository rendered into
a ConfigMap, in the same `oci-sdk` image. `state-backend-certificate` reads the
server certificate off a handshake with the box and needs no credential, so it
is always declared. `state-backend-dumps` lists the dump prefix with the
freshness key, which `k8s-base` holds sealed
(`conventions.sealed.B2_FRESHNESS_DUMPS`); while the stack's configuration
holds no ciphertext for it, neither its SealedSecret nor that CronJob is
declared. Each is a row of the scheduled-job family
(`conventions.schedule.STATE_BACKEND_DUMPS`, `STATE_BACKEND_CERTIFICATE`):
`StateBackendDumpsFailed` or `StateBackendCertificateFailed` for a run that
failed, and the `…Stale` alerts for a check that has not passed in an hour
and a half, all naming physical/state-backend.md §6.

**What the chart would install that this cluster does not** is switched off
(`stack_values`): the sync job, a Helm hook that fetches rules and dashboards
off the upstream projects' default branches at install time, so that what a run
installs would depend on the day it ran; the default rules and dashboards that
job would fetch, which have no playbooks here; the scrape jobs for the
controller manager and the scheduler, which Talos binds to 127.0.0.1, and
for etcd, which the chart finds as a pod behind a Service and Talos runs as
a service of the host; and the operator's pre-delete cleanup, a second hook.
`helm.v4.Chart` drops hooks, so each is off rather than left to vanish
(cluster-infra.md §1.2).

**What vmagent scrapes** is held to what the stores keep. Every job is
scraped at one interval, `SCRAPE_INTERVAL` (`kluster.lib.monitoring.jobs`). The chart's kubelet scrapes, the
kubelet's own metrics and cAdvisor's among them, label a series with its
node's name and none of the node's labels (`KUBELET_RELABELING`): copied
onto every series, a node's labels take it past the 40 labels a store
accepts, and the store ignores the whole row. The API server's scrape drops
the buckets of the histogram families no reading needs
(`APISERVER_DROPPED_HISTOGRAMS`). etcd is scraped on its metrics listener on
each control plane by a node scrape of the component's own
(`etcd_scrape_spec`), from the worker, over KubeSpan. A row a store
ignores for any reason raises `MetricsDropped` (`rules`).

**The control planes are read by `kluster.lib.monitoring.readings`**, each
reading defined once: its recording rule and the SLO alerts over it are rule
groups of their own (`rules.READING_GROUPS`), and the `Control planes`
dashboard draws every reading from the history store beside any earlier
period (`dashboard`), a `ConfigMap` the stack's dashboard sidecar hands to
Grafana. The node
exporter's scrape labels each series with its node (`NODE_EXPORTER_ENDPOINT`),
as the operator's node scrapes do the kubelet's and etcd's, since a reading of
a node groups by it.

**Each store's mutating endpoints take keys** (`AUTH_KEYS`): deleting series,
taking snapshots and forcing merges are otherwise open to any pod that
reaches the store's Service. Each key is generated into the stack's state
and reaches each vmsingle as a file of a Secret it mounts, so no rendered
object carries one; the backup jobs read the snapshot key from it.

**The operator's webhook certificate is generated at render time and stays
put** across runs. The provider renders with a server-side dry run, which lets
a chart's `lookup` read the cluster, and the template reuses the Secret it
finds there (`vm-operator.certs`, while `admissionWebhooks.keepTLSSecret`
holds), so only the first install generates it. A render with no cluster
generates a fresh one every time.

**Grafana's admin password is generated by the program**, into its state, and
reaches the chart as a Secret of the component's own (`GRAFANA_ADMIN_SECRET`,
named in `grafana.admin.existingSecret`). The chart's own password is reused
through `lookup` too, but the checksum of it on the Deployment is not: with no
Secret to find, the annotation hashes a second draw rather than the password
the Secret gets, and once the Secret exists it hashes that password spelled
without the quotes the first render wrote, so the Deployment a first install
applies is never the one the next run renders. Given `existingSecret`, the
chart renders neither the Secret nor the annotation (grafana chart 12.7.3,
`templates/secret.yaml` L1 and `templates/deployment.yaml` L44), and nothing of
Grafana's depends on a draw or on what the cluster holds. The password guards
nothing that outlives the pod, since Grafana keeps no volume here, so it is
escrowed nowhere.

**Its memory on the worker is held to nodes.md §4.4's budget** by the requests
of `memory.WORKER_CONTAINERS` charged to monitoring's line of that table, the
stack's containers; every container the component runs for as long as the
stack does takes its memory from there, metrics-server too, which is charged
to the line for the rest of kube-system, and a job's container is no row of
it (`memory`). Grafana is trimmed to fit it. Its datasources are
the chart's own ConfigMap, mounted where Grafana provisions them from at start
(`grafana_datasources_mount`), rather than watched by a second sidecar, a
Python process as large as the dashboards' one: a datasource changes only when
the chart or its values do, and the pod template carries a digest of both
(`DATASOURCES_DIGEST_ENV`), so a change rolls Grafana and it reads them again.
The dashboards' sidecar stays, since dashboards are declared by the components
that own them, under the `grafana_dashboard` label (workloads.md §1). Grafana
installs no plugin at start: by default it downloads a set from grafana.com on
every start, into its pod's disk, so that what runs would depend on the day it
started, and none of them reads a datasource this cluster has.

**The alert leg** is alertmanager's one receiver, Home Assistant's webhook for
alertmanager, an automation apart from the one the operations repository posts
to, because alertmanager's body is fixed (rfc-007 §7.3). Only the webhook's id
is sealed, `conventions.sealed.ALERT_WEBHOOK`; its SealedSecret renders the URL
around it, Home Assistant's own address from `conventions.HOME_ASSISTANT` with
its port and webhook path (`webhook_url`), and the receiver reads that URL
through the operator's `url_secret`, so the id is in no rendered
configuration. While the id is absent from the stack's configuration, no
SealedSecret is declared for it; the operator then skips the receiver's
configuration, and every alert ends at the chart's `blackhole` receiver until
the value is sealed in (`kluster.lib.k8s.sealed_value`,
physical/bring-up.md §1). The rules are `rules.rule_groups`, among them the
cluster's check-in, which the receiver repeats on a short interval, and the
scrape-target family over every job the other alerts read, which reads each
job by the `job` label `scrape_jobs` gives it. Beside them, in a rule object
of its own, is `rules.kubeconfig_copy`, built from the expiry the stack hands
in: the one rule whose expression is not a constant.

**Scrape objects for Cilium** are declared here rather than in Cilium's
component: they are instances of definitions this chart installs, and Cilium
is installed long before it. The chart declares its own for the kubelet, the
API server and the cluster DNS the same way.

The operator's objects -- the receiver, the rules and the scrape objects --
are declared through `operator_objects`, a layer of its own while the SDK
generated for them cannot be imported.

The namespace is `privileged`, for the node exporter (rfc-007 §8).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random

from kluster import conventions
from kluster.components.backup import s3_endpoint
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.components.monitoring import dashboard, memory, oci_metrics, rules, state_backend, stores
from kluster.components.monitoring.operator_objects import operator_object
from kluster.components.monitoring.stores import Store, stack_fullname
from kluster.conventions import backup, schedule
from kluster.conventions.alert import Tier
from kluster.conventions.backup import BackupFamily
from kluster.conventions.outputs import BOOT_VOLUME
from kluster.lib.k8s import LooserLevel, PodSecurity, helm_chart, namespace, sealed_value
from kluster.lib.monitoring.jobs import ETCD_JOB, KUBE_STATE_METRICS_JOB, KUBELET_JOB, SCRAPE_INTERVAL
from kluster.lib.versions import ChartPin, ImagePin
from putils import Component

__all__ = (
    'APISERVER_DROPPED_HISTOGRAMS',
    'AUTH_KEYS',
    'AUTH_KEYS_SECRET',
    'CILIUM_SCRAPES',
    'DATASOURCES_DIGEST_ENV',
    'ETCD_DROPPED',
    'EVALUATION_INTERVAL',
    'GRAFANA_ADMIN_SECRET',
    'GRAFANA_PASSWORD_KEY',
    'GRAFANA_USER_KEY',
    'HEARTBEAT_GROUP_INTERVAL',
    'HISTORY_DATASOURCE',
    'HISTORY_SIZE',
    'KUBELET_RELABELING',
    'KUBE_STATE_METRICS_ENDPOINT',
    'NODE_EXPORTER_ENDPOINT',
    'RECEIVER',
    'SNAPSHOTS_MAX_AGE',
    'WEBHOOK_URL_KEY',
    'WORKER_AFFINITY',
    'CiliumScrape',
    'Monitoring',
    'OciPull',
    'etcd_scrape_spec',
    'grafana_datasources_mount',
    'history_store_spec',
    'metrics_server_values',
    'receiver_spec',
    'scrape_jobs',
    'stack_fullname',
    'stack_values',
    'webhook_url',
)

#: What places a pod on the homelab worker: every node that is not a control
#: plane node. The control plane is the cloud nodes (nodes.md §4.2), Talos
#: labels each node of that type with `CONTROL_PLANE_LABEL` and no other, and
#: the worker is the one node `physical` declares with any other role; the BGP
#: session selects its node the same way (`components.cilium.bgp`).
WORKER_AFFINITY: Mapping[str, Any] = {
    'nodeAffinity': {
        'requiredDuringSchedulingIgnoredDuringExecution': {
            'nodeSelectorTerms': [{'matchExpressions': [{'key': CONTROL_PLANE_LABEL, 'operator': 'DoesNotExist'}]}],
        },
    },
}

#: `WORKER_AFFINITY`, as a pod the component declares itself takes it.
_WORKER_AFFINITY_ARGS = k8s.core.v1.AffinityArgs(
    node_affinity=k8s.core.v1.NodeAffinityArgs(
        required_during_scheduling_ignored_during_execution=k8s.core.v1.NodeSelectorArgs(
            node_selector_terms=[
                k8s.core.v1.NodeSelectorTermArgs(
                    match_expressions=[
                        k8s.core.v1.NodeSelectorRequirementArgs(key=CONTROL_PLANE_LABEL, operator='DoesNotExist')
                    ]
                )
            ]
        )
    )
)

#: alertmanager's one receiver, Home Assistant's webhook for alertmanager.
RECEIVER = 'home-assistant'

#: The Secret Grafana's admin credentials are read from, in the monitoring
#: namespace: a name apart from the one the chart gives a Secret of its own
#: (`monitoring-grafana`), so that replacing one with the other is a create
#: beside a delete rather than two objects under one name.
GRAFANA_ADMIN_SECRET = 'grafana-admin'
#: The keys of that Secret's data, which the chart is told to read.
GRAFANA_USER_KEY = 'admin-user'
GRAFANA_PASSWORD_KEY = 'admin-password'
#: The admin's user name, the chart's own default.
GRAFANA_ADMIN_USER = 'admin'

#: How often alertmanager may send a new notification for the check-in's group,
#: which bounds how late a repeat is: at most one interval after
#: `rules.HEARTBEAT_REPEAT` has passed.
HEARTBEAT_GROUP_INTERVAL = dt.timedelta(minutes=1)

#: How often vmalert evaluates every rule. Stated rather than left to the
#: chart: with `SCRAPE_INTERVAL`, it is how long a CronJob's status takes to
#: reach the scheduled-job family's rules, which `conventions.schedule.REPORT_LAG`
#: leaves room for.
EVALUATION_INTERVAL = dt.timedelta(seconds=20)

#: kube-state-metrics' one scrape endpoint, which the scheduled-job family
#: reads every CronJob's status from. Stated whole, since the chart's list of
#: endpoints is replaced rather than merged: the chart's own endpoint at
#: victoria-metrics-k8s-stack 0.91.2 (`values.yaml`, `kube-state-metrics.vmScrape`),
#: with the two settings the family relies on written out rather than left to
#: the chart's defaults. `honorLabels` keeps the labels kube-state-metrics
#: publishes over the target's, so a CronJob's series carries the CronJob's
#: own namespace rather than the namespace kube-state-metrics runs in, which
#: is the namespace a row's rules select it by. The interval is vmagent's,
#: `SCRAPE_INTERVAL`, which `conventions.schedule.REPORT_LAG` leaves room for.
KUBE_STATE_METRICS_ENDPOINT: Mapping[str, Any] = {
    'port': 'http',
    'interval': backup.duration(SCRAPE_INTERVAL),
    'honorLabels': True,
    'metricRelabelConfigs': [{'action': 'labeldrop', 'regex': '(uid|container_id|image_id)'}],
}

#: The relabeling of the chart's kubelet scrapes, which the chart builds from
#: one spec (`kubelet.vmScrape.spec`): the kubelet's own metrics, cAdvisor's,
#: the probes' and the resource metrics. In place of the chart's, which maps
#: every node label onto every series. A node carries tens of labels,
#: node-feature-discovery's most of them, and a series that takes them all
#: crosses the 40 labels vmsingle accepts (`-maxLabelsPerTimeseries`), which
#: ignores the whole row. The node's name is the one node label a reading
#: groups by, and the operator labels every node scrape's targets with it as
#: `node` already. The other two entries are the chart's, kept.
KUBELET_RELABELING: tuple[Mapping[str, Any], ...] = (
    {'sourceLabels': ['__metrics_path__'], 'targetLabel': 'metrics_path'},
    {'targetLabel': 'job', 'replacement': KUBELET_JOB},
)

#: The node exporter's one scrape endpoint: the chart's, with the name of the
#: node each series is read on written onto it as `node`. The chart scrapes
#: the exporter's pods behind its Service, and the operator labels such a
#: target with its pod and none of its node (`servicescrape.go` L120-175 at
#: operator v0.74.0), while a reading of a node groups by `node`, as it does
#: the kubelet's series and etcd's, which the operator's node scrapes label
#: so. The legacy cluster's node exporter labels its series the same way
#: (cluster/migration.md §2.1), so a reading of a node reads both clusters
#: alike. Stated whole, since Helm replaces a list: the chart's drop of the
#: kubelet's pod mounts is kept (`values.yaml` L2205-2210 at stack chart
#: 0.91.2).
NODE_EXPORTER_ENDPOINT: Mapping[str, Any] = {
    'port': 'metrics',
    'relabelConfigs': [{'sourceLabels': ['__meta_kubernetes_pod_node_name'], 'targetLabel': 'node'}],
    'metricRelabelConfigs': [
        {'action': 'drop', 'sourceLabels': ['mountpoint'], 'regex': '/var/lib/kubelet/pods.+'},
    ],
}

#: The API server's histogram families whose buckets are dropped at scrape:
#: the largest of its series, read by no reading the cluster keeps. Their
#: `_count` and `_sum` stay, so a family's rate and mean are still answered.
APISERVER_DROPPED_HISTOGRAMS: tuple[str, ...] = (
    'etcd_request_duration_seconds',
    'apiserver_request_sli_duration_seconds',
    'apiserver_request_body_size_bytes',
    'apiserver_watch_list_duration_seconds',
    'apiserver_watch_cache_read_wait_seconds',
    'apiserver_watch_events_sizes',
    'apiserver_response_sizes',
)

#: The API server's one scrape endpoint at the chart's values, which the drop
#: list is added to: Helm replaces a list whole, so the endpoint is restated
#: rather than amended.
_APISERVER_ENDPOINT: Mapping[str, Any] = {
    'bearerTokenFile': '/var/run/secrets/kubernetes.io/serviceaccount/token',
    'port': 'https',
    'scheme': 'https',
    'tlsConfig': {'caFile': '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt', 'serverName': 'kubernetes'},
}

#: The families of etcd's metrics listener dropped at scrape, as regular
#: expressions over the metric's name: the gRPC server's per-method series,
#: and etcd's debugging series, which etcd itself names unstable. Together
#: they are most of a member's series.
ETCD_DROPPED: tuple[str, ...] = ('grpc_server_.*', 'etcd_debugging_.*')


def scrape_jobs(release: str, chart: str) -> tuple[rules.ScrapeJob, ...]:
    """The scrape jobs whose series the rules read by name alone, each by the `job` label vmagent gives it.

    The operator's scrape of one of its own objects -- a vmsingle, vmagent or
    alertmanager -- takes the job from the name of the object's Service
    (`servicescrape.go` L209-220 at operator v0.74.0), which is the object's
    `PrefixedName`; the stack names its objects `stack_fullname`, and the
    history store is the component's. kube-state-metrics' is
    `KUBE_STATE_METRICS_JOB`.

    What each exports is the namespace its exporter names its families in:
    `kube_` for kube-state-metrics, the API server's own `kube_apiserver_`
    families apart, and `alertmanager_` and `vmagent_` for those two.
    VictoriaMetrics names every one of its components' series `vm_`, so a
    `vm_` name is claimed by the components whose source registers it: the
    count of rows a limit ignored by the stores' ingestion and by vmagent
    (`lib/timeserieslimits`, which `app/vmagent` and `app/vminsert` import
    and `app/vmalert` does not, at VictoriaMetrics v1.150.0).

    Nothing the program declares holds these claims, since no scrape here
    keeps only what it names: the recent store holds them, and operations.md
    §6.4 gives the queries that read them, which are run again when an
    exporter changes.
    """
    fullname = stack_fullname(release, chart)
    ignored_rows = 'vm_rows_ignored_total'
    return (
        rules.ScrapeJob(KUBE_STATE_METRICS_JOB, 'kube_(?!apiserver_).+'),
        rules.ScrapeJob(f'vmalertmanager-{fullname}', 'alertmanager_.+'),
        rules.ScrapeJob(stores.vmagent_service(release, chart), f'vmagent_.+|{ignored_rows}'),
        *(rules.ScrapeJob(store.workload, ignored_rows) for store in stores.stores(release, chart)),
    )


#: The keys vmsingle's mutating endpoints take, by the flag that sets each:
#: deleting series, taking snapshots, and forcing a merge. A flag left unset
#: leaves its endpoint open to any caller that reaches the store.
AUTH_KEYS: tuple[str, ...] = ('deleteAuthKey', 'snapshotAuthKey', 'forceMergeAuthKey')

#: The Secret holding `AUTH_KEYS`, one key of it per flag, which vmsingle
#: mounts. Named rather than autonamed: the chart's values name it.
AUTH_KEYS_SECRET = 'vmsingle-auth-keys'

#: Where the operator mounts each Secret a VictoriaMetrics object lists in its
#: `secrets`, under the Secret's name.
_SECRETS_DIR = '/etc/vm/secrets'

#: Where Grafana provisions its datasources from at start.
GRAFANA_DATASOURCES_DIR = '/etc/grafana/provisioning/datasources'

#: The environment variable on Grafana's container that carries the digest of
#: what its datasources are rendered from. Grafana reads only `GF_`-prefixed
#: variables as configuration, so this one changes the pod template and
#: nothing else.
DATASOURCES_DIGEST_ENV = 'KLUSTER_DATASOURCES_DIGEST'

#: The history store's claim, a budget line on the worker's disk rather than
#: a limit, which `local-path` does not enforce: the class grows by 3 to 6 GB a
#: year (Aetf/kluster-ops#790), and this is about two years of it.
HISTORY_SIZE = '10Gi'

#: How long a store keeps a snapshot before deleting it: well past any backup
#: run's deadline, so no run loses the snapshot it is copying.
SNAPSHOTS_MAX_AGE = dt.timedelta(days=1)

#: The history store as Grafana's datasource, beside the chart's own for the
#: recent store.
HISTORY_DATASOURCE = 'VictoriaMetrics history'


def grafana_datasources_mount(release: str, chart: str) -> dict[str, object]:
    """Grafana's mount of the stack's datasources ConfigMap, at the directory it provisions datasources from.

    The stack renders the ConfigMap as `<fullname>-grafana-ds`
    (`templates/grafana/datasource.yaml` at 0.91.2), with the sidecar off only
    while `forceDeployDatasource` holds.
    """
    return {
        'name': 'datasources',
        'mountPath': GRAFANA_DATASOURCES_DIR,
        'configMap': f'{stack_fullname(release, chart)}-grafana-ds',
        'readOnly': True,
    }


def _store_keys() -> dict[str, object]:
    """What a vmsingle reads its mutating endpoints' keys from, a file per flag of the mounted Secret, and its snapshots' age.

    A snapshot a backup cut off at its deadline leaves behind holds its parts'
    hard links, and so keeps every part merged away since on disk;
    `-snapshotsMaxAge` deletes it once `SNAPSHOTS_MAX_AGE` has passed.
    """
    return {
        'secrets': [AUTH_KEYS_SECRET],
        'extraArgs': {
            **{flag: f'file://{_SECRETS_DIR}/{AUTH_KEYS_SECRET}/{flag}' for flag in AUTH_KEYS},
            'snapshotsMaxAge': backup.duration(SNAPSHOTS_MAX_AGE),
        },
    }


def _restore(restore: Sequence[Mapping[str, Any]] | None) -> dict[str, object]:
    """A vmsingle's restore: its init containers, and the volumes they mount beside its data; nothing where it has none."""
    if restore is None:
        return {}
    return {'initContainers': [dict(it) for it in restore], 'volumes': stores.restore_volumes()}


def stack_values(
    release: str, chart: ChartPin, *, restore: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, object]:
    """The VictoriaMetrics stack chart's values, installed as `release` from `chart`.

    Placed on the worker, the chart's extras off, Grafana's admin given, every
    container's memory from `memory.WORKER_CONTAINERS`, Grafana trimmed to fit
    it, the recent store's retention and keys, and the history store written
    to and read. `restore` holds the recent store's restore containers, `None`
    while the writer key is not sealed in. The last value is the digest of
    everything else and the chart, on Grafana's container, since the
    datasources it reads at start are rendered from them.
    """
    values = memory.merged(_stack_values(release, chart, restore), memory.chart_values(memory.STACK_CHART))
    rendered_from = json.dumps({'chart': [chart.reference, chart.version], 'values': values}, sort_keys=True)
    digest = hashlib.sha256(rendered_from.encode()).hexdigest()
    return memory.merged(values, {'grafana': {'env': {DATASOURCES_DIGEST_ENV: digest}}})


def _stack_values(release: str, chart: ChartPin, restore: Sequence[Mapping[str, Any]] | None) -> dict[str, object]:
    _, history = stores.stores(release, chart.name)
    return {
        # Fetches rules and dashboards at install time, and is a hook.
        'syncJob': {'enabled': False},
        # What the sync job would fetch. Off on their own as well, so that
        # neither arrives should the job ever run.
        'defaultRules': {'enabled': False},
        'defaultDashboards': {'enabled': False},
        # Talos binds the controller manager and the scheduler to 127.0.0.1,
        # and runs etcd as a service of the host rather than a pod, which the
        # chart's etcd job finds through a Service: each job would scrape
        # nothing and alert on its own absence (rfc-007 §7.1). etcd is
        # scraped by the component's own node scrape (`etcd_scrape_spec`).
        'kubeControllerManager': {'enabled': False},
        'kubeScheduler': {'enabled': False},
        'kubeEtcd': {'enabled': False},
        # Read by every scrape the chart builds from this spec, cAdvisor's among them.
        'kubelet': {'vmScrape': {'spec': {'relabelConfigs': [dict(entry) for entry in KUBELET_RELABELING]}}},
        'prometheus-node-exporter': {'vmScrape': {'spec': {'endpoints': [dict(NODE_EXPORTER_ENDPOINT)]}}},
        'kubeApiServer': {
            'vmScrape': {
                'spec': {
                    'endpoints': [
                        {
                            **_APISERVER_ENDPOINT,
                            'metricRelabelConfigs': [
                                {
                                    'action': 'drop',
                                    'sourceLabels': ['__name__'],
                                    'regex': '|'.join(f'{family}_bucket' for family in APISERVER_DROPPED_HISTOGRAMS),
                                }
                            ],
                        }
                    ],
                },
            },
        },
        'victoria-metrics-operator': {
            # The stack turns the operator chart's pre-delete hook on; a hook
            # is off rather than left to vanish.
            'crds': {'cleanup': {'enabled': False}},
            # The chart's default, stated: it is what keeps the webhook's
            # certificate from being generated afresh on every run.
            'admissionWebhooks': {'keepTLSSecret': True},
            'affinity': WORKER_AFFINITY,
        },
        'vmsingle': {
            'spec': {
                'affinity': WORKER_AFFINITY,
                # Stated rather than left to the cluster's default class: a
                # claim made before any class is the default would be made with
                # none, and never bind.
                'storage': {'storageClassName': conventions.SC_LOCAL_PATH},
                'retentionPeriod': backup.duration(stores.RECENT_RETENTION),
                # Each key read from its file at start, so none is in the
                # object the operator renders the store's arguments from.
                **_store_keys(),
                **_restore(restore),
            },
        },
        'vmagent': {
            'spec': {'affinity': WORKER_AFFINITY, 'scrapeInterval': backup.duration(SCRAPE_INTERVAL)},
            'additionalRemoteWrites': [stores.history_remote_write(history)],
        },
        # The recording rules reach the stores through vmagent, so the history
        # keeps those of its class as it keeps scraped series.
        'vmalert': {
            'spec': {'affinity': WORKER_AFFINITY, 'evaluationInterval': backup.duration(EVALUATION_INTERVAL)},
            'remoteWriteVMAgent': True,
        },
        'defaultDatasources': {
            'extra': [
                {
                    'name': HISTORY_DATASOURCE,
                    'uid': conventions.HISTORY_STORE,
                    'type': 'prometheus',
                    'access': 'proxy',
                    'url': history.url,
                    'isDefault': False,
                }
            ]
        },
        'alertmanager': {
            'spec': {
                'affinity': WORKER_AFFINITY,
                # The receiver's configuration is a VMAlertmanagerConfig, whose
                # route the operator otherwise confines to alerts carrying its
                # own namespace's label; alertmanager here has one receiver for
                # every alert, and the check-in carries no namespace at all.
                'disableNamespaceMatcher': True,
            },
        },
        'grafana': {
            'affinity': WORKER_AFFINITY,
            # The credentials are the component's Secret, so that the chart
            # draws no password and renders no checksum of one.
            'admin': {
                'existingSecret': GRAFANA_ADMIN_SECRET,
                'userKey': GRAFANA_USER_KEY,
                'passwordKey': GRAFANA_PASSWORD_KEY,
            },
            # The datasources are read from the chart's ConfigMap at start,
            # which the chart renders with its sidecar off only when forced.
            'sidecar': {'datasources': {'enabled': False}},
            'forceDeployDatasource': True,
            'extraConfigmapMounts': [grafana_datasources_mount(release, chart.name)],
            'env': {
                'GF_PLUGINS_PREINSTALL_DISABLED': 'true',
                'GOGC': str(memory.GRAFANA_GOGC),
            },
        },
        'kube-state-metrics': {
            'affinity': WORKER_AFFINITY,
            'vmScrape': {'spec': {'endpoints': [dict(KUBE_STATE_METRICS_ENDPOINT)]}},
        },
    }


def metrics_server_values() -> dict[str, object]:
    """metrics-server's values: on the worker, with its memory, and reading each kubelet over the TLS it serves.

    Talos' kubelet serves a self-signed certificate, which no cluster authority
    signs, so metrics-server is told not to verify it; the stack's own kubelet
    scrapes skip the same verification at the chart's defaults. The
    acceptance, its actor and the clause it fails, are recorded in
    item 6 of declarative/cluster-infra.md §1.
    """
    return memory.merged(
        {'args': ['--kubelet-insecure-tls'], 'affinity': WORKER_AFFINITY},
        memory.chart_values(memory.METRICS_SERVER_CHART),
    )


@dataclass(frozen=True)
class CiliumScrape:
    """One of Cilium's workloads, as vmagent finds its pods, and the container ports its metrics are served on."""

    name: str
    selector: Mapping[str, str]
    ports: tuple[str, ...]


#: Cilium's four metrics endpoints, in the pod labels and port names of the
#: pinned chart (Cilium v1.20.4, the agent's and Envoy's DaemonSets and the
#: operator's Deployment, with the metrics the cilium component enables): the
#: agent's own and Hubble's on the agent, Envoy's, and the operator's. Each is
#: on the host network, at a port the node firewall opens to the cluster's own
#: ranges (`components.talos.POD_REACHED_PORTS`).
CILIUM_SCRAPES: tuple[CiliumScrape, ...] = (
    CiliumScrape('cilium-agent', {'k8s-app': 'cilium'}, ('prometheus', 'hubble-metrics')),
    CiliumScrape('cilium-envoy', {'k8s-app': 'cilium-envoy'}, ('envoy-metrics',)),
    CiliumScrape('cilium-operator', {'io.cilium/app': 'operator'}, ('prometheus',)),
)


@dataclass(frozen=True)
class OciPull:
    """What the pull of OCI's block-volume series is declared from, once the metrics reader's key is sealed in."""

    ciphertext: Mapping[str, str]
    """The ciphertext the stack's configuration holds for `conventions.sealed.OCI_METRICS`."""
    volume_ids: pulumi.Output[dict[str, dict[str, str]]]
    """`physical`'s `volume_ids` output, read through `kluster.lib.stack_volumes`."""


class Monitoring(Component, pulumi_type='kluster:monitoring:Monitoring'):
    """The VictoriaMetrics stack and metrics-server, the history store, the rules, the receiver and the scrape objects.

    The scrape objects are Cilium's and etcd's. The stores' keys are a Secret
    beside the stack, declared ahead of it, since each vmsingle mounts it.

    `writer_key` is the ciphertext the stack's configuration holds for the
    namespace's writer key on the backup bucket, `None` while it holds none;
    until it holds one, neither the stores' backups nor their restores are
    declared. `history_image`, `backup_image`, `restore_image` and
    `bucket_image` are the pins the history store, `vmbackup`, `vmrestore`
    and the bucket's client run.

    `after` is what the stack is installed behind: Cilium, and the sealing
    controller, whose chart defines the receiver's SealedSecret. It is stated
    on the children rather than left to the component's own `depends_on`,
    which Pulumi does not push down to a component's children.

    `alert_webhook` is the ciphertext the stack's configuration holds for
    `conventions.sealed.ALERT_WEBHOOK`, `None` while it holds none; the `None`
    is the bring-up's tolerance, and goes with it (physical/bring-up.md §1).

    `kubeconfig_expiry` is the `NotAfter` of the client certificate in the
    stack's copy of the cluster-admin kubeconfig, declassified, from which
    `KubeconfigCopyExpiring` is built (`rules.kubeconfig_copy`).

    `oci_pull` is what the pull of OCI's block-volume series is declared
    from, `None` while the configuration holds no ciphertext for the metrics
    reader's key; until it holds one, the pull is not declared. The `None` is
    the bring-up's tolerance, and goes with it (physical/bring-up.md §1).
    `oci_sdk_image` is the pin it runs, and the pin the state backend's checks
    run.

    `freshness_key` is the ciphertext the stack's configuration holds for
    `conventions.sealed.B2_FRESHNESS_DUMPS`, `None` while it holds none; until
    it holds one, the dump-age check is not declared, and the certificate
    check is declared all the same.
    """

    def __init__(
        self,
        name: str,
        *,
        stack_chart: ChartPin,
        metrics_server_chart: ChartPin,
        after: Sequence[pulumi.Resource],
        alert_webhook: Mapping[str, str] | None,
        kubeconfig_expiry: pulumi.Output[dt.datetime],
        writer_key: Mapping[str, str] | None,
        history_image: ImagePin,
        backup_image: ImagePin,
        restore_image: ImagePin,
        bucket_image: ImagePin,
        oci_pull: OciPull | None,
        oci_sdk_image: ImagePin,
        freshness_key: Mapping[str, str] | None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))
        recent, history = stores.stores(name, stack_chart.name)

        self.namespace = namespace(
            f'{name}-namespace',
            name=conventions.MONITORING_NAMESPACE,
            looser=LooserLevel(
                level=PodSecurity.PRIVILEGED,
                reason="the node exporter reads the host's network, its PID namespace and its paths",
            ),
            opts=behind,
        )
        self.grafana_password = random.RandomPassword(
            f'{name}-grafana-password',
            # The shape the chart itself draws: forty letters and digits.
            length=40,
            special=False,
            opts=behind,
        )
        self.grafana_admin = k8s.core.v1.Secret(
            f'{name}-grafana-admin',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=GRAFANA_ADMIN_SECRET, namespace=self.namespace.metadata.name),
            string_data={GRAFANA_USER_KEY: GRAFANA_ADMIN_USER, GRAFANA_PASSWORD_KEY: self.grafana_password.result},
            opts=self.child_opts(),
        )
        # A caller passes a key in the query string (`authKey=`), so a key
        # holds nothing a URL would need escaped.
        keys = {
            flag: random.RandomPassword(f'{name}-{flag}', length=48, special=False, opts=behind) for flag in AUTH_KEYS
        }
        self.auth_keys = k8s.core.v1.Secret(
            f'{name}-auth-keys',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=AUTH_KEYS_SECRET, namespace=self.namespace.metadata.name),
            string_data={flag: key.result for flag, key in keys.items()},
            opts=self.child_opts(depends_on=[self.namespace]),
        )
        # Behind the sealing controller, whose chart defines the kind, and the
        # namespace the key is sealed to.
        writer = conventions.sealed.writer_key(conventions.MONITORING_NAMESPACE, BackupFamily.VM)
        self.writer_key = sealed_value(
            f'{name}-writer-key',
            writer,
            ciphertext=writer_key,
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )
        backups = (
            _Backups(
                images=stores.Images(backup=str(backup_image), restore=str(restore_image), bucket=str(bucket_image)),
                bucket=stores.Bucket(
                    # The SealedSecret's name is the row's, and so is the Secret's.
                    writer_secret=writer.name,
                    endpoint=s3_endpoint(conventions.B2_ACCOUNT.region),
                    region=conventions.B2_ACCOUNT.region,
                ),
            )
            if self.writer_key is not None
            else None
        )
        self.shell = (
            k8s.core.v1.ConfigMap(
                f'{name}-backup-shell',
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name=stores.SHELL_CONFIGMAP, namespace=self.namespace.metadata.name
                ),
                data=dict(stores.SHELL),
                opts=self.child_opts(depends_on=[self.namespace]),
            )
            if backups is not None
            else None
        )
        # What a store's pod mounts or reads before it starts: the restore's
        # scripts and key, where it has a restore.
        restored = [it for it in (self.shell, self.writer_key) if it is not None]
        self.chart = helm_chart(
            name,
            pin=stack_chart,
            namespace=self.namespace.metadata.name,
            values=stack_values(name, stack_chart, restore=backups.restore(recent) if backups is not None else None),
            # Behind Grafana's Secret, which its pod reads at start, and the
            # stores' keys, which vmsingle mounts and does not start without, and
            # the recent store's restore, where it has one.
            opts=self.child_opts(depends_on=[*after, self.grafana_admin, self.auth_keys, *restored]),
        )
        self.metrics_server = helm_chart(
            f'{name}-metrics-server',
            pin=metrics_server_chart,
            namespace=self.namespace.metadata.name,
            values=metrics_server_values(),
            opts=behind,
        )

        webhook = conventions.sealed.ALERT_WEBHOOK
        (webhook_id,) = webhook.keys
        # Behind the sealing controller, whose chart defines the kind, and
        # the namespace the value is sealed to.
        self.alert_webhook = sealed_value(
            f'{name}-webhook',
            webhook,
            ciphertext=alert_webhook,
            rendered={WEBHOOK_URL_KEY: webhook_url(f'{{{webhook_id}}}')},
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )

        # Instances of the chart's definitions, so behind the chart. Declared
        # through `operator_objects`, while the SDK for them cannot be imported.
        after_the_chart = self.child_opts(depends_on=[self.chart])
        self.receiver = operator_object(
            f'{name}-receiver',
            kind='VMAlertmanagerConfig',
            # The webhook's Secret is read in this object's own namespace.
            metadata=k8s.meta.v1.ObjectMetaArgs(name=RECEIVER, namespace=webhook.namespace),
            spec=receiver_spec(),
            opts=after_the_chart,
        )
        self.rules = operator_object(
            f'{name}-rules',
            kind='VMRule',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=self.namespace.metadata.name),
            spec={'groups': [_rule_group(group) for group in rules.rule_groups(scrape_jobs(name, stack_chart.name))]},
            opts=after_the_chart,
        )
        self.kubeconfig_rules = operator_object(
            f'{name}-kubeconfig-rules',
            kind='VMRule',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=self.namespace.metadata.name),
            spec={'groups': kubeconfig_expiry.apply(lambda expiry: [_rule_group(rules.kubeconfig_copy(expiry))])},
            opts=after_the_chart,
        )
        self.cilium_scrapes = [
            operator_object(
                f'{name}-{scrape.name}',
                kind='VMPodScrape',
                metadata=k8s.meta.v1.ObjectMetaArgs(namespace=self.namespace.metadata.name),
                spec={
                    'selector': {'matchLabels': dict(scrape.selector)},
                    'namespaceSelector': {'matchNames': [conventions.CILIUM_NAMESPACE]},
                    'podMetricsEndpoints': [{'port': port} for port in scrape.ports],
                },
                opts=after_the_chart,
            )
            for scrape in CILIUM_SCRAPES
        ]
        self.etcd_scrape = operator_object(
            f'{name}-etcd',
            kind='VMNodeScrape',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=self.namespace.metadata.name),
            spec=etcd_scrape_spec(),
            opts=after_the_chart,
        )
        self.dashboard = k8s.core.v1.ConfigMap(
            f'{name}-dashboard',
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=dashboard.CONFIGMAP,
                namespace=self.namespace.metadata.name,
                labels=dict([dashboard.SIDECAR_LABEL]),
            ),
            data={dashboard.FILE: json.dumps(dashboard.model(conventions.HISTORY_STORE), indent=1)},
            opts=self.child_opts(depends_on=[self.namespace]),
        )
        self.history = operator_object(
            f'{name}-history',
            kind='VMSingle',
            # Named, since vmagent's remote write and Grafana's datasource
            # address the Service the operator names after it.
            metadata=k8s.meta.v1.ObjectMetaArgs(name=history.vmsingle, namespace=self.namespace.metadata.name),
            spec=history_store_spec(
                image=history_image, restore=backups.restore(history) if backups is not None else None
            ),
            opts=self.child_opts(depends_on=[self.chart, self.auth_keys, *restored]),
        )
        self.backups = (
            [
                k8s.batch.v1.CronJob(
                    f'{name}-{store.backup.cronjob}',
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        name=store.backup.cronjob, namespace=self.namespace.metadata.name
                    ),
                    spec=backups.cronjob(store),
                    opts=self.child_opts(depends_on=[self.chart, self.history, *restored]),
                )
                for store in (recent, history)
            ]
            if backups is not None
            else []
        )

        # Behind the sealing controller, whose chart defines the kind, and the
        # namespace the key is sealed to.
        self.metrics_reader_key = sealed_value(
            f'{name}-oci-metrics',
            conventions.sealed.OCI_METRICS,
            ciphertext=oci_pull.ciphertext if oci_pull is not None else None,
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )
        self.oci_pull_program = None
        self.oci_pull = None
        if oci_pull is not None and self.metrics_reader_key is not None:
            compartment = conventions.OCI_TENANCY.compartments[conventions.PHYSICAL].require()
            importer = stores.vmagent_url(name, stack_chart.name)
            settings = oci_pull.volume_ids.apply(
                lambda ids: oci_metrics.settings(
                    oci_metrics.targets(
                        ids,
                        nodes=conventions.CLOUD_NODES,
                        boot_volume=BOOT_VOLUME,
                        node_volumes=conventions.NODE_VOLUMES,
                    ),
                    compartment=compartment,
                    history=history.url,
                    importer=importer,
                )
            )
            self.oci_pull_program = k8s.core.v1.ConfigMap(
                f'{name}-oci-pull',
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name=oci_metrics.PULL_CONFIGMAP, namespace=self.namespace.metadata.name
                ),
                data={oci_metrics.PROGRAM_FILE: oci_metrics.PROGRAM, oci_metrics.SETTINGS_FILE: settings},
                opts=self.child_opts(depends_on=[self.namespace]),
            )
            # Behind the stores it writes to, and what its pod mounts.
            self.oci_pull = k8s.batch.v1.CronJob(
                f'{name}-{schedule.OCI_METRICS_PULL.cronjob}',
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name=schedule.OCI_METRICS_PULL.cronjob, namespace=self.namespace.metadata.name
                ),
                spec=oci_metrics.cronjob_spec(image=oci_sdk_image, affinity=_WORKER_AFFINITY_ARGS),
                opts=self.child_opts(
                    depends_on=[self.chart, self.history, self.oci_pull_program, self.metrics_reader_key]
                ),
            )

        self.state_backend_checks = k8s.core.v1.ConfigMap(
            f'{name}-state-backend-checks',
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=state_backend.CHECKS_CONFIGMAP, namespace=self.namespace.metadata.name
            ),
            data={
                state_backend.PROGRAM_FILE: state_backend.PROGRAM,
                state_backend.SETTINGS_FILE: state_backend.settings(),
            },
            opts=self.child_opts(depends_on=[self.namespace]),
        )
        # Behind what its pod mounts.
        self.state_backend_certificate = k8s.batch.v1.CronJob(
            f'{name}-{schedule.STATE_BACKEND_CERTIFICATE.cronjob}',
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=schedule.STATE_BACKEND_CERTIFICATE.cronjob, namespace=self.namespace.metadata.name
            ),
            spec=state_backend.certificate_cronjob_spec(image=oci_sdk_image, affinity=_WORKER_AFFINITY_ARGS),
            opts=self.child_opts(depends_on=[self.state_backend_checks]),
        )
        # Behind the sealing controller, whose chart defines the kind, and the
        # namespace the key is sealed to.
        self.freshness_key = sealed_value(
            f'{name}-b2-freshness-dumps',
            conventions.sealed.B2_FRESHNESS_DUMPS,
            ciphertext=freshness_key,
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )
        # Behind what its pod mounts: the program's ConfigMap and the key.
        self.state_backend_dumps = (
            k8s.batch.v1.CronJob(
                f'{name}-{schedule.STATE_BACKEND_DUMPS.cronjob}',
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name=schedule.STATE_BACKEND_DUMPS.cronjob, namespace=self.namespace.metadata.name
                ),
                spec=state_backend.dumps_cronjob_spec(image=oci_sdk_image, affinity=_WORKER_AFFINITY_ARGS),
                opts=self.child_opts(depends_on=[self.state_backend_checks, self.freshness_key]),
            )
            if self.freshness_key is not None
            else None
        )

        self.register_outputs({})


@dataclass(frozen=True)
class _Backups:
    """What the stores' backups and restores are declared from, once the writer key is sealed in."""

    images: stores.Images
    bucket: stores.Bucket

    def restore(self, store: Store) -> list[dict[str, Any]]:
        return stores.restore_containers(store, images=self.images, bucket=self.bucket)

    def cronjob(self, store: Store) -> k8s.batch.v1.CronJobSpecArgs:
        """The store's backup every hour at its minute, one run at a time, cut off before the next is due.

        A failed run is left to the next hour's. A run that hangs is cut off
        at its row's deadline (`Store.backup`), which `Forbid` would otherwise
        let hold every later run back.
        """
        return k8s.batch.v1.CronJobSpecArgs(
            schedule=store.schedule,
            concurrency_policy='Forbid',
            job_template=k8s.batch.v1.JobTemplateSpecArgs(
                spec=k8s.batch.v1.JobSpecArgs(
                    backoff_limit=0,
                    active_deadline_seconds=int(store.backup.deadline.total_seconds()),
                    template=k8s.core.v1.PodTemplateSpecArgs(
                        spec=stores.backup_pod(
                            store,
                            images=self.images,
                            bucket=self.bucket,
                            snapshot_key=(AUTH_KEYS_SECRET, 'snapshotAuthKey'),
                        ),
                    ),
                ),
            ),
        )


def history_store_spec(*, image: ImagePin, restore: Sequence[Mapping[str, Any]] | None) -> dict[str, object]:
    """The history store's vmsingle: its pinned release, for good, on the worker, its own memory and claim, the keys.

    The release is pinned by the component, since the operator otherwise runs
    a vmsingle at its own default rather than at the chart's; a tag carrying
    the digest is pulled as tag and digest (`Image.Reference`,
    `api/operator/v1beta1/vmextra_types.go` L1738-1743 at operator v0.74.0).
    `restore` holds its restore containers, `None` while the writer key is not
    sealed in.
    """
    return {
        'image': {'repository': image.repository, 'tag': f'{image.tag}@{image.digest}'},
        'retentionPeriod': stores.HISTORY_RETENTION,
        'affinity': WORKER_AFFINITY,
        'resources': memory.HISTORY_STORE.memory.resources(),
        'storage': {
            'storageClassName': conventions.SC_LOCAL_PATH,
            'accessModes': ['ReadWriteOnce'],
            'resources': {'requests': {'storage': HISTORY_SIZE}},
        },
        **_store_keys(),
        **_restore(restore),
    }


def etcd_scrape_spec() -> dict[str, object]:
    """The node scrape of etcd's metrics listener: each control plane, on `conventions.ETCD_METRICS_PORT`, over plain HTTP.

    The control planes are the nodes carrying `CONTROL_PLANE_LABEL`, selected
    by its presence: Talos gives it an empty value, and the operator turns a
    `matchLabels` entry into a relabeling that keeps a target whose label
    matches the value, which an absent label, read as empty, does too.
    vmagent runs on the worker (`WORKER_AFFINITY`), so its request reaches a
    control plane over KubeSpan, which the node firewall accepts ahead of
    every rule: the port takes no opening (`components.talos.HOST_OPENINGS`).
    """
    return {
        'selector': {'matchExpressions': [{'key': CONTROL_PLANE_LABEL, 'operator': 'Exists'}]},
        'port': str(conventions.ETCD_METRICS_PORT),
        'path': '/metrics',
        'scheme': 'http',
        'relabelConfigs': [{'targetLabel': 'job', 'replacement': ETCD_JOB}],
        'metricRelabelConfigs': [{'action': 'drop', 'sourceLabels': ['__name__'], 'regex': '|'.join(ETCD_DROPPED)}],
    }


#: The key of the webhook's Secret that holds the URL alertmanager posts to,
#: which the SealedSecret renders around the sealed id.
WEBHOOK_URL_KEY = 'url'


def webhook_url(webhook_id: str) -> str:
    """The URL of Home Assistant's webhook `webhook_id`, at Home Assistant's own address.

    Its one caller passes the format field `'{id}'`, so what it returns there
    is the form `sealed_value` renders: the convention's values spliced
    around that field, which the controller fills with the decrypted id.

    A direct post, at the IPv4 address `conventions.HOME_ASSISTANT` holds --
    the one the gateway's rules for Home Assistant follow too -- rather than at
    its name, which answers with the cluster's media gateway, and over neither
    the cluster's relay nor the overlay (operations.md §4). Only the homelab
    worker reaches it, which is where monitoring runs.
    """
    return (
        f'http://{conventions.HOME_ASSISTANT.v4}:{conventions.HOME_ASSISTANT_PORT}'
        f'{conventions.HOME_ASSISTANT_WEBHOOK_PATH}{webhook_id}'
    )


def receiver_spec() -> dict[str, object]:
    """alertmanager's receiver and route: every alert to Home Assistant's webhook, the check-in on a short repeat.

    The webhook's URL is read from `conventions.sealed.ALERT_WEBHOOK`'s
    Secret (`url_secret`), where its SealedSecret renders it around the
    sealed id, so the id is in no rendered configuration. Resolved
    alerts are not sent: Home Assistant pushes what fires, and a resolution
    closes nothing on the phone. The check-in is repeated well inside the
    timer it restarts: alertmanager sends a repeat on the first group interval
    after the repeat interval has passed.
    """
    webhook = conventions.sealed.ALERT_WEBHOOK
    return {
        'receivers': [
            {
                'name': RECEIVER,
                'webhook_configs': [
                    {'url_secret': {'name': webhook.name, 'key': WEBHOOK_URL_KEY}, 'send_resolved': False},
                ],
            }
        ],
        'route': {
            'receiver': RECEIVER,
            'group_by': ['alertname'],
            'routes': [
                {
                    'receiver': RECEIVER,
                    'matchers': [f'{rules.TIER_LABEL}="{Tier.HEARTBEAT.value}"'],
                    'group_wait': '0s',
                    'group_interval': backup.duration(HEARTBEAT_GROUP_INTERVAL),
                    'repeat_interval': backup.duration(rules.HEARTBEAT_REPEAT),
                }
            ],
        },
    }


def _rule_group(group: rules.RuleGroup) -> dict[str, object]:
    """One rule group as the definition spells it, every alert labeled with its tier and annotated with the rest."""
    return {'name': group.name, 'rules': [_rule(rule) for rule in group.rules]}


def _rule(rule: rules.AlertRule | rules.RecordingRule) -> dict[str, object]:
    if isinstance(rule, rules.RecordingRule):
        return {'record': rule.record, 'expr': rule.expr}
    return {
        'alert': rule.alert,
        'expr': rule.expr,
        **({'for': backup.duration(rule.pending)} if rule.pending is not None else {}),
        'labels': {**rule.labels, rules.TIER_LABEL: rule.tier.value},
        'annotations': {'summary': rule.summary, 'playbook': str(rule.playbook)},
    }
