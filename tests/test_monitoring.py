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

import datetime as dt
import importlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
import yaml
from fences import prose
from k8s_base_installation import SEALED, VERSIONS_CONFIG, Autonaming, Run, applied, release_assets
from mock_monitor import declaring, run_under_backstop
from section_numbers import sections

from kluster import conventions
from kluster.components.monitoring import DATASOURCES_DIGEST_ENV, RECEIVER, memory, stack_values
from kluster.components.monitoring.operator_objects import type_token
from kluster.components.monitoring.rules import HEARTBEAT, HEARTBEAT_TIMER
from kluster.components.talos import ETCD_METRICS_PORT
from kluster.conventions.alert import Tier
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

#: Every kind of the operator's the component declares.
OPERATOR_KINDS = (VM_RULE, VM_ALERTMANAGER_CONFIG, VM_POD_SCRAPE, VM_NODE_SCRAPE)

#: The logical names of the component's two charts.
STACK = 'monitoring'
METRICS_SERVER = 'monitoring-metrics-server'

WEBHOOK = conventions.sealed.ALERT_WEBHOOK

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
    """Each container's `resources`, read where its chart reads them in the values the program installs."""
    found: dict[str, Mapping[str, Any]] = {}
    for container in containers:
        value: Any = run.values(INSTALLED_AS[container.chart])
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


def test_the_receiver_reads_the_webhooks_address_from_its_sealed_secret(absent: tuple[Run, list[str]]) -> None:
    """The address is in no rendered configuration: every webhook names the sealed value's Secret, in its namespace.

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
        assert webhook['url_secret']['key'] in WEBHOOK.keys


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
    """Sealed in, the value becomes the Secret the receiver names, at the scope the census row seals it."""
    receiver = _receiver(sealed)
    (secret,) = [
        it.inputs for it in sealed.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WEBHOOK.name
    ]

    assert secret['metadata']['namespace'] == receiver['metadata']['namespace']
    assert secret['spec']['encryptedData'] == SEALED[WEBHOOK.name]
    assert secret['metadata']['annotations'] == {f'sealedsecrets.bitnami.com/{WEBHOOK.scope.value}': 'true'}


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
