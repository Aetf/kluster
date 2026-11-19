"""Monitoring, declared against mocks: the stack's settings, its placement, the rules and alertmanager's receiver.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), with what the stack file holds: every sealed value
the stack declares sealed in (rfc-007 §7). The random provider's password is
answered as it would be generated into state.

Literals written here are upstream contracts: the charts' value names, the
label Talos puts on a control plane node, Kubernetes' node-affinity and Pod
Security semantics, and how the VictoriaMetrics operator and alertmanager read
a receiver's route. What the program derives from a convention, a pin or a
census is held to that.
"""

from __future__ import annotations

import base64
import datetime as dt
import importlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import fake_b2_shell
import image_busybox
import process_sessions
import promql_parser
import pulumi
import pytest
import pytest_asyncio
import yaml
from client_kubeconfig import KUBECONFIG, NOT_AFTER
from fake_b2_shell import AUTHORIZATION_V4, BUCKET, FAKE_KEY, FAKE_TOKENS, authorization, fake_env
from fences import prose
from k8s_base_installation import (
    PUBLISHED,
    SEALED,
    VERSIONS_CONFIG,
    Autonaming,
    Physical,
    Run,
    release_assets,
)
from mock_monitor import Recorder, declaring, run_under_backstop
from renovate_text import effective, package_rules, scalar
from section_numbers import sections

from kluster import conventions
from kluster.components.apps.database import Drill, Resources, database
from kluster.components.monitoring import (
    DATASOURCES_DIGEST_ENV,
    RECEIVER,
    WORKER_AFFINITY,
    dashboard,
    memory,
    rules,
    scrape_jobs,
    stack_values,
)
from kluster.components.monitoring.operator_objects import type_token
from kluster.components.monitoring.rules import (
    HEARTBEAT,
    HEARTBEAT_TIMER,
    KUBECONFIG_COPY,
    SCRAPER_DOWN,
    TIER_LABEL,
    ScrapeJob,
    inputs,
    kubeconfig_copy,
    target_down,
)
from kluster.conventions import backup, schedule
from kluster.conventions.alert import Tier
from kluster.conventions.backup import BackupFamily
from kluster.conventions.cluster import KUBECONFIG_ALERT_MARGIN, KUBECONFIG_RENEWAL
from kluster.conventions.outputs import BOOT_VOLUME
from kluster.conventions.providers import OCI_BLOCKSTORE_METRICS
from kluster.lib.k8s import KUBECONFIG_KEY
from kluster.lib.monitoring import readings
from kluster.lib.monitoring.selectors import JOB_LABEL, Selector, selectors
from kluster.lib.state_backend import settings as backend_settings
from kluster.lib.versions import ChartPin, ImagePin, versions
from putils import Component

ROOT = Path(__file__).resolve().parents[1]

CHART = 'kubernetes:helm.sh/v4:Chart'
NAMESPACE = 'kubernetes:core/v1:Namespace'
SECRET = 'kubernetes:core/v1:Secret'
PASSWORD = 'random:index/randomPassword:RandomPassword'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
VM_RULE = type_token('VMRule')
VM_ALERTMANAGER_CONFIG = type_token('VMAlertmanagerConfig')
VM_POD_SCRAPE = type_token('VMPodScrape')
VM_NODE_SCRAPE = type_token('VMNodeScrape')
VM_SINGLE = type_token('VMSingle')
CRONJOB = 'kubernetes:batch/v1:CronJob'
CONFIGMAP = 'kubernetes:core/v1:ConfigMap'

#: Every kind of the operator's the component declares.
OPERATOR_KINDS = (VM_RULE, VM_ALERTMANAGER_CONFIG, VM_POD_SCRAPE, VM_NODE_SCRAPE, VM_SINGLE)

#: The logical names of the component's two charts.
STACK = 'monitoring'
METRICS_SERVER = 'monitoring-metrics-server'

WEBHOOK = conventions.sealed.ALERT_WEBHOOK
WRITER_KEY = conventions.sealed.writer_key(conventions.MONITORING_NAMESPACE, BackupFamily.VM)

#: The label Talos puts on every control plane node and on no other
#: (`node_label_spec.go` L83-85 at Talos v1.13.9).
TALOS_CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'
#: The label the kubelet gives every node, its own name.
HOSTNAME_LABEL = 'kubernetes.io/hostname'

#: How a secret value crosses to the engine: wrapped, under the signature key,
#: with the secret signature beside it (`pulumi/runtime/rpc.py` L65 and L83).
SECRET_ENVELOPE = {'4dabf18193072939515e22adb298388d': '1b47061264138c4ac30d75fd1eb44270'}

#: A playbook as the payload carries it: a document from the repository's root,
#: and a section (`conventions.alert.FIELDS`).
PLAYBOOK = re.compile(r'^(?P<document>\S+\.md) §(?P<section>\d+(?:\.\d+)*)$')


class Generating(Autonaming):
    """`Autonaming`, answering a password's `result` as the random provider would: a value of its own per resource."""

    @staticmethod
    def generated(name: str) -> str:
        """What the random provider generates for the password of this name, as it would into state."""
        return f'generated-in-state-for-{name}'

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        outputs = super().computed(args)
        return outputs | {'result': self.generated(args.name)} if args.typ == PASSWORD else outputs


async def declare() -> Run:
    """The whole program with the stack file's sealed values."""
    from kluster.stacks import k8s_base

    config = {
        f'kluster:{KUBECONFIG_KEY}': KUBECONFIG,
        f'kluster:{conventions.sealed.CONFIG_KEY}': json.dumps(SEALED),
    }
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=[f'kluster:{KUBECONFIG_KEY}'])
    monitor = await run_under_backstop(Generating(), stack=conventions.STACK_NAMES.k8s_base)
    with release_assets() as fetched:
        async with declaring():
            await k8s_base.main()
    return Run(monitor, fetched)


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The stack as its file holds it: every sealed value it declares sealed in, the webhook among them."""
    return await declare()


def _values(run: Run) -> dict[str, Any]:
    return run.values(STACK)


def _declared(run: Run) -> list[dict[str, Any]]:
    return _rules(run.monitor)


def _rules(monitor: Recorder) -> list[dict[str, Any]]:
    """Every rule a run's `VMRule`s declare, in whatever namespace."""
    return [
        rule
        for declared in monitor.of_type(VM_RULE)
        for group in declared.inputs['spec']['groups']
        for rule in group['rules']
    ]


def _alert_rules(monitor: Recorder) -> list[dict[str, Any]]:
    return [rule for rule in _rules(monitor) if 'alert' in rule]


def _declared_rules(run: Run) -> list[dict[str, Any]]:
    """Every alerting rule the program declares."""
    return [rule for rule in _declared(run) if 'alert' in rule]


def _declared_records(run: Run) -> dict[str, str]:
    """Every recording rule the program declares: the series it writes, and its expression."""
    return {rule['record']: rule['expr'] for rule in _declared(run) if 'record' in rule}


def _receiver(run: Run) -> dict[str, Any]:
    (declared,) = run.monitor.of_type(VM_ALERTMANAGER_CONFIG)
    return declared.inputs


# -- The installation -------------------------------------------------------------


@pytest.mark.parametrize('chart', [STACK, METRICS_SERVER])
def test_each_chart_is_installed_from_its_pin(applied: Run, chart: str) -> None:
    """The reference and the version are the pin's; an HTTP repository's address is passed beside the name."""
    pin = versions.chart[{STACK: 'victoria-metrics-k8s-stack', METRICS_SERVER: 'metrics-server'}[chart]]
    inputs = applied.inputs(CHART, chart)

    assert (inputs['chart'], inputs['version']) == (pin.reference, pin.version)
    assert inputs.get('repositoryOpts', {}).get('repo') == (None if pin.oci else pin.repository)


@pytest.mark.parametrize(
    'path',
    [
        ('syncJob', 'enabled'),
        ('defaultRules', 'enabled'),
        ('defaultDashboards', 'enabled'),
        ('kubeControllerManager', 'enabled'),
        ('kubeScheduler', 'enabled'),
        ('kubeEtcd', 'enabled'),
        ('victoria-metrics-operator', 'crds', 'cleanup', 'enabled'),
    ],
    ids='.'.join,
)
def test_what_the_chart_would_fetch_hook_or_scrape_for_nothing_is_off(applied: Run, path: tuple[str, ...]) -> None:
    """The sync job and the operator's cleanup are hooks, the defaults are downloads, and the three jobs scrape nothing.

    Each is on at the chart's own values (rfc-007 §7.1, cluster-infra.md §1.2),
    so each has to be stated off.
    """
    value: Any = _values(applied)
    for key in path:
        value = value[key]

    assert value is False


def test_the_namespace_is_privileged_for_the_node_exporter(applied: Run) -> None:
    """Talos enforces `baseline`, which refuses the node exporter's host network, PID namespace and paths."""
    namespaces = applied.monitor.by_name(NAMESPACE)

    (labels,) = [
        ns['metadata']['labels']
        for ns in namespaces.values()
        if ns['metadata']['name'] == conventions.MONITORING_NAMESPACE
    ]
    assert labels['pod-security.kubernetes.io/enforce'] == 'privileged'
    assert applied.inputs(CHART, STACK)['namespace'] == conventions.MONITORING_NAMESPACE


def _admits(node: Mapping[str, str], selector: Mapping[str, str], affinity: Mapping[str, Any]) -> bool:
    """Whether a pod with this `nodeSelector` and `affinity` may be scheduled on a node with these labels.

    Kubernetes' rule for the required part: every `nodeSelector` pair matches,
    and at least one `nodeSelectorTerm` has every expression hold.
    """
    if any(node.get(key) != value for key, value in selector.items()):
        return False
    required = affinity.get('nodeAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution')
    if required is None:
        return True

    def holds(expression: Mapping[str, Any]) -> bool:
        key, operator, values = expression['key'], expression['operator'], expression.get('values', [])
        return {
            'In': lambda: node.get(key) in values,
            'NotIn': lambda: node.get(key) not in values,
            'Exists': lambda: key in node,
            'DoesNotExist': lambda: key not in node,
        }[operator]()

    return any(all(holds(e) for e in term.get('matchExpressions', [])) for term in required['nodeSelectorTerms'])


def _talos_nodes() -> dict[str, dict[str, str]]:
    """The cluster's nodes, labeled as Talos labels them: every cloud node is a control plane node, the worker is not."""
    return {
        **{node: {HOSTNAME_LABEL: node, TALOS_CONTROL_PLANE_LABEL: ''} for node in conventions.CLOUD_NODES},
        conventions.HOMELAB_NODE: {HOSTNAME_LABEL: conventions.HOMELAB_NODE},
    }


@pytest.mark.parametrize('workload', ['alertmanager', 'vmagent'])
def test_the_workload_can_schedule_on_the_worker_alone(applied: Run, workload: str) -> None:
    """Each holds a condition that only the worker meets.

    Alertmanager posts to Home Assistant at an address only the worker
    reaches (rfc-008 §4.1). vmagent reads etcd's metrics listener on every
    control plane, which the node firewall admits from KubeSpan alone; from a
    control plane, its call to its own node's listener would arrive from a
    pod address, which no opening admits (`components.talos.HOST_OPENINGS`).
    """
    spec = _values(applied)[workload]['spec']

    schedulable = {
        name
        for name, labels in _talos_nodes().items()
        if _admits(labels, spec.get('nodeSelector', {}), spec.get('affinity', {}))
    }

    assert schedulable == {conventions.HOMELAB_NODE}


# -- Grafana's admin -----------------------------------------------------------------


def _grafana_admin(run: Run) -> tuple[dict[str, Any], dict[str, Any]]:
    """The chart's `grafana.admin` values, and the Secret declared under the name they give."""
    admin = _values(run)['grafana']['admin']
    (secret,) = [
        it.inputs for it in run.monitor.of_type(SECRET) if it.inputs['metadata']['name'] == admin['existingSecret']
    ]
    return admin, secret


def test_grafanas_admin_is_a_secret_the_program_declares_with_a_password_generated_in_state(
    applied: Run,
) -> None:
    """The chart is handed Grafana's credentials, so no render draws a password or hashes one.

    Left to its own, the grafana chart draws the password at render time and
    reuses its Secret through `lookup`, but the Deployment's `checksum/secret`
    hashes a second draw on a first install and the found Secret afterward
    (`templates/_helpers.tpl` L131-138, `templates/_config.tpl` L172-184 at
    grafana chart 12.7.3), so the first `up` applies a Deployment no later run
    renders. Given `admin.existingSecret` it renders neither the Secret nor the
    annotation (`templates/secret.yaml` L1, `templates/deployment.yaml` L44),
    and its pod reads the two keys `userKey` and `passwordKey` name from that
    Secret, in the release's namespace (`templates/_pod.tpl` L1420-1433).
    """
    admin, secret = _grafana_admin(applied)
    data = secret['stringData']

    assert secret['metadata']['namespace'] == applied.inputs(CHART, STACK)['namespace']
    # The password reaches the engine as a secret, and so state as one.
    assert {key: data[key] for key in SECRET_ENVELOPE} == SECRET_ENVELOPE
    assert admin['userKey'] in data['value']
    assert data['value'][admin['passwordKey']] in {
        Generating.generated(it.name) for it in applied.monitor.of_type(PASSWORD)
    }


def test_the_chart_is_installed_behind_grafanas_secret(applied: Run) -> None:
    """Grafana's pod reads the Secret when it starts, and pulumi-kubernetes waits for the pod to be ready."""
    admin, _ = _grafana_admin(applied)
    (declared,) = [
        it for it in applied.monitor.of_type(SECRET) if it.inputs['metadata']['name'] == admin['existingSecret']
    ]

    assert applied.urn(SECRET, declared.name) in applied.dependencies(CHART, STACK)


# -- What vmagent scrapes ------------------------------------------------------------

#: The number of labels vmsingle accepts on a series, `__name__` counted, when
#: `-maxLabelsPerTimeseries` is not set; a row with more is ignored whole
#: (`lib/timeserieslimits/timeseries_limits.go` L30, L86-95 at VictoriaMetrics
#: v1.150.0).
STORE_LABEL_LIMIT = 40

#: The kubelet scrapes at the stack chart's own values, which the component's
#: values are merged over: one spec every scrape starts from, and each scrape's
#: own (`values.yaml` L2231-2281 at victoria-metrics-k8s-stack 0.91.2).
CHART_KUBELET: Mapping[str, Any] = {
    'vmScrapes': {
        'cadvisor': {'enabled': True, 'spec': {'path': '/metrics/cadvisor'}},
        'probes': {'enabled': True, 'spec': {'path': '/metrics/probes'}},
        'resources': {'enabled': True, 'spec': {'path': '/metrics/resource'}},
        'kubelet': {'spec': {}},
    },
    'vmScrape': {
        'kind': 'VMNodeScrape',
        'spec': {
            'scheme': 'https',
            'interval': '30s',
            'metricRelabelConfigs': [
                {'action': 'labeldrop', 'regex': '(uid)'},
                {'action': 'labeldrop', 'regex': '(id|name)'},
            ],
            'relabelConfigs': [
                {'action': 'labelmap', 'regex': '__meta_kubernetes_node_label_(.+)'},
                {'sourceLabels': ['__metrics_path__'], 'targetLabel': 'metrics_path'},
                {'targetLabel': 'job', 'replacement': 'kubelet'},
            ],
        },
    },
}

#: The interval vmagent scrapes at where nothing sets one, at the stack
#: chart's own values (`values.yaml`, `vmagent.spec.scrapeInterval`, at
#: 0.91.2).
CHART_SCRAPE_INTERVAL = '20s'

#: The API server's scrape endpoint at the stack chart's own values
#: (`values.yaml` L2289-2297 at 0.91.2).
CHART_APISERVER_ENDPOINT: Mapping[str, Any] = {
    'bearerTokenFile': '/var/run/secrets/kubernetes.io/serviceaccount/token',
    'port': 'https',
    'scheme': 'https',
    'tlsConfig': {'caFile': '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt', 'serverName': 'kubernetes'},
}

#: A cAdvisor series as the kubelet serves it, before any relabeling.
CADVISOR_SAMPLE = {
    '__name__': 'container_cpu_usage_seconds_total',
    'container': 'app',
    'cpu': 'total',
    'id': '/kubepods/burstable/pod0/0123',
    'image': 'docker.io/library/app:1',
    'name': '0123',
    'namespace': 'apps',
    'pod': 'app-0',
}


def _helm_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Values merged as Helm merges them, and as the chart's `mergeOverwrite` does: maps key by key, anything else replaced."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _helm_merge(cast('Mapping[str, Any]', merged[key]), cast('Mapping[str, Any]', value))
        else:
            merged[key] = value
    return merged


def _kubelet_scrapes(run: Run) -> dict[str, dict[str, Any]]:
    """Each kubelet scrape's spec as the chart renders it: the shared spec, each scrape's own merged over it."""
    kubelet = _helm_merge(CHART_KUBELET, _values(run).get('kubelet', {}))
    return {
        name: _helm_merge(kubelet['vmScrape']['spec'], scrape.get('spec', {}))
        for name, scrape in kubelet['vmScrapes'].items()
        if scrape.get('enabled', True)
    }


def _sanitized(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', name)


def _expand(match: re.Match[str], replacement: str) -> str:
    return re.sub(r'\$\{?(\d+)\}?', lambda ref: match.group(int(ref[1])) or '', replacement)


def _relabel(labels: Mapping[str, str], configs: list[Mapping[str, Any]]) -> dict[str, str] | None:
    """Labels after a relabeling, `None` where it drops them: Prometheus' semantics, which vmagent follows.

    Each regular expression is anchored at both ends; a source label that is
    absent reads as empty; a replacement that comes out empty removes the label.
    """
    out = dict(labels)
    for config in configs:
        action = config.get('action', 'replace')
        regex: re.Pattern[str] = re.compile(config.get('regex', '(.*)'))
        sources: list[str] = config.get('sourceLabels', config.get('source_labels', []))
        value = str(config.get('separator', ';')).join(out.get(name, '') for name in sources)
        if action == 'replace':
            match = regex.fullmatch(value)
            if match:
                target = config.get('targetLabel', config.get('target_label'))
                replaced = _expand(match, config.get('replacement', '$1'))
                if replaced:
                    out[target] = replaced
                else:
                    out.pop(target, None)
        elif action == 'keep':
            if not regex.fullmatch(value):
                return None
        elif action == 'drop':
            if regex.fullmatch(value):
                return None
        elif action == 'labelmap':
            for name, label in list(out.items()):
                match = regex.fullmatch(name)
                if match:
                    out[_expand(match, config.get('replacement', '$1'))] = label
        elif action == 'labeldrop':
            out = {name: label for name, label in out.items() if not regex.fullmatch(name)}
        else:
            raise AssertionError(f'no relabeling action {action!r} in this model')
    return out


def _selector_relabeling(selector: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """A node scrape's selector as the operator turns it into relabeling (`vmscrapes.go` L426-475 at operator v0.74.0)."""
    configs: list[Mapping[str, Any]] = [
        {'action': 'keep', 'sourceLabels': [f'__meta_kubernetes_node_label_{_sanitized(key)}'], 'regex': value}
        for key, value in sorted(selector.get('matchLabels', {}).items())
    ]
    for expression in selector.get('matchExpressions', []):
        label = _sanitized(expression['key'])
        configs.append(
            {
                'In': {'action': 'keep', 'sourceLabels': [f'__meta_kubernetes_node_label_{label}']},
                'NotIn': {'action': 'drop', 'sourceLabels': [f'__meta_kubernetes_node_label_{label}']},
                'Exists': {'action': 'keep', 'sourceLabels': [f'__meta_kubernetes_node_labelpresent_{label}']},
                'DoesNotExist': {'action': 'drop', 'sourceLabels': [f'__meta_kubernetes_node_labelpresent_{label}']},
            }[expression['operator']]
            | {'regex': '|'.join(expression.get('values', [])) or 'true'}
        )
    return configs


def _scraped(
    spec: Mapping[str, Any], scrape_name: str, node: str, node_labels: Mapping[str, str], sample: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str] | None] | None:
    """What a node scrape makes of one node: its target, and one sample's series; `None` where the node is not scraped.

    The relabeling the operator writes ahead of the spec's own is its selector,
    `node` from the node's name, and a `job` from the object's namespace and
    name, and the port where the spec names one (`nodescrape.go` L53-97 at
    operator v0.74.0). A label of the target's that starts with `__` is gone
    once relabeling ends, `instance` defaulting to the address. With
    `honorLabels`, the sample's own labels win over the target's.
    """
    target = {
        '__address__': f'{node}.internal:10250',
        '__metrics_path__': spec.get('path', '/metrics'),
        '__scheme__': spec.get('scheme', 'http'),
        '__meta_kubernetes_node_name': node,
        **{f'__meta_kubernetes_node_label_{_sanitized(k)}': v for k, v in node_labels.items()},
        **{f'__meta_kubernetes_node_labelpresent_{_sanitized(k)}': 'true' for k in node_labels},
    }
    operator: list[Mapping[str, Any]] = [
        *_selector_relabeling(spec.get('selector', {})),
        {'sourceLabels': ['__meta_kubernetes_node_name'], 'targetLabel': 'node'},
        {'targetLabel': 'job', 'replacement': f'{conventions.MONITORING_NAMESPACE}/{scrape_name}'},
    ]
    if spec.get('port'):
        operator.append(
            {
                'sourceLabels': ['__address__'],
                'targetLabel': '__address__',
                'regex': '^(.*):(.*)',
                'replacement': f'${{1}}:{spec["port"]}',
            }
        )
    relabeled = _relabel(target, [*operator, *spec.get('relabelConfigs', [])])
    if relabeled is None:
        return None
    relabeled.setdefault('instance', relabeled['__address__'])
    kept = {name: value for name, value in relabeled.items() if not name.startswith('__')}
    series = {**kept, **sample} if spec.get('honorLabels') else {**sample, **kept}
    return relabeled, _relabel(series, spec.get('metricRelabelConfigs', []))


def test_no_kubelet_scrape_maps_the_nodes_labels_onto_its_series(applied: Run) -> None:
    """A node's labels are many and grow with what runs there; copied onto every series, they cost the series."""
    node_label = '__meta_kubernetes_node_label_feature_node_kubernetes_io_cpu_model_vendor_id'
    scrapes = _kubelet_scrapes(applied)

    assert {'kubelet', 'cadvisor'} <= set(scrapes)
    for name, spec in scrapes.items():
        for config in spec.get('relabelConfigs', []):
            mapped = config.get('action') == 'labelmap' and re.fullmatch(config.get('regex', '(.*)'), node_label)
            assert not mapped, f'{name} maps every node label: {config}'


def test_a_series_from_a_node_with_more_labels_than_the_store_accepts_is_stored_labeled_by_its_node(
    applied: Run,
) -> None:
    """Every kubelet scrape of a node carrying twice the store's limit in labels yields a series the store keeps.

    The series still names its node, the one node label a reading groups by.
    """
    limit = int(
        _values(applied)['vmsingle']['spec'].get('extraArgs', {}).get('maxLabelsPerTimeseries', STORE_LABEL_LIMIT)
    )
    node = conventions.HOMELAB_NODE
    labels = {
        HOSTNAME_LABEL: node,
        **{f'feature.node.kubernetes.io/feature-{i}': 'true' for i in range(2 * STORE_LABEL_LIMIT)},
    }

    for name, spec in _kubelet_scrapes(applied).items():
        scraped = _scraped({**spec, 'selector': {}}, name, node, labels, CADVISOR_SAMPLE)
        assert scraped is not None, name
        _, series = scraped
        assert series is not None, name
        assert len(series) <= limit, f'{name}: {len(series)} labels'
        assert series['node'] == node, name


def test_etcd_is_scraped_on_every_control_plane_and_nowhere_else(applied: Run) -> None:
    """On its metrics listener, over plain HTTP, under one `job` whatever name the object is given.

    Talos gives the control plane label an empty value, and the worker none: a
    selector the operator reads as "the label's value is empty" keeps both.
    """
    (scrape,) = applied.monitor.of_type(VM_NODE_SCRAPE)
    spec = scrape.inputs['spec']
    sample = {'__name__': 'etcd_disk_wal_fsync_duration_seconds_bucket', 'le': '0.008'}

    scraped = {node: _scraped(spec, scrape.name, node, labels, sample) for node, labels in _talos_nodes().items()}
    targets = {node: it for node, it in scraped.items() if it is not None}

    assert set(targets) == set(conventions.CLOUD_NODES)
    jobs: set[str] = set()
    for node, (target, series) in targets.items():
        assert target['__address__'].rsplit(':', 1)[1] == str(conventions.ETCD_METRICS_PORT), node
        assert (target['__scheme__'], target['__metrics_path__']) == ('http', '/metrics'), node
        assert series is not None, node
        assert series['node'] == node
        jobs.add(series['job'])
    (control_plane, *_) = conventions.CLOUD_NODES
    renamed = _scraped(spec, 'another-name', control_plane, _talos_nodes()[control_plane], sample)
    assert renamed is not None
    assert renamed[1] is not None
    assert jobs == {renamed[1]['job']}, "the job follows the object's name"


@pytest.mark.parametrize(
    ('metric', 'kept'),
    [
        ('etcd_disk_wal_fsync_duration_seconds_bucket', True),
        ('etcd_disk_backend_commit_duration_seconds_bucket', True),
        ('etcd_server_is_leader', True),
        ('process_resident_memory_bytes', True),
        ('process_cpu_seconds_total', True),
        ('grpc_server_handled_total', False),
        ('grpc_server_started_total', False),
        ('etcd_debugging_mvcc_keys_total', False),
    ],
)
def test_etcds_scrape_drops_the_grpc_and_debugging_families_and_keeps_the_rest(
    applied: Run, metric: str, kept: bool
) -> None:
    """etcd's per-method gRPC series and its debugging series are most of a member's; what a reading reads stays."""
    (scrape,) = applied.monitor.of_type(VM_NODE_SCRAPE)

    series = _relabel({'__name__': metric}, scrape.inputs['spec'].get('metricRelabelConfigs', []))

    assert (series is not None) is kept


def test_the_api_servers_dropped_histograms_lose_their_buckets_and_keep_their_count_and_sum(
    applied: Run,
) -> None:
    """The buckets are the series; the count and the sum still answer a rate and a mean, and the endpoint is the chart's."""
    from kluster.components.monitoring import APISERVER_DROPPED_HISTOGRAMS

    (endpoint,) = _values(applied)['kubeApiServer']['vmScrape']['spec']['endpoints']
    configs = endpoint.get('metricRelabelConfigs', [])

    assert {key: endpoint.get(key) for key in CHART_APISERVER_ENDPOINT} == dict(CHART_APISERVER_ENDPOINT)
    assert APISERVER_DROPPED_HISTOGRAMS
    for family in APISERVER_DROPPED_HISTOGRAMS:
        assert _relabel({'__name__': f'{family}_bucket', 'le': '1'}, configs) is None, family
        for kept in ('_count', '_sum'):
            assert _relabel({'__name__': f'{family}{kept}'}, configs) is not None, family + kept
    for other in (
        'apiserver_request_total',
        'apiserver_current_inflight_requests',
        'apiserver_request_duration_seconds_bucket',
    ):
        assert _relabel({'__name__': other}, configs) is not None, other


def test_every_scrape_runs_at_vmagents_one_interval(applied: Run) -> None:
    """A rate over a window then reads the same number of samples from every source."""
    interval = _values(applied)['vmagent']['spec'].get('scrapeInterval', CHART_SCRAPE_INTERVAL)
    declared = [it.inputs['spec'] for typ in (VM_NODE_SCRAPE, VM_POD_SCRAPE) for it in applied.monitor.of_type(typ)]
    intervals = {
        **{f'kubelet {name}': spec.get('interval') for name, spec in _kubelet_scrapes(applied).items()},
        **{f'declared {i}': spec.get('interval') for i, spec in enumerate(declared)},
        **{
            f'declared {i} endpoint': endpoint.get('interval')
            for i, spec in enumerate(declared)
            for endpoint in spec.get('podMetricsEndpoints', [])
        },
    }

    assert {name: it or interval for name, it in intervals.items()} == dict.fromkeys(intervals, interval)


#: The flags of vmsingle's endpoints that delete or copy what it stores: series
#: deletion, snapshots, and forced merges (`app/vmselect/main.go` L34 and
#: `app/vmstorage/main.go` L44-45 at VictoriaMetrics v1.150.0). Each takes a
#: file's contents where its value is `file://<path>` (`lib/flagutil/password.go`
#: L21-23, L96-97).
MUTATING_ENDPOINT_FLAGS = ('deleteAuthKey', 'snapshotAuthKey', 'forceMergeAuthKey')

#: Where the operator mounts each Secret a VictoriaMetrics object lists in its
#: `secrets` (`docs/api.md` at operator v0.74.0).
OPERATOR_SECRETS_DIR = '/etc/vm/secrets'


def test_each_mutating_endpoint_of_the_store_takes_a_generated_key_no_rendered_object_carries(
    applied: Run,
) -> None:
    """Each flag names a file of a Secret vmsingle mounts, holding a password of its own generated into state.

    The chart is installed behind the Secret, since vmsingle does not start
    without what it mounts.
    """
    spec = _values(applied)['vmsingle']['spec']
    mounted = set(spec.get('secrets', []))
    secrets = {
        (it.inputs['metadata']['namespace'], it.inputs['metadata']['name']): it
        for it in applied.monitor.of_type(SECRET)
    }
    generated = {Generating.generated(it.name) for it in applied.monitor.of_type(PASSWORD)}
    keys: list[str] = []

    for flag in MUTATING_ENDPOINT_FLAGS:
        path = spec.get('extraArgs', {}).get(flag, '')
        match = re.fullmatch(rf'file://{OPERATOR_SECRETS_DIR}/([^/]+)/([^/]+)', path)
        assert match, f'{flag}: {path!r} reads no mounted file'
        secret_name, key = match.groups()
        assert secret_name in mounted, flag
        secret = secrets[(conventions.MONITORING_NAMESPACE, secret_name)]
        # The SDK marks a Secret's `stringData` secret whatever it is given.
        value = secret.inputs['stringData']['value'][key]
        assert value in generated, flag
        keys.append(value)
        assert applied.urn(SECRET, secret.name) in applied.dependencies(CHART, STACK)
    assert len(set(keys)) == len(keys), 'two endpoints share a key'


# -- Memory on the worker -----------------------------------------------------------

#: The logical name of the chart each pin installs, as the program declares it.
INSTALLED_AS = {memory.STACK_CHART: STACK, memory.METRICS_SERVER_CHART: METRICS_SERVER}

#: The datasources ConfigMap the pinned stack chart renders for the release
#: `monitoring` (`templates/grafana/datasource.yaml` at 0.91.2), the name it
#: has on the cluster.
STACK_DATASOURCES = 'monitoring-victoria-metrics-k8s-stack-grafana-ds'

#: The directory Grafana provisions datasources from at start, which the
#: chart's own datasource sidecar writes to (`templates/_pod.tpl` L992 at
#: grafana chart 12.7.3).
GRAFANA_DATASOURCES_DIR = '/etc/grafana/provisioning/datasources'


def _mib(quantity: str) -> int:
    """A memory quantity in the one unit the program writes, `Mi`."""
    match = re.fullmatch(r'(\d+)Mi', quantity)
    assert match, f'{quantity!r} is not in Mi'
    return int(match[1])


def _installed_memory(
    run: Run, containers: tuple[memory.Container, ...] = memory.WORKER_CONTAINERS
) -> dict[str, Mapping[str, Any]]:
    """Each container's `resources`, read where its chart reads them in the values the program installs.

    A container of an object the component declares itself, the history
    store, is read in that object's declared inputs.
    """
    found: dict[str, Mapping[str, Any]] = {}
    for container in containers:
        value: Any = run.values(INSTALLED_AS[container.chart]) if container.chart is not None else _history(run)
        for key in container.path:
            value = value[key]
        found[container.name] = value
    return found


def test_the_memory_requests_on_the_worker_fit_monitorings_budget(applied: Run) -> None:
    """What the scheduler reserves for monitoring on the worker is within nodes.md §4.4's budget, rfc-008 §6.1's request.

    The requests are those of the containers charged to monitoring's line of
    nodes.md §4.4, read from the values each chart is installed with, at the
    path that chart reads each container's memory from. metrics-server is
    charged to the line for the rest of kube-system.
    """
    charged = memory.charged_to(memory.Line.MONITORING)
    requested = {name: _mib(it['requests']['memory']) for name, it in _installed_memory(applied, charged).items()}

    assert sum(requested.values()) <= memory.BUDGET_MIB, requested


def test_the_history_stores_request_is_its_own_line_beside_monitorings(applied: Run) -> None:
    """The history store is charged to a line of its own, as the operator ruled (Aetf/kluster-ops#790, option A).

    Its request is read where the declared store reads it, so monitoring's
    worker memory is the two lines together: the stack's containers within
    `BUDGET_MIB`, the history store within `HISTORY_BUDGET_MIB`.
    """
    charged = memory.charged_to(memory.Line.HISTORY)
    requested = {name: _mib(it['requests']['memory']) for name, it in _installed_memory(applied, charged).items()}
    monitoring = {
        name: _mib(it['requests']['memory'])
        for name, it in _installed_memory(applied, memory.charged_to(memory.Line.MONITORING)).items()
    }

    assert requested
    assert sum(requested.values()) <= memory.HISTORY_BUDGET_MIB, requested
    assert sum(requested.values()) + sum(monitoring.values()) <= memory.BUDGET_MIB + memory.HISTORY_BUDGET_MIB


def test_every_memory_limit_is_at_least_its_request(applied: Run) -> None:
    """A limit under its request is refused by the API server, for the pod, at apply."""
    for name, resources in _installed_memory(applied).items():
        if 'limits' in resources:
            assert _mib(resources['limits']['memory']) >= _mib(resources['requests']['memory']), name


def test_every_memory_value_leaves_cpu_to_the_charts_defaults(applied: Run) -> None:
    """Only memory is set, so each chart and the operator keep the CPU they would give a container.

    The operator fills each resource it finds unset from its defaults, CPU
    apart from memory (`build.Resources`, `factory/build/container.go`
    L97-142 at operator v0.74.0); a chart merges the values handed to it
    over its own.
    """
    for name, resources in _installed_memory(applied).items():
        assert {key for side in resources.values() for key in side} == {'memory'}, name


def test_grafana_reads_the_stacks_datasources_from_a_mount_rather_than_a_sidecar(
    applied: Run,
) -> None:
    """The datasource sidecar is off, the stack still renders its ConfigMap, and Grafana mounts it where it provisions.

    With its datasource sidecar off, the stack renders the ConfigMap only
    under `forceDeployDatasource` (`templates/grafana/datasource.yaml` L1 at
    0.91.2).
    """
    grafana = _values(applied)['grafana']

    assert grafana['sidecar']['datasources']['enabled'] is False
    assert grafana['forceDeployDatasource'] is True
    (mount,) = [it for it in grafana['extraConfigmapMounts'] if it['mountPath'] == GRAFANA_DATASOURCES_DIR]
    assert mount['configMap'] == STACK_DATASOURCES
    assert 'subPath' not in mount


def test_grafana_installs_no_plugin_at_start(applied: Run) -> None:
    """Grafana's preinstall, its download of a plugin set on every start, is off, and no plugin is listed to install.

    `GF_PLUGINS_PREINSTALL_DISABLED` is Grafana's environment form of
    `[plugins] preinstall_disabled`; the chart installs what `plugins` lists.
    """
    grafana = _values(applied)['grafana']

    assert grafana['env']['GF_PLUGINS_PREINSTALL_DISABLED'] == 'true'
    assert not grafana.get('plugins')


def _digest(chart: ChartPin) -> str:
    return cast(
        'str', cast('dict[str, Any]', stack_values(STACK, chart, restore=[])['grafana'])['env'][DATASOURCES_DIGEST_ENV]
    )


def test_grafanas_pod_changes_when_what_renders_its_datasources_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grafana reads its datasources at start, so its pod template carries a digest of the chart and the values.

    The same chart and values give the same digest, so a run that changes
    neither leaves Grafana running; another version of the chart, or another
    value, gives another, and the Deployment rolls.
    """
    pin = ChartPin(
        name=memory.STACK_CHART,
        repository='oci://registry.example/charts',
        version='1.0.0',
        digest='sha256:a',
        definitions=True,
    )
    bumped = ChartPin(
        name=memory.STACK_CHART,
        repository='oci://registry.example/charts',
        version='1.0.1',
        digest='sha256:b',
        definitions=True,
    )
    first = _digest(pin)

    assert _digest(pin) == first
    assert _digest(bumped) != first
    head, *rest = memory.WORKER_CONTAINERS
    grown = memory.Container(
        head.name, head.chart, head.path, memory.Memory(head.memory.request + 1, head.memory.limit)
    )
    monkeypatch.setattr(memory, 'WORKER_CONTAINERS', (grown, *rest))
    assert _digest(pin) != first


# -- The rules ---------------------------------------------------------------------


def test_every_declared_rule_carries_a_census_tier_a_summary_and_a_playbook_that_resolves(
    applied: Run, wired: Recorder
) -> None:
    """The playbook rule as a gate: an alert whose response procedure is not written down is not shipped.

    Every rule the programs declare, whatever built it, is read off the
    declaration: `k8s-base`'s, and those of `apps` with a database wired
    beside it, whose component declares its drill's rules. Its tier is one
    `conventions.alert.Tier` defines, its summary is a line, and its playbook
    names a document of this repository and a section that document has
    (architecture.md §4.3, operations.md §5).
    """
    declared = _declared_rules(applied)
    beside = _alert_rules(wired)
    tiers = {tier.value for tier in Tier}

    assert declared, 'the program declared no rule'
    assert beside, 'the wired database declared no rule'
    for rule in [*declared, *beside]:
        assert rule['labels'].get('tier') in tiers, rule['alert']
        summary = rule['annotations'].get('summary', '')
        assert summary.strip() and '\n' not in summary, rule['alert']
        playbook = PLAYBOOK.match(rule['annotations'].get('playbook', ''))
        assert playbook, f'{rule["alert"]}: {rule["annotations"].get("playbook")!r} names no document and section'
        document = ROOT / playbook['document']
        assert document.is_file(), f'{rule["alert"]}: no {playbook["document"]}'
        assert playbook['section'] in sections(prose(document.read_text())), (
            f'{rule["alert"]}: {playbook["document"]} has no §{playbook["section"]}'
        )


#: The counter vmsingle keeps of the rows it received and did not store, one
#: series per `reason` (`lib/timeserieslimits/timeseries_limits.go` L52-63 and
#: `app/vmstorage/main.go` L489-497 at VictoriaMetrics v1.150.0).
IGNORED_ROWS = 'vm_rows_ignored_total'


def test_a_row_the_store_ignores_for_any_reason_raises_an_alert(applied: Run) -> None:
    """A series the store ignores is lost whole and silently, so some rule reads the counter for every reason.

    A matcher on the counter would narrow the rule to the reasons it names,
    and a reason the store adds later would go unseen.
    """
    reading = re.compile(rf'\b{IGNORED_ROWS}\b(?!\s*\{{)')

    assert [rule['alert'] for rule in _declared_rules(applied) if reading.search(rule['expr'])]


def test_the_rules_are_declared_behind_the_chart_that_defines_them(applied: Run) -> None:
    """pulumi-kubernetes waits for workloads, not for a definition's registration, so the order is declared."""
    chart = applied.urn(CHART, STACK)
    objects = [(typ, declared.name) for typ in OPERATOR_KINDS for declared in applied.monitor.of_type(typ)]

    assert objects
    for typ, name in objects:
        assert chart in applied.dependencies(typ, name), name


# -- The readings, their records, their alerts and the dashboard ---------------------


def _vector_selectors(expr: str) -> list[Any]:
    """Every vector selector of a PromQL expression, as `promql_parser` parses it."""
    found: list[Any] = []

    def visit(node: promql_parser.Expr) -> bool:
        if isinstance(node, promql_parser.VectorSelector):
            found.append(node)
        return True

    _ = promql_parser.walk(promql_parser.parse(expr), pre_visit=visit)
    return found


def _ranges(expr: str) -> set[dt.timedelta]:
    """The range of every range selector of a PromQL expression."""
    found: set[dt.timedelta] = set()

    def visit(node: promql_parser.Expr) -> bool:
        if isinstance(node, promql_parser.MatrixSelector):
            found.add(node.range)
        return True

    _ = promql_parser.walk(promql_parser.parse(expr), pre_visit=visit)
    return found


def _equalities(selector: Any) -> set[tuple[str, str]]:
    return {(m.name, m.value) for m in selector.matchers.matchers if m.op == promql_parser.MatchOp.Equal}


def test_the_node_exporters_series_are_labeled_with_the_node_they_are_read_on(applied: Run) -> None:
    """A reading of a node groups by `node`, which the operator gives a pod's target no other way.

    The endpoint is stated whole, so the chart's own drop of the kubelet's pod
    mounts is held too.
    """
    (endpoint,) = _values(applied)['prometheus-node-exporter']['vmScrape']['spec']['endpoints']
    target = {'__meta_kubernetes_pod_node_name': 'cp2', 'pod': 'node-exporter-x', 'namespace': STACK}
    mounted = {'__name__': 'node_filesystem_avail_bytes', 'mountpoint': '/var/lib/kubelet/pods/0/volumes/v'}

    assert (_relabel(target, endpoint['relabelConfigs']) or {}).get('node') == 'cp2'
    assert _relabel(mounted, endpoint['metricRelabelConfigs']) is None
    assert _relabel({**mounted, 'mountpoint': '/var'}, endpoint['metricRelabelConfigs']) is not None


def test_each_reading_is_recorded_once_under_its_name_and_the_window_it_computes_over(
    applied: Run,
) -> None:
    """The window a record's name carries is the range of every range selector of its expression."""
    records = _declared_records(applied)

    assert records == {reading.record: reading.expr for reading in readings.READINGS}
    for reading in readings.READINGS:
        assert reading.record.startswith(f'{readings.RECORD_PREFIX}:{reading.name}:'), reading.record
        assert _ranges(reading.expr) == {reading.window}, reading.record


def test_every_reading_selects_this_clusters_series_of_one_job(applied: Run) -> None:
    """The history holds the legacy cluster's series beside this one's, under `cluster="legacy"`.

    A sum by node, pod or namespace over both adds the two together, so every
    selector names this cluster, and every scraped one its job.
    """
    for record, expr in _declared_records(applied).items():
        for selector in _vector_selectors(expr):
            assert (readings.CLUSTER_LABEL, '') in _equalities(selector), f'{record}: {selector}'
            scraped = not re.fullmatch(rules.IMPORTED, selector.name or '')
            named = any(matcher.name == JOB_LABEL for matcher in selector.matchers.matchers)
            assert not scraped or named, f'{record}: {selector}'


def test_every_reading_reads_series_the_history_keeps_and_its_record_is_kept(applied: Run) -> None:
    """A reading is computed again over every period on record, which the history holds only of the class it keeps.

    The one reading whose inputs the history does not keep says so, and its
    record is kept all the same.
    """
    relabeling = _history_write(applied)['inlineUrlRelabelConfig']

    for reading in readings.READINGS:
        kept = [
            _relabel(
                {'__name__': selector.metric, **({JOB_LABEL: min(selector.jobs)} if selector.jobs else {})}, relabeling
            )
            is not None
            for selector in selectors(reading.expr)
        ]
        assert all(kept) is reading.inputs_kept, reading.record
        assert _relabel({'__name__': reading.record}, relabeling) is not None, reading.record


def test_every_recorded_series_an_alert_or_the_dashboard_reads_is_declared(applied: Run) -> None:
    records = _declared_records(applied)
    prefix = f'{readings.RECORD_PREFIX}:'
    read = [
        selector.metric
        for rule in _declared_rules(applied)
        for selector in selectors(rule['expr'])
        if selector.metric.startswith(prefix)
    ] + [
        selector.metric
        for panel in _dashboard(applied)['panels']
        for target in panel.get('targets', [])
        for selector in selectors(_offset(target['expr']))
        if selector.metric.startswith(prefix)
    ]

    assert read
    assert set(read) <= set(records)


#: An SLO alert's expression: a recorded series, the matcher its budget binds
#: by, and the comparison with the budget's limit.
_SLO = re.compile(r'^(?P<record>[\w:]+)\{(?P<scope>[^}]*)\} (?P<bound>[<>]) (?P<limit>\S+)$')


def test_each_slo_alert_fires_where_its_reading_leaves_its_budget(applied: Run) -> None:
    """The threshold of an alert over a reading is the reading's budget, on the series the budget binds."""
    by_record = {reading.record: reading for reading in readings.READINGS}
    prefix = f'{readings.RECORD_PREFIX}:'
    over_readings = [rule for rule in _declared_rules(applied) if rule['expr'].startswith(prefix)]

    assert over_readings
    for rule in over_readings:
        match = _SLO.match(rule['expr'])
        assert match, rule['expr']
        budget = by_record[match['record']].budget
        assert budget is not None, rule['alert']
        assert (match['scope'], match['bound'], float(match['limit'])) == (budget.scope, budget.bound, budget.limit)


#: The multiplier of each unit a budget is stated in, to the unit its reading is in.
_BUDGET_UNITS = {'ms': 1e-3, 's': 1.0, '%': 1e-2, 'GiB': 2.0**30, 'MB/s': 1e6, 'core': 1.0, 'IOPS': 1.0}
_STATED = re.compile(r'(?<![\w.])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|%|GiB|MB/s|core|IOPS)?(?!\w)')

#: The window a budget's phrase states it over, where it states one.
_STATED_WINDOW = re.compile(r'(?:in|over) every (?P<minutes>\d+) minutes|(?P<hour>averaged over an hour)')


def _section(document: str, number: str) -> str:
    """The prose of one numbered section of a document, its subsections included, on one line."""
    lines = prose(document).splitlines()
    heading = re.compile(rf'^(#+)\s+§?{re.escape(number)}\.?\s')
    ((start, opened),) = [(index, match) for index, line in enumerate(lines) if (match := heading.match(line))]
    depth = len(opened[1])
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if (other := re.match(r'^(#+)\s', lines[index])) and len(other[1]) <= depth
        ),
        len(lines),
    )
    return ' '.join(' '.join(lines[start:end]).split())


def _limit(stated: str) -> float:
    """The number a budget's phrase states, in its reading's unit: none at all where it states an absence."""
    if stated.startswith('no ') or stated.endswith('not increasing'):
        return 0.0
    match = _STATED.search(stated)
    assert match, stated
    return float(match['number']) * _BUDGET_UNITS[match['unit'] or 's']


def test_each_budget_is_the_one_its_source_states(applied: Run) -> None:
    """A budget's limit is the number of the canon's phrase, and the phrase stands in the section it names.

    Where the phrase states the window the budget holds over, that is the
    reading's window.
    """
    _ = applied
    budgeted = [reading for reading in readings.READINGS if reading.budget is not None]

    assert budgeted
    for reading in budgeted:
        budget = reading.budget
        assert budget is not None
        text = _section((ROOT / budget.source.document).read_text(), budget.source.section)
        assert budget.stated in text, f'{reading.record}: {budget.source} does not say {budget.stated!r}'
        assert _limit(budget.stated) == pytest.approx(budget.limit), reading.record
        if window := _STATED_WINDOW.search(budget.stated):
            stated = dt.timedelta(hours=1) if window['hour'] else dt.timedelta(minutes=int(window['minutes']))
            assert stated == reading.window, reading.record


def _namespaces(run: Run) -> set[str]:
    """Every namespace the program declares an object in, a chart's installation among them."""
    found: set[str] = set()
    for declared in run.monitor.declared:
        metadata: Mapping[str, Any] = declared.inputs.get('metadata') or {}
        named: list[object] = [metadata.get('namespace'), declared.inputs.get('namespace')]
        if declared.typ == NAMESPACE:
            named.append(metadata['name'])
        found.update(it for it in named if isinstance(it, str))
    return found


def test_the_platforms_namespaces_are_the_ones_k8s_base_declares_objects_in(applied: Run) -> None:
    """A pod in a namespace `k8s-base` uses is the platform's, so a namespace it adds is never read as a workload's."""
    assert set(readings.PLATFORM_NAMESPACES) == _namespaces(applied)


def _dashboard(run: Run) -> dict[str, Any]:
    (declared,) = [
        it for it in run.monitor.of_type(CONFIGMAP) if it.inputs['metadata'].get('name') == dashboard.CONFIGMAP
    ]
    return json.loads(declared.inputs['data'][dashboard.FILE])


def _offset(expr: str) -> str:
    """A panel's query as Grafana sends it, the offset variable and the panel's step at durations."""
    return expr.replace('$__interval', '1m').replace(f'${dashboard.OFFSET_VARIABLE}', '1d')


def test_the_dashboard_is_a_configmap_the_charts_sidecar_hands_to_grafana(applied: Run) -> None:
    """The stack's sidecar collects a `ConfigMap` labeled `grafana_dashboard: "1"` in the namespace it runs in.

    `values.yaml`, `grafana.sidecar.dashboards`, at stack chart 0.91.2, with
    the grafana chart's `searchNamespace` left unset (12.7.3).
    """
    (declared,) = [
        it for it in applied.monitor.of_type(CONFIGMAP) if it.inputs['metadata'].get('name') == dashboard.CONFIGMAP
    ]
    metadata = declared.inputs['metadata']

    assert metadata['labels'] == {'grafana_dashboard': '1'}
    assert metadata['namespace'] == conventions.MONITORING_NAMESPACE
    assert 'dashboards' not in _values(applied)['grafana'].get('sidecar', {})


def test_every_reading_has_a_panel_computing_it_and_an_earlier_period_from_the_history(
    applied: Run,
) -> None:
    """Each panel computes its reading from the history's series, and draws the same query as of an earlier time.

    The history keeps every period of a reading's inputs, so a panel reads
    those, and the record only where the history keeps no input. A reading
    per node is drawn for the control planes.
    """
    model = _dashboard(applied)
    (history,) = [
        it['uid'] for it in _values(applied)['defaultDatasources']['extra'] if it['url'] == _history_service(applied)
    ]
    variables = {it['name'] for it in model['templating']['list']}
    panels = [panel for panel in model['panels'] if panel['type'] != 'row']

    assert dashboard.OFFSET_VARIABLE in variables
    assert len(panels) == len(readings.READINGS)
    for reading in readings.READINGS:
        (panel,) = [it for it in panels if it['description'].startswith(f'`{reading.record}`')]
        now, earlier = panel['targets']
        computed = reading.expr if reading.inputs_kept else reading.record
        assert {panel['datasource']['uid'], now['datasource']['uid'], earlier['datasource']['uid']} == {history}
        assert set(selectors(now['expr'])) == set(selectors(computed)), reading.record
        assert set(selectors(_offset(earlier['expr']))) == set(selectors(computed)), reading.record
        assert f'offset ${dashboard.OFFSET_VARIABLE}' in earlier['expr'], reading.record
        shown = set(re.findall(r'"node", "([^"]+)"', now['expr']))
        assert shown == (set(conventions.CLOUD_NODES) if 'node' in reading.per else set()), reading.record


# -- The scrape-target family -------------------------------------------------------

#: The scrape-target family's two alerts, by name.
TARGET_DOWN = target_down('a-job').alert

#: The stack chart's name.
STACK_CHART = 'victoria-metrics-k8s-stack'


def _scrape_jobs() -> tuple[ScrapeJob, ...]:
    return scrape_jobs(STACK, STACK_CHART)


def _family(declared: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """The declared rules split in two: the scrape-target family's, and every other."""
    family = {SCRAPER_DOWN.alert, TARGET_DOWN}
    return [rule for rule in declared if rule['alert'] in family], [
        rule for rule in declared if rule['alert'] not in family
    ]


@pytest.mark.parametrize(
    ('expr', 'expected'),
    [
        ('increase(a_total[15m]) > 0', (Selector('a_total', None),)),
        ('vector(1)', ()),
        (
            'time() - max by (namespace, name) (a_seconds{class="daily"}) > 36h',
            (Selector('a_seconds', None),),
        ),
        (
            'sum(rate(a_total{job="x"}[5m])) by (instance) / on(instance) group_left(node) b_info',
            (Selector('a_total', frozenset({'x'})), Selector('b_info', None)),
        ),
        (
            'a offset 1h30m or b{namespace="n", job=~"x|y-z"} unless absent(c) > bool 0',
            (Selector('a', None), Selector('b', frozenset({'x', 'y-z'})), Selector('c', None)),
        ),
        ('max without (cpu) (a) > 0', (Selector('a', None),)),
        ('max_over_time((a > 0)[1h:5m])', (Selector('a', None),)),
        ('a * on() group_left b', (Selector('a', None), Selector('b', None))),
    ],
)
def test_the_reader_finds_each_selector_and_nothing_else(expr: str, expected: tuple[Selector, ...]) -> None:
    """Functions, aggregations, keywords, grouping labels, durations and label values select nothing."""
    assert selectors(expr) == expected


@pytest.mark.parametrize(
    'expr',
    [
        '{job="x"} == 0',
        '{__name__="a"}',
        'a{__name__=~"b"}',
        'a{__name__="a"}',
        'a{job!="x"}',
        'a{job=~"x.*"}',
        'a{job="x", job="y"}',
        'a $ b',
        'a{job="x" or job="y"}',
        'a{job=~"a.b"}',
        'a{job=""}',
        'a{job=~"x|"}',
        'a{job="x|y"}',
        'a default 0',
    ],
)
def test_the_reader_refuses_an_expression_it_cannot_follow(expr: str) -> None:
    """A selector it could only read short is refused rather than skipped, and so is what the parser refuses.

    That is no metric, alternatives of matchers, no definite set of jobs, and
    MetricsQL's own syntax.
    """
    with pytest.raises(ValueError, match=re.escape(repr(expr))):
        _ = selectors(expr)


def test_a_metric_read_by_name_alone_is_read_from_each_job_that_exports_it() -> None:
    """A selector naming its job reads that job; one naming none reads every job whose `exports` claims the name."""
    jobs = (ScrapeJob('one', 'shared_.+|one_.+'), ScrapeJob('two', 'shared_.+'))

    assert inputs('one_a + two_b{job="two"}', jobs) == {'one', 'two'}
    assert inputs('shared_a > 0', jobs) == {'one', 'two'}
    assert inputs('one_a{job="elsewhere"}', jobs) == {'elsewhere'}


@pytest.mark.parametrize('metric', ['cilium_drop_count_total', 'kube_apiserver_pod_logs_backend_tls_failure_total'])
def test_a_metric_no_job_exports_is_refused_naming_it(metric: str) -> None:
    """A rule cannot read a series whose job would go unwatched: the name must be a job's, or the selector name one.

    The API server's `kube_apiserver_` families share kube-state-metrics'
    prefix and are not its.
    """
    with pytest.raises(ValueError, match=re.escape(repr(metric))):
        _ = inputs(f'{metric} > 0', _scrape_jobs())


def test_a_recorded_series_is_read_from_the_jobs_its_recording_rule_reads() -> None:
    """An alert over a reading reads the jobs the reading does, through every record it is built on."""
    jobs = (ScrapeJob('one', 'one_.+'),)
    records = {'kluster:a:5m': 'sum(rate(one_a[5m]))', 'kluster:b:5m': 'kluster:a:5m + two_b{job="two"}'}

    assert inputs('kluster:b:5m > 0', jobs, records) == {'one', 'two'}
    with pytest.raises(ValueError, match=re.escape("'kluster:b:5m'")):
        _ = inputs('kluster:b:5m > 0', jobs)


def test_an_imported_series_is_read_from_no_scrape_job() -> None:
    """OCI's series are written by the pull, which its own scheduled-job rows watch, and no target stands for them."""
    metric = f'{OCI_BLOCKSTORE_METRICS}_VolumeThrottledIOs'

    assert inputs(f'{metric} > 0', _scrape_jobs()) == frozenset()


def test_every_job_an_alerting_rule_reads_has_a_target_alert_and_no_other_job_has_one(
    applied: Run,
) -> None:
    """The family is a definition: the jobs whose series the declared rules read, each watched once.

    Read off the declaration, every alert outside the family through the
    reader, the declared recording rules and the scrape jobs' exports,
    against the job each target alert is labeled with and reads.
    """
    watching, reading = _family(_declared_rules(applied))
    jobs = _scrape_jobs()
    records = _declared_records(applied)
    read = {job for rule in reading for job in inputs(rule['expr'], jobs, records)}
    watched = [rule['labels'][JOB_LABEL] for rule in watching if rule['alert'] == TARGET_DOWN]

    assert read, 'no rule reads a scrape job'
    assert sorted(watched) == sorted(read)
    for rule in watching:
        if rule['alert'] == TARGET_DOWN:
            named = {selector.jobs for selector in selectors(rule['expr']) if selector.jobs is not None}
            assert named == {frozenset({rule['labels'][JOB_LABEL]})}, rule['expr']


def test_the_family_waits_out_two_scrapes_and_an_evaluation_and_is_actionable(applied: Run) -> None:
    """A single failed scrape, a restart, or the stagger of jobs going stale as vmagent stops, reports nothing.

    Both intervals are read off what the program declares, as the
    scheduled-job family's lag is.
    """
    values = _values(applied)
    scrape = _duration(values['vmagent']['spec']['scrapeInterval'])
    evaluation = _duration(values['vmalert']['spec']['evaluationInterval'])
    watching, _ = _family(_declared_rules(applied))

    assert watching
    for rule in watching:
        assert _duration(rule['for']) > 2 * scrape + evaluation, rule['alert']
        assert rule['labels'][TIER_LABEL] == Tier.ACTIONABLE.value, rule['alert']


def test_the_scrapes_the_job_names_follow_keep_the_job_they_are_derived_from(applied: Run) -> None:
    """`scrape_jobs` derives each job from a default the declared objects leave in place.

    The operator's scrape of its own objects takes the job from the Service's
    name (`servicescrape.go` L209-220 at operator v0.74.0) unless the object's
    `serviceScrapeSpec` says otherwise, and the stack chart's scrape of
    kube-state-metrics takes it from its `jobLabel`, which the component's
    values leave to the chart, as no endpoint they state relabels `job`. That the live series carry the names is the recent
    store's to show (operations.md §6.4).
    """
    values = _values(applied)
    history = _history(applied)
    specs = {name: values[name]['spec'] for name in ('vmsingle', 'vmagent', 'alertmanager')} | {
        'history': history['spec']
    }

    for name, spec in specs.items():
        assert 'serviceScrapeSpec' not in spec, name
    ksm = values['kube-state-metrics'].get('vmScrape', {}).get('spec', {})
    assert 'jobLabel' not in ksm
    for endpoint in ksm.get('endpoints', []):
        relabeled = [it.get('targetLabel') for it in endpoint.get('relabelConfigs', [])]
        assert 'job' not in relabeled, endpoint


# -- alertmanager's receiver ----------------------------------------------------------


def test_the_receiver_reads_the_webhooks_url_from_its_sealed_secret(applied: Run) -> None:
    """The URL is in no rendered configuration: every webhook names the sealed value's Secret, in its namespace.

    The operator reads `url_secret` from the receiver object's own namespace
    (`vmalertmanagerconfig_types.go` L533-563 at operator v0.74.0), which is
    therefore the namespace the value is sealed to.
    """
    receiver = _receiver(applied)
    webhooks = [config for spec in receiver['spec']['receivers'] for config in spec.get('webhook_configs', [])]

    assert receiver['metadata']['namespace'] == WEBHOOK.namespace
    assert webhooks
    for webhook in webhooks:
        assert 'url' not in webhook
        assert webhook['url_secret']['name'] == WEBHOOK.name


def test_the_receiver_sends_resolutions(applied: Run) -> None:
    """Every webhook sends an alert's resolution, which withdraws its notification on the phone (operations.md §4).

    The field is stated on every webhook rather than left to a default, so
    the rendered configuration itself says what reaches Home Assistant.
    """
    webhooks = [
        config for spec in _receiver(applied)['spec']['receivers'] for config in spec.get('webhook_configs', [])
    ]

    assert webhooks
    for webhook in webhooks:
        assert webhook.get('send_resolved') is True


def test_the_sealed_webhook_is_declared_where_the_receiver_reads_it(applied: Run) -> None:
    """Sealed in, the value becomes the Secret the receiver names, at the scope the census row seals it.

    The key the receiver reads is one the SealedSecret renders rather than
    the sealed id, and it renders a URL at Home Assistant's own address
    around the id; `test_sealing` opens it as the controller does.
    """
    receiver = _receiver(applied)
    (secret,) = [
        it.inputs for it in applied.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WEBHOOK.name
    ]
    keys = {config['url_secret']['key'] for spec in receiver['spec']['receivers'] for config in spec['webhook_configs']}
    rendered = secret['spec']['template']['data']

    assert secret['metadata']['namespace'] == receiver['metadata']['namespace']
    assert secret['spec']['encryptedData'] == SEALED[WEBHOOK.name]
    assert secret['metadata']['annotations'] == {f'sealedsecrets.bitnami.com/{WEBHOOK.scope.value}': 'true'}
    assert keys
    assert keys <= set(rendered) - set(WEBHOOK.keys)
    for key in keys:
        url = urlsplit(cast('str', rendered[key]))
        assert (url.scheme, url.hostname, url.port) == (
            'http',
            str(conventions.HOME_ASSISTANT.v4),
            conventions.HOME_ASSISTANT_PORT,
        )


_DURATION = re.compile(r'^(\d+)([smhdw])$')
_UNITS = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days', 'w': 'weeks'}


def _duration(text: str) -> dt.timedelta:
    match = _DURATION.match(text)
    assert match, f'{text!r} is no single-unit duration'
    return dt.timedelta(**{_UNITS[match[2]]: int(match[1])})


def _route(route: Mapping[str, Any], labels: Mapping[str, str]) -> Mapping[str, Any]:
    """The route an alert with these labels ends on, below one that already matched it.

    alertmanager's walk: the first child whose matchers all hold, recursively;
    a child with `continue` passes the alert on to its later siblings too,
    which none here sets. Each matcher is an equality, the one form written
    here. A child inherits what it does not set.
    """
    for child in route.get('routes', []):
        if all(_matches(matcher, labels) for matcher in child.get('matchers', [])):
            return _route({**{k: v for k, v in route.items() if k not in ('routes', 'matchers')}, **child}, labels)
    return route


def _matches(matcher: str, labels: Mapping[str, str]) -> bool:
    name, value = re.fullmatch(r'\s*([\w]+)\s*=\s*"(.*)"\s*', matcher).groups()  # pyright: ignore[reportOptionalMemberAccess] -- an unparsable matcher fails the case
    return labels.get(name) == value


def _top_route(run: Run) -> Mapping[str, Any]:
    """The receiver's route as the operator places it under alertmanager's root.

    The operator adds `namespace = "<the object's namespace>"` to the top route
    of every VMAlertmanagerConfig unless the VMAlertmanager disables it
    (`config.go` `buildRoute` at operator v0.74.0).
    """
    receiver = _receiver(run)
    route = dict(receiver['spec']['route'])
    if not _values(run)['alertmanager']['spec'].get('disableNamespaceMatcher', False):
        route['matchers'] = [*route.get('matchers', []), f'namespace="{receiver["metadata"]["namespace"]}"']
    return route


def test_every_rule_reaches_home_assistant_and_the_check_in_repeats_inside_its_timer(
    applied: Run,
) -> None:
    """Each declared rule's alert, carrying its own labels alone, ends on Home Assistant's receiver.

    The check-in carries no label but its tier, so a route confined to a
    namespace would drop it. Its repeat reaches Home Assistant at most a group
    interval after the repeat interval, which is inside the timer it restarts
    (rfc-007 §7.3).
    """
    top = _top_route(applied)
    declared = _declared_rules(applied)

    for rule in declared:
        labels = cast('Mapping[str, str]', rule['labels'])
        top_matches = all(_matches(matcher, labels) for matcher in top.get('matchers', []))
        assert top_matches, f'{rule["alert"]} does not reach the receiver'
        assert _route(top, labels)['receiver'] == RECEIVER, rule['alert']

    (check_in,) = [rule for rule in declared if rule['alert'] == HEARTBEAT.alert]
    route = _route(top, check_in['labels'])
    latest = _duration(route['repeat_interval']) + _duration(route['group_interval'])
    assert latest < HEARTBEAT_TIMER


# -- The kubeconfig copy's expiry ---------------------------------------------------


def _kubeconfig_copy(run: Run) -> Mapping[str, Any]:
    (rule,) = [rule for rule in _declared_rules(run) if rule['alert'] == KUBECONFIG_COPY]
    return rule


def test_the_kubeconfig_copys_rule_fires_from_the_margin_before_its_certificate_expires(
    applied: Run,
) -> None:
    """The threshold is read off the copy the program is configured with: its certificate's `NotAfter`, less the margin.

    The expression keeps every evaluation from the threshold on, and none
    before it: the evaluation's time against a number of seconds since the
    epoch. It reaches the engine in the clear, so a preview shows the date
    move when a new copy is committed.
    """
    rule = _kubeconfig_copy(applied)
    tree = promql_parser.parse(rule['expr'])

    assert isinstance(tree, promql_parser.BinaryExpr), rule['expr']
    assert str(tree.op) == '>'
    assert isinstance(tree.lhs, promql_parser.Call) and str(tree.lhs) == 'vector(time())', rule['expr']
    assert isinstance(tree.rhs, promql_parser.NumberLiteral), rule['expr']
    assert dt.datetime.fromtimestamp(tree.rhs.val, dt.UTC) == NOT_AFTER - KUBECONFIG_ALERT_MARGIN
    assert rule['labels'][TIER_LABEL] == Tier.ACTIONABLE.value
    assert 'for' not in rule


def test_an_expiry_with_no_time_zone_is_refused() -> None:
    """A date with no zone is no instant, and its threshold would move with the zone the program ran in."""
    with pytest.raises(ValueError, match='time zone'):
        _ = kubeconfig_copy(NOT_AFTER.replace(tzinfo=None))


def test_the_alert_margin_lies_inside_the_renewal_window() -> None:
    """The playbook's `physical` apply issues a new certificate only within the window, so the alert fires inside it.

    Fired earlier, the apply its playbook asks for would issue nothing, and
    the sync after it would find nothing new to copy.
    """
    assert dt.timedelta(0) < KUBECONFIG_ALERT_MARGIN < KUBECONFIG_RENEWAL


# -- The workaround layer: its tripwire, and what it gives up --------------------------


def test_the_operators_generated_sdk_still_cannot_be_imported() -> None:
    """While it cannot, `components.monitoring.operator_objects` declares the operator's kinds untyped.

    The day this fails, the SDK imports (Aetf/kluster-ops#720): each call
    there becomes the SDK class of its kind, and that module goes.
    """
    with pytest.raises(SyntaxError, match='duplicate argument'):
        importlib.import_module('pulumi_crds.operator.v1beta1')


#: The definitions the program's custom resources are generated from, as
#: `update_crds` bundled them from the pinned charts.
BUNDLE = ROOT / 'packages' / 'crds' / 'crds.yaml'


def _spec_schemas() -> dict[tuple[str, str], Mapping[str, Any]]:
    """Each bundled definition's `spec` schema, by `apiVersion` and kind."""
    schemas: dict[tuple[str, str], Mapping[str, Any]] = {}
    for loaded in yaml.safe_load_all(BUNDLE.read_text()):
        document = cast('dict[str, Any] | None', loaded)
        if not isinstance(document, dict) or document.get('kind') != 'CustomResourceDefinition':
            continue
        body = document['spec']
        for version in body['versions']:
            spec = version['schema']['openAPIV3Schema']['properties'].get('spec')
            if spec is not None:
                schemas[(f'{body["group"]}/{version["name"]}', body['names']['kind'])] = spec
    return schemas


def _unknown_fields(value: object, schema: Mapping[str, Any], path: str) -> list[str]:
    """Every path in `value` its schema does not declare, where the schema fixes its keys.

    An object the schema leaves open -- `x-kubernetes-preserve-unknown-fields`
    or `additionalProperties` -- takes any key, as the API server does; a
    field outside a closed object is pruned by the API server on apply,
    silently, which is the failure this walk exists to catch.
    """
    if isinstance(value, Mapping):
        mapping = cast('Mapping[str, object]', value)
        properties = cast('Mapping[str, Any]', schema.get('properties', {}))
        open_object = schema.get('x-kubernetes-preserve-unknown-fields') is True or schema.get(
            'additionalProperties'
        ) not in (None, False)
        found: list[str] = []
        for key, item in mapping.items():
            if key in properties:
                found += _unknown_fields(item, properties[key], f'{path}.{key}')
            elif not open_object:
                found.append(f'{path}.{key}')
        return found
    if isinstance(value, list):
        items = cast('Mapping[str, Any]', schema.get('items', {}))
        return [
            field
            for i, item in enumerate(cast('list[object]', value))
            for field in _unknown_fields(item, items, f'{path}[{i}]')
        ]
    return []


@pytest.fixture(scope='module', name='spec_schemas')
def spec_schemas_fixture() -> dict[tuple[str, str], Mapping[str, Any]]:
    return _spec_schemas()


def test_every_untyped_operator_object_is_spelled_as_its_definition_declares(
    applied: Run, spec_schemas: dict[tuple[str, str], Mapping[str, Any]]
) -> None:
    """The check the SDK's typed `Args` would make, while the layer declares the operator's kinds untyped.

    It goes with `components.monitoring.operator_objects` (Aetf/kluster-ops#720).
    """
    declared = [it for typ in OPERATOR_KINDS for it in applied.monitor.of_type(typ)]

    assert declared
    for it in declared:
        schema = spec_schemas[(it.inputs['apiVersion'], it.inputs['kind'])]
        assert _unknown_fields(it.inputs['spec'], schema, 'spec') == [], it.name


def test_the_spelling_check_names_a_field_its_definition_lacks(
    spec_schemas: dict[tuple[str, str], Mapping[str, Any]],
) -> None:
    """Its control: a misspelled field below a closed object is named; one below an open object is not."""
    schema = spec_schemas[('operator.victoriametrics.com/v1beta1', 'VMAlertmanagerConfig')]
    spec = {
        'receivers': [{'name': 'r', 'webhook_configs': [{'send_resolvd': False}]}],
        'route': {'receiver': 'r', 'routes': [{'anything': 'goes'}]},
    }

    assert _unknown_fields(spec, schema, 'spec') == ['spec.receivers[0].webhook_configs[0].send_resolvd']


# -- The two stores --------------------------------------------------------------------


def _history(run: Run) -> dict[str, Any]:
    (store,) = run.monitor.of_type(VM_SINGLE)
    return store.inputs


def _history_service(run: Run) -> str:
    """The address of the history store's Service, as the operator names it: `vmsingle-<object>` on 8428.

    `PrefixedName` and the Service's one port (`api/operator/v1beta1/vmsingle_types.go`
    L340-345 and `factory/build/service.go` L45-65 at operator v0.74.0).
    """
    metadata = _history(run)['metadata']
    return f'http://vmsingle-{metadata["name"]}.{metadata["namespace"]}.svc:8428'


def test_the_history_store_runs_on_the_worker_alone(applied: Run) -> None:
    """It is a second vmsingle beside the chart's, on the worker's local disk, as every store of the stack is."""
    spec = _history(applied)['spec']

    schedulable = {
        name
        for name, labels in _talos_nodes().items()
        if _admits(labels, spec.get('nodeSelector', {}), spec.get('affinity', {}))
    }

    assert schedulable == {conventions.HOMELAB_NODE}
    assert spec['storage']['storageClassName'] == conventions.SC_LOCAL_PATH


def test_the_history_stores_mutating_endpoints_take_the_stacks_keys(applied: Run) -> None:
    """Deleting the history is behind a key, as deleting the recent store's series is."""
    spec = _history(applied)['spec']
    recent = _values(applied)['vmsingle']['spec']

    assert {flag: spec['extraArgs'].get(flag) for flag in MUTATING_ENDPOINT_FLAGS} == {
        flag: recent['extraArgs'][flag] for flag in MUTATING_ENDPOINT_FLAGS
    }
    assert set(recent['secrets']) <= set(spec.get('secrets', []))


def _history_write(run: Run) -> dict[str, Any]:
    (write,) = [
        it
        for it in _values(run)['vmagent'].get('additionalRemoteWrites', [])
        if it['url'].startswith(_history_service(run))
    ]
    return write


def test_vmagent_writes_to_the_history_stores_service(applied: Run) -> None:
    """On its write path, where vmsingle takes Prometheus remote write (`/api/v1/write`)."""

    assert _history_write(applied)['url'] == f'{_history_service(applied)}/api/v1/write'


@pytest.mark.parametrize(
    ('series', 'kept'),
    [
        ({'__name__': 'node_pressure_cpu_waiting_seconds_total', 'job': 'node-exporter'}, True),
        ({'__name__': 'node_disk_written_bytes_total', 'job': 'node-exporter'}, True),
        ({'__name__': 'node_memory_MemAvailable_bytes', 'job': 'node-exporter'}, True),
        ({'__name__': 'etcd_disk_wal_fsync_duration_seconds_bucket', 'job': 'etcd'}, True),
        ({'__name__': 'process_resident_memory_bytes', 'job': 'etcd'}, True),
        ({'__name__': 'container_cpu_usage_seconds_total', 'job': 'kubelet'}, True),
        ({'__name__': 'kube_pod_container_resource_requests', 'job': 'kube-state-metrics'}, True),
        ({'__name__': 'kube_pod_status_phase', 'job': 'kube-state-metrics'}, True),
        ({'__name__': 'apiserver_request_total', 'job': 'apiserver'}, True),
        ({'__name__': 'kluster:etcd_wal_fsync_p99:5m'}, True),
        ({'__name__': 'oci_blockstore_VolumeThrottledIOs', 'job': 'oci'}, True),
        ({'__name__': 'etcd_debugging_mvcc_keys_total', 'job': 'etcd'}, False),
        ({'__name__': 'process_resident_memory_bytes', 'job': 'kubelet'}, False),
        ({'__name__': 'etcd_request_duration_seconds_count', 'job': 'apiserver'}, False),
        ({'__name__': 'node_memory_Cached_bytes', 'job': 'node-exporter'}, False),
        ({'__name__': 'vm_rows', 'job': 'vmsingle'}, False),
        ({'__name__': 'up', 'job': 'kubelet'}, False),
    ],
    ids=lambda it: it if isinstance(it, bool) else f'{it.get("job", "")}:{it["__name__"]}',
)
def test_the_history_store_is_written_the_class_it_keeps_and_nothing_else(
    applied: Run, series: dict[str, str], kept: bool
) -> None:
    """What a node, etcd or a container consumes and how long the control plane takes; etcd's process, not another's.

    The class of Aetf/kluster-ops#790's design, §2, read through the remote
    write's relabeling as vmagent applies it to a series it sends.
    """

    assert (_relabel(series, _history_write(applied)['inlineUrlRelabelConfig']) is not None) is kept


def test_vmalerts_recording_rules_reach_the_stores_through_vmagent(applied: Run) -> None:
    """Written to vmagent, a rule's series takes the history's relabeling as a scraped one does.

    The chart points vmalert's remote write at vmagent under this value, and at
    the recent store otherwise (`templates/_helpers.tpl` L79-83 at 0.91.2).
    """

    assert _values(applied)['vmalert'].get('remoteWriteVMAgent') is True


def test_grafana_reads_the_history_store(applied: Run) -> None:
    """A datasource of its own, at the address vmagent writes the history to, beside the chart's default."""
    extra = _values(applied)['defaultDatasources']['extra']

    assert [it['url'] for it in extra if it['type'] == 'prometheus'] == [_history_service(applied)]
    assert not any(it.get('isDefault') for it in extra)


#: The release each store and its backups run, as `update_crds` recorded the
#: stack chart's `appVersion` beside the bundle.
APP_VERSIONS = ROOT / 'packages' / 'crds' / 'app-versions.json'

#: The image pins the monitoring component runs at the stack chart's release,
#: its `appVersion` (`Pulumi.yaml`): the test below holds each to it, and the
#: grouping test holds each to the chart's group.
AT_THE_CHARTS_RELEASE = ('metrics-history', 'metrics-backup', 'metrics-restore')


@pytest.mark.parametrize('pin', AT_THE_CHARTS_RELEASE)
def test_the_stores_images_are_the_release_the_chart_runs_the_recent_store_at(pin: str) -> None:
    """`vmbackup` and `vmrestore` read the parts the store writes, and the two stores are one release.

    A chart bump that moves the release is red here once `update_crds` has
    rewritten the record on its branch. Renovate's `victoriametrics` group
    moves the chart and the three pins in one pull request, which is red here
    until a chart release records the images' release.
    """
    from kluster.lib.versions import ProjectFile, Versions
    from kluster.scripts.update_crds import sources

    pinned = Versions(ProjectFile(sources.read_config(ROOT / 'Pulumi.yaml'))).image[pin]
    recorded = json.loads(APP_VERSIONS.read_text())['versions:chart-victoria-metrics-k8s-stack']['appVersion']

    assert pinned.tag == recorded


def test_renovate_moves_every_pin_held_to_the_charts_release_with_the_chart_in_a_group_of_its_own() -> None:
    """The chart and every pin held to its release are one group, apart from every other, so a lagging chart holds back those pins alone.

    The pins are the image pins `AT_THE_CHARTS_RELEASE` names and the
    `vmutils` archive that puts `vmalert-tool` on the suite's path, which
    `tests/test_alert_evaluation.py` holds to the chart's release. Apart,
    each is red on its own and the chart red without them, so neither pull
    request could pass. For each, presented as its manager extracts it, the
    rule renovate takes its group from -- the last rule that sets one and can
    match the update -- is the chart's, and no other rule names that group's
    slug. Renovate reads its configuration from the default branch, so
    nothing goes red there when this breaks.
    """
    import tomllib

    from kluster.lib.versions import OCI_SCHEME, ProjectFile, Versions
    from kluster.scripts.update_crds import sources

    pinned = Versions(ProjectFile(sources.read_config(ROOT / 'Pulumi.yaml')))
    images = sorted(pinned.image[name].repository for name in AT_THE_CHARTS_RELEASE)
    tools: dict[str, Any] = tomllib.loads((ROOT / 'mise.toml').read_text())['tools']
    # The pin that puts `vmalert-tool` on the path: the subject of the test
    # holding it to the chart's release.
    (vmutils,) = [
        key
        for key, tool in tools.items()
        if isinstance(tool, dict)
        and 'vmalert-tool' in str(cast('dict[str, Any]', tool).get('filter_bins', '')).split(',')
    ]

    def update(
        name: str, file: str, managers: tuple[str, ...], datasource: str, package: str | None = None
    ) -> dict[str, tuple[str, ...]]:
        return {
            'matchDepNames': (name,),
            'matchPackageNames': (package or name,),
            'matchManagers': managers,
            'matchFileNames': (file,),
            'matchDatasources': (datasource,),
            'matchUpdateTypes': ('minor',),
        }

    pulumi_yaml = ('custom.regex', 'regex')
    # A chart served from an OCI registry is read as an image: its package is
    # the registry path with the chart's name appended. The mise manager names
    # a GitHub backend's tool by its key and its package by the repository.
    chart_pin = pinned.chart['victoria-metrics-k8s-stack']
    chart_update = (
        update(
            chart_pin.name,
            'Pulumi.yaml',
            pulumi_yaml,
            'docker',
            f'{chart_pin.repository.removeprefix(OCI_SCHEME)}/{chart_pin.name}',
        )
        if chart_pin.oci
        else update(chart_pin.name, 'Pulumi.yaml', pulumi_yaml, 'helm')
    )
    updates = {
        'the chart': chart_update,
        **{image: update(image, 'Pulumi.yaml', pulumi_yaml, 'docker') for image in images},
        vmutils: update(vmutils, 'mise.toml', ('mise',), 'github-releases', vmutils.partition(':')[2]),
    }
    config = (ROOT / 'renovate.json5').read_text()
    grouping = {name: effective(config, presented, 'groupSlug') for name, presented in updates.items()}
    slugs = {name: None if rule is None else scalar(rule, 'groupSlug') for name, rule in grouping.items()}
    assert len(set(grouping.values())) == 1, f'the chart and the pins at its release are grouped apart: {slugs}'
    (chart,) = set(grouping.values())
    assert chart is not None
    slug = scalar(chart, 'groupSlug')

    assert [rule for rule in package_rules(config) if scalar(rule, 'groupSlug') == slug] == [chart]


# -- Backups and restores ------------------------------------------------------------


def _restores(run: Run) -> dict[str, list[dict[str, Any]]]:
    """Each store's init containers, by store: the chart's vmsingle and the history store."""
    return {
        'recent': _values(run)['vmsingle']['spec'].get('initContainers', []),
        'history': _history(run)['spec'].get('initContainers', []),
    }


def _backup_jobs(run: Run) -> dict[str, dict[str, Any]]:
    """Each backup CronJob's spec, by the claim its pod mounts."""
    names = {job.cronjob for job in schedule.METRICS_BACKUPS.values()}
    jobs: dict[str, dict[str, Any]] = {}
    for job in run.monitor.of_type(CRONJOB):
        if job.inputs['metadata']['name'] not in names:
            continue
        pod = job.inputs['spec']['jobTemplate']['spec']['template']['spec']
        (claim,) = [it['persistentVolumeClaim'] for it in pod['volumes'] if 'persistentVolumeClaim' in it]
        jobs[claim['claimName']] = job.inputs
    return jobs


def test_each_store_is_backed_up_hourly_from_its_own_claim_read_only(applied: Run) -> None:
    """The operator's claim of each store (`vmsingle-<object>`, `factory/vmsingle/vmsingle.go` L59-62), at two minutes.

    Mounted read-only, so the job's pod lands on the store's node with the
    claim and can write nothing there.
    """
    jobs = _backup_jobs(applied)
    (history,) = applied.monitor.of_type(VM_SINGLE)
    chart_store = f'vmsingle-{conventions.MONITORING_NAMESPACE}-victoria-metrics-k8s-stack'

    assert set(jobs) == {f'vmsingle-{history.inputs["metadata"]["name"]}', chart_store}
    minutes: set[str] = set()
    for claim, job in jobs.items():
        minute, *every = job['spec']['schedule'].split()
        assert every == ['*'] * 4, claim
        minutes.add(minute)
        pod = job['spec']['jobTemplate']['spec']['template']['spec']
        assert all(it['persistentVolumeClaim']['readOnly'] for it in pod['volumes'] if 'persistentVolumeClaim' in it)
        assert job['spec']['concurrencyPolicy'] == 'Forbid'
    assert len(minutes) == len(jobs), 'two backups at one minute'


def _pod_containers(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A backup job's containers in the order its pod runs them: the init containers, then the rest."""
    pod = job['spec']['jobTemplate']['spec']['template']['spec']
    return [*pod.get('initContainers', []), *pod['containers']]


def test_a_snapshot_a_cut_off_backup_leaves_is_deleted_well_after_any_run_ends(applied: Run) -> None:
    """Its hard links would keep every part merged away since on disk; no run loses the snapshot it copies."""
    longest = max(job.deadline for job in schedule.METRICS_BACKUPS.values())

    for spec in (_values(applied)['vmsingle']['spec'], _history(applied)['spec']):
        assert _duration(spec['extraArgs']['snapshotsMaxAge']) > longest


def _env(container: Mapping[str, Any]) -> dict[str, Any]:
    return {it['name']: it.get('value', it.get('valueFrom')) for it in container['env']}


def test_the_writer_key_is_declared_where_the_backups_and_restores_read_it(applied: Run) -> None:
    """Sealed in, the key becomes the Secret every backup and restore container reads both halves from."""
    (secret,) = [
        it.inputs for it in applied.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WRITER_KEY.name
    ]
    containers = [
        *[container for job in _backup_jobs(applied).values() for container in _pod_containers(job)],
        *[container for restores in _restores(applied).values() for container in restores],
    ]

    assert secret['metadata']['namespace'] == conventions.MONITORING_NAMESPACE
    assert secret['spec']['encryptedData'] == SEALED[WRITER_KEY.name]
    assert containers
    for container in containers:
        env = _env(container)
        for field in conventions.WRITER_KEY_FIELDS:
            assert env[field] == {'secretKeyRef': {'name': secret['metadata']['name'], 'key': field}}


# -- The scripts, run --------------------------------------------------------------------

#: `vmrestore` and `vmbackup`: each records its arguments, and fails where its
#: name is in the state directory's `fails`; `vmrestore` writes a part into
#: the data directory its arguments name.
FAKE_BINARY = """
import os, sys
from pathlib import Path

state = Path(os.environ['FAKE_STATE'])
name = Path(sys.argv[0]).name
(state / f'{name}.args').write_text('\\n'.join(sys.argv[1:]))
if (state / 'fails').exists() and name in (state / 'fails').read_text().split():
    sys.exit(1)
for arg in sys.argv[1:]:
    if name == 'vmrestore' and arg.startswith('-storageDataPath='):
        (Path(arg.split('=', 1)[1]) / 'restored-part').write_text('part')
"""

#: The bound on one script's run, which takes a fraction of a second.
SHELL_TIMEOUT = 30

#: The shell a script container's command names, which is its image's busybox.
SHELL = '/bin/sh'

#: The pins of the images a backup or restore pod runs a script in, each
#: script under its own image's busybox (`image_busybox`): curl's runs every
#: script but `restore.sh`, which `vmrestore`'s runs.
SCRIPT_IMAGES = ('metrics-bucket', 'metrics-restore')

#: The pin each image reference the installation is configured with comes from,
#: so a container's `image` names the pin whose busybox it runs its script under.
IMAGE_PINS = {
    value: key.removeprefix('versions:image-')
    for key, value in VERSIONS_CONFIG.items()
    if key.startswith('versions:image-')
}


@pytest.fixture(scope='module', name='shells')
def shells_fixture(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """For each pin a script runs in, a directory of its image's busybox applets, as the image lays them out under one `PATH`."""
    cache = image_busybox.cache_directory(request.config, tmp_path_factory)
    return {
        pin: image_busybox.applets(
            image_busybox.extracted(image_busybox.pinned(pin), cache), tmp_path_factory.mktemp(pin)
        )
        for pin in SCRIPT_IMAGES
    }


@pytest.mark.parametrize('kind', ['restore', 'backup'])
def test_each_script_runs_under_the_busybox_of_its_containers_own_image(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path], kind: str
) -> None:
    """The `/bin/sh` a container runs a script under is its image's `/bin/busybox` and the loader it names, byte for byte.

    The files are the ones the pod launches for the container's `/bin/sh`,
    and the sums are the ones the container's own pinned image computes
    itself, so they are read apart from the copy the suite runs.
    """
    pod = (_restore_pod if kind == 'restore' else _backup_pod)(applied, 'history', tmp_path, shells)
    scripted = [it for it in pod.containers if SHELL in it['command']]
    assert scripted, f'the {kind} pod runs no script'

    for container in scripted:
        image = image_busybox.pinned(IMAGE_PINS[container['image']])
        argv = pod.argv(container)
        *loader, binary = image_busybox.launched(Path(argv[list(container['command']).index(SHELL)]))
        interpreter = image_busybox.interpreter(binary.read_bytes())
        named = [image_busybox.BINARY, *([interpreter] if interpreter is not None else [])]

        in_image = image_busybox.sums_in(image, *named)

        assert [image_busybox.sha256(it) for it in (binary, *loader)] == [in_image[it] for it in named], (
            f'{container["name"]} runs its script under files {image} does not carry'
        )


class Pod:
    """A store's restore or backup pod, its containers run in order against the fakes, each under its image's busybox.

    The init containers run as the kubelet runs them, one after another, the
    next only once the one before it has succeeded, and then the pod's main
    container. The kubelet starts a pod's main containers together, which a
    sequence does not model, so a pod with more than one is refused. A
    restore's pod is its store's, whose main container, the store itself,
    is not run. A container's `/bin/sh` and its `PATH`'s applets are the
    busybox of the pin its `image` is configured from, and `PATH` holds the
    fakes beside them alone, as the images hold no other tool. `vmbackup`'s
    image runs its binary and no script, so its busybox is never read, and
    a container whose command names `/bin/sh` in an image whose busybox is
    not read is refused, naming the image.
    """

    def __init__(
        self,
        tmp_path: Path,
        shells: Mapping[str, Path],
        init: Sequence[Mapping[str, Any]],
        main: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        from kluster.components.monitoring import stores

        assert len(main) <= 1, f'the kubelet runs {len(main)} main containers together, which a sequence does not model'
        containers = [*init, *main]

        self.state = tmp_path / 'state'
        self.data = tmp_path / 'data'
        self.work = tmp_path / 'work'
        self.bucket = self.state / 'bucket'
        shell, self.fakes = tmp_path / 'shell', tmp_path / 'bin'
        for directory in (self.state, self.data, self.work, self.bucket, shell, self.fakes):
            directory.mkdir(parents=True, exist_ok=True)
        for name, text in stores.SHELL.items():
            (shell / name).write_text(text)
        fake_b2_shell.install(self.state, self.fakes)
        for name, text in (('vmrestore', FAKE_BINARY), ('vmbackup', FAKE_BINARY)):
            binary = self.fakes / name
            binary.write_text(f'#!{sys.executable}\n{text}')
            binary.chmod(0o755)
        self.local = {
            stores.SHELL_DIR: str(shell),
            stores.DATA_DIR: str(self.data),
            stores.WORK_DIR: str(self.work),
            '/vmrestore-prod': str(self.fakes / 'vmrestore'),
            '/vmbackup-prod': str(self.fakes / 'vmbackup'),
        }
        self.shells = shells
        self.containers = containers
        self.key = ':'.join(FAKE_KEY)
        self.restrict([BUCKET])

    def restrict(self, buckets: Sequence[Mapping[str, str | None]]) -> None:
        """Answer the authorization with the key restricted to `buckets`."""
        (self.state / 'authorization.json').write_text(json.dumps(authorization(buckets), indent=2))

    def _shell(self, container: Mapping[str, Any]) -> Path | None:
        """The applets of the busybox in `container`'s image, `None` for an image whose busybox is not read."""
        image = container['image']
        assert image in IMAGE_PINS, f'{container["name"]} runs {image}, which no `versions:image-*` pin configures'
        shell = self.shells.get(IMAGE_PINS[image])
        assert shell is not None or SHELL not in container['command'], (
            f'{container["name"]} names {SHELL} in {image}, whose busybox the suite does not read'
        )
        return shell

    def _here(self, value: str, shell: Path | None) -> str:
        for remote, path in self.local.items():
            value = value.replace(remote, path)
        return value.replace(SHELL, str(shell / 'sh')) if shell is not None else value

    def fake_env(self) -> dict[str, str]:
        """What the fakes read: the state directory, the key B2 admits, and the bucket it is restricted to."""
        return fake_env(self.state, self.key)

    def _env(self, container: Mapping[str, Any], shell: Path | None) -> dict[str, str]:
        halves = dict(zip(sorted(conventions.WRITER_KEY_FIELDS), FAKE_KEY, strict=True))
        return {
            'PATH': ':'.join(str(it) for it in (self.fakes, shell) if it is not None),
            **self.fake_env(),
            **{
                it['name']: self._here(it['value'], shell)
                if 'value' in it
                else halves.get(it['name'], 'the-snapshot-key')
                for it in container.get('env', [])
            },
        }

    def argv(self, container: Mapping[str, Any]) -> list[str]:
        """`container`'s command as the pod launches it here."""
        shell = self._shell(container)
        return [self._here(arg, shell) for arg in container['command']]

    def run(self) -> list[subprocess.CompletedProcess[str]]:
        """Each container's run, up to and including the first that fails."""
        runs: list[subprocess.CompletedProcess[str]] = []
        for container in self.containers:
            completed = process_sessions.run(
                self.argv(container), env=self._env(container, self._shell(container)), text=True, timeout=SHELL_TIMEOUT
            )
            runs.append(completed)
            if completed.returncode != 0:
                break
        return runs

    def succeeded(self) -> bool:
        runs = self.run()
        self.stderr = '\n'.join(it.stderr for it in runs)
        return len(runs) == len(self.containers) and all(it.returncode == 0 for it in runs)

    def ran(self, binary: str) -> list[str] | None:
        """The arguments `binary` was run with, `None` where it was not run."""
        found = self.state / f'{binary}.args'
        return found.read_text().split('\n') if found.exists() else None

    def curl_arguments(self) -> list[list[str]]:
        found = self.state / 'curl.argv'
        return [json.loads(line) for line in found.read_text().splitlines()] if found.exists() else []


def _restore_pod(run: Run, store: str, tmp_path: Path, shells: Mapping[str, Path]) -> Pod:
    return Pod(tmp_path, shells, _restores(run)[store])


def _backup_pod(run: Run, store: str, tmp_path: Path, shells: Mapping[str, Path]) -> Pod:
    (job,) = [
        it.inputs
        for it in run.monitor.of_type(CRONJOB)
        if it.inputs['metadata']['name'] == schedule.METRICS_BACKUPS[store].cronjob
    ]
    pod = job['spec']['jobTemplate']['spec']['template']['spec']
    return Pod(tmp_path, shells, pod.get('initContainers', []), pod['containers'])


def _prefix(pod: Pod) -> str:
    (check, *_) = pod.containers
    return _env(check)['PREFIX']


def _record(pod: Pod) -> Path:
    (check, *_) = pod.containers
    return pod.bucket / _env(check)['RECORD']


def _backed_up(pod: Pod, prefix: str) -> None:
    """A complete backup under `prefix`, as `vmbackup` leaves one."""
    for name in ('backup_complete.ignore', 'data/small/2026_11/part/index.bin'):
        target = pod.bucket / prefix / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('')


#: The pin of the image the bucket's scripts run in, curl's (`SCRIPT_IMAGES`).
BUCKET_PIN = 'metrics-bucket'


def _authorized(pod: Pod, shells: Mapping[str, Path], then: str = '') -> subprocess.CompletedProcess[str]:
    """`b2_authorize` from `b2.sh` under curl's busybox against the fake bucket, then `then`, then what it set, a line each."""
    from kluster.components.monitoring import stores

    shell = shells[BUCKET_PIN]
    script = (
        '. "$1" && b2_authorize && ' + (f'{then} && ' if then else '') + 'printf "%s\\n" '
        '"$B2_BUCKET_ID" "$B2_BUCKET_NAME" "$B2_API_URL" "$B2_DOWNLOAD_URL"'
    )
    return process_sessions.run(
        [str(shell / 'sh'), '-c', script, 'sh', f'{pod.local[stores.SHELL_DIR]}/b2.sh'],
        env={
            'PATH': f'{pod.fakes}:{shell}',
            'WORK': str(pod.work),
            **pod.fake_env(),
            **dict(zip(sorted(conventions.WRITER_KEY_FIELDS), FAKE_KEY, strict=True)),
        },
        text=True,
        timeout=SHELL_TIMEOUT,
    )


def test_the_writer_key_authorizes_on_v4_as_the_one_bucket_its_answer_lists(
    tmp_path: Path, shells: Mapping[str, Path]
) -> None:
    """The bucket's id and name from `allowed.buckets`, the addresses from `apiInfo.storageApi`, and every call on v4.

    The answer is B2's own example with the key restricted to one bucket, so
    the Partner API's address and the S3 address beside the storage API's are
    there to be mistaken for it, and the listing that follows is addressed to
    the storage API's.
    """
    pod = Pod(tmp_path, shells, [])
    storage = AUTHORIZATION_V4['apiInfo']['storageApi']

    run = _authorized(pod, shells, 'b2_listed vm/ >/dev/null')

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [BUCKET['id'], BUCKET['name'], storage['apiUrl'], storage['downloadUrl']]
    assert [call[-1] for call in pod.curl_arguments()] == [
        'https://api.backblazeb2.com/b2api/v4/b2_authorize_account',
        f'{storage["apiUrl"]}/b2api/v4/b2_list_file_names',
    ]
    token = AUTHORIZATION_V4['authorizationToken']
    assert (pod.work / 'b2-account').read_text().startswith(f'Authorization: {token}\n')


@pytest.mark.parametrize(
    ('buckets', 'refusal'),
    [
        ([], 'to no bucket'),
        (AUTHORIZATION_V4['apiInfo']['storageApi']['allowed']['buckets'][:2], 'to more than one bucket'),
        ([{'id': None, 'name': BUCKET['name']}], 'no id'),
    ],
    ids=['none', 'two', 'no id'],
)
def test_an_authorization_that_does_not_list_one_bucket_with_an_id_is_refused(
    tmp_path: Path, shells: Mapping[str, Path], buckets: list[dict[str, str | None]], refusal: str
) -> None:
    """No bucket, two, or one B2 gives no id: refused by what is wrong, before any call reads the bucket.

    The key is restricted to one bucket, so an answer listing any other
    number is not the key the scripts were handed, and every call but a
    download by name addresses the bucket by its id.
    """
    pod = Pod(tmp_path, shells, [])
    pod.restrict(buckets)

    run = _authorized(pod, shells)

    assert run.returncode != 0
    assert refusal in run.stderr
    assert not (pod.work / 'b2-account').exists()
    assert len(pod.curl_arguments()) == 1


def test_a_bucket_b2_names_null_authorizes_and_only_a_read_by_name_is_refused(
    tmp_path: Path, shells: Mapping[str, Path]
) -> None:
    """B2's own example answers `null` for a bucket's name: the listing by id goes on, and `b2_cat` refuses, naming why.

    A refusal ahead of the call is what keeps a download from being sent to
    `/file//<name>`, an address of no bucket.
    """
    pod = Pod(tmp_path, shells, [])
    pod.restrict([{'id': BUCKET['id'], 'name': None}])
    (pod.bucket / 'vm').mkdir()
    (pod.bucket / 'vm' / 'record').write_text('42')

    listed = _authorized(pod, shells, 'b2_listed vm/ >/dev/null')
    read = _authorized(pod, shells, 'b2_cat vm/record')

    assert listed.returncode == 0, listed.stderr
    assert read.returncode != 0
    assert "named no bucket, and vm/record is read by its bucket's name" in read.stderr
    assert not [call for call in pod.curl_arguments() if '/file/' in call[-1]]


@pytest.mark.parametrize('store', ['recent', 'history'])
def test_an_empty_store_with_a_backup_is_restored(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path], store: str
) -> None:
    """From its own prefix, by the `vmrestore` command the container declares, into the store's data directory."""
    pod = _restore_pod(applied, store, tmp_path, shells)
    _backed_up(pod, _prefix(pod))

    assert pod.succeeded(), pod.stderr
    args = pod.ran('vmrestore')
    assert args is not None
    assert f'-storageDataPath={pod.data}' in args
    assert any(arg.startswith('-src=s3://') and arg.endswith('/' + _prefix(pod).rstrip('/')) for arg in args)
    assert (pod.data / 'restored-part').exists()


def test_an_empty_store_with_no_backup_starts_empty(applied: Run, tmp_path: Path, shells: Mapping[str, Path]) -> None:
    """The first install: nothing at the store's prefix, whatever another store's prefix holds."""
    pod = _restore_pod(applied, 'history', tmp_path, shells)
    _backed_up(pod, 'vm/monitoring/recent/')

    assert pod.succeeded(), pod.stderr
    assert pod.ran('vmrestore') is None
    assert list(pod.data.iterdir()) == []


def test_a_store_holding_data_starts_on_it_without_reading_the_bucket(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path]
) -> None:
    """A restarted store keeps what it holds, with the bucket unreachable all the while."""
    pod = _restore_pod(applied, 'history', tmp_path, shells)
    _backed_up(pod, _prefix(pod))
    (pod.data / 'flock.lock').write_text('')
    (pod.state / 'down').write_text('')

    assert pod.succeeded(), pod.stderr
    assert pod.ran('vmrestore') is None


@pytest.mark.parametrize('failure', ['bucket down', 'key refused', 'listing fails', 'restore fails', 'data unreadable'])
def test_a_backup_that_cannot_be_read_keeps_the_store_from_starting(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path], failure: str
) -> None:
    """Whatever stops the read -- the bucket, the key, its listing, `vmrestore`, the data directory -- the store waits.

    A listing that fails after the key is authorized reads as nothing listed
    unless its failure stops the container, and nothing listed starts the
    store empty over the backup that exists.
    """
    pod = _restore_pod(applied, 'history', tmp_path, shells)
    _backed_up(pod, _prefix(pod))
    if failure == 'bucket down':
        (pod.state / 'down').write_text('')
    elif failure == 'key refused':
        pod.key = 'another-id:another-secret'
    elif failure == 'listing fails':
        (pod.state / 'list-down').write_text('')
    elif failure == 'restore fails':
        (pod.state / 'fails').write_text('vmrestore')
    else:
        pod.data.rmdir()

    assert not pod.succeeded()
    assert (pod.ran('vmrestore') is not None) is (failure == 'restore fails')
    assert not pod.data.exists() or list(pod.data.iterdir()) == []


@pytest.mark.parametrize('held', [True, False])
def test_a_restore_that_did_not_finish_is_run_again_and_never_started_on(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path], held: bool
) -> None:
    """`vmrestore`'s lock file left in the directory: restored again, or refused where the backup is gone."""
    pod = _restore_pod(applied, 'history', tmp_path, shells)
    if held:
        _backed_up(pod, _prefix(pod))
    (pod.data / 'restore-in-progress').write_text('')

    assert pod.succeeded() is held, pod.stderr
    assert (pod.ran('vmrestore') is not None) is held


def _metrics(pod: Pod, rows: int) -> None:
    """The store's exposition with `rows` rows across its parts, beside its index's items, which are not rows."""
    small, big = rows // 3, rows - rows // 3
    (pod.state / 'metrics').write_text(
        f'vm_rows{{type="indexdb/file"}} 999999999\n'
        f'vm_rows{{type="storage/inmemory"}} 0\n'
        f'vm_rows{{type="storage/small"}} {small}\n'
        f'vm_rows{{type="storage/big"}} {big}\n'
        'vm_rows_ignored_total{reason="too_many_labels"} 7\n'
    )


def _recorded(pod: Pod, text: str) -> Path:
    record = _record(pod)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(text)
    return record


@pytest.mark.parametrize('recorded', [None, 41_000_000_000, 42_000_000_000])
def test_the_history_store_is_backed_up_and_its_rows_recorded_where_it_holds_no_fewer(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path], recorded: int | None
) -> None:
    """The backup runs against the store's snapshot endpoint, and the count read becomes the record."""
    from kluster.components.monitoring import stores

    pod = _backup_pod(applied, 'history', tmp_path, shells)
    _metrics(pod, 42_000_000_000)
    if recorded is not None:
        _ = _recorded(pod, str(recorded))

    assert pod.succeeded(), pod.stderr
    args = pod.ran('vmbackup')
    assert args is not None
    assert any(
        arg.startswith('-snapshot.createURL=http://')
        and arg.endswith(f'/snapshot/create?authKey=%{{{stores.SNAPSHOT_KEY_ENV}}}')
        for arg in args
    )
    assert _record(pod).read_text() == '42000000000'


@pytest.mark.parametrize('failure', ['fewer rows', 'listing fails', 'record not a number', 'record empty'])
def test_the_history_store_is_not_backed_up_where_its_record_does_not_clear_it(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path], failure: str
) -> None:
    """Fewer rows than the record, a record that cannot be listed, or one that is no count: no backup, the record kept.

    An empty record that is listed is no count either, rather than no record.

    A store that started without its history would otherwise overwrite the
    history in the bucket, and lower the record so that every later run
    passes. A listing that fails after the key is authorized reads as no
    record unless its failure stops the container.
    """
    pod = _backup_pod(applied, 'history', tmp_path, shells)
    _metrics(pod, 1_000)
    held = {'record not a number': 'some rows', 'record empty': ''}.get(failure, '42000000000')
    record = _recorded(pod, held)
    if failure == 'listing fails':
        (pod.state / 'list-down').write_text('')

    assert not pod.succeeded()
    assert pod.ran('vmbackup') is None
    assert record.read_text() == held


def test_a_failed_backup_records_nothing(applied: Run, tmp_path: Path, shells: Mapping[str, Path]) -> None:
    """The record is of the last backup that completed, so a count is never recorded for a backup that is not there."""
    pod = _backup_pod(applied, 'history', tmp_path, shells)
    _metrics(pod, 1_000)
    (pod.state / 'fails').write_text('vmbackup')

    assert not pod.succeeded()
    assert not _record(pod).exists()


def test_the_recent_store_is_backed_up_whatever_it_holds(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path]
) -> None:
    """Its rows age out with its retention, so its count falls, and its backup reads neither the store nor a record."""
    pod = _backup_pod(applied, 'recent', tmp_path, shells)
    (pod.state / 'down').write_text('')

    assert pod.succeeded(), pod.stderr
    assert pod.ran('vmbackup') is not None
    assert pod.curl_arguments() == []


def test_no_credential_is_an_argument_of_any_process_the_scripts_start(
    applied: Run, tmp_path: Path, shells: Mapping[str, Path]
) -> None:
    """The node exporter reads every process's arguments on the node; the key and B2's tokens reach `curl` as files.

    Every call the restore and the history's backup make, recorded by the
    fake `curl`, is read for the key's two halves, the Basic credential made
    of them, and each token the fake bucket hands out. The files they are
    written to instead are their owner's alone, since the node exporter
    reads the host's filesystem too.
    """
    basic = base64.b64encode(':'.join(FAKE_KEY).encode()).decode()
    secrets = (*FAKE_KEY, basic, *FAKE_TOKENS)
    restore = _restore_pod(applied, 'history', tmp_path / 'restore', shells)
    _backed_up(restore, _prefix(restore))
    backup = _backup_pod(applied, 'history', tmp_path / 'backup', shells)
    _metrics(backup, 1_000)
    _ = _recorded(backup, '1')

    assert restore.succeeded(), restore.stderr
    assert backup.succeeded(), backup.stderr
    calls = [*restore.curl_arguments(), *backup.curl_arguments()]
    assert len(calls) >= 6
    for call in calls:
        assert not [secret for secret in secrets if any(secret in arg for arg in call)], call
    written = [path for pod in (restore, backup) for path in pod.work.iterdir()]
    assert written
    for path in written:
        assert path.stat().st_mode & 0o077 == 0, f'{path.name} is readable beyond its owner'


def test_no_container_is_handed_a_secret_in_its_command(applied: Run) -> None:
    """The snapshot key and the writer key reach every container through its environment, from their Secrets."""
    from kluster.components.monitoring import AUTH_KEYS_SECRET

    containers = [
        *[container for job in _backup_jobs(applied).values() for container in _pod_containers(job)],
        *[container for restores in _restores(applied).values() for container in restores],
    ]
    keyed = {
        it['name']: it['valueFrom']['secretKeyRef']
        for container in containers
        for it in container.get('env', [])
        if 'valueFrom' in it
    }

    assert {ref['name'] for ref in keyed.values()} == {WRITER_KEY.name, AUTH_KEYS_SECRET}
    for container in containers:
        assert not [arg for arg in container['command'] if 'generated-in-state' in arg or 'AgB-sealed' in arg]


# -- The scheduled-job family ------------------------------------------------------


async def _apps_program(*beside: Callable[[], object]) -> Recorder:
    """The `apps` program, every sealed value its stack declares sealed in, against a `physical` that has published.

    `beside` is declared in the same run after the program, as a land wires a
    component into it.
    """
    from kluster.stacks import apps as program

    secrets = {
        f'kluster:{KUBECONFIG_KEY}': 'a-fake-kubeconfig-that-reaches-no-cluster',
        f'kluster:{program.CLOUDFLARE_API_TOKEN}': 'a-zones-token-that-reaches-no-account',
    }
    sealed = {
        value.name: {key: f'AgB-sealed-{value.name}-{key}' for key in value.keys}
        for value in conventions.sealed.VALUES.values()
        if value.stack == conventions.STACK_NAMES.apps
    }
    config = secrets | {f'kluster:{conventions.sealed.CONFIG_KEY}': json.dumps(sealed)}
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=list(secrets))
    monitor = await run_under_backstop(Physical(), stack=conventions.STACK_NAMES.apps)
    async with declaring():
        await program.main()
        for declare in beside:
            _ = declare()
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def apps() -> Recorder:
    """The `apps` program as its stack file holds it."""
    return await _apps_program()


#: The type the provider registers a Kubernetes provider under, which every
#: program that declares a CronJob opens.
KUBERNETES_PROVIDER = 'pulumi:providers:kubernetes'


def _cronjobs(*monitors: Recorder) -> dict[tuple[str, str], dict[str, Any]]:
    """Every CronJob the runs declare, by its namespace and its name: its spec."""
    declared: dict[tuple[str, str], dict[str, Any]] = {}
    for monitor in monitors:
        for job in monitor.of_type(CRONJOB):
            metadata = job.inputs['metadata']
            key = (metadata['namespace'], metadata['name'])
            assert key not in declared, f'two CronJobs declared as {key}'
            declared[key] = job.inputs['spec']
    return declared


#: A cron line's period, by which of its fields hold a value rather than `*`:
#: the minute alone is hourly, the minute and the hour daily, and those and a
#: day of the week weekly (crontab(5)).
CRON_PERIODS: dict[tuple[bool, ...], dt.timedelta] = {
    (True, False, False, False, False): dt.timedelta(hours=1),
    (True, True, False, False, False): dt.timedelta(days=1),
    (True, True, False, False, True): dt.timedelta(weeks=1),
}


def _period(schedule: str) -> dt.timedelta:
    """How often a cron line of one of `CRON_PERIODS`' shapes fires, each field it sets one number."""
    fields = schedule.split()
    assert all(field == '*' or field.isdigit() for field in fields), schedule
    shape = tuple(field != '*' for field in fields)
    assert shape in CRON_PERIODS, f'{schedule!r} is no cron line of a single period'
    return CRON_PERIODS[shape]


class _WiredDatabase(Component, pulumi_type='kluster:test:WiredDatabase'):
    """One database, declared as an application's component declares it at its land: a CronJob and its rules beside it."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        image = ImagePin('registry.example.invalid/an-image', 'v1', f'sha256:{"d" * 64}')
        _ = database(
            f'{name}-database',
            'a-database',
            1,
            backup.STANDARD,
            namespace=WIRED_NAMESPACE,
            image=image,
            dbname='app',
            resources=Resources(requests={'cpu': '100m', 'memory': '256Mi'}, limits={'cpu': '1', 'memory': '512Mi'}),
            node_affinity=WORKER_AFFINITY['nodeAffinity'],
            writer_key=dict.fromkeys(conventions.WRITER_KEY_FIELDS, 'AgB-sealed'),
            drill=Drill(table='a_table'),
            drill_image=image,
            opts=self.child_opts(),
        )
        self.register_outputs({})


#: The namespace of the database `wired` declares beside the `apps` program.
WIRED_NAMESPACE = 'a-namespace'


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def wired() -> Recorder:
    """The `apps` program as `apps` runs it, and beside it a database, which declares a CronJob and its rules itself."""
    return await _apps_program(lambda: _WiredDatabase('wired'))


#: What a scheduled-job family rule selects its CronJob's series by.
_CRONJOB_SELECTOR = re.compile(r'\{namespace="(?P<namespace>[^"]+)", cronjob="(?P<cronjob>[^"]+)"\}')


def _watched(*monitors: Recorder) -> dict[tuple[str, str], list[str]]:
    """The family's alerts the runs declare, by the namespace and the CronJob each selects: the alerts' names."""
    watched: dict[tuple[str, str], list[str]] = {}
    for monitor in monitors:
        for declared in monitor.of_type(VM_RULE):
            for group in declared.inputs['spec']['groups']:
                for rule in group['rules']:
                    found = _CRONJOB_SELECTOR.search(rule.get('expr', ''))
                    if 'alert' in rule and found is not None:
                        watched.setdefault((found['namespace'], found['cronjob']), []).append(rule['alert'])
    return watched


def test_every_cronjob_a_program_declares_is_watched_by_the_family_and_every_family_rule_by_a_cronjob(
    applied: Run, wired: Recorder
) -> None:
    """A CronJob the family does not watch is a job nothing reports stopping, wherever its job's facts are spelled.

    Read off the two programs that open the cluster, `k8s-base` and `apps`,
    each with every sealed value in, which is when each declares its every
    CronJob, and a database beside `apps`, whose component declares its
    drill's CronJob and the family's rules over it. Each CronJob is watched by
    exactly one stale and one failed alert, whether `k8s-base` declares them
    from a row of `SCHEDULED_JOBS` or the declaring component beside it: a
    CronJob watched both ways, or by two rows, raises each alert twice, one
    per group. A family rule over a CronJob nobody declares is two alerts
    over no series.
    """
    for monitor in (applied.monitor, wired):
        assert monitor.of_type(KUBERNETES_PROVIDER), 'a run that opened no cluster'
    rows = [(job.namespace, job.cronjob) for job in schedule.SCHEDULED_JOBS]
    cronjobs = set(_cronjobs(applied.monitor, wired))
    watched = _watched(applied.monitor, wired)

    assert len(set(rows)) == len(rows)
    assert set(rows) <= cronjobs
    assert (WIRED_NAMESPACE, 'a-database-restore-drill') in cronjobs
    for key in sorted(cronjobs):
        alerts = watched.get(key, [])
        stale = [name for name in alerts if name.endswith('Stale')]
        failed = [name for name in alerts if name.endswith('Failed')]
        assert len(stale) == len(failed) == 1 and len(alerts) == 2, (key, alerts)
    assert set(watched) == cronjobs


def test_each_cronjob_runs_at_its_rows_period_and_is_cut_off_at_its_rows_deadline(applied: Run, apps: Recorder) -> None:
    """The period is what the stale alert is held to and the deadline what the failed one waits for.

    A run is cut off at the deadline, which `Forbid` would otherwise let hold
    every later run back, so the failed alert finds the run either succeeded
    or failed by then. A run starts no later than the row's starting
    deadline, which the row holds inside its slack with the deadline, and a
    CronJob whose row has none sets none.
    """
    rows = {(job.namespace, job.cronjob): job for job in schedule.SCHEDULED_JOBS}

    # A CronJob whose component declares its rules beside it holds its job's
    # facts there, and that component's tests hold the CronJob to them.
    for key, spec in _cronjobs(applied.monitor, apps).items():
        row = rows.get(key)
        if row is None:
            continue
        starting = row.starting_deadline.total_seconds() if row.starting_deadline is not None else None
        assert _period(spec['schedule']) == row.period, key
        assert spec['jobTemplate']['spec']['activeDeadlineSeconds'] == row.deadline.total_seconds(), key
        assert spec.get('startingDeadlineSeconds') == starting, key
        assert spec['concurrencyPolicy'] == 'Forbid', key


def test_each_row_raises_a_stale_and_a_failed_alert_on_its_cronjob_naming_its_playbook(
    applied: Run,
) -> None:
    """Stale over `max_age` of the row's period, failed held for its deadline, both `actionable`, on its CronJob alone.

    The expressions' behavior over kube-state-metrics' series is not read
    here: what holds is each rule's relation to its row.
    """
    declared = _declared_rules(applied)

    for job in schedule.SCHEDULED_JOBS:
        selector = f'{{namespace="{job.namespace}", cronjob="{job.cronjob}"}}'
        reading = {rule['alert']: rule for rule in declared if selector in rule['expr']}
        assert set(reading) == {f'{job.alert}Stale', f'{job.alert}Failed'}, job.cronjob
        stale, failed = reading[f'{job.alert}Stale'], reading[f'{job.alert}Failed']

        assert stale['expr'].endswith(f'> {backup.duration(backup.max_age(job.period))}'), stale['expr']
        assert 'for' not in stale
        assert _duration(failed['for']) == job.deadline

        for rule in (stale, failed):
            assert rule['labels'][TIER_LABEL] == Tier.ACTIONABLE.value
            assert rule['annotations']['playbook'] == str(job.playbook)
            assert job.cronjob in rule['annotations']['summary']


def _kube_state_metrics_endpoint(run: Run) -> dict[str, Any]:
    """The one endpoint of kube-state-metrics' scrape, which publishes every CronJob's status."""
    (endpoint,) = _values(run)['kube-state-metrics']['vmScrape']['spec']['endpoints']
    return endpoint


def test_vmalert_evaluates_the_rules_declared_in_every_namespace(applied: Run) -> None:
    """A component that declares a CronJob and its rules beside it declares them in its own namespace.

    The operator selects every `VMRule` in every namespace it watches when
    `selectAllByDefault` is set and no rule selector narrows it
    (`VMAlert.spec`, at operator v0.74.0); a database's drill declares its
    rules in its application's namespace, and nothing else would read them.
    """
    spec = _values(applied)['vmalert']['spec']

    assert spec['selectAllByDefault'] is True
    assert 'ruleSelector' not in spec
    assert 'ruleNamespaceSelector' not in spec


def test_a_status_reaches_the_rules_inside_the_lag_every_deadline_leaves(applied: Run) -> None:
    """A CronJob's status is published at the next scrape and read at the next evaluation after it.

    Both intervals are read off what the program declares: the interval of
    kube-state-metrics' scrape, which publishes the status, and vmalert's
    evaluation interval.
    """
    scrape = _duration(_kube_state_metrics_endpoint(applied)['interval'])
    evaluation = _duration(_values(applied)['vmalert']['spec']['evaluationInterval'])

    assert scrape + evaluation < schedule.REPORT_LAG


#: The labels the operator gives a target of kube-state-metrics' scrape, from
#: the Service and the pod it finds it through, in the namespace
#: kube-state-metrics runs in (`vmservicescrape.go` at operator v0.74.0).
KUBE_STATE_METRICS_TARGET = {
    'namespace': conventions.MONITORING_NAMESPACE,
    'service': 'kube-state-metrics',
    'pod': 'kube-state-metrics-0',
    'container': 'kube-state-metrics',
    'endpoint': 'http',
    'job': 'kube-state-metrics',
    'instance': '10.244.0.10:8080',
}


def test_a_cronjobs_series_carries_the_namespace_its_rows_rules_select_it_by(applied: Run) -> None:
    """kube-state-metrics publishes a CronJob's series under the CronJob's namespace, which the target's own would replace.

    Without `honorLabels` a label the target carries wins, and the sample's is
    kept as `exported_<name>` (Prometheus' semantics, which vmagent follows),
    so every series would carry the namespace kube-state-metrics runs in. A
    row in that namespace would still match; a row in any other would match
    no series, and its alerts would never fire.
    """
    endpoint = _kube_state_metrics_endpoint(applied)

    for job in schedule.SCHEDULED_JOBS:
        sample = {
            '__name__': 'kube_cronjob_status_last_successful_time',
            'namespace': job.namespace,
            'cronjob': job.cronjob,
        }
        if endpoint.get('honorLabels'):
            scraped = {**KUBE_STATE_METRICS_TARGET, **sample}
        else:
            clashing = {
                f'exported_{name}': value for name, value in sample.items() if name in KUBE_STATE_METRICS_TARGET
            }
            scraped = {**sample, **clashing, **KUBE_STATE_METRICS_TARGET}
        series = _relabel(scraped, endpoint.get('metricRelabelConfigs', []))

        assert series is not None, job.cronjob
        assert (series['namespace'], series['cronjob']) == (job.namespace, job.cronjob)


def _row(
    *, period: dt.timedelta, deadline: dt.timedelta, starting_deadline: dt.timedelta | None = None
) -> schedule.ScheduledJob:
    return schedule.ScheduledJob(
        alert='Drill',
        namespace='a-namespace',
        cronjob='a-job',
        period=period,
        deadline=deadline,
        starting_deadline=starting_deadline,
        playbook=conventions.alert.Playbook('docs/operations.md', '4'),
    )


@pytest.mark.parametrize('period', [dt.timedelta(hours=1), dt.timedelta(days=1), dt.timedelta(weeks=1)])
def test_a_deadline_past_what_its_period_leaves_is_refused_naming_the_cronjob(period: dt.timedelta) -> None:
    """A run that eventually succeeds is never reported stale, and the failed alert fires before the stale one.

    The stale alert fires one and a half periods after the last success; the
    next run starts at the latest one period after it, and its success takes
    `REPORT_LAG` to reach the rules. So a deadline may be half a period less
    the lag, and no more.
    """
    slack = period / 2 - schedule.REPORT_LAG

    assert _row(period=period, deadline=slack).deadline == slack
    for deadline in (slack + dt.timedelta(seconds=1), dt.timedelta(0)):
        with pytest.raises(ValueError, match="'a-job'"):
            _ = _row(period=period, deadline=deadline)


@pytest.mark.parametrize('period', [dt.timedelta(hours=1), dt.timedelta(days=1), dt.timedelta(weeks=1)])
def test_a_starting_deadline_takes_its_share_of_the_slack_and_one_that_starts_nothing_is_refused(
    period: dt.timedelta,
) -> None:
    """A run that starts as late as the starting deadline succeeds that much later, so the two share the slack.

    A starting deadline of nothing would skip every run, and is refused too.
    """
    slack = period / 2 - schedule.REPORT_LAG
    late = slack / 3

    assert _row(period=period, deadline=slack - late, starting_deadline=late).deadline == slack - late
    for deadline, starting in ((slack - late + dt.timedelta(seconds=1), late), (slack / 2, dt.timedelta(0))):
        with pytest.raises(ValueError, match="'a-job'"):
            _ = _row(period=period, deadline=deadline, starting_deadline=starting)


# -- The pull of OCI's block-volume series ------------------------------------------

OCI_KEY = conventions.sealed.OCI_METRICS


def _named(run: Run, typ: str, name: str) -> list[dict[str, Any]]:
    return [it.inputs for it in run.monitor.of_type(typ) if it.inputs.get('metadata', {}).get('name') == name]


def _pull(run: Run) -> tuple[dict[str, Any], dict[str, Any]]:
    """The pull's CronJob spec and its ConfigMap's data."""
    from kluster.components.monitoring import oci_metrics

    (job,) = _named(run, CRONJOB, schedule.OCI_METRICS_PULL.cronjob)
    (program,) = _named(run, CONFIGMAP, oci_metrics.PULL_CONFIGMAP)
    return job['spec'], program['data']


def _pull_pod(run: Run) -> dict[str, Any]:
    job, _ = _pull(run)
    return job['jobTemplate']['spec']['template']['spec']


def _settings(run: Run, tmp_path: Path) -> Any:
    """The pull's settings, read as the program reads them."""
    from kluster.components.monitoring import oci_metrics, oci_pull

    _, data = _pull(run)
    path = tmp_path / oci_metrics.SETTINGS_FILE
    path.write_text(data[oci_metrics.SETTINGS_FILE])
    return oci_pull.Job.read(path, credentials=tmp_path)


def test_the_pull_runs_hourly_one_run_at_a_time_apart_from_the_backups(applied: Run) -> None:
    """No run shares a minute with a store's backup; how long a run may take is its row's (the family's cases)."""
    job, _ = _pull(applied)
    minute, *every = job['schedule'].split()
    backups = {it['spec']['schedule'].split()[0] for it in _backup_jobs(applied).values()}

    assert every == ['*'] * 4
    assert minute not in backups
    assert job['concurrencyPolicy'] == 'Forbid'


def test_the_pull_runs_its_pin_on_the_program_and_settings_its_config_map_carries(applied: Run) -> None:
    """The image is the runtime alone; the program and its settings arrive as files of the mounted ConfigMap."""
    from kluster.components.monitoring import oci_metrics, oci_pull

    pod = _pull_pod(applied)
    _, data = _pull(applied)
    (container,) = pod['containers']
    (program_volume,) = [
        it for it in pod['volumes'] if it.get('configMap', {}).get('name') == oci_metrics.PULL_CONFIGMAP
    ]
    (mount,) = [it for it in container['volumeMounts'] if it['name'] == program_volume['name']]
    files = [arg for arg in container['command'] if arg.startswith(f'{mount["mountPath"]}/')]

    assert container['image'] == VERSIONS_CONFIG['versions:image-oci-sdk']
    assert [Path(it).name for it in files] == [oci_metrics.PROGRAM_FILE, oci_metrics.SETTINGS_FILE]
    assert data[oci_metrics.PROGRAM_FILE] == Path(oci_pull.__file__).read_text()


def test_the_key_is_declared_where_the_pull_reads_it_as_the_files_the_program_opens(applied: Run) -> None:
    """The SealedSecret opens into the Secret the pod mounts, at the directory the program is told, a file per key."""
    from kluster.components.monitoring import oci_pull

    (secret,) = _named(applied, SEALED_SECRET, OCI_KEY.name)
    pod = _pull_pod(applied)
    (container,) = pod['containers']
    (key_volume,) = [it for it in pod['volumes'] if it.get('secret', {}).get('secretName') == OCI_KEY.name]
    (mount,) = [it for it in container['volumeMounts'] if it['name'] == key_volume['name']]

    assert secret['metadata']['namespace'] == conventions.MONITORING_NAMESPACE
    assert secret['spec']['encryptedData'] == SEALED[OCI_KEY.name]
    assert set(secret['spec']['encryptedData']) == set(oci_pull.CREDENTIAL_KEYS)
    assert container['command'][-1] == mount['mountPath']
    assert not [arg for arg in container['command'] if 'AgB-sealed' in arg]


def _census() -> set[tuple[str, str]]:
    """Every volume the census declares: each cloud node's boot volume, and each node volume on its node."""
    return {
        *((node, BOOT_VOLUME) for node in conventions.CLOUD_NODES),
        *((entry.attached_node, name) for name, entry in conventions.NODE_VOLUMES.items()),
    }


def test_the_pulls_targets_are_every_declared_volume_by_the_ocid_physical_published(
    applied: Run, tmp_path: Path
) -> None:
    """OCI names a volume by its OCID alone, so the settings carry the node and the name `conventions` gives it."""
    published = cast('dict[str, dict[str, str]]', PUBLISHED[conventions.PHYSICAL_OUTPUTS.volume_ids])

    targets = _settings(applied, tmp_path).targets

    assert {(it.node, it.volume) for it in targets.values()} == _census()
    assert all(published[it.node][it.volume] == ocid for ocid, it in targets.items())


def test_the_pull_reads_the_history_store_and_writes_through_vmagent_in_physicals_compartment(
    applied: Run, tmp_path: Path
) -> None:
    """The newest samples are the history's; the samples reach both stores the way a scraped series does.

    vmagent's address is the operator's Service for the stack's VMAgent, which
    the chart names as it names the recent store (`vmagent-<object>` on 8429,
    `vmagent_types.go` L567-572 and L615-619 at operator v0.74.0).
    """
    settings = _settings(applied, tmp_path)
    stack_object = f'{conventions.MONITORING_NAMESPACE}-victoria-metrics-k8s-stack'

    assert f'{settings.history}/api/v1/write' == _history_write(applied)['url']
    assert settings.importer == f'http://vmagent-{stack_object}.{conventions.MONITORING_NAMESPACE}.svc:8429'
    assert settings.compartment == conventions.OCI_TENANCY.compartments[conventions.PHYSICAL].ocid


def test_every_series_the_pull_writes_is_kept_by_the_history_store(applied: Run) -> None:
    """The pull names its series after the namespace its requests name, and the history's class keeps that prefix."""
    from kluster.components.monitoring import oci_pull

    relabeling = _history_write(applied)['inlineUrlRelabelConfig']

    for metric in oci_pull.METRICS:
        series = {'__name__': f'{OCI_BLOCKSTORE_METRICS}_{metric}', 'node': 'cp1', 'volume': BOOT_VOLUME}
        assert _relabel(series, relabeling) is not None, metric


def test_the_pull_can_schedule_on_the_worker_alone(applied: Run) -> None:
    """With the rest of the stack, off the cores the cloud nodes share with etcd."""
    pod = _pull_pod(applied)

    schedulable = {
        name
        for name, labels in _talos_nodes().items()
        if _admits(labels, pod.get('nodeSelector', {}), pod.get('affinity', {}))
    }

    assert schedulable == {conventions.HOMELAB_NODE}


def test_a_declared_volume_physical_published_no_ocid_for_is_refused_by_name() -> None:
    """A targeted `physical` apply that left a volume uncreated would otherwise leave it pulled by nothing, silently."""
    from kluster.components.monitoring import oci_metrics

    (name, entry), *_ = conventions.NODE_VOLUMES.items()
    published = {node: {BOOT_VOLUME: f'ocid1.bootvolume.oc1.phx.{node}'} for node in conventions.CLOUD_NODES}

    with pytest.raises(oci_metrics.VolumeUnpublished) as refused:
        _ = oci_metrics.targets(
            published, nodes=conventions.CLOUD_NODES, boot_volume=BOOT_VOLUME, node_volumes=conventions.NODE_VOLUMES
        )

    assert refused.value.target == oci_metrics.Target(entry.attached_node, name)
    assert repr(name) in str(refused.value)
    assert entry.attached_node in str(refused.value)


def test_a_volume_physical_published_that_the_census_does_not_declare_is_not_pulled() -> None:
    """Only what the census declares is a target; the map's other entries are ignored."""
    from kluster.components.monitoring import oci_metrics

    published = {
        node: {
            BOOT_VOLUME: f'ocid1.bootvolume.oc1.phx.{node}',
            'undeclared': f'ocid1.volume.oc1.phx.undeclared-{node}',
            **{
                name: f'ocid1.volume.oc1.phx.{name}'
                for name, entry in conventions.NODE_VOLUMES.items()
                if entry.attached_node == node
            },
        }
        for node in conventions.CLOUD_NODES
    }

    targets = oci_metrics.targets(
        published, nodes=conventions.CLOUD_NODES, boot_volume=BOOT_VOLUME, node_volumes=conventions.NODE_VOLUMES
    )

    assert {(it.node, it.volume) for it in targets.values()} == _census()


# -- The state backend's checks -------------------------------------------------------

FRESHNESS_KEY = conventions.sealed.B2_FRESHNESS_DUMPS


def _check_pod(run: Run, row: schedule.ScheduledJob) -> dict[str, Any]:
    """The pod spec of the check `row` is the row of."""
    (job,) = _named(run, CRONJOB, row.cronjob)
    return job['spec']['jobTemplate']['spec']['template']['spec']


#: Each check, by its row.
CHECKS = (schedule.STATE_BACKEND_DUMPS, schedule.STATE_BACKEND_CERTIFICATE)


@pytest.mark.parametrize('row', CHECKS, ids=lambda row: row.cronjob)
def test_each_check_runs_its_pin_on_the_program_and_settings_its_config_map_carries(
    applied: Run, tmp_path: Path, row: schedule.ScheduledJob
) -> None:
    """The image is the runtime alone; the program and the settings arrive as files of the mounted ConfigMap."""
    from kluster.components.monitoring import state_backend, state_backend_check

    pod = _check_pod(applied, row)
    (configmap,) = _named(applied, CONFIGMAP, state_backend.CHECKS_CONFIGMAP)
    (container,) = pod['containers']
    (program_volume,) = [
        it for it in pod['volumes'] if it.get('configMap', {}).get('name') == state_backend.CHECKS_CONFIGMAP
    ]
    (mount,) = [it for it in container['volumeMounts'] if it['name'] == program_volume['name']]
    files = [arg for arg in container['command'] if arg.startswith(f'{mount["mountPath"]}/')]
    path = tmp_path / state_backend.SETTINGS_FILE
    path.write_text(configmap['data'][state_backend.SETTINGS_FILE])

    assert container['image'] == VERSIONS_CONFIG['versions:image-oci-sdk']
    assert [Path(it).name for it in files] == [state_backend.PROGRAM_FILE, state_backend.SETTINGS_FILE]
    assert configmap['data'][state_backend.PROGRAM_FILE] == Path(state_backend_check.__file__).read_text()
    assert state_backend_check.Settings.read(path).address == backend_settings.ADDRESS


def test_the_dump_check_reads_the_key_its_row_seals_at_the_directory_its_command_passes(applied: Run) -> None:
    """The SealedSecret opens into the Secret the pod mounts, in the row's namespace, at the directory the program is told."""
    (secret,) = _named(applied, SEALED_SECRET, FRESHNESS_KEY.name)
    pod = _check_pod(applied, schedule.STATE_BACKEND_DUMPS)
    (container,) = pod['containers']
    (key_volume,) = [it for it in pod['volumes'] if it.get('secret', {}).get('secretName') == FRESHNESS_KEY.name]
    (mount,) = [it for it in container['volumeMounts'] if it['name'] == key_volume['name']]

    assert secret['metadata']['namespace'] == schedule.STATE_BACKEND_DUMPS.namespace
    assert secret['spec']['encryptedData'] == SEALED[FRESHNESS_KEY.name]
    assert container['command'][-1] == mount['mountPath']
    assert not [arg for arg in container['command'] if 'AgB-sealed' in arg]


def test_the_certificate_check_mounts_no_secret(applied: Run) -> None:
    """The handshake takes no credential, so the pod that makes it is handed none."""
    pod = _check_pod(applied, schedule.STATE_BACKEND_CERTIFICATE)

    assert [it for it in pod['volumes'] if 'secret' in it or 'projected' in it] == []
    assert pod['automountServiceAccountToken'] is False


@pytest.mark.parametrize('row', CHECKS, ids=lambda row: row.cronjob)
def test_each_check_can_schedule_on_the_worker_alone(applied: Run, row: schedule.ScheduledJob) -> None:
    """With the rest of the stack, off the cores the cloud nodes share with etcd."""
    pod = _check_pod(applied, row)

    schedulable = {
        name
        for name, labels in _talos_nodes().items()
        if _admits(labels, pod.get('nodeSelector', {}), pod.get('affinity', {}))
    }

    assert schedulable == {conventions.HOMELAB_NODE}
