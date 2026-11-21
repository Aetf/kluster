"""The layer that makes the VictoriaMetrics operator's updates whole (`components.monitoring.operator_updates`), and its tripwire.

**The tripwire** runs the operator image the cluster runs, `OPERATOR`,
against the control plane's API server in containers
(`tests/control_plane.py`), with the release's controller manager running
the workload controllers beside it, so that a workload is reported ready and
the operator finishes each reconcile. The operator watches one namespace,
with its webhook off, as the server's admin, and its definitions are the
bundle the cluster installs (`packages/crds/crds.yaml`). Each case is run on
a `VMAgent`, which the operator renders as a Deployment, and on a
`VMAlertmanager`, which it renders as a StatefulSet, each of zero replicas,
since no node runs a pod here. The first generation of each places its pod
(`PLACED`); the second is the case's.

A case judges by no elapsed time. It waits for the operator to have
finished reconciling the resource's generation (`reconciled`): the
resource's `status.observedGeneration` at its `metadata.generation`, with
`status.updateStatus` `operational`, which the operator writes once the
workload is ready (`reconcile/status.go` L405-425 at operator v0.74.0).
`control_plane.READY_TIMEOUT` is the stop-loss.

-   **The defect**: a second generation that drops the three fields leaves
    the workload carrying them. Red once the operator applies a removed
    field by itself, which is the layer's removal condition.
-   **The detour**: the same two generations, each marked by the layer,
    leave the workload carrying none of them and the second's mark. Red
    once what the layer leans on moves.
-   **The control**: a second generation that changes one of the fields
    reaches the workload, so a case that reads a field kept has a reconcile
    of its second generation behind it.

The layer's own cases need no container: each digest moves with what the
resource is rendered from and with nothing else, and every resource of a
kind that carries `spec.managedMetadata` is marked, the chart's by the
record `update_crds` keeps of the chart's values
(`packages/crds/chart-values.json`), the component's own by the bundle's
definitions.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pinned_image
import pinned_tools
import process_sessions
import pytest
import pytest_asyncio
import yaml
from control_plane import COMMAND_TIMEOUT, CONTROL_PLANE_PINS, READY_TIMEOUT, ApiServer, InServer, running, until
from k8s_base_installation import Run, applied

from kluster.components.monitoring import history_store_spec, memory, stack_values
from kluster.components.monitoring.operator_objects import API_VERSION
from kluster.components.monitoring.operator_updates import RENDERED_FROM, marked_spec, marked_values
from kluster.lib.k8s import at_home
from kluster.lib.versions import CHART as CHART_PIN
from kluster.lib.versions import NAMESPACE as PIN_NAMESPACE
from kluster.lib.versions import ChartPin, ImagePin, ProjectFile, Versions
from kluster.scripts.update_crds import record, sources

ROOT = Path(__file__).resolve().parents[1]
#: The definitions the cluster installs.
BUNDLE = ROOT / 'packages' / 'crds' / 'crds.yaml'
#: The operator's group.
GROUP = API_VERSION.split('/', 1)[0]

#: The image of the operator the stack chart installs: the operator chart's
#: default, at the release the cluster runs.
OPERATOR = ImagePin(
    'docker.io/victoriametrics/operator',
    'v0.74.0',
    'sha256:c4fde419a4f03c7c45d7ee554e7039c12c75af161f7f7e1baef1b343015e3df9',
)
#: The controllers a workload of the tripwire's needs to be reported ready.
CONTROLLERS = ('deployment', 'replicaset', 'statefulset')
#: The label a container started here carries, the census of what a run left.
LABEL = 'kluster-test=test_operator_updates'
#: The namespace the operator watches, and every resource here is in.
WATCHED = 'monitoring'

#: The bound on a tripwire case, past the suite's own: the first a worker
#: runs starts the server and the controllers behind the pulls of the control
#: plane's pins and the operator's, and waits for the server and the
#: definitions; each case waits for two reconciles.
TRIPWIRE_TIMEOUT = (len(CONTROL_PLANE_PINS) + 1) * pinned_image.PULL_TIMEOUT + 4 * READY_TIMEOUT

#: What a first generation sets: each of the three fields a pod's constraints
#: emit (`kluster.lib.k8s.Constraints.pod_fields_json`), set whole.
PLACED: dict[str, Any] = {
    'affinity': {
        'nodeAffinity': {
            'requiredDuringSchedulingIgnoredDuringExecution': {
                'nodeSelectorTerms': [
                    {'matchExpressions': [{'key': 'kluster.test/site', 'operator': 'In', 'values': ['home']}]}
                ]
            }
        }
    },
    'tolerations': [{'key': 'kluster.test/held', 'operator': 'Exists', 'effect': 'NoSchedule'}],
    'priorityClassName': 'kluster-test-first',
}

#: The workload each kind is rendered as, and the prefix of its name
#: (`PrefixedName`, `vmagent_types.go` and `vmalertmanager_types.go` at
#: operator v0.74.0).
WORKLOADS = {'VMAgent': ('deployment', 'vmagent'), 'VMAlertmanager': ('statefulset', 'vmalertmanager')}
#: What a kind takes beside the case's fields: a VMAgent writes somewhere.
REQUIRED: dict[str, dict[str, Any]] = {
    'VMAgent': {'remoteWrite': [{'url': 'http://store.invalid:8428/api/v1/write'}]},
    'VMAlertmanager': {},
}


def stack_chart() -> ChartPin:
    """The stack chart as `Pulumi.yaml` pins it, which installs the operator."""
    return Versions(ProjectFile(sources.read_config(ROOT / 'Pulumi.yaml'))).chart[memory.STACK_CHART]


def definitions() -> list[dict[str, Any]]:
    """The bundle's definitions of the operator's group."""
    found: list[dict[str, Any]] = []
    for document in yaml.safe_load_all(BUNDLE.read_text()):
        if isinstance(document, dict):
            typed = cast('dict[str, Any]', document)
            if typed.get('kind') == 'CustomResourceDefinition' and typed['spec']['group'] == GROUP:
                found.append(typed)
    assert found, f'{BUNDLE} defines nothing of {GROUP}'
    return found


@dataclass(frozen=True)
class Operated:
    """The server, with the operator watching `WATCHED` beside it, in the container `operator`."""

    apiserver: ApiServer
    operator: str


@pytest.fixture(scope='module')
def operated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Operated]:
    pinned_image.require_podman('the tripwire runs the operator image the cluster runs')
    operator = pinned_image.pulled(OPERATOR)
    kubectl = pinned_tools.located('kubectl')
    files = tmp_path_factory.mktemp('apiserver')
    with running(LABEL, files, InServer(kubectl), controllers=CONTROLLERS) as apiserver:
        bundled = definitions()
        _ = apiserver.must('create', '-f', '-', document={'apiVersion': 'v1', 'kind': 'List', 'items': bundled})
        _ = apiserver.must(
            'wait', '--for=condition=Established', *(f'crd/{it["metadata"]["name"]}' for it in bundled),
            f'--timeout={READY_TIMEOUT}s',
        )  # fmt: skip
        _ = apiserver.must('create', 'namespace', WATCHED)
        with apiserver.alongside('vm-operator', operator, env={'WATCH_NAMESPACE': WATCHED}) as container:
            yield Operated(apiserver, container)


def resource(kind: str, name: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    """A `kind` of zero replicas named `name`, with `spec` beside what the kind requires."""
    return {
        'apiVersion': API_VERSION,
        'kind': kind,
        'metadata': {'name': name, 'namespace': WATCHED},
        'spec': {'replicaCount': 0, **REQUIRED[kind], **spec},
    }


def read(apiserver: ApiServer, kind: str, name: str) -> dict[str, Any]:
    return cast('dict[str, Any]', json.loads(apiserver.must('get', kind, name, '--namespace', WATCHED, '-o', 'json')))


def reconciled(operated: Operated, kind: str, name: str) -> None:
    """Return once the operator has finished reconciling the resource's generation, within the stop-loss.

    A miss carries the operator's last log lines, which name what it waits on.
    """

    def finished() -> bool:
        stored = read(operated.apiserver, kind, name)
        status = stored.get('status', {})
        return (
            status.get('observedGeneration') == stored['metadata']['generation']
            and status.get('updateStatus') == 'operational'
        )

    try:
        until(finished, f'the operator finished no reconcile of {kind} {name}')
    except AssertionError as missed:
        logs = process_sessions.run(
            ['podman', 'logs', '--tail', '20', operated.operator], text=True, timeout=COMMAND_TIMEOUT
        )
        missed.add_note(logs.stdout + logs.stderr)
        raise


def workload(operated: Operated, kind: str, name: str) -> dict[str, Any]:
    """The workload the operator renders the resource `name` of `kind` as."""
    typ, prefix = WORKLOADS[kind]
    return read(operated.apiserver, typ, f'{prefix}-{name}')


def pod_fields(rendered: Mapping[str, Any]) -> dict[str, Any]:
    """Each field of `PLACED` the workload's pod template carries."""
    pod = rendered['spec']['template']['spec']
    return {key: pod[key] for key in PLACED if key in pod}


def updated(
    operated: Operated, kind: str, name: str, first: Mapping[str, Any], second: Mapping[str, Any]
) -> dict[str, Any]:
    """The workload of a resource created with the spec `first` and replaced with `second`, each reconciled."""
    _ = operated.apiserver.must('create', '-f', '-', document=resource(kind, name, first))
    reconciled(operated, kind, name)
    assert pod_fields(workload(operated, kind, name)) == {key: first[key] for key in PLACED if key in first}, (
        'the first generation did not reach the workload'
    )
    stored = read(operated.apiserver, kind, name)
    stored['spec'] = resource(kind, name, second)['spec']
    _ = operated.apiserver.must('replace', '-f', '-', document=stored)
    reconciled(operated, kind, name)
    return workload(operated, kind, name)


@pytest.mark.timeout(TRIPWIRE_TIMEOUT)
@pytest.mark.parametrize('kind', sorted(WORKLOADS))
def test_the_operator_leaves_on_the_workload_a_field_its_resource_stops_setting(operated: Operated, kind: str) -> None:
    """The defect: a second generation that sets none of the three fields leaves the workload carrying all three.

    The comparison skips a difference whose desired value is a nil pointer,
    an empty string or an empty slice, and nothing else changed. Red once
    the operator applies the removal by itself: `operator_updates` then goes.
    """
    rendered = updated(operated, kind, f'defect-{kind.lower()}', PLACED, {})

    assert pod_fields(rendered) == PLACED


@pytest.mark.timeout(TRIPWIRE_TIMEOUT)
@pytest.mark.parametrize('kind', sorted(WORKLOADS))
def test_a_resource_the_layer_marks_reaches_its_workload_whole(operated: Operated, kind: str) -> None:
    """The detour: the same two generations, each marked, leave the workload with none of the three, and the second's mark.

    The operator copies the mark onto the workload and, on a changed
    annotation, replaces the workload's whole spec. Red once either stops.
    """
    chart = stack_chart()
    first, second = marked_spec(chart, PLACED), marked_spec(chart, {})

    rendered = updated(operated, kind, f'detour-{kind.lower()}', first, second)

    assert pod_fields(rendered) == {}
    assert rendered['metadata']['annotations'][RENDERED_FROM] == mark_of(second)


@pytest.mark.timeout(TRIPWIRE_TIMEOUT)
@pytest.mark.parametrize('kind', sorted(WORKLOADS))
def test_a_field_its_resource_changes_reaches_the_workload(operated: Operated, kind: str) -> None:
    """The control: a second generation that changes one field reaches the workload, so the second is reconciled."""
    second = {**PLACED, 'priorityClassName': 'kluster-test-second'}

    rendered = updated(operated, kind, f'control-{kind.lower()}', PLACED, second)

    assert pod_fields(rendered) == second


# The layer's own cases.

#: The release the stack is installed as here, which names nothing the layer reads.
RELEASE = 'monitoring'


def pinned(version: str) -> ChartPin:
    return ChartPin(
        name=memory.STACK_CHART,
        repository='oci://registry.example/charts',
        version=version,
        digest=f'sha256:{version}',
        definitions=True,
    )


def mark_of(spec: object) -> str | None:
    """The mark `spec` carries, or `None`."""
    if not isinstance(spec, Mapping):
        return None
    metadata = cast('Mapping[str, Any]', spec).get('managedMetadata', {})
    return cast('str | None', metadata.get('annotations', {}).get(RENDERED_FROM))


def marks(values: Mapping[str, Any]) -> dict[str, str]:
    """Each resource the values mark, by its key, with its mark."""
    found: dict[str, str] = {}
    for key, entry in values.items():
        if isinstance(entry, Mapping):
            mark = mark_of(cast('Mapping[str, Any]', entry).get('spec'))
            if mark is not None:
                found[key] = mark
    return found


def unmarked_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """`spec` without its mark, and without a `managedMetadata` that held only the mark."""
    copied = copy.deepcopy(dict(spec))
    metadata = copied.get('managedMetadata')
    if metadata is None:
        return copied
    annotations = metadata.get('annotations', {})
    annotations.pop(RENDERED_FROM, None)
    if not annotations:
        metadata.pop('annotations', None)
    if not metadata:
        del copied['managedMetadata']
    return copied


def unmarked(values: Mapping[str, Any]) -> dict[str, Any]:
    """The values without any resource's mark."""
    result = dict(values)
    for key in marks(values):
        result[key] = {**values[key], 'spec': unmarked_spec(values[key]['spec'])}
    return result


def history(restore: list[dict[str, Any]]) -> dict[str, object]:
    image = ImagePin('docker.io/victoriametrics/victoria-metrics', 'v1.0.0', 'sha256:a')
    return history_store_spec(image=image, restore=restore)


def test_a_field_dropped_from_a_resource_moves_its_mark() -> None:
    """A field a resource stops setting gives it another mark: a constraint off vmagent, a store without its restore.

    A free pod emits none of the fields its constraints would, and a store
    with no restore emits an empty list of init containers: both are shapes
    the defect skips.
    """
    chart = pinned('1.0.0')
    free = unmarked(stack_values(RELEASE, chart, restore=[]))
    held = memory.merged(free, {'vmagent': {'spec': at_home('a case').pod_fields_json()}})
    restoring = [{'name': 'restore', 'image': 'example.invalid/restore:1'}]

    before, after = marks(marked_values(chart, held)), marks(marked_values(chart, free))
    assert before.keys() == after.keys()
    assert all(before[key] != after[key] for key in before)
    assert mark_of(marked_spec(chart, history(restoring))) != mark_of(marked_spec(chart, history([])))


def test_a_mark_moves_with_the_chart() -> None:
    """Another chart release renders through another operator, so it gives every resource another mark."""
    values = unmarked(stack_values(RELEASE, pinned('1.0.0'), restore=[]))
    spec = history([])

    first, bumped = pinned('1.0.0'), pinned('1.0.1')
    assert set(marks(marked_values(first, values)).values()).isdisjoint(marks(marked_values(bumped, values)).values())
    assert mark_of(marked_spec(first, spec)) != mark_of(marked_spec(bumped, spec))


def test_the_same_inputs_give_the_same_marks() -> None:
    """A run that changes nothing a resource is rendered from writes nothing to it."""
    chart = pinned('1.0.0')

    assert stack_values(RELEASE, chart, restore=[]) == stack_values(RELEASE, chart, restore=[])
    assert marked_spec(chart, history([])) == marked_spec(chart, history([]))


def test_the_layer_adds_the_mark_and_nothing_else() -> None:
    """Without its mark, what the layer hands on is what it was handed, a `managedMetadata` of the caller's kept."""
    chart = pinned('1.0.0')
    labelled = {'managedMetadata': {'labels': {'team': 'a'}, 'annotations': {'note': 'kept'}}}
    values = memory.merged(unmarked(stack_values(RELEASE, chart, restore=[])), {'vmsingle': {'spec': labelled}})
    spec = {**history([]), **labelled}

    assert unmarked(marked_values(chart, values)) == values
    assert unmarked_spec(marked_spec(chart, spec)) == spec
    assert unmarked_spec(marked_spec(chart, history([]))) == history([])


def test_a_spec_that_sets_the_mark_itself_is_refused() -> None:
    """The mark is the layer's alone, so a spec carrying one would have it overwritten."""
    spec = {'managedMetadata': {'annotations': {RENDERED_FROM: 'the caller'}}}

    with pytest.raises(ValueError, match=RENDERED_FROM):
        _ = marked_spec(pinned('1.0.0'), spec)


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The whole program against a `physical` that has published every output."""
    return await applied()


#: The registration the program installs the stack chart under.
STACK = 'monitoring'


def test_every_resource_the_chart_renders_from_the_values_is_marked(applied: Run) -> None:
    """Each key the values configure that the chart renders a resource from is marked, and only a key whose kind takes the mark.

    The record `update_crds` keeps of the pinned chart's values names
    `<key>.spec` for every top-level key the chart renders a resource's spec
    from, and `<key>.spec.managedMetadata.annotations` where the record
    passes that resource's definition through. A key is configured where the
    values hold it and do not switch it off. A configured key left unmarked
    is a resource whose workload keeps a removed field; a marked key the
    record has no such path for is a rename, or a kind whose definition the
    record does not pass through, which asks for a decision.
    """
    values = applied.values(STACK)
    marked = set(marks(values))

    assert marked  # Not vacuous.
    assert unmarked_resources(values) == set()
    assert marked <= {key for key in rendered_keys() if f'{key}.spec.managedMetadata.annotations' in record_paths()}


def test_a_resource_the_values_switch_on_unmarked_is_found() -> None:
    """A key the chart renders a resource from, switched on in the values and left unmarked, is named."""
    values = stack_values(RELEASE, pinned('1.0.0'), restore=[])

    assert unmarked_resources({**values, 'vmauth': {'enabled': True}}) == {'vmauth'}


def record_paths() -> set[str]:
    """The paths the record `update_crds` keeps of the pinned stack chart's values."""
    written = sources.bundle_path(ROOT / 'Pulumi.yaml').parent / record.CHART_VALUES_FILE_NAME
    entry = json.loads(written.read_text())[f'{PIN_NAMESPACE}:{CHART_PIN}-{memory.STACK_CHART}']
    return set(cast('list[str]', entry['paths']))


def rendered_keys() -> set[str]:
    """Each top-level key of the values whose `spec` the chart renders a resource from."""
    paths = record_paths()
    return {path.removesuffix('.spec') for path in paths if path.endswith('.spec') and path.count('.') == 1}


def unmarked_resources(values: Mapping[str, Any]) -> set[str]:
    """Each key the values configure, and the chart renders a resource from, that the values leave unmarked."""
    configured = {
        key
        for key, entry in values.items()
        if isinstance(entry, Mapping) and cast('Mapping[str, Any]', entry).get('enabled') is not False
    }
    return (rendered_keys() & configured) - set(marks(values))


def test_every_resource_the_component_declares_of_a_kind_that_takes_managed_metadata_is_marked(applied: Run) -> None:
    """Each object of the operator's group whose kind's definition has `spec.managedMetadata` carries the mark."""
    taking = {
        it['spec']['names']['kind']
        for it in definitions()
        if any('managedMetadata' in v['schema']['openAPIV3Schema']['properties']['spec'].get('properties', {})
               for v in it['spec']['versions'])
    }  # fmt: skip
    prefix = f'kubernetes:{API_VERSION}:'
    declared = [it for it in applied.monitor.declared if it.typ.startswith(prefix)]
    taken = [it for it in declared if it.typ.removeprefix(prefix) in taking]

    assert taken  # Not vacuous: the history store is one.
    assert [it.name for it in taken if mark_of(it.inputs.get('spec')) is None] == []
