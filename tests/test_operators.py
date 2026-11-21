"""The database, backup and GPU operators `k8s-base` installs, declared against mocks.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), and the cases read what it handed the provider:
CloudNativePG and its Barman Cloud plugin, VolSync and the volume snapshot
definitions it watches, NFD and the Intel GPU plugin (rfc-007 §8), the
namespaces each creates, the order they are installed
in (declarative/cluster-infra.md §1), the policy that holds the name, labels,
annotations and `inheritedMetadata` of a database drill's cluster to its
manifest, which the database operator declares, and what keeps the GPU plugin inert until the worker's GPU is bound.
NFD's worker and the GPU plugin run at the class the node exporter runs at;
the plugin's comes from a mutating admission policy, which the API server the
cluster runs applies here to DaemonSets the way the operator writes its own.

Literals written here are upstream contracts: the charts' value names, the
label the GPU plugin chart's `NodeFeatureRule` sets. What the program derives
from a pin is held to that pin.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pinned_image
import pinned_tools
import pytest
import pytest_asyncio
import yaml
from control_plane import CASE_TIMEOUT, ApiServer, InServer, running, typed, until
from k8s_base_installation import Run, applied, source_file

from kluster.lib.versions import versions

CHART = 'kubernetes:helm.sh/v4:Chart'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CONFIG_GROUP = 'kubernetes:yaml/v2:ConfigGroup'
ADMISSION_POLICY = 'kubernetes:admissionregistration.k8s.io/v1:ValidatingAdmissionPolicy'
ADMISSION_BINDING = 'kubernetes:admissionregistration.k8s.io/v1:ValidatingAdmissionPolicyBinding'
MUTATING_POLICY = 'kubernetes:admissionregistration.k8s.io/v1:MutatingAdmissionPolicy'
MUTATING_BINDING = 'kubernetes:admissionregistration.k8s.io/v1:MutatingAdmissionPolicyBinding'

#: The label the GPU plugin chart's `NodeFeatureRule` puts on a node with an
#: Intel display-class PCI device whose kernel has `i915` or `xe`
#: (`templates/gpu.yaml` of `intel-device-plugins-gpu` 0.36.0).
INTEL_GPU_NODE_LABEL = 'intel.feature.node.kubernetes.io/gpu'


def operators_daemonset(plugin: str) -> str:
    """The name the device-plugin operator gives the DaemonSet it creates for the `GpuDevicePlugin` named `plugin`.

    `NewDaemonSet` in its `pkg/controllers/gpu/controller.go` at v0.36.0
    suffixes the name of the DaemonSet in its embedded manifest,
    `intel-gpu-plugin`, with the plugin's name, joined by `-`.
    """
    return f'intel-gpu-plugin-{plugin}'


#: The logical name of each component the program declares, and of each chart.
CNPG = 'cloudnative-pg'
BARMAN_CLOUD = 'cloudnative-pg-barman-cloud'
VOLSYNC = 'volsync'
NFD = 'node-feature-discovery'
INTEL = 'intel-device-plugins'
INTEL_OPERATOR = 'intel-device-plugins-operator'
INTEL_GPU = 'intel-device-plugins-gpu'
CERT_MANAGER = 'cert-manager'
CILIUM = 'cilium'
SNAPSHOT_DEFINITIONS = 'snapshot-definitions'

#: Each chart, by its logical name, and the `versions:chart-<name>` pin it is
#: installed from.
PINS = {
    CNPG: 'cloudnative-pg',
    BARMAN_CLOUD: 'plugin-barman-cloud',
    VOLSYNC: 'volsync',
    NFD: 'node-feature-discovery',
    INTEL_OPERATOR: 'intel-device-plugins-operator',
    INTEL_GPU: 'intel-device-plugins-gpu',
}

POSTGRES_OPERATOR = 'kluster:postgres:PostgresOperator'
VOLSYNC_OPERATOR = 'kluster:backup:VolSync'
NODE_FEATURE_DISCOVERY = 'kluster:gpu:NodeFeatureDiscovery'
INTEL_GPU_PLUGIN = 'kluster:gpu:IntelGpuPlugin'
#: The monitoring stack's chart, whose node exporter is the per-node agent the
#: others are held to.
MONITORING_STACK = 'monitoring'


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The whole program against a `physical` that has published every output."""
    return await applied()


# -- Each chart, from its pin -------------------------------------------------


@pytest.mark.parametrize(('chart', 'pin'), PINS.items(), ids=list(PINS))
def test_each_chart_is_installed_from_its_pin(applied: Run, chart: str, pin: str) -> None:
    """The reference and the version are the pin's; an HTTP repository's address is passed beside the name."""
    pinned = versions.chart[pin]
    inputs = applied.inputs(CHART, chart)

    assert (inputs['chart'], inputs['version']) == (pinned.reference, pinned.version)
    assert inputs.get('repositoryOpts', {}).get('repo') == (None if pinned.oci else pinned.repository)


# -- NFD ------------------------------------------------------------------------


def test_nfds_post_delete_cleanup_is_off(applied: Run) -> None:
    """The cleanup is a Helm hook, which `helm.v4.Chart` drops; it is switched off rather than left to vanish."""
    assert applied.values(NFD)['postDeleteCleanup'] is False


def node_exporters_class(applied: Run) -> str:
    """The class the node exporter runs at, which `tests/test_monitoring.py` holds above every pod the scheduler places."""
    return applied.values(MONITORING_STACK)['prometheus-node-exporter']['priorityClassName']


def test_nfds_worker_runs_at_the_class_the_node_exporter_runs_at(applied: Run) -> None:
    """Its DaemonSet keeps it on its node, so no pod placed there, a held one preempting included, may take its room."""
    assert applied.values(NFD)['worker']['priorityClassName'] == node_exporters_class(applied)


# -- The GPU plugin -------------------------------------------------------------


def test_the_gpu_plugin_runs_only_on_nodes_its_own_rule_labels(applied: Run) -> None:
    """Inert until the worker's GPU is bound: no node carries the label before, so its DaemonSet schedules nowhere.

    The rule is the chart's, evaluated by NFD; the selector is the label that
    rule sets, and nothing else.
    """
    values = applied.values(INTEL_GPU)

    assert values['nodeFeatureRule'] is True
    assert values['nodeSelector'] == {INTEL_GPU_NODE_LABEL: 'true'}


def test_the_device_plugin_operator_runs_the_gpus_controller_alone(applied: Run) -> None:
    """The operator manages every Intel device kind unless told which; this cluster has a GPU and nothing else."""
    assert applied.values(INTEL_OPERATOR)['manager']['devices'] == {'gpu': True}


# -- Namespaces ---------------------------------------------------------------


@pytest.mark.parametrize(
    ('chart', 'component'),
    [
        (CNPG, CNPG),
        (BARMAN_CLOUD, CNPG),
        (VOLSYNC, VOLSYNC),
        (NFD, NFD),
        (INTEL_OPERATOR, INTEL),
        (INTEL_GPU, INTEL),
    ],
    ids=list(PINS),
)
def test_each_chart_installs_into_the_namespace_its_component_creates(applied: Run, chart: str, component: str) -> None:
    """Behind it, so it exists first; the backup plugin shares the database operator's, the GPU plugin the operator's."""
    created = applied.inputs(NAMESPACE, f'{component}-namespace')['metadata']['name']

    assert applied.inputs(CHART, chart)['namespace'] == created
    assert applied.urn(NAMESPACE, f'{component}-namespace') in applied.dependencies(CHART, chart)


# -- Order --------------------------------------------------------------------


@pytest.mark.parametrize(
    ('component', 'after'),
    [
        (POSTGRES_OPERATOR, CERT_MANAGER),
        (VOLSYNC_OPERATOR, CERT_MANAGER),
        (NODE_FEATURE_DISCOVERY, CILIUM),
        (INTEL_GPU_PLUGIN, CERT_MANAGER),
        (INTEL_GPU_PLUGIN, NFD),
    ],
    ids=['database-cert-manager', 'backup-cert-manager', 'nfd-cilium', 'gpu-cert-manager', 'gpu-nfd'],
)
def test_every_child_is_installed_behind_what_its_component_is_ordered_after(
    applied: Run, component: str, after: str
) -> None:
    """The database and backup operators come after cert-manager (cluster-infra.md §1); NFD needs only the CNI.

    The GPU plugin's operator declares a cert-manager `Issuer` and
    `Certificate`, and its plugin chart NFD's `NodeFeatureRule`s. Held on every
    resource the component registers, since a component's own `depends_on`
    reaches none of its children; the chart is what the preceding entry
    installs.
    """
    children = applied.children(component)
    behind = applied.urn(CHART, after)

    assert children
    assert [(typ, name) for typ, name, dependencies in children if behind not in dependencies] == []


def test_volsync_is_installed_behind_the_snapshot_definitions_it_watches(applied: Run) -> None:
    """Its controller's caches wait on `VolumeSnapshot`, so a cluster that does not serve the kind never runs it.

    Held on every resource the component registers, as the order above is.
    """
    children = applied.children(VOLSYNC_OPERATOR)
    behind = applied.urn(CONFIG_GROUP, SNAPSHOT_DEFINITIONS)

    assert children
    assert [(typ, name) for typ, name, dependencies in children if behind not in dependencies] == []


def test_the_snapshot_definitions_are_the_files_of_their_pin(applied: Run) -> None:
    """Every file the source pin names, each as the fetch returned it, and nothing else."""
    pin = versions.source['external-snapshotter']
    applied_text = applied.inputs(CONFIG_GROUP, SNAPSHOT_DEFINITIONS)['yaml']

    assert applied_text.split('\n---\n') == [source_file(pin, path) for path in pin.files]


@pytest.mark.parametrize(
    ('chart', 'after'),
    [(BARMAN_CLOUD, CNPG), (INTEL_GPU, INTEL_OPERATOR)],
    ids=['barman-cloud', 'gpu'],
)
def test_a_plugin_chart_is_installed_behind_its_operators(applied: Run, chart: str, after: str) -> None:
    """The backup plugin is the database operator's; the `GpuDevicePlugin` is the device-plugin operator's definition."""
    assert applied.urn(CHART, after) in applied.dependencies(CHART, chart)


# -- The drills' admission policy ----------------------------------------------


def test_the_database_operator_declares_the_drills_policy_and_its_binding_behind_its_chart(applied: Run) -> None:
    """Both are cluster-scoped, so `k8s-base`'s, and judge `Cluster`s, whose definition the operator's chart serves.

    What the policy admits is judged by the API server in
    `tests/test_apps_database.py`; here, that the program declares it at all,
    and its binding binds it.
    """
    children = {(typ, name): dependencies for typ, name, dependencies in applied.children(POSTGRES_OPERATOR)}
    (policy,) = (name for typ, name in children if typ == ADMISSION_POLICY)
    (binding,) = (name for typ, name in children if typ == ADMISSION_BINDING)
    behind = applied.urn(CHART, CNPG)

    assert behind in children[ADMISSION_POLICY, policy]
    assert behind in children[ADMISSION_BINDING, binding]
    assert (
        applied.inputs(ADMISSION_BINDING, binding)['spec']['policyName']
        == applied.inputs(ADMISSION_POLICY, policy)['metadata']['name']
    )


# -- The GPU plugin's class -----------------------------------------------------


def priority_policy(applied: Run) -> tuple[str, str]:
    """The logical names of the one mutating admission policy the GPU plugin's component declares, and of its binding."""
    children = applied.children(INTEL_GPU_PLUGIN)
    (policy,) = (name for typ, name, _ in children if typ == MUTATING_POLICY)
    (binding,) = (name for typ, name, _ in children if typ == MUTATING_BINDING)
    return policy, binding


def test_the_plugins_class_policy_matches_the_daemonset_the_operator_creates_for_the_programs_plugin(
    applied: Run,
) -> None:
    """The DaemonSet named after the chart's `GpuDevicePlugin`, in the namespace the operator runs in, on every write.

    The operator creates the plugin's DaemonSet in its own namespace, which is
    the one the plugin's chart installs into, and names it after the
    `GpuDevicePlugin` the chart declares (`operators_daemonset`).
    """
    policy, binding = priority_policy(applied)
    match = applied.inputs(MUTATING_POLICY, policy)['spec']['matchConstraints']
    (rule,) = match['resourceRules']

    assert rule['resourceNames'] == [operators_daemonset(applied.values(INTEL_GPU)['name'])]
    assert (rule['apiGroups'], rule['resources'], sorted(rule['operations'])) == (
        ['apps'],
        ['daemonsets'],
        ['CREATE', 'UPDATE'],
    )
    assert match['namespaceSelector'] == {
        'matchLabels': {'kubernetes.io/metadata.name': applied.inputs(CHART, INTEL_OPERATOR)['namespace']}
    }
    assert (
        applied.inputs(MUTATING_BINDING, binding)['spec']['policyName']
        == applied.inputs(MUTATING_POLICY, policy)['metadata']['name']
    )


#: The definitions the operator's chart serves, as the program installs them.
BUNDLE = Path(__file__).resolve().parents[1] / 'packages' / 'crds' / 'crds.yaml'
GPU_DEVICE_PLUGINS = 'gpudeviceplugins.deviceplugin.intel.com'


def bundled(name: str) -> dict[str, Any]:
    """The bundled definition `name`, as the cluster installs it."""
    for document in yaml.safe_load_all(BUNDLE.read_text()):
        if isinstance(document, dict) and cast('dict[str, Any]', document).get('metadata', {}).get('name') == name:
            return cast('dict[str, Any]', document)
    raise AssertionError(f'{BUNDLE} defines no {name}')


def properties(schema: Mapping[str, Any], path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """The path of every property `schema` declares, at any depth, items of arrays included."""
    for key, below in schema.get('properties', {}).items():
        yield (*path, key)
        yield from properties(below, (*path, key))
    if isinstance(items := schema.get('items'), Mapping):
        yield from properties(cast('Mapping[str, Any]', items), path)


def test_no_gpu_device_plugin_names_a_priority_class_while_the_policy_stands_in() -> None:
    """The class policy's removal condition, red the day the operator's `GpuDevicePlugin` takes a class itself.

    Then `gpu_values` sets the class, and the policy and its binding go
    (`components.gpu.plugin_priority` names everything they take with them).
    """
    found = [
        (version['name'], '.'.join(path))
        for version in bundled(GPU_DEVICE_PLUGINS)['spec']['versions']
        for path in properties(version['schema']['openAPIV3Schema']['properties']['spec'])
        if 'priorit' in path[-1].lower()
    ]

    assert found == [], f'{GPU_DEVICE_PLUGINS} now takes a priority class ({found}): remove the class policy'


def test_the_plugins_chart_is_installed_behind_the_policy_and_its_binding(applied: Run) -> None:
    """The operator creates the DaemonSet once the `GpuDevicePlugin` exists, so the policy is written to the API first.

    Writing it orders nothing about when the API server takes it in, which
    `components.gpu.plugin_priority` says.
    """
    policy, binding = priority_policy(applied)
    dependencies = applied.dependencies(CHART, INTEL_GPU)

    assert applied.urn(MUTATING_POLICY, policy) in dependencies
    assert applied.urn(MUTATING_BINDING, binding) in dependencies


# The policy is applied by the API server the cluster runs
# (`tests/control_plane.py`), with the `kubectl` `mise.toml` pins copied into
# the server's container as the client.

#: A namespace other than the operator's.
ELSEWHERE = 'elsewhere'
#: The label a container started here carries, the census of what a run left.
LABEL = 'kluster-test=test_operators'


def written(apiserver: ApiServer, verb: str, document: Mapping[str, Any]) -> dict[str, Any]:
    """`document` as admission leaves it for a `create` or a `replace`, nothing stored."""
    done = apiserver.written(verb, document)
    assert done.returncode == 0, done.stderr
    return cast('dict[str, Any]', json.loads(done.stdout))


def daemonset(name: str, namespace: str) -> dict[str, Any]:
    """A DaemonSet as the operator writes the plugin's: its embedded manifest's shape, naming no class."""
    labels = {'app': 'intel-gpu-plugin'}
    return {
        'apiVersion': 'apps/v1',
        'kind': 'DaemonSet',
        'metadata': {'name': name, 'namespace': namespace, 'labels': labels},
        'spec': {
            'selector': {'matchLabels': labels},
            'template': {
                'metadata': {'labels': labels},
                'spec': {'containers': [{'name': 'intel-gpu-plugin', 'image': 'registry.example.invalid/plugin:1'}]},
            },
        },
    }


def priority_class(document: Mapping[str, Any]) -> str | None:
    return document['spec']['template']['spec'].get('priorityClassName')


@dataclass(frozen=True)
class Plugin:
    """The API server holding the plugin's class policy, and where the operator writes the program's plugin."""

    apiserver: ApiServer
    name: str
    namespace: str


@pytest.fixture(scope='module')
def plugin(applied: Run, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Plugin]:
    """The control plane's API server, holding the policy the program declares for the GPU plugin's class."""
    pinned_image.require_podman("the plugin's class policy is applied by the API server image the cluster runs")
    kubectl = pinned_tools.located('kubectl')
    files = tmp_path_factory.mktemp('apiserver')
    with running(LABEL, files, InServer(kubectl)) as apiserver:
        namespace = applied.inputs(CHART, INTEL_OPERATOR)['namespace']
        for each in (namespace, ELSEWHERE):
            _ = apiserver.must('create', 'namespace', each)
        policy, binding = priority_policy(applied)
        group = 'admissionregistration.k8s.io/v1'
        _ = apiserver.must(
            'create',
            '-f',
            '-',
            document=typed(group, 'MutatingAdmissionPolicy', applied.inputs(MUTATING_POLICY, policy)),
        )
        _ = apiserver.must(
            'create', '-f', '-',
            document=typed(group, 'MutatingAdmissionPolicyBinding', applied.inputs(MUTATING_BINDING, binding)),
        )  # fmt: skip
        name = operators_daemonset(applied.values(INTEL_GPU)['name'])
        # The server takes in a policy behind its informers, so the first
        # writes can come before it applies; this is the first it mutates.
        until(
            lambda: priority_class(written(apiserver, 'create', daemonset(name, namespace))) is not None,
            'the policy was not applied',
        )
        yield Plugin(apiserver, name, namespace)


@pytest.mark.timeout(CASE_TIMEOUT)
def test_the_operators_daemonset_is_created_at_the_class_the_node_exporter_runs_at(
    applied: Run, plugin: Plugin
) -> None:
    """The operator's first write of the plugin's DaemonSet names no class; the policy gives it the node exporter's."""
    created = written(plugin.apiserver, 'create', daemonset(plugin.name, plugin.namespace))

    assert priority_class(created) == node_exporters_class(applied)


@pytest.mark.timeout(CASE_TIMEOUT)
def test_an_update_of_the_operators_daemonset_keeps_the_class(applied: Run, plugin: Plugin) -> None:
    """The operator writes the whole DaemonSet back when the plugin's spec changes; a write that drops the class gets it again."""
    _ = plugin.apiserver.must('create', '-f', '-', document=daemonset(plugin.name, plugin.namespace))
    try:
        stored = cast('dict[str, Any]', json.loads(plugin.apiserver.must(
            'get', 'daemonset', plugin.name, '--namespace', plugin.namespace, '-o', 'json'
        )))  # fmt: skip
        del stored['spec']['template']['spec']['priorityClassName']

        assert priority_class(written(plugin.apiserver, 'replace', stored)) == node_exporters_class(applied)
    finally:
        _ = plugin.apiserver.must('delete', 'daemonset', plugin.name, '--namespace', plugin.namespace)


@pytest.mark.timeout(CASE_TIMEOUT)
@pytest.mark.parametrize(
    ('name', 'namespace'),
    [('intel-gpu-plugin-other', None), (None, ELSEWHERE)],
    ids=['another name in its namespace', 'its name in another namespace'],
)
def test_no_other_daemonset_is_given_the_class(plugin: Plugin, name: str | None, namespace: str | None) -> None:
    """The policy is a layer over the operator's one DaemonSet, and leaves every other DaemonSet as written."""
    created = written(plugin.apiserver, 'create', daemonset(name or plugin.name, namespace or plugin.namespace))

    assert priority_class(created) is None
