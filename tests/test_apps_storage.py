"""The application base's storage, declared against mocks: `claim`, `off`, a move, and `nas_claim`.

Every claim here is the test's own, each in a namespace of its own, so what is
held is how a claim's arguments decide what is emitted. The retention class is
the test's own too, with a schedule and counts no class of `conventions`
carries, so a source scheduled from anything but the class it is handed reads
differently. The writer keys are the test's own ciphertext, one per namespace.

Each namespace's workload is a pod of the test's own, pinned by a placement of
the test's own to one pool of the cluster's nodes, or to none, so that where a
mover may run is read against where the workload may, both as the scheduler
reads them.

Literals written here are the upstream projects' contracts: VolSync's fields
and the key its manual trigger is reported under, restic's environment, the
Pulumi Kubernetes provider's await annotations, Cilium's policy fields, the
label Talos gives a control plane node and the one Kubernetes gives every
node its name in.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

import pulumi
import pulumi_kubernetes as k8s
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pulumi import runtime

from kluster import conventions
from kluster.components.apps.storage import claim, nas_claim, off
from kluster.components.backup import s3_endpoint
from kluster.conventions.backup import RetentionClass
from kluster.lib.k8s import WORKER_AFFINITY_JSON

CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
DESTINATION = 'crds:volsync.backube/v1alpha1:ReplicationDestination'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'
PASSWORD = 'random:index/randomPassword:RandomPassword'

#: What the random provider generates for every password, as it would into state.
GENERATED = 'generated-in-state'

#: A class of the test's own, unlike any of `conventions`.
WEEKDAY = RetentionClass(name='weekday', schedule='17 5 * * 2', period=dt.timedelta(weeks=1), daily=9, monthly=2)

#: The label the copy moves a claim to.
TARGET = {'example.com/disk': 'fast'}

UID = 1234

#: The label Talos gives a control plane node, and the one Kubernetes gives every node its name in.
TALOS_CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'
HOSTNAME_LABEL = 'kubernetes.io/hostname'

#: The cluster's nodes, labeled as Talos labels them: every cloud node is a control plane node, the worker is not.
NODES: dict[str, dict[str, str]] = {
    **{node: {HOSTNAME_LABEL: node, TALOS_CONTROL_PLANE_LABEL: ''} for node in conventions.CLOUD_NODES},
    conventions.HOMELAB_NODE: {HOSTNAME_LABEL: conventions.HOMELAB_NODE},
}


def pinned(operator: str) -> k8s.core.v1.AffinityArgs:
    """A placement on the control plane nodes (`Exists`) or off them (`DoesNotExist`)."""
    return k8s.core.v1.AffinityArgs(
        node_affinity=k8s.core.v1.NodeAffinityArgs(
            required_during_scheduling_ignored_during_execution=k8s.core.v1.NodeSelectorArgs(
                node_selector_terms=[
                    k8s.core.v1.NodeSelectorTermArgs(
                        match_expressions=[
                            k8s.core.v1.NodeSelectorRequirementArgs(key=TALOS_CONTROL_PLANE_LABEL, operator=operator)
                        ]
                    )
                ]
            )
        )
    )


CLOUD = pinned('Exists')
WORKER = pinned('DoesNotExist')


def requirement(key: str, *values: str) -> k8s.core.v1.NodeSelectorRequirementArgs:
    return k8s.core.v1.NodeSelectorRequirementArgs(key=key, operator='In', values=list(values))


#: Two terms, one on a node's label and one on its name, which admit the first
#: cloud node and the worker, and a preference among them for the worker.
TWO_TERMS = k8s.core.v1.AffinityArgs(
    node_affinity=k8s.core.v1.NodeAffinityArgs(
        required_during_scheduling_ignored_during_execution=k8s.core.v1.NodeSelectorArgs(
            node_selector_terms=[
                k8s.core.v1.NodeSelectorTermArgs(
                    match_expressions=[requirement(HOSTNAME_LABEL, conventions.CLOUD_NODES[0])]
                ),
                k8s.core.v1.NodeSelectorTermArgs(match_fields=[requirement('metadata.name', conventions.HOMELAB_NODE)]),
            ]
        ),
        preferred_during_scheduling_ignored_during_execution=[
            k8s.core.v1.PreferredSchedulingTermArgs(
                weight=10,
                preference=k8s.core.v1.NodeSelectorTermArgs(
                    match_expressions=[requirement(HOSTNAME_LABEL, conventions.HOMELAB_NODE)]
                ),
            )
        ],
    )
)

#: A placement stated against other pods: none of the workload's replicas beside another.
APART = k8s.core.v1.AffinityArgs(
    pod_anti_affinity=k8s.core.v1.PodAntiAffinityArgs(
        required_during_scheduling_ignored_during_execution=[
            k8s.core.v1.PodAffinityTermArgs(topology_key=HOSTNAME_LABEL)
        ]
    )
)

#: A placement on one node, which a pod mounting more than one backed claim takes.
ONE_NODE = k8s.core.v1.AffinityArgs(
    node_affinity=k8s.core.v1.NodeAffinityArgs(
        required_during_scheduling_ignored_during_execution=k8s.core.v1.NodeSelectorArgs(
            node_selector_terms=[
                k8s.core.v1.NodeSelectorTermArgs(
                    match_expressions=[requirement(HOSTNAME_LABEL, conventions.CLOUD_NODES[0])]
                )
            ]
        )
    )
)

#: Preferences alone, of two weights: the heavier on a node's name, the lighter on a label.
PREFERS = k8s.core.v1.AffinityArgs(
    node_affinity=k8s.core.v1.NodeAffinityArgs(
        preferred_during_scheduling_ignored_during_execution=[
            k8s.core.v1.PreferredSchedulingTermArgs(
                weight=90,
                preference=k8s.core.v1.NodeSelectorTermArgs(
                    match_fields=[requirement('metadata.name', conventions.CLOUD_NODES[1])]
                ),
            ),
            k8s.core.v1.PreferredSchedulingTermArgs(
                weight=10,
                preference=k8s.core.v1.NodeSelectorTermArgs(
                    match_expressions=[requirement(HOSTNAME_LABEL, conventions.HOMELAB_NODE)]
                ),
            ),
        ]
    )
)

#: The placement each namespace's workload states.
PLACEMENTS: dict[str, k8s.core.v1.AffinityArgs] = {
    'notes': ONE_NODE,
    'models': WORKER,
    'copying': WORKER,
    'moved': CLOUD,
    'cache': WORKER,
    'anywhere': k8s.core.v1.AffinityArgs(),
    'two': TWO_TERMS,
    'prefers': PREFERS,
}


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


class Generating(Recorder):
    """A monitor that answers a password's `result` as the random provider would."""

    def computed(self, args: runtime.MockResourceArgs) -> dict[str, Any]:
        return {'result': GENERATED} if args.typ == PASSWORD else {}


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """One of each kind of claim, each in its own namespace, and a NAS claim."""
    monitor = await run_with(Generating(), stack='apps')
    async with declaring():
        workload = k8s.core.v1.ConfigMap('workload')
        for namespace, placement in PLACEMENTS.items():
            _ = k8s.core.v1.Pod(
                f'{namespace}-workload',
                metadata=k8s.meta.v1.ObjectMetaArgs(name='workload', namespace=namespace),
                spec=k8s.core.v1.PodSpecArgs(
                    affinity=placement, containers=[k8s.core.v1.ContainerArgs(name='app', image='example.com/app')]
                ),
            )
        # Two backed claims of one pod, on one node, the second handed the first's placement.
        notes = claim(
            'notes',
            'data',
            2,
            WEEKDAY,
            namespace='notes',
            uid=UID,
            writer_key=writer_key('notes'),
            placement=PLACEMENTS['notes'],
        )
        _ = claim(
            'notes-index',
            'index',
            1,
            WEEKDAY,
            namespace='notes',
            uid=UID,
            writer_key=writer_key('notes'),
            placement=notes.placement,
        )
        _ = claim(
            'models',
            'models',
            11,
            off('re-derived on start'),
            namespace='models',
            uid=UID,
            writer_key=writer_key('models'),
            placement=PLACEMENTS['models'],
        )
        # The copy of a backed claim, and the finish of the same move.
        _ = claim(
            'copying',
            'data',
            3,
            WEEKDAY,
            namespace='copying',
            uid=UID,
            writer_key=writer_key('copying'),
            placement=PLACEMENTS['copying'],
            generation=1,
            moving_to=TARGET,
            stopped=workload,
            protect=True,
        )
        _ = claim(
            'moved',
            'data',
            3,
            WEEKDAY,
            namespace='moved',
            uid=UID,
            writer_key=writer_key('moved'),
            placement=PLACEMENTS['moved'],
            generation=1,
        )
        # The copy of a claim that nothing backs up.
        _ = claim(
            'cache',
            'cache',
            50,
            off('its network is its redundancy'),
            namespace='cache',
            uid=UID,
            writer_key=writer_key('cache'),
            placement=PLACEMENTS['cache'],
            generation=2,
            moving_to=TARGET,
            stopped=workload,
        )
        # A backed claim whose workload states no placement.
        _ = claim(
            'anywhere',
            'data',
            1,
            WEEKDAY,
            namespace='anywhere',
            uid=UID,
            writer_key=writer_key('anywhere'),
            placement=PLACEMENTS['anywhere'],
        )
        # A backed claim whose workload's placement has two terms and a preference.
        _ = claim(
            'two', 'data', 1, WEEKDAY, namespace='two', uid=UID, writer_key=writer_key('two'), placement=TWO_TERMS
        )
        # A backed claim whose workload's placement states preferences alone.
        _ = claim(
            'prefers',
            'data',
            1,
            WEEKDAY,
            namespace='prefers',
            uid=UID,
            writer_key=writer_key('prefers'),
            placement=PREFERS,
        )
        # A claim nothing backs up, whose workload keeps apart from its own replicas.
        _ = claim(
            'apart',
            'data',
            1,
            off('rebuilt on start'),
            namespace='apart',
            uid=UID,
            writer_key=writer_key('apart'),
            placement=APART,
        )
        _ = nas_claim('library', 'library', 'Photos/Library', namespace='photos', read_only=False)
    return monitor


def in_namespace(monitor: Recorder, typ: str, namespace: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == namespace]


def names(monitor: Recorder, typ: str, namespace: str) -> list[str]:
    return sorted(it['metadata']['name'] for it in in_namespace(monitor, typ, namespace))


def movers(monitor: Recorder, namespace: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every mover the run declared, in one namespace or in all, by its resource's name, with its mover's spec."""
    for typ in (SOURCE, DESTINATION):
        for it in monitor.of_type(typ):
            if namespace is not None and it.inputs['metadata']['namespace'] != namespace:
                continue
            for kind in ('restic', 'rsyncTLS'):
                if kind in it.inputs['spec']:
                    yield it.name, it.inputs['spec'][kind]


def egress(monitor: Recorder, namespace: str) -> list[dict[str, Any]]:
    (policy,) = in_namespace(monitor, POLICY, namespace)
    return policy['spec']['egress']


# -- off ----------------------------------------------------------------------


def test_off_emits_the_claim_alone(declared: Recorder) -> None:
    """No source, no repository Secret, no password and no allow: nothing a mover would need."""
    assert names(declared, CLAIM, 'models') == ['models-g0']
    for typ in (SOURCE, DESTINATION, SEALED_SECRET, POLICY):
        assert in_namespace(declared, typ, 'models') == [], typ
    assert not [it for it in declared.of_type(PASSWORD) if it.name.startswith('models')]


@pytest.mark.parametrize('reason', ['', '   '])
def test_off_refuses_an_empty_reason(reason: str) -> None:
    with pytest.raises(ValueError, match='reason'):
        _ = off(reason)


# -- a backed claim -----------------------------------------------------------


def test_a_backed_claims_source_schedules_by_its_class_and_keeps_its_counts(declared: Recorder) -> None:
    (source,) = [it for it in in_namespace(declared, SOURCE, 'notes') if it['spec']['sourcePVC'] == 'data-g0']
    restic = source['spec']['restic']

    assert source['spec']['trigger'] == {'schedule': WEEKDAY.schedule}
    assert restic['retain'] == {'daily': WEEKDAY.daily, 'monthly': WEEKDAY.monthly}
    assert source['metadata']['labels'][conventions.RETENTION_LABEL] == WEEKDAY.name


def test_a_backed_claims_source_writes_to_the_repository_of_its_namespace_and_name(declared: Recorder) -> None:
    """The source names the Secret of the claim's base name, whose repository is the bucket layout's path for it."""
    sources = {it['spec']['sourcePVC']: it for it in in_namespace(declared, SOURCE, 'notes')}
    secrets = {it['metadata']['name']: it for it in in_namespace(declared, SEALED_SECRET, 'notes')}

    for base in ('data', 'index'):
        repository = secrets[sources[f'{base}-g0']['spec']['restic']['repository']]
        assert repository['metadata']['name'] == base
        endpoint = s3_endpoint(conventions.B2_ACCOUNT.region)
        assert repository['spec']['template']['data']['RESTIC_REPOSITORY'] == (
            f's3:{endpoint}/{conventions.BUCKET_BACKUP}/{conventions.volsync_repo_path("notes", base)}'
        )


def test_the_repository_secret_carries_the_namespaces_writer_key_and_a_password_generated_in_state(
    declared: Recorder,
) -> None:
    """Every claim's Secret in a namespace carries that namespace's one ciphertext, which opens under any name in it."""
    secrets = in_namespace(declared, SEALED_SECRET, 'notes')

    assert sorted(it['metadata']['name'] for it in secrets) == ['data', 'index']
    for secret in secrets:
        assert secret['spec']['encryptedData'] == writer_key('notes')
        assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}
        assert secret['spec']['template']['data']['RESTIC_PASSWORD'] == GENERATED
    assert {it.name for it in declared.of_type(PASSWORD)} >= {'notes-restic-password', 'notes-index-restic-password'}


def test_a_claim_of_less_than_a_gib_is_refused() -> None:
    with pytest.raises(ValueError, match='1 GiB'):
        _ = claim(
            'empty', 'data', 0, WEEKDAY, namespace='empty', uid=UID, writer_key=writer_key('empty'), placement=WORKER
        )


def test_a_backed_claims_password_is_protected_and_template_safe(declared: Recorder) -> None:
    """The repository's only key, so a replace is refused; and with no special character, nothing the template reads."""
    assert declared.options_of('notes-restic-password', PASSWORD).protect is True
    assert declared.inputs_of('notes-restic-password', PASSWORD)['special'] is False


def test_a_protected_claim_is_registered_protected_and_so_is_the_claim_it_copies(declared: Recorder) -> None:
    assert declared.options_of('copying-g1', CLAIM).protect is True
    assert declared.options_of('copying-g0', CLAIM).protect is True
    assert not declared.options_of('moved-g1', CLAIM).protect


def test_a_writer_key_of_other_fields_is_refused() -> None:
    with pytest.raises(ValueError, match='AWS_SECRET_ACCESS_KEY'):
        _ = claim(
            'bad',
            'data',
            1,
            WEEKDAY,
            namespace='bad',
            uid=UID,
            writer_key={'AWS_ACCESS_KEY_ID': 'only-half'},
            placement=WORKER,
        )


def schedulable(spec: Mapping[str, Any]) -> set[str]:
    """The nodes a pod of `spec` may be scheduled to, by its affinity, as the scheduler reads it.

    Kubernetes' rule for the required part: at least one `nodeSelectorTerm` has
    every requirement hold, a field requirement read against the node's
    `metadata.name`, and a term with no requirement matches no node; a pod with
    no required node affinity may go to any node.
    """
    required = spec.get('affinity', {}).get('nodeAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution')
    if required is None:
        return set(NODES)
    return {node for node in NODES if any(admits(term, node) for term in required['nodeSelectorTerms'])}


def holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether one node selector requirement holds of `labels`."""
    key, operator, values = expression['key'], expression['operator'], expression.get('values', [])
    match operator:
        case 'In':
            return labels.get(key) in values
        case 'NotIn':
            return labels.get(key) not in values
        case 'Exists':
            return key in labels
        case 'DoesNotExist':
            return key not in labels
        case _:
            raise AssertionError(f'no reading of {operator!r} here')


def admits(term: Mapping[str, Any], node: str) -> bool:
    """Whether a node selector term matches `node`: its label requirements and its field requirements all hold."""
    expressions, fields = term.get('matchExpressions', []), term.get('matchFields', [])
    if not expressions and not fields:
        return False
    return all(holds(it, NODES[node]) for it in expressions) and all(
        holds(it, {'metadata.name': node}) for it in fields
    )


def preferred(spec: Mapping[str, Any]) -> set[str]:
    """The nodes the scheduler picks among those `spec` may go to, by its preferred node affinity alone.

    Each preferred term a node matches adds the term's weight to the node's
    score, and the nodes of the highest score are the ones it picks.
    """
    terms = spec.get('affinity', {}).get('nodeAffinity', {}).get('preferredDuringSchedulingIgnoredDuringExecution', [])
    scores = {node: sum(it['weight'] for it in terms if admits(it['preference'], node)) for node in schedulable(spec)}
    best = max(scores.values(), default=0)
    return {node for node, score in scores.items() if score == best}


def test_a_restic_mover_runs_only_where_its_claims_workload_can(declared: Recorder) -> None:
    """Whichever of the two reaches a claim that is not yet bound binds it on a node the other can run on.

    A `local-path` claim binds where its first consumer is scheduled, and
    VolSync gives a mover of a claim no running pod uses no node selection of
    its own, so the affinity the source states is all that holds its mover.
    """
    pods = {it.inputs['metadata']['namespace']: it.inputs['spec'] for it in declared.of_type('kubernetes:core/v1:Pod')}
    restic = [(it.name, it.inputs) for it in declared.of_type(SOURCE) if 'restic' in it.inputs['spec']]

    assert {source['metadata']['namespace'] for _, source in restic} == set(PLACEMENTS) - {'models', 'cache'}
    for name, source in restic:
        mover = {'affinity': source['spec']['restic'].get('moverAffinity', {})}
        pod = pods[source['metadata']['namespace']]
        assert schedulable(mover) == schedulable(pod), name
        assert preferred(mover) == preferred(pod), name
    # The pools are what the placements pin to: each a proper part of the cluster.
    assert schedulable(pods['notes']) == {conventions.CLOUD_NODES[0]}
    assert schedulable(pods['moved']) == set(conventions.CLOUD_NODES)
    assert schedulable(pods['copying']) == {conventions.HOMELAB_NODE}
    assert schedulable(pods['anywhere']) == set(NODES)
    # Two terms admit what each admits, and the preference weighs the worker above the cloud node.
    assert schedulable(pods['two']) == {conventions.CLOUD_NODES[0], conventions.HOMELAB_NODE}
    assert preferred(pods['two']) == {conventions.HOMELAB_NODE}
    # Preferences alone weigh every node, the heavier term's above the lighter's.
    assert preferred(pods['prefers']) == {conventions.CLOUD_NODES[1]}


@pytest.mark.parametrize(
    'placement',
    [
        k8s.core.v1.AffinityArgs(
            node_affinity=CLOUD.node_affinity,
            pod_anti_affinity=k8s.core.v1.PodAntiAffinityArgs(
                required_during_scheduling_ignored_during_execution=[
                    k8s.core.v1.PodAffinityTermArgs(topology_key=HOSTNAME_LABEL)
                ]
            ),
        ),
        k8s.core.v1.AffinityArgs(
            pod_affinity=k8s.core.v1.PodAffinityArgs(
                required_during_scheduling_ignored_during_execution=[
                    k8s.core.v1.PodAffinityTermArgs(topology_key=HOSTNAME_LABEL)
                ]
            )
        ),
    ],
    ids=['anti-affinity', 'affinity'],
)
def test_a_placement_stated_against_other_pods_is_refused(placement: k8s.core.v1.AffinityArgs) -> None:
    """The mover is not those pods, so it could bind the claim where the workload could then never run."""
    with pytest.raises(ValueError, match='pod affinity'):
        _ = claim(
            'beside',
            'data',
            1,
            WEEKDAY,
            namespace='beside',
            uid=UID,
            writer_key=writer_key('beside'),
            placement=placement,
        )


@pytest.mark.asyncio
async def test_a_placement_known_only_at_apply_is_refused() -> None:
    """The mover's affinity is built from the placement's terms, which an output does not hold yet."""
    placement = k8s.core.v1.AffinityArgs(node_affinity=pulumi.Output.from_input(CLOUD.node_affinity))
    with pytest.raises(ValueError, match='as a value'):
        _ = claim(
            'later', 'data', 1, WEEKDAY, namespace='later', uid=UID, writer_key=writer_key('later'), placement=placement
        )


def test_a_claim_with_no_mover_takes_a_placement_stated_against_other_pods(declared: Recorder) -> None:
    """The refusal is the mover's, and a claim nothing backs up has none: its claim is declared alone."""
    assert names(declared, CLAIM, 'apart') == ['data-g0']
    assert in_namespace(declared, SOURCE, 'apart') == []


# -- a move -------------------------------------------------------------------


def test_a_copy_adds_the_target_claim_and_an_rsync_tls_pair_started_by_hand(declared: Recorder) -> None:
    """The claim before stays, the new one is added, and the pair copies the one into the other, held to the target."""
    assert names(declared, CLAIM, 'copying') == ['data-g0', 'data-g1']
    (source,) = [it for it in in_namespace(declared, SOURCE, 'copying') if 'rsyncTLS' in it['spec']]
    (destination,) = in_namespace(declared, DESTINATION, 'copying')

    assert source['spec']['sourcePVC'] == 'data-g0'
    assert destination['spec']['rsyncTLS']['destinationPVC'] == 'data-g1'
    trigger = source['spec']['trigger']
    assert set(trigger) == {'manual'}
    assert destination['spec']['trigger'] == trigger
    # The apply waits until VolSync reports the manual sync done.
    assert source['metadata']['annotations']['pulumi.com/waitFor'] == (
        f'jsonpath={{.status.lastManualSync}}={trigger["manual"]}'
    )
    assert int(source['metadata']['annotations']['pulumi.com/timeoutSeconds']) > 0
    (term,) = destination['spec']['rsyncTLS']['moverAffinity']['nodeAffinity'][
        'requiredDuringSchedulingIgnoredDuringExecution'
    ]['nodeSelectorTerms']
    assert term['matchExpressions'] == [
        {'key': key, 'operator': 'In', 'values': [value]} for key, value in TARGET.items()
    ]
    assert source['metadata']['name'] == destination['metadata']['name']
    assert source['spec']['rsyncTLS']['address'].startswith(f'volsync-rsync-tls-dst-{destination["metadata"]["name"]}.')


def test_a_copy_is_ordered_after_a_workload_it_is_handed() -> None:
    with pytest.raises(ValueError, match='scale-down'):
        _ = claim(
            'early',
            'data',
            1,
            WEEKDAY,
            namespace='early',
            uid=UID,
            writer_key=writer_key('early'),
            placement=WORKER,
            generation=1,
            moving_to=TARGET,
        )


def test_a_copy_names_where_it_moves_to() -> None:
    stopped = cast('pulumi.Resource', object())
    with pytest.raises(ValueError, match='node labels'):
        _ = claim(
            'nowhere',
            'data',
            1,
            WEEKDAY,
            namespace='nowhere',
            uid=UID,
            writer_key=writer_key('nowhere'),
            placement=WORKER,
            generation=1,
            moving_to={},
            stopped=stopped,
        )


def test_a_copy_of_a_name_too_long_for_the_destinations_service_is_refused() -> None:
    """VolSync hashes a Service name past 63 characters, and the source's address would then name no Service."""
    stopped = cast('pulumi.Resource', object())
    with pytest.raises(ValueError, match='63'):
        _ = claim(
            'long',
            'x' * 40,
            1,
            WEEKDAY,
            namespace='long',
            uid=UID,
            writer_key=writer_key('long'),
            placement=WORKER,
            generation=1,
            moving_to=TARGET,
            stopped=stopped,
        )


def test_a_copy_raises_the_generation() -> None:
    with pytest.raises(ValueError, match='generation'):
        _ = claim(
            'flat',
            'data',
            1,
            WEEKDAY,
            namespace='flat',
            uid=UID,
            writer_key=writer_key('flat'),
            placement=WORKER,
            moving_to=TARGET,
        )


def test_a_copy_leaves_the_claim_it_copies_as_it_was_made(declared: Recorder) -> None:
    """The copy's call states the old claim's spec but never changes it, so a class or size changed in it cannot replace it.

    A changed class is force-new on a claim, and a replace of an explicitly
    named one deletes it first, which empties it.
    """
    copied = declared.options_of('copying-g0', CLAIM)
    target = declared.options_of('copying-g1', CLAIM)

    assert list(copied.ignoreChanges) == ['spec']
    assert list(target.ignoreChanges) == []


def test_a_copy_goes_through_no_bucket(declared: Recorder) -> None:
    """The claim being filled has no restic source, and the one that copies uses no repository."""
    sources = in_namespace(declared, SOURCE, 'copying')
    restic = [it for it in sources if 'restic' in it['spec']]

    assert [it['spec']['sourcePVC'] for it in restic] == ['data-g0']
    assert in_namespace(declared, DESTINATION, 'cache')[0]['spec'].keys() == {'trigger', 'rsyncTLS'}
    assert [it for it in in_namespace(declared, SOURCE, 'cache') if 'restic' in it['spec']] == []


def test_the_copy_is_ordered_after_the_workloads_scale_down(declared: Recorder) -> None:
    (source,) = [it for it in declared.of_type(SOURCE) if it.name == 'copying-move-g1']
    assert any(dependency.endswith('::workload') for dependency in declared.depends_on(source.name, SOURCE))


def test_after_the_finish_the_claim_and_its_source_are_the_new_generations_and_the_repository_is_the_same(
    declared: Recorder,
) -> None:
    """The finish is the new generation without `moving_to`: its claim alone, its source, the repository of the base name."""
    assert names(declared, CLAIM, 'moved') == ['data-g1']
    (source,) = in_namespace(declared, SOURCE, 'moved')
    (secret,) = in_namespace(declared, SEALED_SECRET, 'moved')

    assert source['metadata']['name'] == 'data-g1'
    assert source['spec']['sourcePVC'] == 'data-g1'
    assert source['spec']['restic']['repository'] == secret['metadata']['name'] == 'data'
    assert secret['spec']['template']['data']['RESTIC_REPOSITORY'].endswith(
        f'/{conventions.volsync_repo_path("moved", "data")}'
    )
    assert in_namespace(declared, DESTINATION, 'moved') == []


# -- every mover --------------------------------------------------------------


def test_every_mover_states_its_security_context_and_resources_and_copies_directly(declared: Recorder) -> None:
    """`restricted` refuses a mover without its context, and the context is the workload's user."""
    found = list(movers(declared))
    # The backed claims' sources, both copies' pairs, and the source the copy
    # of a backed claim keeps.
    assert sorted(name for name, _ in found) == sorted(
        [
            'notes-restic-g0',
            'notes-index-restic-g0',
            'anywhere-restic-g0',
            'two-restic-g0',
            'prefers-restic-g0',
            'moved-restic-g1',
            'copying-restic-g0',
            'copying-move-g1',
            'copying-move-g1',
            'cache-move-g2',
            'cache-move-g2',
        ]
    )
    for name, spec in found:
        context = spec['moverSecurityContext']
        assert context['runAsUser'] == context['runAsGroup'] == context['fsGroup'] == UID, name
        assert context['runAsNonRoot'] is True, name
        assert context['seccompProfile'] == {'type': 'RuntimeDefault'}, name
        assert spec['moverResources']['limits']['cpu'], name
        assert spec['moverResources']['requests']['memory'], name
        assert spec['copyMethod'] == 'Direct', name


def test_every_mover_carries_the_label_its_allow_selects(declared: Recorder) -> None:
    for namespace in ('moved', 'copying', 'cache'):
        (policy,) = in_namespace(declared, POLICY, namespace)
        selected = policy['spec']['endpointSelector']['matchLabels']
        found = list(movers(declared, namespace))
        assert found
        for name, spec in found:
            assert selected.items() <= spec['moverPodLabels'].items(), name


def kind(rule: dict[str, Any]) -> str:
    """What one egress rule of a movers' policy admits, or `other`.

    The namespace's own pods, with no port named; the cluster's DNS on 53
    through Cilium's DNS proxy, which is how a `toFQDNs` rule learns its
    addresses; or one host by name on 443.
    """
    if rule == {'toEndpoints': [{}]}:
        return 'namespace'
    if rule.get('toEndpoints') == [
        {'matchLabels': {'io.kubernetes.pod.namespace': 'kube-system', 'k8s-app': 'kube-dns'}}
    ]:
        (ports,) = rule['toPorts']
        if {(it['port'], it['protocol']) for it in ports['ports']} == {('53', 'UDP'), ('53', 'TCP')} and ports.get(
            'rules'
        ) == {'dns': [{'matchPattern': '*'}]}:
            return 'dns'
    if rule.keys() == {'toFQDNs', 'toPorts'} and rule['toPorts'] == [{'ports': [{'port': '443', 'protocol': 'TCP'}]}]:
        return 'bucket'
    return 'other'


@pytest.mark.parametrize(
    ('namespace', 'kinds'),
    [
        ('moved', ['bucket', 'dns', 'namespace']),
        ('copying', ['bucket', 'dns', 'namespace']),
        ('cache', ['dns', 'namespace']),
    ],
)
def test_a_movers_traffic_stays_inside_its_namespace_and_reaches_the_clusters_dns(
    declared: Recorder, namespace: str, kinds: list[str]
) -> None:
    """Every rule is one of the kinds a mover needs, and nothing else: its first egress rule denies the rest."""
    assert sorted(kind(rule) for rule in egress(declared, namespace)) == kinds


def test_a_restic_mover_reaches_the_bucket_and_a_copy_alone_does_not(declared: Recorder) -> None:
    """The bucket's host is the endpoint the repository names; a claim with no class reaches no bucket."""
    (secret,) = in_namespace(declared, SEALED_SECRET, 'moved')
    repository: str = secret['spec']['template']['data']['RESTIC_REPOSITORY']
    bucket = urlsplit(repository.removeprefix('s3:')).hostname

    (rule,) = [rule for rule in egress(declared, 'moved') if 'toFQDNs' in rule]
    assert rule['toFQDNs'] == [{'matchName': bucket}]
    assert rule['toPorts'] == [{'ports': [{'port': '443', 'protocol': 'TCP'}]}]
    assert [rule for rule in egress(declared, 'cache') if 'toFQDNs' in rule] == []


# -- the NAS ------------------------------------------------------------------


def test_a_nas_claim_is_scheduled_to_the_worker_alone(declared: Recorder) -> None:
    """The volume's node affinity is the worker's, which every pod that mounts it is placed by."""
    (volume,) = [it.inputs for it in declared.of_type(VOLUME)]

    assert volume['spec']['nodeAffinity'] == {
        'required': WORKER_AFFINITY_JSON['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
    }


def test_a_nas_claim_is_its_volume_under_the_export_bound_to_it_alone(declared: Recorder) -> None:
    (volume,) = [it.inputs for it in declared.of_type(VOLUME)]
    (bound,) = in_namespace(declared, CLAIM, 'photos')

    assert volume['spec']['nfs'] == {
        'server': str(conventions.HOMELAB_HOST_LEG_IPV4),
        'path': f'{conventions.NAS_EXPORT}/Photos/Library',
        'readOnly': False,
    }
    assert volume['spec']['claimRef'] == {'namespace': 'photos', 'name': 'library'}
    assert bound['spec']['volumeName'] == volume['metadata']['name']
    assert volume['spec']['persistentVolumeReclaimPolicy'] == 'Retain'
    assert bound['spec']['storageClassName'] == volume['spec']['storageClassName']


@pytest.mark.parametrize('path', ['/Photos', 'Photos/../Downloads', '..'])
def test_a_nas_path_stays_under_the_export(path: str) -> None:
    with pytest.raises(ValueError, match='export'):
        _ = nas_claim('escape', 'escape', path, namespace='escape', read_only=True)
