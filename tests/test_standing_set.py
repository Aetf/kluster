"""The operators `k8s-base` installs that need no secret, declared against mocks.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), and the cases read what it handed the provider:
the sealing controller, cert-manager, local-path-provisioner and reloader
(rfc-007 §6.1, §8), the namespaces each creates, and the order they are
installed in (declarative/cluster-infra.md §1).

Literals written here are upstream contracts: the name and namespace
`kubeseal` looks for the controller under, the charts' value names, the
provisioner's configuration format, and Kubernetes' own label and annotation
keys and Pod Security levels. What the program derives from a convention or a
pin is held to that convention or pin.

The namespace helper every one of those namespaces is created through is
held at the end, on its own.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from k8s_base_installation import AUTONAME_SUFFIX, Run, applied
from mock_monitor import Recorder, declaring, run_with
from renovate_text import as_python_spells_it, as_renovate_spells_it, listed, package_rules

from kluster import conventions
from kluster.components import local_path, reloader
from kluster.lib.k8s import PodSecurity, namespace
from kluster.lib.versions import versions

ROOT = Path(__file__).resolve().parents[1]

CHART = 'kubernetes:helm.sh/v4:Chart'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
STORAGE_CLASS = 'kubernetes:storage.k8s.io/v1:StorageClass'
SERVICE_ACCOUNT = 'kubernetes:core/v1:ServiceAccount'
ROLE = 'kubernetes:rbac.authorization.k8s.io/v1:Role'
ROLE_BINDING = 'kubernetes:rbac.authorization.k8s.io/v1:RoleBinding'
CLUSTER_ROLE = 'kubernetes:rbac.authorization.k8s.io/v1:ClusterRole'
CLUSTER_ROLE_BINDING = 'kubernetes:rbac.authorization.k8s.io/v1:ClusterRoleBinding'

#: The name and namespace `kubeseal` fetches the controller's certificate from
#: unless told otherwise (`cmd/kubeseal/main.go` at v0.39.1, `controllerName`
#: and `controllerNs`).
KUBESEAL_CONTROLLER = ('sealed-secrets-controller', 'kube-system')

#: The label the Pod Security admission controller reads a namespace's level
#: from.
ENFORCE = 'pod-security.kubernetes.io/enforce'

#: The annotation that makes a StorageClass the cluster's default.
DEFAULT_CLASS = 'storageclass.kubernetes.io/is-default-class'

#: The provisioner's configuration key for "every node not listed by name".
EVERY_NODE = 'DEFAULT_PATH_FOR_NON_LISTED_NODES'

#: The provisioner's permissions as the release's own manifest grants them, rule
#: for rule (`deploy/local-path-storage.yaml` at v0.0.37,
#: https://github.com/rancher/local-path-provisioner/blob/v0.0.37/deploy/local-path-storage.yaml):
#: the helper pods it creates and deletes in its own namespace, and the
#: volumes it creates and deletes for the whole cluster.
RELEASE_ROLE_RULES = [
    {
        'apiGroups': [''],
        'resources': ['pods'],
        'verbs': ['get', 'list', 'watch', 'create', 'patch', 'update', 'delete'],
    },
]
RELEASE_CLUSTER_ROLE_RULES = [
    {
        'apiGroups': [''],
        'resources': ['nodes', 'persistentvolumeclaims', 'configmaps', 'pods', 'pods/log'],
        'verbs': ['get', 'list', 'watch'],
    },
    {
        'apiGroups': [''],
        'resources': ['persistentvolumes'],
        'verbs': ['get', 'list', 'watch', 'create', 'patch', 'update', 'delete'],
    },
    {'apiGroups': [''], 'resources': ['events'], 'verbs': ['create', 'patch']},
    {'apiGroups': ['storage.k8s.io'], 'resources': ['storageclasses'], 'verbs': ['get', 'list', 'watch']},
]

#: The logical name of each component the program declares, which its chart
#: or its objects carry.
SEALING = 'sealed-secrets'
CERT_MANAGER = 'cert-manager'
LOCAL_PATH = 'local-path'
RELOADER = 'reloader'
CILIUM = 'cilium'


def local_path_configuration(run: Run) -> dict[str, str]:
    """What the provisioner's ConfigMap holds: its configuration, the helper pod's template and its scripts."""
    return run.inputs(CONFIG_MAP, LOCAL_PATH)['data']


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The whole program against a `physical` that has published every output."""
    return await applied()


# -- Each chart, from its pin -------------------------------------------------


@pytest.mark.parametrize('chart', [SEALING, CERT_MANAGER, RELOADER])
def test_each_chart_is_installed_from_its_pin(applied: Run, chart: str) -> None:
    """The reference and the version are the pin's; an HTTP repository's address is passed beside the name."""
    pin = versions.chart[chart]
    inputs = applied.inputs(CHART, chart)

    assert (inputs['chart'], inputs['version']) == (pin.reference, pin.version)
    assert inputs.get('repositoryOpts', {}).get('repo') == (None if pin.oci else pin.repository)


# -- The sealing controller ---------------------------------------------------


def test_the_controller_is_named_and_placed_where_kubeseal_looks_for_it(applied: Run) -> None:
    """The chart's own name would be `sealed-secrets`, which `kubeseal` would not find without a flag."""
    inputs = applied.inputs(CHART, SEALING)

    assert (inputs['values']['fullnameOverride'], inputs['namespace']) == KUBESEAL_CONTROLLER


# -- cert-manager ---------------------------------------------------------------


def test_cert_managers_definitions_are_installed_and_its_start_up_check_is_off(applied: Run) -> None:
    """The definitions are behind a value, off by default; the start-up check is a hook `helm.v4.Chart` would drop."""
    values = applied.values(CERT_MANAGER)

    assert values['crds']['enabled'] is True
    assert values['startupapicheck']['enabled'] is False


# -- local-path-provisioner ------------------------------------------------------


def test_local_paths_configuration_hands_its_class_the_user_volume(applied: Run) -> None:
    """Every node's volumes of the `local-path` class are under the user volume, and no other class has a path."""
    configuration = json.loads(local_path_configuration(applied)['config.json'])

    assert configuration['nodePathMap'] == []
    assert configuration['storageClassConfigs'] == {
        conventions.SC_LOCAL_PATH: {'nodePathMap': [{'node': EVERY_NODE, 'paths': [conventions.LOCAL_PATH_ROOT]}]}
    }


def test_local_path_is_the_default_class_reclaims_delete_and_binds_on_scheduling(applied: Run) -> None:
    """The class the configuration names, served by the provisioner the Deployment runs."""
    storage_class = applied.inputs(STORAGE_CLASS, LOCAL_PATH)

    assert storage_class['metadata']['name'] == conventions.SC_LOCAL_PATH
    assert storage_class['metadata']['annotations'] == {DEFAULT_CLASS: 'true'}
    assert storage_class['reclaimPolicy'] == 'Delete'
    assert storage_class['volumeBindingMode'] == 'WaitForFirstConsumer'
    # The release's default provisioner name, which the Deployment sets no other.
    (container,) = applied.inputs(DEPLOYMENT, LOCAL_PATH)['spec']['template']['spec']['containers']
    assert storage_class['provisioner'] == 'rancher.io/local-path'
    assert '--provisioner-name' not in container['command']
    assert 'PROVISIONER_NAME' not in {variable['name'] for variable in container['env']}


def test_both_local_path_images_come_from_their_pins(applied: Run) -> None:
    """The provisioner's image is its container's, and the helper's is the helper pod template's."""
    (container,) = applied.inputs(DEPLOYMENT, LOCAL_PATH)['spec']['template']['spec']['containers']
    helper = yaml.safe_load(local_path_configuration(applied)['helperPod.yaml'])

    assert container['image'] == str(versions.image['local-path-provisioner'])
    assert [it['image'] for it in helper['spec']['containers']] == [str(versions.image['local-path-helper'])]


def test_the_provisioner_reads_the_configmap_the_component_declares(applied: Run) -> None:
    """Named by the provider, so the Deployment takes the name from the ConfigMap rather than spelling one."""
    (container,) = applied.inputs(DEPLOYMENT, LOCAL_PATH)['spec']['template']['spec']['containers']
    command = container['command']
    configmap = applied.monitor.inputs_of(LOCAL_PATH, CONFIG_MAP)

    assert command[command.index('--configmap-name') + 1] == f'{LOCAL_PATH}-{AUTONAME_SUFFIX}'
    assert 'name' not in configmap['metadata']
    assert {'setup', 'teardown'} <= local_path_configuration(applied).keys()


def test_the_helper_pods_run_under_the_account_the_component_declares(applied: Run) -> None:
    """The provisioner sets the account it is told on every helper pod, and its default names none that exists.

    So the name is read off the provisioner's own pod, whose account is the
    one declared beside it.
    """
    account = applied.inputs(SERVICE_ACCOUNT, LOCAL_PATH)['metadata']
    pod = applied.inputs(DEPLOYMENT, LOCAL_PATH)['spec']['template']['spec']
    (container,) = pod['containers']
    told = {variable['name']: variable for variable in container['env']}['SERVICE_ACCOUNT_NAME']

    assert told == {'name': 'SERVICE_ACCOUNT_NAME', 'valueFrom': {'fieldRef': {'fieldPath': 'spec.serviceAccountName'}}}
    assert pod['serviceAccountName'] == account['name']
    assert account['namespace'] == local_path.NAMESPACE


@pytest.mark.parametrize(
    ('binding', 'role', 'rules'),
    [(ROLE_BINDING, ROLE, RELEASE_ROLE_RULES), (CLUSTER_ROLE_BINDING, CLUSTER_ROLE, RELEASE_CLUSTER_ROLE_RULES)],
    ids=['helper-pods', 'volumes'],
)
def test_the_account_is_granted_the_releases_permissions(
    applied: Run, binding: str, role: str, rules: list[dict[str, list[str]]]
) -> None:
    """Each role carries the release's rules and is bound to the provisioner's account, and to nothing else."""
    account = applied.inputs(SERVICE_ACCOUNT, LOCAL_PATH)['metadata']
    granted = applied.inputs(role, LOCAL_PATH)
    bound = applied.inputs(binding, LOCAL_PATH)
    kind = role.rsplit(':', 1)[-1]

    assert granted['rules'] == rules
    assert bound['roleRef'] == {
        'apiGroup': 'rbac.authorization.k8s.io',
        'kind': kind,
        'name': granted['metadata']['name'],
    }
    assert bound['subjects'] == [{'kind': 'ServiceAccount', 'name': account['name'], 'namespace': account['namespace']}]


#: The image manager's pattern, as `renovate.json5` writes it: the dependency
#: is the reference before the tag, the version the tag.
IMAGE_MATCH_STRING = (
    r'versions:image-[\w-]+:\s*(?<depName>\S+?):(?<currentValue>[^\s:@/]+)@(?<currentDigest>sha256:[0-9a-f]{64})'
)


def test_the_provisioners_patch_releases_reach_a_pull_request() -> None:
    """Every provisioner release is `v0.0.N`, a patch to docker versioning, and patches are off unless a rule says so.

    The rule names the dependency the image manager reads off the pin's line,
    re-enables patches and nothing else, and names no other dependency: every
    other pin's patches stay folded into its minors. Renovate reads its
    configuration from the default branch, so nothing goes red there when
    this breaks.
    """
    config = (ROOT / 'renovate.json5').read_text()
    pin = re.search(r'^\s*versions:image-local-path-provisioner:.*$', (ROOT / 'Pulumi.yaml').read_text(), re.MULTILINE)
    assert pin is not None
    read = as_python_spells_it(IMAGE_MATCH_STRING).search(pin[0])
    assert read is not None
    rules = [rule for rule in package_rules(config) if read['depName'] in listed(rule, 'matchDepNames')]

    assert as_renovate_spells_it(IMAGE_MATCH_STRING) in config
    assert re.fullmatch(r'v0\.0\.\d+', read['currentValue'])
    assert len(rules) == 1
    (rule,) = rules
    assert listed(rule, 'matchDepNames') == [read['depName']]
    assert listed(rule, 'matchUpdateTypes') == ['patch']
    assert re.search(r'^\s*enabled: true,$', rule, re.MULTILINE)
    assert re.search(r'^\s*patch: \{\s*(//[^\n]*\s*)*enabled: false,', config, re.MULTILINE)


# -- reloader -----------------------------------------------------------------


def test_reloaders_container_drops_every_capability_and_refuses_privilege_escalation(applied: Run) -> None:
    """What `restricted` asks of a container, which the chart leaves empty, on the chart's read-only root."""
    values = applied.values(RELOADER)['reloader']

    assert values['deployment']['containerSecurityContext'] == {
        'allowPrivilegeEscalation': False,
        'capabilities': {'drop': ['ALL']},
    }
    assert values['readOnlyRootFileSystem'] is True


# -- Namespaces ---------------------------------------------------------------


@pytest.mark.parametrize(
    ('component', 'name', 'level'),
    [
        (CERT_MANAGER, conventions.CERT_MANAGER_NAMESPACE, 'restricted'),
        (RELOADER, reloader.NAMESPACE, 'restricted'),
        (LOCAL_PATH, local_path.NAMESPACE, 'privileged'),
    ],
)
def test_each_namespace_enforces_its_pod_security_level(applied: Run, component: str, name: str, level: str) -> None:
    """`restricted` unless the component's pods need the host; local-path's helper pods mount the path they provision."""
    namespace_ = applied.inputs(NAMESPACE, f'{component}-namespace')['metadata']

    assert namespace_['name'] == name
    assert namespace_['labels'] == {ENFORCE: level}


@pytest.mark.parametrize('chart', [CERT_MANAGER, RELOADER])
def test_each_chart_installs_into_the_namespace_its_component_creates(applied: Run, chart: str) -> None:
    """Into the namespace that states the level, and behind it, so the namespace exists before the chart's objects."""
    created = applied.inputs(NAMESPACE, f'{chart}-namespace')['metadata']['name']

    assert applied.inputs(CHART, chart)['namespace'] == created
    assert applied.urn(NAMESPACE, f'{chart}-namespace') in applied.dependencies(CHART, chart)


def test_the_sealing_controller_creates_no_namespace(applied: Run) -> None:
    """`kube-system` is under Talos' Pod Security exemption, and nobody else's to create."""
    assert 'kube-system' not in {it.inputs['metadata']['name'] for it in applied.monitor.of_type(NAMESPACE)}


# -- Order --------------------------------------------------------------------


@pytest.mark.parametrize(
    ('component', 'after'),
    [
        ('kluster:sealing:SealedSecretsController', CILIUM),
        ('kluster:certificates:CertManager', SEALING),
        ('kluster:local_path:LocalPathProvisioner', CILIUM),
        ('kluster:reloader:Reloader', CILIUM),
    ],
    ids=[SEALING, CERT_MANAGER, LOCAL_PATH, RELOADER],
)
def test_every_child_is_installed_behind_what_cluster_infra_orders_its_component_after(
    applied: Run, component: str, after: str
) -> None:
    """Nothing schedules before the CNI, and cert-manager's solver credential is a sealed value.

    Held on every resource the component registers, since a component's own
    `depends_on` reaches none of its children; the chart is what the preceding
    entry installs.
    """
    (parent,) = [urn for urn, request in applied.monitor.registrations.items() if request.type == component]
    children = [request for request in applied.monitor.registrations.values() if request.parent == parent]
    behind = applied.urn(CHART, after)

    assert children
    assert [(it.type, it.name) for it in children if behind not in it.dependencies] == []


def test_every_child_carries_its_components_name(applied: Run) -> None:
    assert applied.monitor.children_not_named_for_their_component() == {}


# -- The namespace helper ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_namespace_enforces_the_level_it_is_created_with() -> None:
    """The level is the admission controller's label, with the value it reads, and the namespace keeps its name.

    One level, the one furthest from the house default: every level takes the
    same path to the label, and a helper that wrote the default whatever it
    was handed would pass for the default itself.
    """
    level = PodSecurity.PRIVILEGED
    monitor = await run_with(Recorder(), stack='test')
    async with declaring():
        _ = namespace('owner-namespace', name='owned', pod_security=level)

    metadata = monitor.inputs_of('owner-namespace', NAMESPACE)['metadata']
    assert metadata == {'name': 'owned', 'labels': {ENFORCE: level.value}}


def test_the_levels_are_the_pod_security_standards_own() -> None:
    """The admission controller refuses a label whose value is not one of its levels."""
    assert {level.value for level in PodSecurity} == {'privileged', 'baseline', 'restricted'}
