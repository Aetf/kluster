"""The application base's storage, declared against mocks: `claim`, `off`, a move, and `nas_claim`.

Every claim here is the test's own, each in a namespace of its own, so what is
held is how a claim's arguments decide what is emitted. The retention class is
the test's own too, with a schedule and counts no class of `conventions`
carries, so a source scheduled from anything but the class it is handed reads
differently. The writer keys are the test's own ciphertext, one per namespace,
handed to the claims under a class and to none under `off`.

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
from kluster.components.apps.storage import Backed, Off, backed, claim, nas_claim, off
from kluster.components.backup import s3_endpoint
from kluster.conventions.backup import RetentionClass
from kluster.lib.k8s import (
    UNCONSTRAINED,
    BackedVolumeBesideNodeVolume,
    Constraints,
    Keeper,
    MovingVolumeBesideVolume,
    NodeRequirement,
    TwoBackedVolumes,
    VolumeOffBinder,
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


#: The constraints each namespace's workload states, before its claims' holds.
CONSTRAINTS: dict[str, Constraints] = {
    'notes': ARM64,
    'models': HOME,
    'copying': HOME,
    'moved': ARM64,
    'cache': HOME,
    'transcode': HOME & DEVICE,
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
            backed(WEEKDAY, writer_key('notes')),
            namespace='notes',
            uid=UID,
            constraints=CONSTRAINTS['notes'],
        )
        _ = claim(
            'notes-index',
            'index',
            1,
            backed(WEEKDAY, writer_key('notes')),
            namespace='notes',
            uid=UID,
            constraints=CONSTRAINTS['notes'],
        )
        models = claim(
            'models',
            'models',
            11,
            off('re-derived on start'),
            namespace='models',
            uid=UID,
            constraints=CONSTRAINTS['models'],
            storage_class=NODE_VOLUME_CLASS,
        )
        # The copy of a backed claim, and the finish of the same move.
        copying = claim(
            'copying',
            'data',
            3,
            backed(WEEKDAY, writer_key('copying')),
            namespace='copying',
            uid=UID,
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
            backed(WEEKDAY, writer_key('moved')),
            namespace='moved',
            uid=UID,
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
            constraints=CONSTRAINTS['cache'],
            generation=2,
            moving_to=TARGET,
            stopped=workload,
        )
        # The copy of a claim that nothing backs up, of a workload that
        # requests the device.
        transcode = claim(
            'transcode',
            'scratch',
            5,
            off('re-derived on start'),
            namespace='transcode',
            uid=UID,
            constraints=CONSTRAINTS['transcode'],
            generation=1,
            moving_to=TARGET,
            stopped=workload,
        )
        # A backed claim whose workload states no constraint.
        anywhere = claim(
            'anywhere',
            'data',
            1,
            backed(WEEKDAY, writer_key('anywhere')),
            namespace='anywhere',
            uid=UID,
            constraints=CONSTRAINTS['anywhere'],
        )
        # Each workload states what its claims record.
        mounted = {
            'notes': (notes,),
            'models': (models,),
            'copying': (copying,),
            'moved': (moved,),
            'cache': (cache,),
            'transcode': (transcode,),
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
            'empty', 'data', 0, backed(WEEKDAY, writer_key('empty')), namespace='empty', uid=UID, constraints=HOME
        )


def test_a_backed_claims_password_is_protected_and_template_safe(declared: Recorder) -> None:
    """The repository's only key, so a replace is refused; and with no special character, nothing the template reads."""
    assert declared.options_of('notes-restic-password', PASSWORD).protect is True
    assert declared.inputs_of('notes-restic-password', PASSWORD)['special'] is False


def test_a_protected_claim_is_registered_protected_and_so_is_the_claim_it_copies(declared: Recorder) -> None:
    assert declared.options_of('copying-g1', CLAIM).protect is True
    assert declared.options_of('copying-g0', CLAIM).protect is True
    assert not declared.options_of('moved-g1', CLAIM).protect


# -- the writer key -----------------------------------------------------------


def test_a_writer_key_of_other_fields_is_refused() -> None:
    with pytest.raises(ValueError, match='AWS_SECRET_ACCESS_KEY'):
        _ = backed(WEEKDAY, {'AWS_ACCESS_KEY_ID': 'only-half'})


def test_a_backed_retention_cannot_be_made_without_a_writer_key() -> None:
    """Its repository Secret carries the namespace's writer key, which its restic mover writes to the bucket with.

    The ignore is the half the type check holds: were the key optional, it
    would suppress nothing, which the type check refuses.
    """
    with pytest.raises(TypeError, match='writer_key'):
        backed(WEEKDAY)  # pyright: ignore[reportCallIssue]


def test_a_claim_takes_no_writer_key_beside_its_retention() -> None:
    """A key travels in `backed` alone, so an `off` claim, which has no writer scope, has nowhere to be handed one.

    The ignore is the half the type check holds, as above.
    """
    with pytest.raises(TypeError, match='writer_key'):
        claim(
            'keyed',
            'cache',
            1,
            off('a cache'),
            namespace='keyed',
            uid=UID,
            writer_key=writer_key('keyed'),  # pyright: ignore[reportCallIssue]
            constraints=HOME,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('moving_to', [None, TARGET], ids=['in place', 'under a move'])
async def test_an_off_claim_takes_no_writer_key_and_seals_nothing(moving_to: dict[str, str] | None) -> None:
    """No password, no repository and no SealedSecret, in place or under a move, whose copy is made all the same."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        workload = k8s.core.v1.ConfigMap('workload')
        made = claim(
            'keyless',
            'cache',
            1,
            off('a cache'),
            namespace='keyless',
            uid=UID,
            constraints=HOME,
            generation=0 if moving_to is None else 1,
            moving_to=moving_to,
            stopped=None if moving_to is None else workload,
        )

    assert (made.password, made.repository) == (None, None)
    assert monitor.of_type(SEALED_SECRET) == []
    assert (made.move is None) == (moving_to is None)


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


@pytest.mark.parametrize('namespace', ['notes', 'models', 'copying', 'moved', 'cache', 'transcode', 'anywhere'])
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


@pytest.mark.parametrize('namespace', ['notes', 'copying', 'moved', 'cache', 'transcode', 'anywhere'])
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
        (conventions.SC_LOCAL_PATH, backed(WEEKDAY, writer_key('pair')), TwoBackedVolumes),
        (NODE_VOLUME_CLASS, off('a cache'), BackedVolumeBesideNodeVolume),
    ],
    ids=['beside a backed claim', 'beside a node volume'],
)
async def test_a_backed_claim_beside_a_claim_its_mover_could_bind_it_away_from_is_refused(
    first_class: str, first_retention: Backed | Off, refusal: type[ValueError]
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
            constraints=UNCONSTRAINED,
            storage_class=first_class,
        )
        with pytest.raises(refusal):
            _ = claim(
                'pair-two',
                'two',
                1,
                backed(WEEKDAY, writer_key('pair')),
                namespace='pair',
                uid=UID,
                constraints=first.constraints,
            )


#: What `_backed` declares its claim under unless it is handed otherwise.
BACKED_DEVICE = backed(WEEKDAY, writer_key('device'))


async def _backed(constraints: Constraints, retention: Backed | Off = BACKED_DEVICE) -> tuple[storage.Claim, Recorder]:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        made = claim(
            'device',
            'data',
            1,
            retention,
            namespace='device',
            uid=UID,
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

    with pytest.raises(VolumeOffBinder) as refused:
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
    assert source['metadata']['name'] == destination['metadata']['name']
    assert source['spec']['rsyncTLS']['address'].startswith(f'volsync-rsync-tls-dst-{destination["metadata"]["name"]}.')


#: Each of the cluster's nodes four times, by its name, whether it carries the
#: label a move targets, and whether it offers the device: which nodes carry
#: either is not the cluster's to say, so a mover is read on every reading.
WITH_AND_WITHOUT_TARGET_AND_DEVICE: dict[tuple[str, bool, bool], dict[str, str]] = {
    (node, targeted, offers): {
        **labels,
        **(TARGET if targeted else {}),
        **({DEVICE_LABEL.key: DEVICE_LABEL.value} if offers else {}),
    }
    for node, labels in NODES.items()
    for targeted in (False, True)
    for offers in (False, True)
}


def test_a_copys_destination_binds_the_new_claim_only_where_its_workload_can_run_and_on_the_target(
    declared: Recorder,
) -> None:
    """The destination's mover is the new claim's first consumer, so the node it lands on is the workload's after the finish.

    It requests no device, so the nodes its workload admits are read with the
    device the workload requests, on every node with the target label and
    without it, with the device and without it. The destination admits some
    node, every node it admits is one the workload can run on, and each
    carries the target.
    """
    workloads = pods(declared)
    destinations = declared.of_type(DESTINATION)

    assert {it.inputs['metadata']['namespace'] for it in destinations} == {'copying', 'cache', 'transcode'}
    for it in destinations:
        namespace = it.inputs['metadata']['namespace']
        mover = {'affinity': it.inputs['spec']['rsyncTLS'].get('moverAffinity')}
        requests_the_device = any(held.offered_on is not None for held in CONSTRAINTS[namespace].kept)
        workload = {
            node
            for node in schedulable_among(workloads[namespace]['spec'], WITH_AND_WITHOUT_TARGET_AND_DEVICE)
            if node[2] or not requests_the_device
        }
        admitted = schedulable_among(mover, WITH_AND_WITHOUT_TARGET_AND_DEVICE)

        assert admitted, it.name
        assert admitted <= workload, (it.name, sorted(admitted - workload))
        assert all(targeted for _, targeted, _ in admitted), it.name


@pytest.mark.parametrize(
    ('constraints', 'moving_to'),
    [
        (HOME, {conventions.SITE_LABEL: str(conventions.Site.CLOUD)}),
        (HOME & DEVICE, {**TARGET, DEVICE_LABEL.key: 'absent'}),
    ],
    ids=['another site', 'nodes without the device'],
)
def test_a_move_to_a_label_value_its_workloads_binder_requirements_refuse_is_refused(
    constraints: Constraints, moving_to: dict[str, str]
) -> None:
    """The destination is held to both, and no node carries two values of one label: it would never be scheduled."""
    stopped = cast('pulumi.Resource', object())
    with pytest.raises(storage.MoveOffBinder):
        _ = claim(
            'astray',
            'data',
            1,
            off('a cache'),
            namespace='astray',
            uid=UID,
            constraints=constraints,
            generation=1,
            moving_to=moving_to,
            stopped=stopped,
        )


@pytest.mark.asyncio
async def test_a_move_restating_a_required_label_admits_what_one_to_the_target_alone_does() -> None:
    """Only a label fixed to another value is refused: one fixed to the value required narrows nothing further."""
    admitted: list[set[tuple[str, bool, bool]]] = []
    for moving_to in (TARGET, {**TARGET, conventions.SITE_LABEL: str(conventions.Site.HOME)}):
        monitor = await run_with(Recorder(), stack='apps')
        async with declaring():
            workload = k8s.core.v1.ConfigMap('workload')
            _ = claim(
                'restated',
                'data',
                1,
                off('a cache'),
                namespace='restated',
                uid=UID,
                constraints=HOME,
                generation=1,
                moving_to=moving_to,
                stopped=workload,
            )
        (destination,) = monitor.of_type(DESTINATION)
        mover = {'affinity': destination.inputs['spec']['rsyncTLS'].get('moverAffinity')}
        admitted.append(schedulable_among(mover, WITH_AND_WITHOUT_TARGET_AND_DEVICE))

    target_alone, restated = admitted
    assert target_alone
    assert restated == target_alone


async def _moved(constraints: Constraints, retention: Backed | Off) -> storage.Claim:
    """The copy of a move of a claim handed `constraints`, under `retention`."""
    _ = await run_with(Recorder(), stack='apps')
    async with declaring():
        workload = k8s.core.v1.ConfigMap('workload')
        return claim(
            'moving',
            'data',
            1,
            retention,
            namespace='moving',
            uid=UID,
            constraints=constraints,
            generation=1,
            moving_to=TARGET,
            stopped=workload,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'retention', [off('a cache, fetched again'), backed(WEEKDAY, writer_key('moving'))], ids=['unbacked', 'backed']
)
@pytest.mark.parametrize('order', ['claim first', 'device first'])
async def test_a_device_composed_onto_a_moved_claims_constraints_after_it_was_made_is_refused(
    retention: Backed | Off, order: str
) -> None:
    """The copy's destination binds the new claim, and it was not held to the nodes that offer the device.

    So it may bind the claim on a node without the device, which the
    workload, back after the finish, needs beside the claim.
    """
    made = await _moved(HOME, retention)

    with pytest.raises(VolumeOffBinder) as refused:
        _ = made.constraints & DEVICE if order == 'claim first' else DEVICE & made.constraints

    assert 'requests a device of the test' in str(refused.value)
    assert "its claim 'data-g1'" in str(refused.value)


#: Each kind of second claim on a node a pod could mount beside a claim being
#: moved: its class, its retention, and whether it is moved in the same copy.
SECOND_CLAIMS: dict[str, tuple[str, Backed | Off, bool]] = {
    'unbacked': (conventions.SC_LOCAL_PATH, off('a cache'), False),
    'backed': (conventions.SC_LOCAL_PATH, backed(WEEKDAY, writer_key('pair')), False),
    'node volume': (NODE_VOLUME_CLASS, off('a cache'), False),
    'moved with it': (conventions.SC_LOCAL_PATH, off('a cache'), True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('second', list(SECOND_CLAIMS.values()), ids=list(SECOND_CLAIMS))
@pytest.mark.parametrize('moved_first', [True, False], ids=['moved claim made first', 'second claim made first'])
@pytest.mark.parametrize(
    'moved_class', [conventions.SC_LOCAL_PATH, NODE_VOLUME_CLASS], ids=['into local-path', "into a node volume's class"]
)
async def test_a_claim_moved_beside_a_second_claim_on_a_node_of_its_pod_is_refused(
    second: tuple[str, Backed | Off, bool], moved_first: bool, moved_class: str
) -> None:
    """The destination binds the moved claim on the target, apart from the second claim.

    The second stays bound where it is, or, moved too, is bound by a
    destination of its own, placed apart from the first's: after the finish
    the pod, which needs both, could find them on two nodes and run on
    neither. That holds whatever class the claim is moved into, a node
    volume's included, whose node the move names. The second claim is
    handed the first's constraints, the form a pod's second claim takes.
    """
    storage_class, retention, also_moved = second
    _ = await run_with(Recorder(), stack='apps')
    async with declaring():
        workload = k8s.core.v1.ConfigMap('workload')

        def moved(constraints: Constraints) -> storage.Claim:
            return claim(
                'pair-moved',
                'moved',
                1,
                off('a cache'),
                namespace='pair',
                uid=UID,
                constraints=constraints,
                generation=1,
                moving_to=TARGET,
                stopped=workload,
                storage_class=moved_class,
            )

        def other(constraints: Constraints) -> storage.Claim:
            return claim(
                'pair-other',
                'other',
                1,
                retention,
                namespace='pair',
                uid=UID,
                constraints=constraints,
                storage_class=storage_class,
                generation=1 if also_moved else 0,
                moving_to=TARGET if also_moved else None,
                stopped=workload if also_moved else None,
            )

        first, then = (moved, other) if moved_first else (other, moved)
        made = first(HOME)
        with pytest.raises(MovingVolumeBesideVolume) as refused:
            _ = then(made.constraints)

    assert "its claim 'moved-g1'" in str(refused.value)
    assert "its claim 'other-g" in str(refused.value)


@pytest.mark.asyncio
async def test_a_claim_moved_as_the_one_claim_on_a_node_of_its_pod_is_admitted_and_the_finish_lifts_the_rule() -> None:
    """A NAS claim keeps its pod to a site rather than to one node, and after the finish the claim is moved no longer, so a second claim may join it.

    The moved claim's pod mounts a NAS claim beside it; once the move is
    finished, the claim of the new generation takes an unbacked second claim
    beside it, which binds where the pod is placed.
    """
    _ = await run_with(Recorder(), stack='apps')
    async with declaring():
        workload = k8s.core.v1.ConfigMap('workload')
        library = nas_claim('beside-library', 'library', 'Photos/Library', namespace='beside', read_only=False)
        moved = claim(
            'beside-moved',
            'moved',
            1,
            off('a cache'),
            namespace='beside',
            uid=UID,
            constraints=library.constraints,
            generation=1,
            moving_to=TARGET,
            stopped=workload,
        )
        finished = claim(
            'beside-finished',
            'finished',
            1,
            backed(WEEKDAY, writer_key('beside')),
            namespace='beside',
            uid=UID,
            constraints=library.constraints,
            generation=1,
        )
        second = claim(
            'beside-second',
            'second',
            1,
            off('a cache'),
            namespace='beside',
            uid=UID,
            constraints=finished.constraints,
        )

    assert Keeper.MOVING_VOLUME in {it.keeper for it in (moved.constraints & library.constraints).kept}
    assert {it.keeper for it in second.constraints.kept} == {Keeper.BACKED_VOLUME, Keeper.VOLUME}


def test_a_copy_is_ordered_after_a_workload_it_is_handed() -> None:
    with pytest.raises(ValueError, match='scale-down'):
        _ = claim(
            'early',
            'data',
            1,
            backed(WEEKDAY, writer_key('early')),
            namespace='early',
            uid=UID,
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
            backed(WEEKDAY, writer_key('nowhere')),
            namespace='nowhere',
            uid=UID,
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
            backed(WEEKDAY, writer_key('long')),
            namespace='long',
            uid=UID,
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
            backed(WEEKDAY, writer_key('flat')),
            namespace='flat',
            uid=UID,
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
    # The backed claims' sources, every copy's pair, and the source the copy
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
            'transcode-move-g1',
            'transcode-move-g1',
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
