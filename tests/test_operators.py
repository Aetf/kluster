"""The database, backup and GPU operators `k8s-base` installs, declared against mocks.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), and the cases read what it handed the provider:
CloudNativePG and its Barman Cloud plugin, VolSync, NFD and the Intel GPU
plugin (rfc-007 §8), the namespaces each creates, the order they are installed
in (declarative/cluster-infra.md §1), and what keeps the GPU plugin inert until
the worker's GPU is bound.

Literals written here are upstream contracts: the charts' value names, the
label the GPU plugin chart's `NodeFeatureRule` sets, and Kubernetes' own label
key and Pod Security levels. What the program derives from a pin is held to
that pin.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from k8s_base_installation import Run, applied

from kluster.components import gpu, postgres
from kluster.components.backup import volsync
from kluster.lib.versions import versions

CHART = 'kubernetes:helm.sh/v4:Chart'
NAMESPACE = 'kubernetes:core/v1:Namespace'

#: The label the Pod Security admission controller reads a namespace's level
#: from.
ENFORCE = 'pod-security.kubernetes.io/enforce'

#: The label the GPU plugin chart's `NodeFeatureRule` puts on a node with an
#: Intel display-class PCI device whose kernel has `i915` or `xe`
#: (`templates/gpu.yaml` of `intel-device-plugins-gpu` 0.36.0).
INTEL_GPU_NODE_LABEL = 'intel.feature.node.kubernetes.io/gpu'

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
    ('component', 'name', 'level'),
    [
        (CNPG, postgres.NAMESPACE, 'restricted'),
        (VOLSYNC, volsync.NAMESPACE, 'restricted'),
        (NFD, gpu.NFD_NAMESPACE, 'privileged'),
        (INTEL, gpu.INTEL_NAMESPACE, 'privileged'),
    ],
    ids=[CNPG, VOLSYNC, NFD, INTEL],
)
def test_each_namespace_enforces_its_pod_security_level(applied: Run, component: str, name: str, level: str) -> None:
    """The operators meet `restricted`; NFD's worker mounts host paths, and the GPU plugin the GPU's devices."""
    namespace_ = applied.inputs(NAMESPACE, f'{component}-namespace')['metadata']

    assert namespace_['name'] == name
    assert namespace_['labels'] == {ENFORCE: level}


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


@pytest.mark.parametrize(
    ('chart', 'after'),
    [(BARMAN_CLOUD, CNPG), (INTEL_GPU, INTEL_OPERATOR)],
    ids=['barman-cloud', 'gpu'],
)
def test_a_plugin_chart_is_installed_behind_its_operators(applied: Run, chart: str, after: str) -> None:
    """The backup plugin is the database operator's; the `GpuDevicePlugin` is the device-plugin operator's definition."""
    assert applied.urn(CHART, after) in applied.dependencies(CHART, chart)
