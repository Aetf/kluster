"""The application base's storage, declared against mocks: `claim`, `off`, a move, and `nas_claim`.

Every claim here is the test's own, each in a namespace of its own, so what is
held is how a claim's arguments decide what is emitted. The retention class is
the test's own too, with a schedule and counts no class of `conventions`
carries, so a source scheduled from anything but the class it is handed reads
differently. The writer keys are the test's own ciphertext, one per namespace.

Each namespace's workload is a pod of the test's own, under constraints of the
test's own that hold it to some of the cluster's nodes, or to none, and that
state what its claim records for it, so that where a mover may run is read
against where the workload may, both as the scheduler reads them against the
nodes as they are labelled (`node_selection`).

Literals written here are the upstream projects' contracts: VolSync's fields
and the key its manual trigger is reported under, restic's environment, the
Pulumi Kubernetes provider's await annotations, and Cilium's policy fields.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import pulumi
import pulumi_kubernetes as k8s
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from node_selection import CLOUD, NODES, schedulable, schedulable_among, selected
from pulumi import runtime

from kluster import conventions
from kluster.components.apps import storage
from kluster.components.apps.storage import Off, claim, nas_claim, off
from kluster.components.backup import s3_endpoint
from kluster.conventions.backup import RetentionClass
from kluster.lib.k8s import (
    UNCONSTRAINED,
    BackedVolumeBesideNodeVolume,
    BackedVolumeOffBinder,
    Constraints,
    Keeper,
    NodeRequirement,
    TwoBackedVolumes,
    at_home,
    kept,
    on_architecture,
)

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

#: A node volume's class, the first the table holds.
NODE_VOLUME_CLASS = conventions.node_volume_class(next(iter(conventions.NODE_VOLUMES)))

#: The constraints of a workload the home site holds.
HOME = at_home('mounts something only the home site reaches')

#: The constraints of a workload held to the cloud's nodes by their architecture.
ARM64 = on_architecture('arm64', 'runs an image built for arm64 alone')

#: The constraints each namespace's workload states, before its claims' holds.
CONSTRAINTS: dict[str, Constraints] = {
    'notes': ARM64,
    'models': HOME,
    'copying': HOME,
    'moved': ARM64,
    'cache': HOME,
    'anywhere': UNCONSTRAINED,
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
        # Two backed claims in one namespace, of two workloads, each handed
        # its own workload's constraints.
        notes = claim(
            'notes',
            'data',
            2,
            WEEKDAY,
            namespace='notes',
            uid=UID,
            writer_key=writer_key('notes'),
            constraints=CONSTRAINTS['notes'],
        )
        _ = claim(
            'notes-index',
            'index',
            1,
            WEEKDAY,
            namespace='notes',
            uid=UID,
            writer_key=writer_key('notes'),
            constraints=CONSTRAINTS['notes'],
        )
        models = claim(
            'models',
            'models',
            11,
            off('re-derived on start'),
            namespace='models',
            uid=UID,
            writer_key=writer_key('models'),
            constraints=CONSTRAINTS['models'],
            storage_class=NODE_VOLUME_CLASS,
        )
        # The copy of a backed claim, and the finish of the same move.
        copying = claim(
            'copying',
            'data',
            3,
            WEEKDAY,
            namespace='copying',
            uid=UID,
            writer_key=writer_key('copying'),
            constraints=CONSTRAINTS['copying'],
            generation=1,
            moving_to=TARGET,
            stopped=workload,
            protect=True,
        )
        moved = claim(
            'moved',
            'data',
            3,
            WEEKDAY,
            namespace='moved',
            uid=UID,
            writer_key=writer_key('moved'),
            constraints=CONSTRAINTS['moved'],
            generation=1,
        )
        # The copy of a claim that nothing backs up.
        cache = claim(
            'cache',
            'cache',
            50,
            off('its network is its redundancy'),
            namespace='cache',
            uid=UID,
            writer_key=writer_key('cache'),
            constraints=CONSTRAINTS['cache'],
            generation=2,
            moving_to=TARGET,
            stopped=workload,
        )
        # A backed claim whose workload states no constraint.
        anywhere = claim(
            'anywhere',
            'data',
            1,
            WEEKDAY,
            namespace='anywhere',
            uid=UID,
            writer_key=writer_key('anywhere'),
            constraints=CONSTRAINTS['anywhere'],
        )
        # Each workload states what its claims record.
        mounted = {
            'notes': (notes,),
            'models': (models,),
            'copying': (copying,),
            'moved': (moved,),
            'cache': (cache,),
            'anywhere': (anywhere,),
        }
        for namespace, claims in mounted.items():
            constraints = UNCONSTRAINED
            annotations: dict[str, str] = {}
            for it in claims:
                constraints &= it.constraints
                annotations |= it.pod_annotations
            _ = k8s.core.v1.Pod(
                f'{namespace}-workload',
                metadata=k8s.meta.v1.ObjectMetaArgs(name='workload', namespace=namespace, annotations=annotations),
                spec=k8s.core.v1.PodSpecArgs(
                    **constraints.pod_fields(),
                    containers=[k8s.core.v1.ContainerArgs(name='app', image='example.com/app')],
                ),
            )
        library = nas_claim('library', 'library', 'Photos/Library', namespace='photos', read_only=False)
        _ = k8s.core.v1.Pod(
            'photos-workload',
            metadata=k8s.meta.v1.ObjectMetaArgs(name='workload', namespace='photos'),
            spec=k8s.core.v1.PodSpecArgs(
                **library.constraints.pod_fields(),
                containers=[k8s.core.v1.ContainerArgs(name='app', image='example.com/app')],
            ),
        )
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
            'empty', 'data', 0, WEEKDAY, namespace='empty', uid=UID, writer_key=writer_key('empty'), constraints=HOME
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
            constraints=HOME,
        )


def pods(monitor: Recorder) -> dict[str, dict[str, Any]]:
    """Each namespace's workload, by its namespace."""
    return {it.inputs['metadata']['namespace']: it.inputs for it in monitor.of_type('kubernetes:core/v1:Pod')}


def test_a_restic_mover_runs_exactly_where_its_claims_workload_can(declared: Recorder) -> None:
    """Whichever of the two reaches a claim that is not yet bound binds it on a node the other can run on.

    A `local-path` claim binds where its first consumer is scheduled, and
    VolSync gives a mover of a claim no running pod uses no node selection of
    its own, so the affinity the source states is all that holds its mover.
    """
    workloads = pods(declared)
    restic = [(it.name, it.inputs) for it in declared.of_type(SOURCE) if 'restic' in it.inputs['spec']]

    assert {source['metadata']['namespace'] for _, source in restic} == {'notes', 'copying', 'moved', 'anywhere'}
    for name, source in restic:
        mover = {'affinity': source['spec']['restic'].get('moverAffinity')}
        pod = workloads[source['metadata']['namespace']]['spec']
        assert schedulable(mover) == schedulable(pod), name
    # The constraints are each a proper part of the cluster, or none of it.
    assert schedulable(workloads['moved']['spec']) == CLOUD
    assert schedulable(workloads['copying']['spec']) == set(NODES) - CLOUD
    assert schedulable(workloads['anywhere']['spec']) == set(NODES)


def test_an_unconstrained_workloads_mover_states_no_affinity(declared: Recorder) -> None:
    (source,) = [it for it in in_namespace(declared, SOURCE, 'anywhere') if 'restic' in it['spec']]

    assert 'moverAffinity' not in source['spec']['restic']


@pytest.mark.parametrize('namespace', ['notes', 'models', 'copying', 'moved', 'cache', 'anywhere'])
def test_a_claims_pod_is_held_where_its_claim_keeps_it_with_no_affinity_of_its_own(
    declared: Recorder, namespace: str
) -> None:
    """A bound claim keeps its pod to its node, which the scheduler reads from the volume: the pod outranks free ones.

    The claim states no affinity for it, so the pod admits what its
    workload's constraints admit and no fewer nodes.
    """
    spec = pods(declared)[namespace]['spec']

    assert spec['priorityClassName'] == conventions.HELD_PRIORITY_CLASS
    assert schedulable(spec) == schedulable(CONSTRAINTS[namespace].pod_fields_json())


@pytest.mark.parametrize('namespace', ['notes', 'copying', 'moved', 'cache', 'anywhere'])
def test_a_local_path_claims_pod_writes_in_the_class_that_caps_the_disk_etcd_shares_and_its_movers_in_none(
    declared: Recorder, namespace: str
) -> None:
    """The class caps what the pod writes where its node is a control plane; VolSync gives a mover labels alone."""
    annotations = pods(declared)[namespace]['metadata']['annotations']

    assert annotations[conventions.BLOCKIO_POD_ANNOTATION] == conventions.ETCD_DISK_WRITER.name
    for name, mover in movers(declared, namespace):
        assert conventions.ETCD_DISK_WRITER.name not in str(mover), name


def test_a_node_volumes_pod_writes_in_no_class(declared: Recorder) -> None:
    """A node volume is a disk of its own, apart from the one etcd shares."""
    assert conventions.BLOCKIO_POD_ANNOTATION not in pods(declared)['models']['metadata'].get('annotations', {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('first_class', 'first_retention', 'refusal'),
    [
        (conventions.SC_LOCAL_PATH, WEEKDAY, TwoBackedVolumes),
        (NODE_VOLUME_CLASS, off('a cache'), BackedVolumeBesideNodeVolume),
    ],
    ids=['beside a backed claim', 'beside a node volume'],
)
async def test_a_backed_claim_beside_a_claim_its_mover_could_bind_it_away_from_is_refused(
    first_class: str, first_retention: RetentionClass | Off, refusal: type[ValueError]
) -> None:
    """The mover binds the backed claim apart from the first claim, so the two could bind on two nodes, and the pod run on neither."""
    _ = await run_with(Recorder(), stack='apps')
    async with declaring():
        first = claim(
            'pair',
            'one',
            1,
            first_retention,
            namespace='pair',
            uid=UID,
            writer_key=writer_key('pair'),
            constraints=UNCONSTRAINED,
            storage_class=first_class,
        )
        with pytest.raises(refusal):
            _ = claim(
                'pair-two',
                'two',
                1,
                WEEKDAY,
                namespace='pair',
                uid=UID,
                writer_key=writer_key('pair'),
                constraints=first.constraints,
            )


#: A device of the test's own, which the nodes that offer it carry a label of
#: the test's own for.
DEVICE_LABEL = NodeRequirement('example.com/device', 'present')
DEVICE = kept(Keeper.DEVICE, 'requests a device of the test', offered_on=DEVICE_LABEL)

#: Each of the cluster's nodes twice, by its name and whether it offers the
#: device: without its label and with it.
WITH_AND_WITHOUT_DEVICE: dict[tuple[str, bool], dict[str, str]] = {
    (node, offers): {**labels, **({DEVICE_LABEL.key: DEVICE_LABEL.value} if offers else {})}
    for node, labels in NODES.items()
    for offers in (False, True)
}


async def _backed(
    constraints: Constraints, retention: RetentionClass | Off = WEEKDAY
) -> tuple[storage.Claim, Recorder]:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        made = claim(
            'device',
            'data',
            1,
            retention,
            namespace='device',
            uid=UID,
            writer_key=writer_key('device'),
            constraints=constraints,
        )
    return made, monitor


@pytest.mark.asyncio
async def test_the_mover_of_a_claim_handed_a_device_is_held_to_the_nodes_that_offer_it() -> None:
    """The mover requests no device, so the affinity it states is what keeps it off a node the workload could not run on.

    Read on every node with the device and without it, the mover admits the
    nodes the workload's other constraints do, each where it offers the device.
    """
    made, monitor = await _backed(HOME & DEVICE)
    (source,) = [it.inputs for it in monitor.of_type(SOURCE) if 'restic' in it.inputs['spec']]
    mover = {'affinity': source['spec']['restic'].get('moverAffinity')}

    assert schedulable_among(mover, WITH_AND_WITHOUT_DEVICE) == {
        (node, True) for node in schedulable(HOME.pod_fields_json())
    }
    assert DEVICE.kept[0] in made.constraints.kept


@pytest.mark.asyncio
async def test_a_device_composed_onto_a_backed_claims_constraints_after_it_was_made_is_refused() -> None:
    """Its mover was not held to the nodes that offer the device, so it may bind the claim on a node without it."""
    made, _ = await _backed(HOME)

    with pytest.raises(BackedVolumeOffBinder) as refused:
        _ = made.constraints & DEVICE

    assert 'requests a device of the test' in str(refused.value)
    assert "its claim 'data-g0'" in str(refused.value)


@pytest.mark.asyncio
async def test_a_claim_nothing_backs_up_takes_a_device_after_it_was_made() -> None:
    """It has no mover, so it binds where its pod is placed, which is a node that offers the device."""
    made, _ = await _backed(HOME, off('a cache, fetched again'))

    assert DEVICE.kept[0] in (made.constraints & DEVICE).kept


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
            constraints=HOME,
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
            constraints=HOME,
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
            constraints=HOME,
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
            constraints=HOME,
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


def test_a_nas_claims_pod_admits_exactly_the_nodes_its_volume_does_and_those_are_at_home(declared: Recorder) -> None:
    """The export is reachable from home alone; the pod states what the volume's node affinity forces, and is held there."""
    (volume,) = [it.inputs for it in declared.of_type(VOLUME)]
    at_volume = selected(volume['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0], NODES)
    spec = pods(declared)['photos']['spec']

    assert at_volume == set(NODES) - CLOUD
    assert schedulable(spec) == at_volume
    assert spec['priorityClassName'] == conventions.HELD_PRIORITY_CLASS


def test_a_nas_claim_is_its_volume_under_the_export_bound_to_it_alone(declared: Recorder) -> None:
    (volume,) = [it.inputs for it in declared.of_type(VOLUME)]
    (bound,) = in_namespace(declared, CLAIM, 'photos')

    assert volume['spec']['nfs'] == {
        'server': str(conventions.HOMELAB_HOST_LEG_IPV4),
        'path': f'{conventions.NAS_EXPORT}/Photos/Library',
        'readOnly': False,
    }
    assert volume['spec']['claimRef'] == {'namespace': 'photos', 'name': bound['metadata']['name']}
    assert bound['spec']['volumeName'] == volume['metadata']['name']
    assert volume['spec']['persistentVolumeReclaimPolicy'] == 'Retain'
    assert bound['spec']['storageClassName'] == volume['spec']['storageClassName']


@dataclass(frozen=True)
class Rendered:
    """One NAS claim's volume and claim, as one run declared them."""

    volume: dict[str, Any]
    claim: dict[str, Any]

    @property
    def fixed(self) -> tuple[str, str]:
        """What the Kubernetes API refuses to change on each object once it exists.

        A volume's source, node affinity and volume mode
        (`ValidatePersistentVolumeUpdate`), and a claim's spec, whose request
        may only grow and which no class here grows
        (`ValidatePersistentVolumeClaimUpdate`; both in
        `pkg/apis/core/validation/validation.go`, Kubernetes v1.36.3). The
        claim's `volumeName` is left out: it is the volume's name.
        """
        volume = {key: self.volume['spec'].get(key) for key in ('nfs', 'nodeAffinity', 'volumeMode')}
        claim = {key: value for key, value in self.claim['spec'].items() if key != 'volumeName'}
        return json.dumps(volume, sort_keys=True), json.dumps(claim, sort_keys=True)


async def rendered(path: str = 'Photos/Library', *, read_only: bool = False) -> Rendered:
    """`nas_claim('library', ...)` declared alone in a run of its own."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = nas_claim('library', 'library', path, namespace='photos', read_only=read_only)
    (volume,) = [it.inputs for it in monitor.of_type(VOLUME)]
    (bound,) = in_namespace(monitor, CLAIM, 'photos')
    return Rendered(volume=volume, claim=bound)


#: The home site's selector replaced by another site's, typed and as JSON from one requirement.
_ELSEWHERE_KEY, _ELSEWHERE = 'example.com/site', 'elsewhere'
ELSEWHERE_NODE_SELECTOR = k8s.core.v1.NodeSelectorArgs(
    node_selector_terms=[
        k8s.core.v1.NodeSelectorTermArgs(
            match_expressions=[
                k8s.core.v1.NodeSelectorRequirementArgs(key=_ELSEWHERE_KEY, operator='In', values=[_ELSEWHERE])
            ]
        )
    ]
)
ELSEWHERE_NODE_SELECTOR_JSON = {
    'nodeSelectorTerms': [{'matchExpressions': [{'key': _ELSEWHERE_KEY, 'operator': 'In', 'values': [_ELSEWHERE]}]}]
}


@pytest.mark.asyncio
async def test_a_nas_claim_and_its_volume_are_named_by_what_neither_can_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A change to any field the API server holds fixed is a volume and claim of new names; no other change is.

    So the change is a pair created beside the old one rather than an update
    the server refuses, and the same declaration rendered again keeps its
    names. Each variant changes one declared value: the path, the access, the
    home site's selector, the class, the size.
    """
    renders = [await rendered(), await rendered('Photos/Other'), await rendered(read_only=True), await rendered()]
    with monkeypatch.context() as patched:
        patched.setattr(storage, 'HOME_NODE_SELECTOR', ELSEWHERE_NODE_SELECTOR)
        patched.setattr(storage, 'HOME_NODE_SELECTOR_JSON', ELSEWHERE_NODE_SELECTOR_JSON)
        renders.append(await rendered())
    with monkeypatch.context() as patched:
        patched.setattr(conventions, 'SC_NAS', 'nas-elsewhere')
        renders.append(await rendered())
    with monkeypatch.context() as patched:
        patched.setattr(storage, '_NAS_NOMINAL_SIZE', '2Gi')
        renders.append(await rendered())

    assert len({it.fixed for it in renders}) == len(renders) - 1
    for one in renders:
        for other in renders:
            same = one.fixed == other.fixed
            assert (one.volume['metadata']['name'] == other.volume['metadata']['name']) == same
            assert (one.claim['metadata']['name'] == other.claim['metadata']['name']) == same


@pytest.mark.asyncio
async def test_a_nas_claim_binds_the_volume_named_for_it_and_carries_its_base_name() -> None:
    it = await rendered()

    assert it.claim['metadata']['name'].startswith('library-')
    assert it.claim['spec']['volumeName'] == it.volume['metadata']['name']
    assert it.volume['spec']['claimRef'] == {'namespace': 'photos', 'name': it.claim['metadata']['name']}


@pytest.mark.parametrize('path', ['/Photos', 'Photos/../Downloads', '..'])
def test_a_nas_path_stays_under_the_export(path: str) -> None:
    with pytest.raises(ValueError, match='export'):
        _ = nas_claim('escape', 'escape', path, namespace='escape', read_only=True)
