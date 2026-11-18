"""Monitoring, declared against mocks: the stack's settings, its placement, the rules and alertmanager's receiver.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), twice: as it stands before alertmanager's webhook is
sealed in, and with it sealed in (rfc-007 §7, physical/bring-up.md §1). Before,
the random provider's password is answered as it would be generated into state.

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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import image_busybox
import process_sessions
import pulumi
import pytest
import pytest_asyncio
import yaml
from fences import prose
from k8s_base_installation import (
    PUBLISHED,
    SEALED,
    VERSIONS_CONFIG,
    Autonaming,
    Physical,
    Run,
    applied,
    release_assets,
)
from mock_monitor import Recorder, declaring, run_under_backstop
from renovate_text import listed, package_rules, scalar
from section_numbers import sections

from kluster import conventions
from kluster.components.monitoring import DATASOURCES_DIGEST_ENV, RECEIVER, memory, stack_values
from kluster.components.monitoring.operator_objects import type_token
from kluster.components.monitoring.rules import HEARTBEAT, HEARTBEAT_TIMER, TIER_LABEL
from kluster.components.talos import ETCD_METRICS_PORT
from kluster.conventions import backup, schedule
from kluster.conventions.alert import Tier
from kluster.conventions.backup import BackupFamily
from kluster.conventions.outputs import BOOT_VOLUME
from kluster.conventions.providers import OCI_BLOCKSTORE_METRICS
from kluster.lib.k8s import KUBECONFIG_KEY
from kluster.lib.versions import ChartPin, versions

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


async def declare(*, sealed: dict[str, object] | None) -> tuple[Run, list[str]]:
    """The whole program with `sealed` as its sealed values, and every warning it logged."""
    from kluster.stacks import k8s_base

    config = {f'kluster:{KUBECONFIG_KEY}': 'a-fake-kubeconfig-that-reaches-no-cluster'}
    if sealed is not None:
        config[f'kluster:{conventions.sealed.CONFIG_KEY}'] = json.dumps(sealed)
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=[f'kluster:{KUBECONFIG_KEY}'])
    monitor = await run_under_backstop(Generating(), stack=conventions.STACK_NAMES.k8s_base)
    warned: list[str] = []

    def warn(message: str, *_: object, **__: object) -> None:
        warned.append(message)

    with release_assets() as fetched, pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.log, 'warn', warn)
        async with declaring():
            await k8s_base.main()
    return Run(monitor, fetched), warned


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def absent() -> tuple[Run, list[str]]:
    """The stack as it stands before the webhook is sealed in."""
    return await declare(sealed=None)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def sealed() -> Run:
    """Every sealed value the stack declares sealed in, the webhook among them."""
    return await applied(sealed=SEALED)


def _values(run: Run) -> dict[str, Any]:
    return run.values(STACK)


def _declared_rules(run: Run) -> list[dict[str, Any]]:
    return [
        rule
        for declared in run.monitor.of_type(VM_RULE)
        for group in declared.inputs['spec']['groups']
        for rule in group['rules']
    ]


def _receiver(run: Run) -> dict[str, Any]:
    (declared,) = run.monitor.of_type(VM_ALERTMANAGER_CONFIG)
    return declared.inputs


# -- The installation -------------------------------------------------------------


@pytest.mark.parametrize('chart', [STACK, METRICS_SERVER])
def test_each_chart_is_installed_from_its_pin(absent: tuple[Run, list[str]], chart: str) -> None:
    """The reference and the version are the pin's; an HTTP repository's address is passed beside the name."""
    run, _ = absent
    pin = versions.chart[{STACK: 'victoria-metrics-k8s-stack', METRICS_SERVER: 'metrics-server'}[chart]]
    inputs = run.inputs(CHART, chart)

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
def test_what_the_chart_would_fetch_hook_or_scrape_for_nothing_is_off(
    absent: tuple[Run, list[str]], path: tuple[str, ...]
) -> None:
    """The sync job and the operator's cleanup are hooks, the defaults are downloads, and the three jobs scrape nothing.

    Each is on at the chart's own values (rfc-007 §7.1, cluster-infra.md §1.2),
    so each has to be stated off.
    """
    run, _ = absent
    value: Any = _values(run)
    for key in path:
        value = value[key]

    assert value is False


def test_the_namespace_is_privileged_for_the_node_exporter(absent: tuple[Run, list[str]]) -> None:
    """Talos enforces `baseline`, which refuses the node exporter's host network, PID namespace and paths."""
    run, _ = absent
    namespaces = run.monitor.by_name(NAMESPACE)

    (labels,) = [
        ns['metadata']['labels']
        for ns in namespaces.values()
        if ns['metadata']['name'] == conventions.MONITORING_NAMESPACE
    ]
    assert labels['pod-security.kubernetes.io/enforce'] == 'privileged'
    assert run.inputs(CHART, STACK)['namespace'] == conventions.MONITORING_NAMESPACE


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
def test_the_workload_can_schedule_on_the_worker_alone(absent: tuple[Run, list[str]], workload: str) -> None:
    """Each holds a condition that only the worker meets.

    Alertmanager posts to Home Assistant at an address only the worker
    reaches (rfc-008 §4.1). vmagent reads etcd's metrics listener on every
    control plane, which the node firewall admits from KubeSpan alone; from a
    control plane, its call to its own node's listener would arrive from a
    pod address, which no opening admits (`components.talos.HOST_OPENINGS`).
    """
    run, _ = absent
    spec = _values(run)[workload]['spec']

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
    absent: tuple[Run, list[str]],
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
    run, _ = absent
    admin, secret = _grafana_admin(run)
    data = secret['stringData']

    assert secret['metadata']['namespace'] == run.inputs(CHART, STACK)['namespace']
    # The password reaches the engine as a secret, and so state as one.
    assert {key: data[key] for key in SECRET_ENVELOPE} == SECRET_ENVELOPE
    assert admin['userKey'] in data['value']
    assert data['value'][admin['passwordKey']] in {
        Generating.generated(it.name) for it in run.monitor.of_type(PASSWORD)
    }


def test_the_chart_is_installed_behind_grafanas_secret(absent: tuple[Run, list[str]]) -> None:
    """Grafana's pod reads the Secret when it starts, and pulumi-kubernetes waits for the pod to be ready."""
    run, _ = absent
    admin, _ = _grafana_admin(run)
    (declared,) = [it for it in run.monitor.of_type(SECRET) if it.inputs['metadata']['name'] == admin['existingSecret']]

    assert run.urn(SECRET, declared.name) in run.dependencies(CHART, STACK)


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


def test_no_kubelet_scrape_maps_the_nodes_labels_onto_its_series(absent: tuple[Run, list[str]]) -> None:
    """A node's labels are many and grow with what runs there; copied onto every series, they cost the series."""
    run, _ = absent
    node_label = '__meta_kubernetes_node_label_feature_node_kubernetes_io_cpu_model_vendor_id'
    scrapes = _kubelet_scrapes(run)

    assert {'kubelet', 'cadvisor'} <= set(scrapes)
    for name, spec in scrapes.items():
        for config in spec.get('relabelConfigs', []):
            mapped = config.get('action') == 'labelmap' and re.fullmatch(config.get('regex', '(.*)'), node_label)
            assert not mapped, f'{name} maps every node label: {config}'


def test_a_series_from_a_node_with_more_labels_than_the_store_accepts_is_stored_labeled_by_its_node(
    absent: tuple[Run, list[str]],
) -> None:
    """Every kubelet scrape of a node carrying twice the store's limit in labels yields a series the store keeps.

    The series still names its node, the one node label a reading groups by.
    """
    run, _ = absent
    limit = int(_values(run)['vmsingle']['spec'].get('extraArgs', {}).get('maxLabelsPerTimeseries', STORE_LABEL_LIMIT))
    node = conventions.HOMELAB_NODE
    labels = {
        HOSTNAME_LABEL: node,
        **{f'feature.node.kubernetes.io/feature-{i}': 'true' for i in range(2 * STORE_LABEL_LIMIT)},
    }

    for name, spec in _kubelet_scrapes(run).items():
        scraped = _scraped({**spec, 'selector': {}}, name, node, labels, CADVISOR_SAMPLE)
        assert scraped is not None, name
        _, series = scraped
        assert series is not None, name
        assert len(series) <= limit, f'{name}: {len(series)} labels'
        assert series['node'] == node, name


def test_etcd_is_scraped_on_every_control_plane_and_nowhere_else(absent: tuple[Run, list[str]]) -> None:
    """On its metrics listener, over plain HTTP, under one `job` whatever name the object is given.

    Talos gives the control plane label an empty value, and the worker none: a
    selector the operator reads as "the label's value is empty" keeps both.
    """
    run, _ = absent
    (scrape,) = run.monitor.of_type(VM_NODE_SCRAPE)
    spec = scrape.inputs['spec']
    sample = {'__name__': 'etcd_disk_wal_fsync_duration_seconds_bucket', 'le': '0.008'}

    scraped = {node: _scraped(spec, scrape.name, node, labels, sample) for node, labels in _talos_nodes().items()}
    targets = {node: it for node, it in scraped.items() if it is not None}

    assert set(targets) == set(conventions.CLOUD_NODES)
    jobs: set[str] = set()
    for node, (target, series) in targets.items():
        assert target['__address__'].rsplit(':', 1)[1] == str(ETCD_METRICS_PORT), node
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
    absent: tuple[Run, list[str]], metric: str, kept: bool
) -> None:
    """etcd's per-method gRPC series and its debugging series are most of a member's; what a reading reads stays."""
    run, _ = absent
    (scrape,) = run.monitor.of_type(VM_NODE_SCRAPE)

    series = _relabel({'__name__': metric}, scrape.inputs['spec'].get('metricRelabelConfigs', []))

    assert (series is not None) is kept


def test_the_api_servers_dropped_histograms_lose_their_buckets_and_keep_their_count_and_sum(
    absent: tuple[Run, list[str]],
) -> None:
    """The buckets are the series; the count and the sum still answer a rate and a mean, and the endpoint is the chart's."""
    from kluster.components.monitoring import APISERVER_DROPPED_HISTOGRAMS

    run, _ = absent
    (endpoint,) = _values(run)['kubeApiServer']['vmScrape']['spec']['endpoints']
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


def test_every_scrape_runs_at_vmagents_one_interval(absent: tuple[Run, list[str]]) -> None:
    """A rate over a window then reads the same number of samples from every source."""
    run, _ = absent
    interval = _values(run)['vmagent']['spec'].get('scrapeInterval', CHART_SCRAPE_INTERVAL)
    declared = [it.inputs['spec'] for typ in (VM_NODE_SCRAPE, VM_POD_SCRAPE) for it in run.monitor.of_type(typ)]
    intervals = {
        **{f'kubelet {name}': spec.get('interval') for name, spec in _kubelet_scrapes(run).items()},
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
    absent: tuple[Run, list[str]],
) -> None:
    """Each flag names a file of a Secret vmsingle mounts, holding a password of its own generated into state.

    The chart is installed behind the Secret, since vmsingle does not start
    without what it mounts.
    """
    run, _ = absent
    spec = _values(run)['vmsingle']['spec']
    mounted = set(spec.get('secrets', []))
    secrets = {
        (it.inputs['metadata']['namespace'], it.inputs['metadata']['name']): it for it in run.monitor.of_type(SECRET)
    }
    generated = {Generating.generated(it.name) for it in run.monitor.of_type(PASSWORD)}
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
        assert run.urn(SECRET, secret.name) in run.dependencies(CHART, STACK)
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


def test_the_memory_requests_on_the_worker_fit_monitorings_budget(absent: tuple[Run, list[str]]) -> None:
    """What the scheduler reserves for monitoring on the worker is within nodes.md §4.4's budget, rfc-008 §6.1's request.

    The requests are those of the containers charged to monitoring's line of
    nodes.md §4.4, read from the values each chart is installed with, at the
    path that chart reads each container's memory from. metrics-server is
    charged to the line for the rest of kube-system.
    """
    run, _ = absent
    charged = memory.charged_to(memory.Line.MONITORING)
    requested = {name: _mib(it['requests']['memory']) for name, it in _installed_memory(run, charged).items()}

    assert sum(requested.values()) <= memory.BUDGET_MIB, requested


def test_the_history_stores_request_is_its_own_line_beside_monitorings(absent: tuple[Run, list[str]]) -> None:
    """The history store is charged to a line of its own, as the operator ruled (Aetf/kluster-ops#790, option A).

    Its request is read where the declared store reads it, so monitoring's
    worker memory is the two lines together: the stack's containers within
    `BUDGET_MIB`, the history store within `HISTORY_BUDGET_MIB`.
    """
    run, _ = absent
    charged = memory.charged_to(memory.Line.HISTORY)
    requested = {name: _mib(it['requests']['memory']) for name, it in _installed_memory(run, charged).items()}
    monitoring = {
        name: _mib(it['requests']['memory'])
        for name, it in _installed_memory(run, memory.charged_to(memory.Line.MONITORING)).items()
    }

    assert requested
    assert sum(requested.values()) <= memory.HISTORY_BUDGET_MIB, requested
    assert sum(requested.values()) + sum(monitoring.values()) <= memory.BUDGET_MIB + memory.HISTORY_BUDGET_MIB


def test_every_memory_limit_is_at_least_its_request(absent: tuple[Run, list[str]]) -> None:
    """A limit under its request is refused by the API server, for the pod, at apply."""
    run, _ = absent
    for name, resources in _installed_memory(run).items():
        if 'limits' in resources:
            assert _mib(resources['limits']['memory']) >= _mib(resources['requests']['memory']), name


def test_every_memory_value_leaves_cpu_to_the_charts_defaults(absent: tuple[Run, list[str]]) -> None:
    """Only memory is set, so each chart and the operator keep the CPU they would give a container.

    The operator fills each resource it finds unset from its defaults, CPU
    apart from memory (`build.Resources`, `factory/build/container.go`
    L97-142 at operator v0.74.0); a chart merges the values handed to it
    over its own.
    """
    run, _ = absent
    for name, resources in _installed_memory(run).items():
        assert {key for side in resources.values() for key in side} == {'memory'}, name


def test_grafana_reads_the_stacks_datasources_from_a_mount_rather_than_a_sidecar(
    absent: tuple[Run, list[str]],
) -> None:
    """The datasource sidecar is off, the stack still renders its ConfigMap, and Grafana mounts it where it provisions.

    With its datasource sidecar off, the stack renders the ConfigMap only
    under `forceDeployDatasource` (`templates/grafana/datasource.yaml` L1 at
    0.91.2).
    """
    run, _ = absent
    grafana = _values(run)['grafana']

    assert grafana['sidecar']['datasources']['enabled'] is False
    assert grafana['forceDeployDatasource'] is True
    (mount,) = [it for it in grafana['extraConfigmapMounts'] if it['mountPath'] == GRAFANA_DATASOURCES_DIR]
    assert mount['configMap'] == STACK_DATASOURCES
    assert 'subPath' not in mount


def test_grafana_installs_no_plugin_at_start(absent: tuple[Run, list[str]]) -> None:
    """Grafana's preinstall, its download of a plugin set on every start, is off, and no plugin is listed to install.

    `GF_PLUGINS_PREINSTALL_DISABLED` is Grafana's environment form of
    `[plugins] preinstall_disabled`; the chart installs what `plugins` lists.
    """
    run, _ = absent
    grafana = _values(run)['grafana']

    assert grafana['env']['GF_PLUGINS_PREINSTALL_DISABLED'] == 'true'
    assert not grafana.get('plugins')


def _digest(chart: ChartPin) -> str:
    return cast('str', cast('dict[str, Any]', stack_values(STACK, chart)['grafana'])['env'][DATASOURCES_DIGEST_ENV])


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
    absent: tuple[Run, list[str]],
) -> None:
    """The playbook rule as a gate: an alert whose response procedure is not written down is not shipped.

    Every rule the program declares, whatever built it, is read off the
    declaration: its tier is one `conventions.alert.Tier` defines, its summary
    is a line, and its playbook names a document of this repository and a
    section that document has (architecture.md §4.3, operations.md §5).
    """
    run, _ = absent
    declared = _declared_rules(run)
    tiers = {tier.value for tier in Tier}

    assert declared, 'the program declared no rule'
    for rule in declared:
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


def test_a_row_the_store_ignores_for_any_reason_raises_an_alert(absent: tuple[Run, list[str]]) -> None:
    """A series the store ignores is lost whole and silently, so some rule reads the counter for every reason.

    A matcher on the counter would narrow the rule to the reasons it names,
    and a reason the store adds later would go unseen.
    """
    run, _ = absent
    reading = re.compile(rf'\b{IGNORED_ROWS}\b(?!\s*\{{)')

    assert [rule['alert'] for rule in _declared_rules(run) if reading.search(rule['expr'])]


def test_the_rules_are_declared_behind_the_chart_that_defines_them(absent: tuple[Run, list[str]]) -> None:
    """pulumi-kubernetes waits for workloads, not for a definition's registration, so the order is declared."""
    run, _ = absent
    chart = run.urn(CHART, STACK)
    objects = [(typ, declared.name) for typ in OPERATOR_KINDS for declared in run.monitor.of_type(typ)]

    assert objects
    for typ, name in objects:
        assert chart in run.dependencies(typ, name), name


# -- alertmanager's receiver ----------------------------------------------------------


def test_the_receiver_reads_the_webhooks_url_from_its_sealed_secret(absent: tuple[Run, list[str]]) -> None:
    """The URL is in no rendered configuration: every webhook names the sealed value's Secret, in its namespace.

    The operator reads `url_secret` from the receiver object's own namespace
    (`vmalertmanagerconfig_types.go` L533-563 at operator v0.74.0), which is
    therefore the namespace the value is sealed to.
    """
    run, _ = absent
    receiver = _receiver(run)
    webhooks = [config for spec in receiver['spec']['receivers'] for config in spec.get('webhook_configs', [])]

    assert receiver['metadata']['namespace'] == WEBHOOK.namespace
    assert webhooks
    for webhook in webhooks:
        assert 'url' not in webhook
        assert webhook['url_secret']['name'] == WEBHOOK.name


def test_the_receiver_sends_firing_alerts_alone(absent: tuple[Run, list[str]]) -> None:
    """Home Assistant's automation pushes every alert it is sent, reading no status (operations.md §4).

    A resolution sent there would push as a second alarm. Alertmanager sends
    resolutions to a webhook unless told not to, so the field is stated on
    every webhook rather than left to that default.
    """
    run, _ = absent
    webhooks = [config for spec in _receiver(run)['spec']['receivers'] for config in spec.get('webhook_configs', [])]

    assert webhooks
    for webhook in webhooks:
        assert webhook.get('send_resolved') is False


def test_the_sealed_webhook_is_declared_where_the_receiver_reads_it(sealed: Run) -> None:
    """Sealed in, the value becomes the Secret the receiver names, at the scope the census row seals it.

    The key the receiver reads is one the SealedSecret renders rather than
    the sealed id, and it renders a URL at Home Assistant's own address
    around the id; `test_sealing` opens it as the controller does.
    """
    receiver = _receiver(sealed)
    (secret,) = [
        it.inputs for it in sealed.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WEBHOOK.name
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


def test_an_absent_webhook_declares_no_sealed_secret_and_warns_naming_it(absent: tuple[Run, list[str]]) -> None:
    """Until it is sealed in, nothing is declared for it; the receiver is declared all the same, and stays pending."""
    run, warned = absent

    assert [it for it in run.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WEBHOOK.name] == []
    assert [message for message in warned if repr(WEBHOOK.name) in message]
    assert run.monitor.of_type(VM_ALERTMANAGER_CONFIG)


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
    absent: tuple[Run, list[str]],
) -> None:
    """Each declared rule's alert, carrying its own labels alone, ends on Home Assistant's receiver.

    The check-in carries no label but its tier, so a route confined to a
    namespace would drop it. Its repeat reaches Home Assistant at most a group
    interval after the repeat interval, which is inside the timer it restarts
    (rfc-007 §7.3).
    """
    run, _ = absent
    top = _top_route(run)
    declared = _declared_rules(run)

    for rule in declared:
        labels = cast('Mapping[str, str]', rule['labels'])
        top_matches = all(_matches(matcher, labels) for matcher in top.get('matchers', []))
        assert top_matches, f'{rule["alert"]} does not reach the receiver'
        assert _route(top, labels)['receiver'] == RECEIVER, rule['alert']

    (check_in,) = [rule for rule in declared if rule['alert'] == HEARTBEAT.alert]
    route = _route(top, check_in['labels'])
    latest = _duration(route['repeat_interval']) + _duration(route['group_interval'])
    assert latest < HEARTBEAT_TIMER


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
    absent: tuple[Run, list[str]], spec_schemas: dict[tuple[str, str], Mapping[str, Any]]
) -> None:
    """The check the SDK's typed `Args` would make, while the layer declares the operator's kinds untyped.

    It goes with `components.monitoring.operator_objects` (Aetf/kluster-ops#720).
    """
    run, _ = absent
    declared = [it for typ in OPERATOR_KINDS for it in run.monitor.of_type(typ)]

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


def test_the_history_store_runs_on_the_worker_alone(absent: tuple[Run, list[str]]) -> None:
    """It is a second vmsingle beside the chart's, on the worker's local disk, as every store of the stack is."""
    run, _ = absent
    spec = _history(run)['spec']

    schedulable = {
        name
        for name, labels in _talos_nodes().items()
        if _admits(labels, spec.get('nodeSelector', {}), spec.get('affinity', {}))
    }

    assert schedulable == {conventions.HOMELAB_NODE}
    assert spec['storage']['storageClassName'] == conventions.SC_LOCAL_PATH


def test_the_history_stores_mutating_endpoints_take_the_stacks_keys(absent: tuple[Run, list[str]]) -> None:
    """Deleting the history is behind a key, as deleting the recent store's series is."""
    run, _ = absent
    spec = _history(run)['spec']
    recent = _values(run)['vmsingle']['spec']

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


def test_vmagent_writes_to_the_history_stores_service(absent: tuple[Run, list[str]]) -> None:
    """On its write path, where vmsingle takes Prometheus remote write (`/api/v1/write`)."""
    run, _ = absent

    assert _history_write(run)['url'] == f'{_history_service(run)}/api/v1/write'


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
    absent: tuple[Run, list[str]], series: dict[str, str], kept: bool
) -> None:
    """What a node, etcd or a container consumes and how long the control plane takes; etcd's process, not another's.

    The class of Aetf/kluster-ops#790's design, §2, read through the remote
    write's relabeling as vmagent applies it to a series it sends.
    """
    run, _ = absent

    assert (_relabel(series, _history_write(run)['inlineUrlRelabelConfig']) is not None) is kept


def test_vmalerts_recording_rules_reach_the_stores_through_vmagent(absent: tuple[Run, list[str]]) -> None:
    """Written to vmagent, a rule's series takes the history's relabeling as a scraped one does.

    The chart points vmalert's remote write at vmagent under this value, and at
    the recent store otherwise (`templates/_helpers.tpl` L79-83 at 0.91.2).
    """
    run, _ = absent

    assert _values(run)['vmalert'].get('remoteWriteVMAgent') is True


def test_grafana_reads_the_history_store(absent: tuple[Run, list[str]]) -> None:
    """A datasource of its own, at the address vmagent writes the history to, beside the chart's default."""
    run, _ = absent
    extra = _values(run)['defaultDatasources']['extra']

    assert [it['url'] for it in extra if it['type'] == 'prometheus'] == [_history_service(run)]
    assert not any(it.get('isDefault') for it in extra)


#: The release each store and its backups run, as `update_crds` recorded the
#: stack chart's `appVersion` beside the bundle.
APP_VERSIONS = ROOT / 'packages' / 'crds' / 'app-versions.json'


@pytest.mark.parametrize('pin', ['metrics-history', 'metrics-backup', 'metrics-restore'])
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


def test_renovate_moves_the_stores_images_with_the_chart_in_a_group_of_their_own() -> None:
    """The three images and the chart are one group, apart from the in-cluster one, so a lagging chart holds back four pins.

    The test above is red between an images' release and the chart's taking
    it, which in the in-cluster group would hold back every chart and image
    there. The rule sits after the in-cluster rule, which matches the same
    pins by file and data source: the last matching rule wins a field. No
    later rule that sets a group names any of them. Renovate reads its
    configuration from the default branch, so nothing goes red there when
    this breaks.
    """
    from kluster.lib.versions import ProjectFile, Versions
    from kluster.scripts.update_crds import sources

    pinned = Versions(ProjectFile(sources.read_config(ROOT / 'Pulumi.yaml')))
    moved = {
        pinned.chart['victoria-metrics-k8s-stack'].name,
        *(pinned.image[pin].repository for pin in ('metrics-history', 'metrics-backup', 'metrics-restore')),
    }
    rules = package_rules((ROOT / 'renovate.json5').read_text())
    (own,) = [index for index, rule in enumerate(rules) if set(listed(rule, 'matchDepNames')) & moved]
    (in_cluster,) = [
        index
        for index, rule in enumerate(rules)
        if scalar(rule, 'groupSlug') == 'in-cluster' and listed(rule, 'matchFileNames') == ['Pulumi.yaml']
    ]
    slug = scalar(rules[own], 'groupSlug')

    assert set(listed(rules[own], 'matchDepNames')) == moved
    assert listed(rules[own], 'matchFileNames') == ['Pulumi.yaml']
    assert slug is not None
    assert slug not in {scalar(rule, 'groupSlug') for index, rule in enumerate(rules) if index != own}
    assert in_cluster < own
    for later in rules[own + 1 :]:
        if scalar(later, 'groupSlug') is not None:
            assert not set(listed(later, 'matchDepNames')) & moved, f'a later rule regroups them:\n{later}'


# -- Backups and restores ------------------------------------------------------------


def _restores(run: Run) -> dict[str, list[dict[str, Any]]]:
    """Each store's init containers, by store: the chart's vmsingle and the history store."""
    return {
        'recent': _values(run)['vmsingle']['spec'].get('initContainers', []),
        'history': _history(run)['spec'].get('initContainers', []),
    }


def test_without_the_writer_key_nothing_backs_up_or_restores_and_the_key_is_named(
    absent: tuple[Run, list[str]],
) -> None:
    """The bring-up's tolerance: the stores start empty and are copied nowhere until the key is sealed in."""
    run, warned = absent

    assert run.monitor.of_type(CRONJOB) == []
    assert _restores(run) == {'recent': [], 'history': []}
    assert [it for it in run.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WRITER_KEY.name] == []
    assert [message for message in warned if repr(WRITER_KEY.name) in message]


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


def test_each_store_is_backed_up_hourly_from_its_own_claim_read_only(sealed: Run) -> None:
    """The operator's claim of each store (`vmsingle-<object>`, `factory/vmsingle/vmsingle.go` L59-62), at two minutes.

    Mounted read-only, so the job's pod lands on the store's node with the
    claim and can write nothing there.
    """
    jobs = _backup_jobs(sealed)
    (history,) = sealed.monitor.of_type(VM_SINGLE)
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


def test_a_snapshot_a_cut_off_backup_leaves_is_deleted_well_after_any_run_ends(sealed: Run) -> None:
    """Its hard links would keep every part merged away since on disk; no run loses the snapshot it copies."""
    longest = max(job.deadline for job in schedule.METRICS_BACKUPS.values())

    for spec in (_values(sealed)['vmsingle']['spec'], _history(sealed)['spec']):
        assert _duration(spec['extraArgs']['snapshotsMaxAge']) > longest


def _env(container: Mapping[str, Any]) -> dict[str, Any]:
    return {it['name']: it.get('value', it.get('valueFrom')) for it in container['env']}


def test_the_writer_key_is_declared_where_the_backups_and_restores_read_it(sealed: Run) -> None:
    """Sealed in, the key becomes the Secret every backup and restore container reads both halves from."""
    (secret,) = [
        it.inputs for it in sealed.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WRITER_KEY.name
    ]
    containers = [
        *[container for job in _backup_jobs(sealed).values() for container in _pod_containers(job)],
        *[container for restores in _restores(sealed).values() for container in restores],
    ]

    assert secret['metadata']['namespace'] == conventions.MONITORING_NAMESPACE
    assert secret['spec']['encryptedData'] == SEALED[WRITER_KEY.name]
    assert containers
    for container in containers:
        env = _env(container)
        for field in conventions.WRITER_KEY_FIELDS:
            assert env[field] == {'secretKeyRef': {'name': secret['metadata']['name'], 'key': field}}


# -- The scripts, run --------------------------------------------------------------------

#: A B2 account's native API and a store's `/metrics`, answered from a directory:
#: `bucket/` holds the bucket's current files by name, `metrics` the store's
#: exposition, `down` makes B2 answer nothing, `list-down` makes it answer the
#: authorization and nothing after. Its arguments are `curl`'s as the scripts
#: pass them, each run's appended to `curl.argv`, and a `-H @<file>` reads one
#: header a line, as `curl` does. B2's answers are the shape its v2 API
#: documents for `b2_authorize_account`, `b2_list_file_names`,
#: `b2_get_upload_url` and `b2_upload_file`; an error is `curl --fail`'s exit
#: status, 22.
FAKE_CURL = """
import base64, hashlib, json, os, sys
from pathlib import Path
from urllib.parse import urlsplit

state = Path(os.environ['FAKE_STATE'])
with (state / 'curl.argv').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
args, headers, body, url = sys.argv[1:], {}, None, None
while args:
    arg = args.pop(0)
    if arg == '-H':
        value = args.pop(0)
        lines = Path(value[1:]).read_text().splitlines() if value.startswith('@') else [value]
        for line in lines:
            name, found = line.split(':', 1)
            headers[name.strip().lower()] = found.strip()
    elif arg in ('--data', '--data-binary'):
        value = args.pop(0)
        body = Path(value[1:]).read_text() if value.startswith('@') else value
    elif arg == '--max-time':
        args.pop(0)
    elif not arg.startswith('-'):
        url = arg
bucket = state / 'bucket'
path = urlsplit(url).path


def refuse(status):
    print(f'curl: (22) The requested URL returned error: {status}', file=sys.stderr)
    sys.exit(22)


def answer(found):
    print(json.dumps(found, indent=2))
    sys.exit(0)


if path == '/metrics':
    sys.stdout.write((state / 'metrics').read_text())
    sys.exit(0)
if (state / 'down').exists():
    refuse(503)
if path == '/b2api/v2/b2_authorize_account':
    if headers.get('authorization') != 'Basic ' + base64.b64encode(os.environ['FAKE_KEY'].encode()).decode():
        refuse(401)
    answer({
        'accountId': 'account', 'apiUrl': 'https://api.b2.invalid', 'authorizationToken': 'account-token',
        'allowed': {'bucketId': 'bucket-id', 'bucketName': 'bucket-name', 'capabilities': ['listFiles'],
                    'namePrefix': 'vm/monitoring/'},
        'downloadUrl': 'https://download.b2.invalid', 's3ApiUrl': 'https://s3.b2.invalid',
    })
if (state / 'list-down').exists():
    refuse(503)
if headers.get('authorization') not in ('account-token', 'upload-token'):
    refuse(401)
if path == '/b2api/v2/b2_list_file_names':
    request = json.loads(body)
    assert request['bucketId'] == 'bucket-id'
    names = sorted(str(it.relative_to(bucket)) for it in bucket.rglob('*') if it.is_file())
    listed = [name for name in names if name.startswith(request['prefix'])][: request['maxFileCount']]
    answer({'files': [{'fileName': name, 'action': 'upload'} for name in listed], 'nextFileName': None})
if path.startswith('/file/bucket-name/'):
    found = bucket / path.removeprefix('/file/bucket-name/')
    if not found.is_file():
        refuse(404)
    sys.stdout.write(found.read_text())
    sys.exit(0)
if path == '/b2api/v2/b2_get_upload_url':
    answer({'bucketId': 'bucket-id', 'uploadUrl': 'https://pod.b2.invalid/upload', 'authorizationToken': 'upload-token'})
if path == '/upload':
    assert headers['authorization'] == 'upload-token'
    assert headers['x-bz-content-sha1'] == hashlib.sha1(body.encode()).hexdigest()
    target = bucket / headers['x-bz-file-name']
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    answer({'fileName': headers['x-bz-file-name']})
refuse(400)
"""

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

#: The writer key the fake bucket admits, `id:secret`, and the tokens it hands
#: out: every credential a script holds.
FAKE_KEY = ('key-id', 'key-secret')
FAKE_TOKENS = ('account-token', 'upload-token')

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
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path], kind: str
) -> None:
    """The `/bin/sh` a container runs a script under is its image's `/bin/busybox` and the loader it names, byte for byte.

    The files are the ones the pod launches for the container's `/bin/sh`,
    and the sums are the ones the container's own pinned image computes
    itself, so they are read apart from the copy the suite runs.
    """
    pod = (_restore_pod if kind == 'restore' else _backup_pod)(sealed, 'history', tmp_path, shells)
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
        for name, text in (('curl', FAKE_CURL), ('vmrestore', FAKE_BINARY), ('vmbackup', FAKE_BINARY)):
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

    def _env(self, container: Mapping[str, Any], shell: Path | None) -> dict[str, str]:
        halves = dict(zip(sorted(conventions.WRITER_KEY_FIELDS), FAKE_KEY, strict=True))
        return {
            'PATH': ':'.join(str(it) for it in (self.fakes, shell) if it is not None),
            'FAKE_STATE': str(self.state),
            'FAKE_KEY': self.key,
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


@pytest.mark.parametrize('store', ['recent', 'history'])
def test_an_empty_store_with_a_backup_is_restored(
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path], store: str
) -> None:
    """From its own prefix, by the `vmrestore` command the container declares, into the store's data directory."""
    pod = _restore_pod(sealed, store, tmp_path, shells)
    _backed_up(pod, _prefix(pod))

    assert pod.succeeded(), pod.stderr
    args = pod.ran('vmrestore')
    assert args is not None
    assert f'-storageDataPath={pod.data}' in args
    assert any(arg.startswith('-src=s3://') and arg.endswith('/' + _prefix(pod).rstrip('/')) for arg in args)
    assert (pod.data / 'restored-part').exists()


def test_an_empty_store_with_no_backup_starts_empty(sealed: Run, tmp_path: Path, shells: Mapping[str, Path]) -> None:
    """The first install: nothing at the store's prefix, whatever another store's prefix holds."""
    pod = _restore_pod(sealed, 'history', tmp_path, shells)
    _backed_up(pod, 'vm/monitoring/recent/')

    assert pod.succeeded(), pod.stderr
    assert pod.ran('vmrestore') is None
    assert list(pod.data.iterdir()) == []


def test_a_store_holding_data_starts_on_it_without_reading_the_bucket(
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path]
) -> None:
    """A restarted store keeps what it holds, with the bucket unreachable all the while."""
    pod = _restore_pod(sealed, 'history', tmp_path, shells)
    _backed_up(pod, _prefix(pod))
    (pod.data / 'flock.lock').write_text('')
    (pod.state / 'down').write_text('')

    assert pod.succeeded(), pod.stderr
    assert pod.ran('vmrestore') is None


@pytest.mark.parametrize('failure', ['bucket down', 'key refused', 'listing fails', 'restore fails', 'data unreadable'])
def test_a_backup_that_cannot_be_read_keeps_the_store_from_starting(
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path], failure: str
) -> None:
    """Whatever stops the read -- the bucket, the key, its listing, `vmrestore`, the data directory -- the store waits.

    A listing that fails after the key is authorized reads as nothing listed
    unless its failure stops the container, and nothing listed starts the
    store empty over the backup that exists.
    """
    pod = _restore_pod(sealed, 'history', tmp_path, shells)
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
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path], held: bool
) -> None:
    """`vmrestore`'s lock file left in the directory: restored again, or refused where the backup is gone."""
    pod = _restore_pod(sealed, 'history', tmp_path, shells)
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
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path], recorded: int | None
) -> None:
    """The backup runs against the store's snapshot endpoint, and the count read becomes the record."""
    from kluster.components.monitoring import stores

    pod = _backup_pod(sealed, 'history', tmp_path, shells)
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
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path], failure: str
) -> None:
    """Fewer rows than the record, a record that cannot be listed, or one that is no count: no backup, the record kept.

    An empty record that is listed is no count either, rather than no record.

    A store that started without its history would otherwise overwrite the
    history in the bucket, and lower the record so that every later run
    passes. A listing that fails after the key is authorized reads as no
    record unless its failure stops the container.
    """
    pod = _backup_pod(sealed, 'history', tmp_path, shells)
    _metrics(pod, 1_000)
    held = {'record not a number': 'some rows', 'record empty': ''}.get(failure, '42000000000')
    record = _recorded(pod, held)
    if failure == 'listing fails':
        (pod.state / 'list-down').write_text('')

    assert not pod.succeeded()
    assert pod.ran('vmbackup') is None
    assert record.read_text() == held


def test_a_failed_backup_records_nothing(sealed: Run, tmp_path: Path, shells: Mapping[str, Path]) -> None:
    """The record is of the last backup that completed, so a count is never recorded for a backup that is not there."""
    pod = _backup_pod(sealed, 'history', tmp_path, shells)
    _metrics(pod, 1_000)
    (pod.state / 'fails').write_text('vmbackup')

    assert not pod.succeeded()
    assert not _record(pod).exists()


def test_the_recent_store_is_backed_up_whatever_it_holds(
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path]
) -> None:
    """Its rows age out with its retention, so its count falls, and its backup reads neither the store nor a record."""
    pod = _backup_pod(sealed, 'recent', tmp_path, shells)
    (pod.state / 'down').write_text('')

    assert pod.succeeded(), pod.stderr
    assert pod.ran('vmbackup') is not None
    assert pod.curl_arguments() == []


def test_no_credential_is_an_argument_of_any_process_the_scripts_start(
    sealed: Run, tmp_path: Path, shells: Mapping[str, Path]
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
    restore = _restore_pod(sealed, 'history', tmp_path / 'restore', shells)
    _backed_up(restore, _prefix(restore))
    backup = _backup_pod(sealed, 'history', tmp_path / 'backup', shells)
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


def test_no_container_is_handed_a_secret_in_its_command(sealed: Run) -> None:
    """The snapshot key and the writer key reach every container through its environment, from their Secrets."""
    from kluster.components.monitoring import AUTH_KEYS_SECRET

    containers = [
        *[container for job in _backup_jobs(sealed).values() for container in _pod_containers(job)],
        *[container for restores in _restores(sealed).values() for container in restores],
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


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def apps() -> Recorder:
    """The `apps` program, with every sealed value its stack declares sealed in, against a `physical` that has published."""
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
    return monitor


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


def test_every_cronjob_a_program_declares_is_a_rows_and_every_row_a_declared_cronjob(
    sealed: Run, apps: Recorder
) -> None:
    """The family watches a CronJob through its row, so a CronJob without one is a job nothing reports stopping.

    Read off the two programs that open the cluster, `k8s-base` and `apps`,
    each with every sealed value in, which is when each declares its every
    CronJob. A row no program declares a CronJob for is two alerts over no
    series, and two rows of one CronJob are its alerts twice.
    """
    for monitor in (sealed.monitor, apps):
        assert monitor.of_type(KUBERNETES_PROVIDER), 'a run that opened no cluster'
    rows = [(job.namespace, job.cronjob) for job in schedule.SCHEDULED_JOBS]

    assert len(set(rows)) == len(rows)
    assert set(_cronjobs(sealed.monitor, apps)) == set(rows)


def test_each_cronjob_runs_at_its_rows_period_and_is_cut_off_at_its_rows_deadline(sealed: Run, apps: Recorder) -> None:
    """The period is what the stale alert is held to and the deadline what the failed one waits for.

    A run is cut off at the deadline, which `Forbid` would otherwise let hold
    every later run back, so the failed alert finds the run either succeeded
    or failed by then. A run starts no later than the row's starting
    deadline, which the row holds inside its slack with the deadline, and a
    CronJob whose row has none sets none.
    """
    rows = {(job.namespace, job.cronjob): job for job in schedule.SCHEDULED_JOBS}

    for key, spec in _cronjobs(sealed.monitor, apps).items():
        row = rows[key]
        starting = row.starting_deadline.total_seconds() if row.starting_deadline is not None else None
        assert _period(spec['schedule']) == row.period, key
        assert spec['jobTemplate']['spec']['activeDeadlineSeconds'] == row.deadline.total_seconds(), key
        assert spec.get('startingDeadlineSeconds') == starting, key
        assert spec['concurrencyPolicy'] == 'Forbid', key


def test_each_row_raises_a_stale_and_a_failed_alert_on_its_cronjob_naming_its_playbook(
    absent: tuple[Run, list[str]],
) -> None:
    """Stale over `max_age` of the row's period, failed held for its deadline, both `actionable`, on its CronJob alone.

    The expressions' behavior over kube-state-metrics' series is not read
    here: what holds is each rule's relation to its row.
    """
    run, _ = absent
    declared = _declared_rules(run)

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


def test_a_status_reaches_the_rules_inside_the_lag_every_deadline_leaves(absent: tuple[Run, list[str]]) -> None:
    """A CronJob's status is published at the next scrape and read at the next evaluation after it.

    Both intervals are read off what the program declares: the interval of
    kube-state-metrics' scrape, which publishes the status, and vmalert's
    evaluation interval.
    """
    run, _ = absent
    scrape = _duration(_kube_state_metrics_endpoint(run)['interval'])
    evaluation = _duration(_values(run)['vmalert']['spec']['evaluationInterval'])

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


def test_a_cronjobs_series_carries_the_namespace_its_rows_rules_select_it_by(absent: tuple[Run, list[str]]) -> None:
    """kube-state-metrics publishes a CronJob's series under the CronJob's namespace, which the target's own would replace.

    Without `honorLabels` a label the target carries wins, and the sample's is
    kept as `exported_<name>` (Prometheus' semantics, which vmagent follows),
    so every series would carry the namespace kube-state-metrics runs in. A
    row in that namespace would still match; a row in any other would match
    no series, and its alerts would never fire.
    """
    run, _ = absent
    endpoint = _kube_state_metrics_endpoint(run)

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


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def without_oci_key() -> Run:
    """Every sealed value the stack declares sealed in but the metrics reader's key."""
    return await applied(sealed={name: value for name, value in SEALED.items() if name != OCI_KEY.name})


def _named(run: Run, typ: str, name: str) -> list[dict[str, Any]]:
    return [it.inputs for it in run.monitor.of_type(typ) if it.inputs.get('metadata', {}).get('name') == name]


def _monitoring_objects(run: Run) -> set[tuple[str, str]]:
    """Each object the monitoring component declares, by its type and the name it gives it, or its logical name."""
    return {
        (typ, run.inputs(typ, name).get('metadata', {}).get('name', name))
        for typ, name, _ in run.children('kluster:monitoring:Monitoring')
    }


def test_the_metrics_readers_key_adds_its_sealed_secret_the_pulls_config_map_and_its_cron_job(
    sealed: Run, without_oci_key: Run
) -> None:
    """The preview the key's commit shows: three objects created, and the rule already there."""
    from kluster.components.monitoring import oci_metrics

    added = _monitoring_objects(sealed) - _monitoring_objects(without_oci_key)

    assert added == {
        (SEALED_SECRET, OCI_KEY.name),
        (CONFIGMAP, oci_metrics.PULL_CONFIGMAP),
        (CRONJOB, schedule.OCI_METRICS_PULL.cronjob),
    }
    assert _monitoring_objects(without_oci_key) <= _monitoring_objects(sealed)
    assert _declared_rules(sealed) == _declared_rules(without_oci_key)


def test_without_the_metrics_readers_key_the_pull_is_not_declared_and_the_key_is_named(
    absent: tuple[Run, list[str]], without_oci_key: Run
) -> None:
    """The bring-up's tolerance: nothing of the pull until the key is sealed in, and the warning says which value."""
    from kluster.components.monitoring import oci_metrics

    run, warned = absent
    for declared in (run, without_oci_key):
        assert _named(declared, CRONJOB, schedule.OCI_METRICS_PULL.cronjob) == []
        assert _named(declared, CONFIGMAP, oci_metrics.PULL_CONFIGMAP) == []
        assert _named(declared, SEALED_SECRET, OCI_KEY.name) == []
    assert [message for message in warned if repr(OCI_KEY.name) in message]


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


def test_the_pull_runs_hourly_one_run_at_a_time_apart_from_the_backups(sealed: Run) -> None:
    """No run shares a minute with a store's backup; how long a run may take is its row's (the family's cases)."""
    job, _ = _pull(sealed)
    minute, *every = job['schedule'].split()
    backups = {it['spec']['schedule'].split()[0] for it in _backup_jobs(sealed).values()}

    assert every == ['*'] * 4
    assert minute not in backups
    assert job['concurrencyPolicy'] == 'Forbid'


def test_the_pull_runs_its_pin_on_the_program_and_settings_its_config_map_carries(sealed: Run) -> None:
    """The image is the runtime alone; the program and its settings arrive as files of the mounted ConfigMap."""
    from kluster.components.monitoring import oci_metrics, oci_pull

    pod = _pull_pod(sealed)
    _, data = _pull(sealed)
    (container,) = pod['containers']
    (program_volume,) = [
        it for it in pod['volumes'] if it.get('configMap', {}).get('name') == oci_metrics.PULL_CONFIGMAP
    ]
    (mount,) = [it for it in container['volumeMounts'] if it['name'] == program_volume['name']]
    files = [arg for arg in container['command'] if arg.startswith(f'{mount["mountPath"]}/')]

    assert container['image'] == VERSIONS_CONFIG['versions:image-oci-sdk']
    assert [Path(it).name for it in files] == [oci_metrics.PROGRAM_FILE, oci_metrics.SETTINGS_FILE]
    assert data[oci_metrics.PROGRAM_FILE] == Path(oci_pull.__file__).read_text()


def test_the_key_is_declared_where_the_pull_reads_it_as_the_files_the_program_opens(sealed: Run) -> None:
    """The SealedSecret opens into the Secret the pod mounts, at the directory the program is told, a file per key."""
    from kluster.components.monitoring import oci_pull

    (secret,) = _named(sealed, SEALED_SECRET, OCI_KEY.name)
    pod = _pull_pod(sealed)
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
    sealed: Run, tmp_path: Path
) -> None:
    """OCI names a volume by its OCID alone, so the settings carry the node and the name `conventions` gives it."""
    published = cast('dict[str, dict[str, str]]', PUBLISHED[conventions.PHYSICAL_OUTPUTS.volume_ids])

    targets = _settings(sealed, tmp_path).targets

    assert {(it.node, it.volume) for it in targets.values()} == _census()
    assert all(published[it.node][it.volume] == ocid for ocid, it in targets.items())


def test_the_pull_reads_the_history_store_and_writes_through_vmagent_in_physicals_compartment(
    sealed: Run, tmp_path: Path
) -> None:
    """The newest samples are the history's; the samples reach both stores the way a scraped series does.

    vmagent's address is the operator's Service for the stack's VMAgent, which
    the chart names as it names the recent store (`vmagent-<object>` on 8429,
    `vmagent_types.go` L567-572 and L615-619 at operator v0.74.0).
    """
    settings = _settings(sealed, tmp_path)
    stack_object = f'{conventions.MONITORING_NAMESPACE}-victoria-metrics-k8s-stack'

    assert f'{settings.history}/api/v1/write' == _history_write(sealed)['url']
    assert settings.importer == f'http://vmagent-{stack_object}.{conventions.MONITORING_NAMESPACE}.svc:8429'
    assert settings.compartment == conventions.OCI_TENANCY.compartments[conventions.PHYSICAL].ocid


def test_every_series_the_pull_writes_is_kept_by_the_history_store(absent: tuple[Run, list[str]]) -> None:
    """The pull names its series after the namespace its requests name, and the history's class keeps that prefix."""
    from kluster.components.monitoring import oci_pull

    run, _ = absent
    relabeling = _history_write(run)['inlineUrlRelabelConfig']

    for metric in oci_pull.METRICS:
        series = {'__name__': f'{OCI_BLOCKSTORE_METRICS}_{metric}', 'node': 'cp1', 'volume': BOOT_VOLUME}
        assert _relabel(series, relabeling) is not None, metric


def test_the_pull_can_schedule_on_the_worker_alone(sealed: Run) -> None:
    """With the rest of the stack, off the cores the cloud nodes share with etcd."""
    pod = _pull_pod(sealed)

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
