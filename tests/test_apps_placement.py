"""Every application component, and the platform's stores, held to the relations of their node constraints at once.

One run declares every application component the installation defines, and
beside it the `k8s-base` program as its stack file holds it, for the
monitoring stores' values. What the run declares is derived from the
package tree: each package directly under `kluster.components.apps` that
defines an application component, or a function returning one, has a helper
module `tests/apps_<package>.py`, whose `declared()` declares the package's
components with the rows, pins and ciphertexts the package's test module
`test_apps_<package>.py` shares from it. A package whose helper or
`declared()` is missing fails by name, and so does one whose `declared()`
leaves out a component the package defines, so a new application joins the
run from its own helper and needs no edit here. A client of the portal is
declared there with the portal's issuer and the census's client
(`sso_portal`).

Every pod the run declares is read the way the scheduler reads it, against
the nodes as they are labelled (`node_selection`): each workload's pod
template, each CronJob's, each database instance and the restore drill's
recovered one, and each restic mover. So a component that forgets what its
declaration derives, or what its claim records for its pod, fails here
whichever component it is:

-   a pod of a component that declares it keeps working while the uplink is
    down admits no cloud node, and a declaring client of the portal requires
    the portal to declare too (cluster/architecture.md §1.1);
-   a pod that mounts a NAS claim admits exactly the nodes its volume does;
-   a restic mover admits exactly the nodes its workload does, the GPU its
    workload requests included, and the mover of a workload that requests
    the GPU admits only nodes that offer it;
-   a pod held to the binder requirements of what a `local-path` claim
    records admits only nodes the claim's pod can run on, the GPU it
    requests included, so whatever binds the claim in the pod's stead, a
    restic mover or a move's destination, binds it where the pod runs;
-   a pod that mounts a `local-path` claim writes in the blockio class
    `conventions.ETCD_DISK_WRITER`, and a mover in none;
-   a pod a volume or a device keeps to one node runs at the held priority;
-   a pod that serves a client of the portal reaches the issuer's name at the
    issuer's addresses.

Literals written here are the upstream projects' contracts: the key the GPU's
device plugin offers, Kubernetes' and CloudNativePG's field names, VolSync's.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

import component_census
import pytest
import pytest_asyncio
from k8s_base_installation import Run, applied
from mock_monitor import Recorder, declaring, run_with
from node_selection import CLOUD, NODES, schedulable, schedulable_among, selected
from sso_portal import ISSUER

from kluster import conventions
from kluster.components import apps
from kluster.components.apps import UPLINK_DOWN, storage
from kluster.components.apps.authelia import Authelia
from kluster.components.apps.echo import Echo
from kluster.components.gpu import GPU_NODE, GPU_RESOURCE
from kluster.components.monitoring.operator_objects import type_token
from kluster.conventions.sso import Client
from kluster.lib.k8s import Constraints
from putils import Component

DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
CRONJOB = 'kubernetes:batch/v1:CronJob'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLUSTER = 'crds:postgresql.cnpg.io/v1:Cluster'
SERVICE = 'kubernetes:core/v1:Service'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
VM_SINGLE = type_token('VMSingle')

#: The type tokens of the application components, which `kluster.components.apps` defines.
APPS = 'kluster:apps:'

#: The function each application's helper module declares its components with.
FACTORY = 'declared'

#: The monitoring component's chart, whose values hold the recent store.
MONITORING_CHART = 'monitoring'

#: The key of the restore drill's ConfigMap that holds its recovered cluster.
DRILL_MANIFEST = 'cluster.json'

#: Each of the cluster's nodes twice, by its name and whether it offers the
#: GPU: without the label the GPU plugin serves its nodes by
#: (`kluster.components.gpu.GPU_NODE`) and with it. Which node carries the GPU
#: is a fact of the hardware, so a pod's constraints are read on both, beside
#: every node they otherwise admit.
WITH_AND_WITHOUT_GPU: dict[tuple[str, bool], dict[str, str]] = {
    (node, gpu): {**labels, **({GPU_NODE.key: GPU_NODE.value} if gpu else {})}
    for node, labels in NODES.items()
    for gpu in (False, True)
}


@dataclass(frozen=True)
class Application:
    """One package under `kluster.components.apps` that defines an application component, and the helper module that declares it."""

    package: str
    defines: frozenset[type[Component]]
    """Every application component class the package defines, or a function of it returns."""
    module: str
    """The helper module under `tests/` that declares the package's components, `apps_<package>`."""
    factory: Callable[[], Sequence[Component]] | None
    """The module's `declared()`, or `None` where the module or the function is missing."""
    found: bool
    """Whether the helper module is there."""


def mentioned(annotation: object) -> set[object]:
    """Every type an annotation is built from, through unions and generics: `Echo | None` and `tuple[Echo, ...]` each mention `Echo`."""
    if isinstance(annotation, list):  # a `Callable`'s parameters
        return {it for argument in cast('list[object]', annotation) for it in mentioned(argument)}
    return {annotation, *(it for argument in get_args(annotation) for it in mentioned(argument))}


def _package_of(module: str) -> str:
    """The package directly under `kluster.components.apps` that `module` is, or lies in."""
    package, _, _ = module.removeprefix(f'{apps.__name__}.').partition('.')
    assert module.startswith(f'{apps.__name__}.'), (
        f'{module} defines an application component outside any package under {apps.__name__}, '
        'so no helper module is named for it'
    )
    return package


def applications() -> list[Application]:
    """Every package under `kluster.components.apps` that defines an application component, or a function returning one."""
    application_classes = {
        cls
        for cls in component_census.components()
        if (token := component_census.stated(cls)) is not None and token.startswith(APPS)
    }
    defined: dict[str, set[type[Component]]] = {}
    for cls in application_classes:
        defined.setdefault(_package_of(cls.__module__), set()).add(cls)
    for found in pkgutil.walk_packages(apps.__path__, f'{apps.__name__}.'):
        module = importlib.import_module(found.name)
        for value in vars(module).values():
            if inspect.isfunction(value) and value.__module__ == module.__name__:
                returned = mentioned(inspect.get_annotations(value, eval_str=True).get('return'))
                for cls in application_classes & returned:
                    defined.setdefault(_package_of(module.__name__), set()).add(cls)

    listed: list[Application] = []
    for package, classes in sorted(defined.items()):
        name = f'apps_{package}'
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as missing:
            if missing.name != name:
                raise
            listed.append(Application(package, frozenset(classes), name, None, found=False))
        else:
            factory = cast('Callable[[], Sequence[Component]] | None', getattr(module, FACTORY, None))
            listed.append(Application(package, frozenset(classes), name, factory, found=True))
    return listed


@dataclass(frozen=True)
class Declared:
    """One component the run declared: what its declarations derive, its namespaces, the clients its routes serve, and its claims."""

    name: str
    """Its URN."""
    kind: type[Component]
    constraints: Constraints
    namespaces: frozenset[str]
    """Every namespace a resource below it declares."""
    client: bool
    """Whether it is handed a client of the portal."""
    serves: frozenset[str]
    """The census's clients whose name a route in its namespaces serves."""
    claims: tuple[storage.Claim, ...] = ()
    """Every claim the component keeps, as an attribute, with the constraints it records."""


def handed_a_client(kind: type) -> bool:
    """Whether a parameter of the class's constructor takes a client of the portal, alone or within its annotation."""
    return any(Client in mentioned(it.annotation) for it in inspect.signature(kind, eval_str=True).parameters.values())


def _namespaces_below(monitor: Recorder, urn: str) -> frozenset[str]:
    """Every namespace a registration below the component at `urn` declares, through any chain of parents."""

    def below(request: Any) -> bool:
        parent = request.parent
        while parent in monitor.registrations:
            if parent == urn:
                return True
            parent = monitor.registrations[parent].parent
        return False

    return frozenset(
        str(request.object['metadata']['name'])
        for request in monitor.registrations.values()
        if request.type == NAMESPACE and below(request)
    )


def _served(monitor: Recorder, namespaces: frozenset[str]) -> frozenset[str]:
    """The census's clients whose name a route in `namespaces` serves."""
    hostnames = {
        name
        for it in monitor.of_type(HTTP_ROUTE)
        if it.inputs['metadata']['namespace'] in namespaces
        for name in it.inputs['spec'].get('hostnames', [])
    }
    return frozenset(client.name for client in conventions.sso.CLIENTS if f'{client.host}.{client.zone}' in hostnames)


async def _declared(monitor: Recorder, component: Component) -> Declared:
    """What the run reads of one component it declared: its constraints and claims off the component, the rest off its registrations."""
    kind = type(component)
    urn = await component.urn.future()
    assert urn is not None
    namespaces = _namespaces_below(monitor, urn)
    claims = tuple(it for it in cast('dict[str, object]', vars(component)).values() if isinstance(it, storage.Claim))
    return Declared(
        urn,
        kind,
        cast('Constraints', cast('Any', component).constraints),
        namespaces,
        handed_a_client(kind),
        _served(monitor, namespaces),
        claims,
    )


@dataclass(frozen=True)
class Program:
    """What the run declared: each application and the components its helper declared, each component, and the recorder."""

    applications: Mapping[Application, tuple[type[Component], ...]]
    """Each application, with the class of each component its `declared()` declared."""
    components: Sequence[Declared]
    monitor: Recorder


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def program() -> Program:
    """Every application component, each declared by its application's helper module, in one run."""
    monitor = await run_with(Recorder(), stack='apps')
    declared: dict[Application, Sequence[Component]] = {}
    async with declaring():
        for application in applications():
            declared[application] = () if application.factory is None else application.factory()
    return Program(
        {application: tuple(type(it) for it in found) for application, found in declared.items()},
        [await _declared(monitor, it) for found in declared.values() for it in found],
        monitor,
    )


class _OptionalClient:
    def __init__(self, *, client: Client | None = None) -> None: ...


class _NoClient:
    def __init__(self, name: str, *, clients: tuple[str, ...] = ()) -> None: ...


@pytest.mark.parametrize(
    ('annotation', 'mentions'),
    [
        pytest.param(Echo, True, id='the class'),
        pytest.param(Echo | None, True, id='optional'),
        pytest.param(tuple[Echo, ...], True, id='a tuple of them'),
        pytest.param(Sequence[Echo] | None, True, id='nested'),
        pytest.param(Callable[[int], Echo], True, id='a callable returning one'),
        pytest.param(tuple[str, ...], False, id='another type'),
    ],
)
def test_a_function_returning_an_application_component_in_any_shape_mentions_it(
    annotation: object, mentions: bool
) -> None:
    """A factory joins the run whatever shape its return annotation wraps the component in."""
    assert (Echo in mentioned(annotation)) is mentions


@pytest.mark.parametrize(
    ('kind', 'handed'),
    [
        pytest.param(_OptionalClient, True, id='an optional client'),
        pytest.param(_NoClient, False, id='no client'),
    ],
)
def test_a_component_handed_a_client_in_any_shape_is_a_client(kind: type, handed: bool) -> None:
    """A component that may be handed a client is held to the relations of one, whatever its annotation wraps the client in."""
    assert handed_a_client(kind) is handed


def test_every_application_is_declared_by_its_helper_module(program: Program) -> None:
    """The run reaches an application only through `declared()` in its helper module, which declares every component the package defines.

    A package the run did not reach renders nowhere here, and every relation
    below would pass without it; so would a component that declares no
    namespace, since the relations find a component's pods by its namespaces.
    """
    missing = sorted(
        f'{application.package}: tests/{application.module}.py '
        + (f'defines no `{FACTORY}()`' if application.found else 'is missing')
        for application in program.applications
        if application.factory is None
    )
    left_out = sorted(
        f'{application.package}: `{application.module}.{FACTORY}()` declares no {kind.__name__}'
        for application, declared in program.applications.items()
        for kind in application.defines
        if kind not in declared
    )
    unplaced = sorted(it.name for it in program.components if not it.namespaces)

    assert program.applications
    assert not missing, missing
    assert not left_out, left_out
    assert not unplaced, unplaced


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The `k8s-base` program as its stack file holds it, for the monitoring stores."""
    return await applied()


@dataclass(frozen=True)
class Pod:
    """One pod the run declared, as the scheduler and containerd read it."""

    name: str
    namespace: str
    spec: Mapping[str, Any]
    annotations: Mapping[str, str]
    claims: tuple[str, ...]
    """The names of the claims it mounts; an instance's own, on `local-path`, as `<name>-instance`."""
    kind: Literal['pod', 'instance', 'recovered'] = 'pod'
    """A workload's or a CronJob's pod, a database's instance, or a restore drill's recovered instance."""
    labels: Mapping[str, str] = field(default_factory=dict[str, str])
    """A workload's or a CronJob's pod's labels, which a Service selects it by."""


def _template_pod(name: str, namespace: str, template: Mapping[str, Any]) -> Pod:
    spec = cast('Mapping[str, Any]', template['spec'])
    volumes = cast('list[dict[str, Any]]', spec.get('volumes', []))
    metadata = cast('Mapping[str, Any]', template.get('metadata', {}))
    return Pod(
        name,
        namespace,
        spec,
        cast('Mapping[str, str]', metadata.get('annotations') or {}),
        tuple(it['persistentVolumeClaim']['claimName'] for it in volumes if 'persistentVolumeClaim' in it),
        labels=cast('Mapping[str, str]', metadata.get('labels') or {}),
    )


def _instance_pod(name: str, namespace: str, spec: Mapping[str, Any], kind: Literal['instance', 'recovered']) -> Pod:
    """What CloudNativePG puts on an instance's pod from its cluster's spec, its claim among it."""
    affinity = cast('Mapping[str, Any]', spec.get('affinity', {}))
    return Pod(
        name,
        namespace,
        {
            **({'affinity': {'nodeAffinity': affinity['nodeAffinity']}} if 'nodeAffinity' in affinity else {}),
            **({'tolerations': affinity['tolerations']} if 'tolerations' in affinity else {}),
            **({'priorityClassName': spec['priorityClassName']} if 'priorityClassName' in spec else {}),
        },
        cast('Mapping[str, str]', spec['inheritedMetadata'].get('annotations') or {}),
        (f'{name}-instance',) if spec['storage']['storageClass'] == conventions.SC_LOCAL_PATH else (),
        kind,
    )


def pods(monitor: Recorder) -> Iterator[Pod]:
    """Every pod the run declares: each workload's and CronJob's template, each instance, each drill's recovered instance."""
    for it in monitor.of_type(DEPLOYMENT):
        metadata = it.inputs['metadata']
        yield _template_pod(metadata['name'], metadata['namespace'], it.inputs['spec']['template'])
    for it in monitor.of_type(CRONJOB):
        metadata = it.inputs['metadata']
        template = it.inputs['spec']['jobTemplate']['spec']['template']
        yield _template_pod(metadata['name'], metadata['namespace'], template)
    for it in monitor.of_type(CLUSTER):
        metadata = it.inputs['metadata']
        yield _instance_pod(metadata['name'], metadata['namespace'], it.inputs['spec'], 'instance')
    for it in monitor.of_type(CONFIG_MAP):
        data = cast('Mapping[str, str]', it.inputs.get('data') or {})
        if DRILL_MANIFEST in data:
            manifest = cast('dict[str, Any]', json.loads(data[DRILL_MANIFEST]))
            yield _instance_pod(
                manifest['metadata']['name'], manifest['metadata']['namespace'], manifest['spec'], 'recovered'
            )


def local_path_claims(monitor: Recorder) -> set[tuple[str, str]]:
    """Every claim on `local-path` the run declares, by its namespace and name."""
    return {
        (it.inputs['metadata']['namespace'], it.inputs['metadata']['name'])
        for it in monitor.of_type('kubernetes:core/v1:PersistentVolumeClaim')
        if it.inputs['spec'].get('storageClassName') == conventions.SC_LOCAL_PATH
    }


def nas_volumes(monitor: Recorder) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Every NAS claim's volume, by its claim's namespace and name."""
    return {
        (it.inputs['spec']['claimRef']['namespace'], it.inputs['spec']['claimRef']['name']): it.inputs['spec']
        for it in monitor.of_type(VOLUME)
    }


def node_volume_claims(monitor: Recorder) -> set[tuple[str, str]]:
    """Every claim on a node volume's class the run declares, by its namespace and name."""
    return {
        (it.inputs['metadata']['namespace'], it.inputs['metadata']['name'])
        for it in monitor.of_type('kubernetes:core/v1:PersistentVolumeClaim')
        if it.inputs['spec'].get('storageClassName') not in (conventions.SC_LOCAL_PATH, conventions.SC_NAS)
    }


def backed_claims(monitor: Recorder) -> set[tuple[str, str]]:
    """Every claim a restic source backs, by its namespace and name: a claim its mover may bind first."""
    return {
        (it.inputs['metadata']['namespace'], it.inputs['spec']['sourcePVC'])
        for it in monitor.of_type(SOURCE)
        if 'restic' in it.inputs['spec']
    }


def kept_by_a_volume_or_a_device(pod: Pod, monitor: Recorder) -> bool:
    """Whether a claim bound on a node keeps the pod there, or a device a node offers; a drill's fresh claim keeps nothing."""
    on_a_node = local_path_claims(monitor) | node_volume_claims(monitor)
    if pod.kind == 'recovered':
        return False
    return (
        pod.kind == 'instance' or any((pod.namespace, it) in on_a_node for it in pod.claims) or requests_a_device(pod)
    )


def requests_a_device(pod: Pod) -> bool:
    """Whether a container of the pod requests the GPU, which the nodes the GPU plugin serves offer."""
    containers = cast('list[dict[str, Any]]', pod.spec.get('containers', []))
    return any(GPU_RESOURCE in container.get('resources', {}).get('requests', {}) for container in containers)


def admitting(spec: Mapping[str, Any], *, gpu: bool) -> set[tuple[str, bool]]:
    """The nodes of `WITH_AND_WITHOUT_GPU` a pod of `spec` may run on: its node constraints, and the GPU where it requests it.

    The scheduler reads a request for the GPU off the pod and places it only
    on a node whose device plugin offers it. The plugin serves the nodes its
    chart labels, and a labeled node offers `GPU_RESOURCE` while its GPU is
    bound to `i915`, which is the one driver the worker's image carries, so
    the label is read as the offer.
    """
    return {node for node in schedulable_among(spec, WITH_AND_WITHOUT_GPU) if node[1] or not gpu}


def restic_sources(monitor: Recorder) -> Iterator[tuple[Mapping[str, Any], Pod]]:
    """Every restic source the run declares, beside the workload's pod that mounts its claim."""
    by_claim = {(pod.namespace, it): pod for pod in pods(monitor) for it in pod.claims}
    for it in monitor.of_type(SOURCE):
        if 'restic' in it.inputs['spec']:
            yield it.inputs, by_claim[it.inputs['metadata']['namespace'], it.inputs['spec']['sourcePVC']]


def test_the_run_declares_every_kind_of_pod_these_relations_read(program: Program) -> None:
    """The relations below are vacuous over a kind of pod the run does not reach: each kind is here at least once."""
    every = list(pods(program.monitor))
    claims = local_path_claims(program.monitor)
    volumes = nas_volumes(program.monitor)

    assert any(pod.kind == 'instance' for pod in every)
    assert any(pod.kind == 'recovered' for pod in every)
    assert any(any((pod.namespace, it) in claims for it in pod.claims) for pod in every)
    assert any(any((pod.namespace, it) in volumes for it in pod.claims) for pod in every)
    assert any(requests_a_device(pod) for pod in every)


def test_every_pod_of_a_component_that_keeps_working_while_the_uplink_is_down_admits_no_cloud_node(
    program: Program,
) -> None:
    """With the uplink down the worker reaches no cloud node, so the declaration holds every pod at home."""
    declaring_ = [
        it
        for it in program.components
        if it.constraints.site is not None and UPLINK_DOWN in it.constraints.site.declarations
    ]
    assert declaring_

    for component in declaring_:
        held = [pod for pod in pods(program.monitor) if pod.namespace in component.namespaces]
        assert held, component.name
        for pod in held:
            assert schedulable(pod.spec) <= set(NODES) - CLOUD, (component.name, pod.name)
            assert {it['key'] for it in pod.spec.get('tolerations', [])} >= {'node.kubernetes.io/unreachable'}


def test_a_declaring_client_of_the_portal_requires_the_portal_to_declare(program: Program) -> None:
    """A client signs in through the portal while it answers, so the portal keeps working too.

    It holds nothing on `main`, and cannot fail there: no client of the portal
    declares the requirement, so the set it reads is empty, which is the
    state the rulings leave. It holds the first client that declares.
    """

    def declares(component: Declared) -> bool:
        site = component.constraints.site
        return site is not None and UPLINK_DOWN in site.declarations

    clients = [it for it in program.components if it.client and declares(it)]
    portals = [it for it in program.components if issubclass(it.kind, Authelia)]

    assert portals
    if clients:
        assert [it.name for it in portals if not declares(it)] == []


def test_a_pod_that_mounts_a_nas_claim_admits_exactly_the_nodes_its_volume_does(program: Program) -> None:
    volumes = nas_volumes(program.monitor)
    mounting = [pod for pod in pods(program.monitor) if any((pod.namespace, it) in volumes for it in pod.claims)]

    assert mounting
    for pod in mounting:
        for claim in pod.claims:
            if (pod.namespace, claim) in volumes:
                (term,) = volumes[pod.namespace, claim]['nodeAffinity']['required']['nodeSelectorTerms']
                assert schedulable(pod.spec) == selected(term, NODES), (pod.namespace, pod.name)


def test_every_nas_claim_is_mounted_by_a_pod_or_read_by_a_source_of_its_namespace(program: Program) -> None:
    """A NAS claim's name carries a digest of what its volume and it cannot change, so whatever reads it names it as it was given.

    A pod that named it any other way, by its base name say, would mount a
    claim that is not there, and the claim would serve nothing. A seed's
    source is the one reader of a NAS claim that is no pod of the census: it
    reads the claim by name, as `sourcePVC`.
    """
    mounted = {(pod.namespace, it) for pod in pods(program.monitor) for it in pod.claims} | {
        (it.inputs['metadata']['namespace'], it.inputs['spec']['sourcePVC']) for it in program.monitor.of_type(SOURCE)
    }
    claims = set(nas_volumes(program.monitor))

    assert claims
    assert claims <= mounted, sorted(claims - mounted)


def test_a_restic_mover_admits_exactly_the_nodes_its_workload_does(program: Program) -> None:
    """A claim binds where the first of its mover and its workload is placed, so the two admit one set of nodes.

    The mover requests no GPU, so the nodes its workload admits are read with
    the GPU the workload requests, on nodes with and without it.
    """
    sources = list(restic_sources(program.monitor))

    assert sources
    for source, workload in sources:
        mover = {'affinity': source['spec']['restic'].get('moverAffinity')}
        assert admitting(mover, gpu=False) == admitting(workload.spec, gpu=requests_a_device(workload)), source[
            'metadata'
        ]


def test_the_mover_of_a_claim_whose_workload_requests_the_gpu_admits_only_nodes_that_offer_it(
    program: Program,
) -> None:
    """A mover placed first binds the claim where it lands, and the workload, which needs the claim and the GPU, runs only where both are.

    Read on every node twice, with the GPU and without it: the mover admits
    some node, and each node it admits carries the label of a node that
    offers the GPU.
    """
    beside_the_gpu = [
        (source, workload) for source, workload in restic_sources(program.monitor) if requests_a_device(workload)
    ]

    assert beside_the_gpu
    for source, _ in beside_the_gpu:
        mover = {'affinity': source['spec']['restic'].get('moverAffinity')}
        admitted = admitting(mover, gpu=False)
        assert admitted, source['metadata']
        assert all(gpu for _, gpu in admitted), (source['metadata'], sorted(admitted))


def binder_spec(constraints: Constraints) -> dict[str, Any]:
    """A pod held to the requirements of a binder of a claim recorded with `constraints`, as one required term."""
    requirements = constraints.binder_requirements
    if not requirements:
        return {}
    term = {'matchExpressions': [{'key': it.key, 'operator': 'In', 'values': [it.value]} for it in requirements]}
    return {
        'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [term]}}}
    }


def test_a_binder_held_to_what_a_local_path_claim_was_handed_binds_it_only_where_its_pod_can_run(
    program: Program,
) -> None:
    """A restic mover, or a move's destination, binds the claim in its pod's stead, held to the constraints the claim records.

    It requests no GPU, so a constraint the pod composes onto the claim's
    after `claim`, a device's hold most of all, never reaches it. Read on
    every node with the GPU and without it: a pod held to the claim's binder
    requirements admits some node, and only nodes the pod that mounts the
    claim, with the GPU it requests, can run on. Every `local-path` claim
    the run declares is read, whether or not anything binds it today, since
    any of them may be moved, and so each is one a component keeps as an
    attribute, which is where the claim's constraints are read from.
    """
    on_local_path = local_path_claims(program.monitor)
    mounted = {(pod.namespace, it): pod for pod in pods(program.monitor) for it in pod.claims}
    read = [
        (component.name, namespace, claim, mounted[namespace, claim.name])
        for component in program.components
        for claim in component.claims
        for namespace in component.namespaces
        if (namespace, claim.name) in on_local_path
    ]

    assert {(namespace, claim.name) for _, namespace, claim, _ in read} == on_local_path
    assert any(requests_a_device(pod) for _, _, _, pod in read)
    for name, _, claim, pod in read:
        binder = admitting(binder_spec(claim.constraints), gpu=False)
        assert binder, (name, claim.name)
        assert binder <= admitting(pod.spec, gpu=requests_a_device(pod)), (name, claim.name, sorted(binder))


def test_a_pod_that_mounts_a_local_path_claim_writes_in_the_class_and_its_mover_in_none(program: Program) -> None:
    """Wherever the scheduler places it, the class caps what it writes to the disk etcd shares on a control plane."""
    claims = local_path_claims(program.monitor)
    writing = [
        pod
        for pod in pods(program.monitor)
        if any((pod.namespace, it) in claims for it in pod.claims) or f'{pod.name}-instance' in pod.claims
    ]

    assert writing
    for pod in writing:
        assert pod.annotations.get(conventions.BLOCKIO_POD_ANNOTATION) == conventions.ETCD_DISK_WRITER.name, (
            pod.namespace,
            pod.name,
        )
    for source in program.monitor.of_type(SOURCE):
        assert conventions.ETCD_DISK_WRITER.name not in json.dumps(source.inputs['spec']), source.name


def test_a_pod_kept_to_one_node_by_a_volume_or_a_device_runs_at_the_held_priority(program: Program) -> None:
    """Free pods that reach its node first would leave it pending, since it can run nowhere else."""
    kept = [pod for pod in pods(program.monitor) if kept_by_a_volume_or_a_device(pod, program.monitor)]

    assert kept
    for pod in kept:
        assert pod.spec.get('priorityClassName') == conventions.HELD_PRIORITY_CLASS, (pod.namespace, pod.name)


def test_a_restore_drills_recovered_instance_is_kept_nowhere(program: Program) -> None:
    """Its claim is made fresh each run, so it binds wherever the instance lands; held, a drill would preempt a serving pod."""
    for component in program.components:
        for pod in pods(program.monitor):
            if pod.kind == 'recovered' and pod.namespace in component.namespaces:
                assert pod.spec == component.constraints.pod_fields_json(), (component.name, pod.name)


def test_no_pod_mounts_a_backed_claim_beside_a_claim_another_pod_could_bind_it_away_from(program: Program) -> None:
    """A restic mover binds its claim apart from the pod's other claims, so the pod could find them on two nodes.

    Read from the render, however a component composed its constraints: no
    pod mounts two backed claims, or a backed claim beside a node volume's.
    """
    backed = backed_claims(program.monitor)
    on_a_node = node_volume_claims(program.monitor)

    assert backed
    for pod in pods(program.monitor):
        mounted = {(pod.namespace, it) for it in pod.claims}
        assert len(mounted & backed) <= 1, (pod.namespace, pod.name)
        assert not (mounted & backed and mounted & on_a_node), (pod.namespace, pod.name)


def serving(monitor: Recorder, client: Client) -> list[Pod]:
    """Every pod the run declares that serves `client`'s name: the pods a Service selects that a route on the name forwards to."""
    name = f'{client.host}.{client.zone}'
    backends = {
        (it.inputs['metadata']['namespace'], ref['name'])
        for it in monitor.of_type(HTTP_ROUTE)
        if name in it.inputs['spec'].get('hostnames', [])
        for rule in it.inputs['spec']['rules']
        for ref in rule.get('backendRefs', [])
    }
    selectors = [
        (it.inputs['metadata']['namespace'], cast('Mapping[str, str]', it.inputs['spec']['selector']))
        for it in monitor.of_type(SERVICE)
        if (it.inputs['metadata']['namespace'], it.inputs['metadata']['name']) in backends
    ]
    return [
        pod
        for pod in pods(monitor)
        if any(
            pod.namespace == namespace and selector.items() <= pod.labels.items() for namespace, selector in selectors
        )
    ]


def test_every_pod_that_serves_a_client_of_the_portal_reaches_the_issuers_name_at_the_issuers_addresses(
    program: Program,
) -> None:
    """The server a sign-in returns to calls the issuer by its public name, which no resolver a pod asks answers inside the cluster.

    Read over every client of the census whose name a route of the run
    serves, so a component that serves a client and forgets the alias fails
    here whichever component it is. Each component handed a client serves one
    of the census's, at pods a route's Service selects: one declared with a
    client of its own would escape the alias it is held to.
    """
    served = {client.name: found for client in conventions.sso.CLIENTS if (found := serving(program.monitor, client))}
    aliased = {(str(address), (ISSUER.host,)) for address in ISSUER.addresses}
    handed = [it for it in program.components if it.client]

    assert ISSUER.addresses
    assert handed
    for component in handed:
        assert component.serves, component.name
        assert component.serves <= served.keys(), (component.name, sorted(component.serves - served.keys()))
    for name, found in served.items():
        for pod in found:
            aliases = {(it['ip'], tuple(it['hostnames'])) for it in pod.spec.get('hostAliases', [])}
            assert aliases == aliased, (name, pod.namespace, pod.name)


@pytest.mark.parametrize('store', ['recent', 'history'])
def test_each_monitoring_store_writes_in_the_class_and_runs_at_the_held_priority(applied: Run, store: str) -> None:
    """Its `local-path` claim keeps it to its node, wherever the scheduler first placed it."""
    spec = _store(applied, store)

    assert spec['podMetadata']['annotations'][conventions.BLOCKIO_POD_ANNOTATION] == conventions.ETCD_DISK_WRITER.name
    assert spec['priorityClassName'] == conventions.HELD_PRIORITY_CLASS
    assert 'affinity' not in spec


def _store(run: Run, store: str) -> dict[str, Any]:
    """A store's spec: the chart's vmsingle, the recent store, or the history store the component declares."""
    if store == 'recent':
        return run.values(MONITORING_CHART)['vmsingle']['spec']
    (history,) = [it.inputs for it in run.monitor.of_type(VM_SINGLE)]
    return history['spec']
