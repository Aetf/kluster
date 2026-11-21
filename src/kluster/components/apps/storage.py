"""The application base's storage: a claim on a node, a claim on the NAS, and how a claim moves.

Two calls, and a class for a node volume that is not a call at all
(declarative/workloads.md §2, rfc-008 §4.2):

-   **`claim`** is the one claim on a node. Its `retention` is required: a
    retention class (`conventions.backup`), which backs the claim up to the
    bucket on the class's schedule, or `off('<reason>')`, which emits the claim
    alone, so that an unbacked claim is a decision on record rather than an
    omission. A claim of a node volume is an ordinary `claim` whose
    `storage_class` is `conventions.node_volume_class(row)`.
-   **`nas_claim`** is a static PersistentVolume over a path of the host's one
    export and its claim, for data the cluster does not own, scheduled to the
    home site alone, since no node elsewhere can reach the export. The two
    are named with a digest of what neither can change once it exists, so a
    change to any of it is a new volume and claim.

**A backed claim** carries a VolSync `ReplicationSource` with the restic mover,
`copyMethod: Direct` since `local-path` has no snapshots, to the repository at
`conventions.volsync_repo_path` of its namespace and its base name. The
repository's password is generated into state, and its Secret is a SealedSecret
under the base name that carries the namespace's sealed writer key, with the
password and the repository in its template: in state, and in clear in the
SealedSecret object, which is the residual the security audit records.

**A restic mover runs where the workload may.** A `local-path` claim binds
on the node its first consumer is scheduled to, and VolSync places a `Direct`
mover beside the claim's running consumer and anywhere at all while it has
none. So a source that reaches the claim before the workload's pod would bind
it wherever its mover landed, which may be a node the workload can never run
on. `claim` takes the workload's `constraints`
(`kluster.lib.k8s.Constraints`), and the restic mover carries their node
affinity as a pod that binds the claim in the workload's stead
(`Constraints.binder_requirements`): the workload's own requirements, and
the label of the nodes that offer each device the workload requests, which
the mover does not request. So whichever of the mover and the pod is
scheduled first, the claim binds on a node the workload's constraints and
its devices admit. A workload mounts at most one backed claim, and none
beside a node volume's claim: a mover, placed apart from the pod's other
claim, could bind its claim on another node than that claim's, and the pod,
which needs both, would run on neither. `claim_hold` records which kind of
claim keeps the pod, and for a backed claim the requirements its mover was
given, so the composition is refused (`kluster.lib.k8s.TwoBackedVolumes`,
`BackedVolumeBesideNodeVolume`), and so is a constraint composed onto a
backed claim's constraints that its mover was not given: a site, an
architecture or a device the claim was not handed
(`kluster.lib.k8s.VolumeOffBinder`; cluster/storage.md §3.1).

**The `Claim` says what the workload's pod carries for it.** Its
`constraints` are the workload's with the claim's own hold composed in
(`kluster.lib.k8s.kept`): a bound claim keeps its pod to its node, which the
scheduler reads from the volume, so the pod states no affinity for it and
runs at the held priority, ahead of free pods that could otherwise fill the
node it can run nowhere else than. Its `pod_annotations` put a `local-path`
claim's pod in the blockio class `conventions.ETCD_DISK_WRITER`, which caps
what it writes to the disk etcd shares where its node is a control plane
(cluster/nodes.md §1.2); a node volume is a disk of its own and takes no
class. A mover carries no class: VolSync gives it labels alone.

**A move is asked for, and it backs nothing up.** A claim's name and its restic
source's carry a generation, while its repository, its password and its Secret
are keyed by the base name and survive every move. Raising the generation with
`moving_to` set is the copy: the claim of the generation before stays where it
is, the claim of the new one is added, and an `rsync-tls` source and
destination copy the one into the other over a Service in the namespace,
started by hand and waited for by the apply. The destination's mover is the
new claim's first consumer, so it binds the claim in the workload's stead,
and it carries `moving_to` beside the claim's binder requirements; a
`moving_to` that gives one of their labels another value is refused, since
no node admits both (`MoveOffBinder`). Whatever its class and retention,
the new claim's hold is a moving volume's
(`kluster.lib.k8s.Keeper.MOVING_VOLUME`), which records what the destination
was given, so a constraint composed onto the `Claim`'s constraints that the
destination was not given is refused (`kluster.lib.k8s.VolumeOffBinder`). **A claim is moved only while it is
the one claim on a node its pod mounts**, and two claims of one pod are not
moved together: the pod's second claim stays bound where it is, and a second
move's destination is placed apart from the first's, so after the finish
the pod could find its claims on two nodes and run on neither
(`kluster.lib.k8s.MovingVolumeBesideVolume`). Restic has no retention of zero,
so a copy through the bucket would leave a snapshot behind; `rsync-tls` needs
no bucket, repository or credential. The finish is a claim of the new
generation without `moving_to`, whose source, under a class, syncs to the same
repository on its first reconcile (storage.md §3.1).

**A pod writes to the NAS as the export's anonymous user.** The export squashes
every client (`conventions.NAS_EXPORT`), so every write arrives as one user
whatever the pod runs as, `restricted` needs nothing of it, and a path a pod
writes to is that user's. A path written as anything else is re-owned on the
host before the application that writes it lands (rfc-008 §4.2).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s
import pulumi_random as random

from kluster import conventions
from kluster.components.backup import s3_endpoint
from kluster.conventions.backup import RetentionClass
from kluster.conventions.cluster import RETENTION_LABEL, WRITER_KEY_FIELDS
from kluster.conventions.sealed import SealingScope
from kluster.lib.k8s import (
    HOME_NODE_SELECTOR,
    HOME_NODE_SELECTOR_JSON,
    BackedVolumeBesideNodeVolume,
    Constraints,
    Keeper,
    MovingVolumeBesideVolume,
    NodeRequirement,
    SecretTemplate,
    TwoBackedVolumes,
    at_home,
    kept,
    sealed_secret,
    writer_annotations,
)

__all__ = (
    'COPY_SECONDS_PER_GIB',
    'MOVER_LABEL',
    'MOVER_RESOURCES',
    'RSYNC_TLS_PORT',
    'Claim',
    'Move',
    'MoveOffBinder',
    'NasClaim',
    'Off',
    'claim',
    'claim_hold',
    'claim_name',
    'copy_timeout',
    'nas_claim',
    'off',
)

#: The label every mover of a claim carries, with the claim's base name as its
#: value: what the allow for its traffic selects.
MOVER_LABEL = f'{conventions.LABEL_DOMAIN}/volsync-mover'


#: What every mover asks for and is held to. Its CPU is limited because a mover
#: may run on a cloud node beside etcd (declarative/workloads.md §1).
MOVER_RESOURCES: Mapping[str, Mapping[str, str]] = {
    'requests': {'cpu': '100m', 'memory': '256Mi'},
    'limits': {'cpu': '500m', 'memory': '1Gi'},
}

#: How long a copy is waited for per GiB of its claim, beside `_COPY_FLOOR`,
#: which admits a copy as slow as about 8.5 MiB/s. A bound rather than a
#: measurement: what a copy across the KubeSpan link sustains is read on the
#: first move.
COPY_SECONDS_PER_GIB = 120
_COPY_FLOOR = 600

#: The cluster's DNS, the CoreDNS Talos bootstraps, as Cilium names its pods:
#: its namespace, and the label upstream's manifest gives them, which Talos's
#: keeps.
_CLUSTER_DNS = {'io.kubernetes.pod.namespace': 'kube-system', 'k8s-app': 'kube-dns'}

#: The port the `rsync-tls` destination's Service listens on, VolSync's default.
RSYNC_TLS_PORT = 8000


@dataclass(frozen=True)
class Off:
    """A claim that nothing backs up, and why: the decision on record."""

    reason: str


def off(reason: str) -> Off:
    """No backup for a claim, with the reason, which is refused empty."""
    if not reason.strip():
        raise ValueError('a claim that nothing backs up says why: `off` takes a reason')
    return Off(reason)


def claim_name(name: str, generation: int) -> str:
    """The name of a claim of one generation, which its restic source shares."""
    return f'{name}-g{generation}'


class MoveOffBinder(ValueError):
    """A move to a label value the workload's binder requirements refuse: the destination, which binds the new claim, would admit no node."""


@dataclass(frozen=True)
class Move:
    """What a copy adds: the claim being copied, and the source and destination that copy it."""

    copied: k8s.core.v1.PersistentVolumeClaim
    source: crds.volsync.v1alpha1.ReplicationSource
    destination: crds.volsync.v1alpha1.ReplicationDestination


@dataclass(frozen=True)
class Claim:
    """What `claim` emitted.

    `name` is the claim the workload mounts, which is `claim`'s: of the new
    generation during a copy, whose workload is scaled to zero.
    """

    name: str
    claim: k8s.core.v1.PersistentVolumeClaim
    sources: tuple[crds.volsync.v1alpha1.ReplicationSource, ...]
    """The restic sources: the claim's own, or during a copy the one of the claim being copied."""
    password: random.RandomPassword | None
    repository: crds.bitnami.v1alpha1.SealedSecret | None
    move: Move | None
    movers: crds.cilium.v2.CiliumNetworkPolicy | None
    constraints: Constraints
    """What the workload's pod states: the constraints `claim` was handed, with the claim's hold composed in."""
    pod_annotations: Mapping[str, str]
    """What the workload's pod carries in its metadata for the claim: a `local-path` claim's blockio class, or nothing."""


def claim(
    resource_name: str,
    name: str,
    size: int,
    retention: RetentionClass | Off,
    *,
    namespace: str,
    uid: int,
    writer_key: Mapping[str, pulumi.Input[str]],
    constraints: Constraints,
    generation: int = 0,
    moving_to: Mapping[str, str] | None = None,
    stopped: pulumi.Resource | None = None,
    storage_class: str = conventions.SC_LOCAL_PATH,
    protect: bool = False,
    opts: pulumi.ResourceOptions | None = None,
) -> Claim:
    """A claim on a node, backed up by its retention class or `off`, and moved when it is asked to.

    Under a class it emits, beside the claim, the restic source, the password
    generated in state and the repository Secret; under `off`, the claim alone.
    With `moving_to` it is the copy of a move instead: the claim of the
    generation before, the claim of this one, and the `rsync-tls` pair from the
    one to the other. Every claim with a mover emits the allow for the movers'
    traffic.

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param name: The base name, which the claim's of each generation, the
        repository's path and its Secret are derived from.
    :param size: The claim's size, in GiB.
    :param retention: A retention class, or `off` with its reason.
    :param namespace: The application's namespace, which the repository's
        path is under; the caller's `opts` carries the dependency on the
        Namespace resource, since this is a plain name.
    :param uid: The user the workload runs as, and so every mover, which has
        to read and write the data as the workload does; the mover's group
        and its volumes' group take the same number.
    :param writer_key: The ciphertext of the namespace's sealed writer key, as
        the program read it, sealed namespace-wide so that it opens under the
        repository Secret's name (`kluster.lib.k8s.sealed_ciphertext`).
    :param constraints: Every node constraint the workload is under: a pod
        that mounts a `nas_claim` beside this claim states its home site
        (`NasClaim.constraints`), a pod that mounts a second claim hands
        it the first's (`<first>.constraints`), and a pod that requests a
        device hands it the device's hold (`kluster.components.apps.GPU_HOLD`).
        Every restic mover of the claim carries their node affinity with the
        label of the nodes that offer each device
        (`Constraints.binder_requirements`), so that it binds the claim only on
        a node they admit. The `Claim` records them with its own hold
        composed in, and the workload's pod states them from there, so the
        two are one value. A backed claim handed the constraints of another
        backed claim, or of a node volume's claim, is refused: its mover
        could bind it on another node than the other claim's. During a copy
        they are the workload's on the claim being copied: the workload moves
        to the target in the finish. The copy's destination, under any
        retention, binds the new claim, and carries the same requirements
        beside `moving_to`; the `Claim` records them in the claim's hold, so
        a constraint composed onto its constraints afterwards that they
        leave out is refused, and so is a claim moved beside a second claim
        on a node of its pod, whichever of the two is made first.
    :param generation: The claim's generation, raised by one for each move.
    :param moving_to: The node labels the new generation's claim is made on,
        which the destination's mover selects beside the binder requirements
        of `constraints`; set for the copy alone. A label those requirements
        fix to another value is refused (`MoveOffBinder`).
    :param stopped: The workload, whose update the copy's source depends on;
        required with `moving_to`. What that orders is the apply that sets
        its replicas to zero, not the end of its pods, which can still be
        writing their shutdown inside their grace period while the mover
        reads the claim; the wait that closes it is Aetf/kluster-ops#724,
        ahead of the first move.
    :param storage_class: `local-path`, or a node volume's class.
    :param protect: Whether a claim is protected: a precious claim, and a node
        volume's, whose class retains its data (storage.md §3.3). A protected
        claim of the generation before is unprotected by a merge of its own
        ahead of the finish.
    """
    if size < 1:
        raise ValueError(f'{resource_name}: a claim is at least 1 GiB, and {name!r} asks for {size}')
    copy = _copy(resource_name, name, generation, moving_to, stopped, constraints.binder_requirements)
    if frozenset(writer_key) != WRITER_KEY_FIELDS:
        raise ValueError(
            f'{resource_name}: the writer key of {namespace!r} holds {sorted(writer_key)}, '
            f'and a repository Secret carries {sorted(WRITER_KEY_FIELDS)}'
        )

    backed = isinstance(retention, RetentionClass)
    mover = constraints.binder_requirements if backed else ()
    try:
        held = constraints & claim_hold(
            f'its claim {claim_name(name, generation)!r}',
            storage_class,
            backed=backed,
            # The claim the pod mounts during a copy is the new one, which the
            # destination binds, held to what `_copy` gave it.
            binder_held_to=mover if copy is None else copy[0],
            moving=copy is not None,
        )
    except (TwoBackedVolumes, BackedVolumeBesideNodeVolume, MovingVolumeBesideVolume) as refused:
        raise type(refused)(f'{resource_name}: {refused}') from None
    affinity = _mover_affinity(mover) if backed else None

    child = pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(protect=protect))
    target = _claim(resource_name, name, generation, size, storage_class, namespace=namespace, opts=child)

    sources: list[crds.volsync.v1alpha1.ReplicationSource] = []
    password: random.RandomPassword | None = None
    repository: crds.bitnami.v1alpha1.SealedSecret | None = None
    move: Move | None = None

    if isinstance(retention, RetentionClass):
        password = random.RandomPassword(
            f'{resource_name}-restic-password',
            length=64,
            # A Go template carries it in the SealedSecret, so it holds nothing
            # a template would read as an action.
            special=False,
            # The repository's only key, escrowed nowhere (credentials.md §3):
            # a new one would leave every snapshot taken so far unopened.
            opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(protect=True)),
        )
        repository = _repository(
            resource_name, name, namespace=namespace, password=password, writer_key=writer_key, opts=opts
        )

    if copy is not None:
        requirements, workload = copy
        # The claim that holds the data, as it was made: this call's size and
        # class are the new claim's, and a class changed here would otherwise
        # replace this one, which empties it.
        as_made = pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(ignore_changes=['spec']))
        copied = _claim(resource_name, name, generation - 1, size, storage_class, namespace=namespace, opts=as_made)
        source, destination = _pair(
            resource_name,
            name,
            generation,
            size,
            namespace=namespace,
            uid=uid,
            requirements=requirements,
            stopped=workload,
            claims=(copied, target),
            opts=opts,
        )
        move = Move(copied=copied, source=source, destination=destination)
        # The copied claim keeps its own source until the finish removes both:
        # a source on the new claim would back up a claim the copy has not
        # filled, on its first reconcile.
        if isinstance(retention, RetentionClass):
            sources.append(
                _restic(
                    resource_name,
                    name,
                    generation - 1,
                    retention,
                    namespace=namespace,
                    uid=uid,
                    affinity=affinity,
                    opts=opts,
                )
            )
    elif isinstance(retention, RetentionClass):
        sources.append(
            _restic(
                resource_name, name, generation, retention, namespace=namespace, uid=uid, affinity=affinity, opts=opts
            )
        )

    movers = (
        _movers(resource_name, name, namespace=namespace, bucket=backed, opts=opts)
        if backed or move is not None
        else None
    )
    return Claim(
        name=claim_name(name, generation),
        claim=target,
        sources=tuple(sources),
        password=password,
        repository=repository,
        move=move,
        movers=movers,
        constraints=held,
        pod_annotations=writer_annotations(storage_class),
    )


def claim_hold(
    what: str,
    storage_class: str,
    *,
    backed: bool,
    binder_held_to: tuple[NodeRequirement, ...] = (),
    moving: bool = False,
) -> Constraints:
    """The hold of a pod to the node a claim of `storage_class` is bound on, saying `what` the claim is.

    The new claim of a move's copy, `moving`, is bound by the copy's
    destination (`Keeper.MOVING_VOLUME`), whatever its class and retention.
    Otherwise, a node volume's claim is on its node before any pod is placed
    (`Keeper.NODE_VOLUME`), and a `local-path` claim binds on the node its
    first consumer is placed on: the pod's, or, for a backed claim, a restic
    mover's (`Keeper.BACKED_VOLUME`), since VolSync places a mover of a
    claim no running pod uses wherever its own affinity admits. The hold of
    a claim with a binder records `binder_held_to`, the requirements the
    binder carries, so a constraint whose requirement they leave out is
    refused beside it (`kluster.lib.k8s.VolumeOffBinder`).
    """
    if moving:
        return kept(
            Keeper.MOVING_VOLUME,
            f"{what} is bound on the node its copy's destination is placed on",
            binder_held_to=binder_held_to,
        )
    if storage_class != conventions.SC_LOCAL_PATH:
        return kept(Keeper.NODE_VOLUME, f'{what} is on the node its node volume is attached to')
    if backed:
        return kept(
            Keeper.BACKED_VOLUME,
            f'{what} is bound on the node it or its mover is first placed on',
            binder_held_to=binder_held_to,
        )
    return kept(Keeper.VOLUME, f'{what} is bound on the node it is first placed on')


def _copy(
    resource_name: str,
    name: str,
    generation: int,
    moving_to: Mapping[str, str] | None,
    stopped: pulumi.Resource | None,
    binder: tuple[NodeRequirement, ...],
) -> tuple[tuple[NodeRequirement, ...], pulumi.Resource] | None:
    """What a copy's destination requires of its node and the workload the copy is ordered after, refused where a copy cannot be made.

    The destination binds the new claim, so it is held as every binder of
    the claim is, to `binder`, the workload's binder requirements, and to
    `moving_to` beside them. Taken together, `moving_to` narrows the nodes
    the workload admits, which is the move: a disk label, a node by name.
    A `moving_to` that sets a label of `binder` to another value narrows
    them to none, and a destination no node admits is never scheduled,
    which the apply would wait out to the copy's timeout; so that one case
    is refused here.
    """
    if moving_to is None:
        return None
    if generation < 1:
        raise ValueError(f'{resource_name}: a move raises the generation, and {name!r} is at generation 0')
    if not moving_to:
        raise ValueError(f'{resource_name}: a move names the node labels it moves {name!r} to')
    if stopped is None:
        raise ValueError(
            f"{resource_name}: a move is ordered after the scale-down of {name!r}'s workload, which it is not handed"
        )
    contrary = [
        f'{it.key}={moving_to[it.key]} where its workload requires {it.value}'
        for it in binder
        if it.key in moving_to and moving_to[it.key] != it.value
    ]
    if contrary:
        raise MoveOffBinder(
            f'{resource_name}: {name!r} is moved to {"; ".join(contrary)}, so no node admits the destination '
            'that binds its new claim'
        )
    service = _destination_service(_pair_name(name, generation))
    if len(service) > _NAME_LIMIT:
        raise ValueError(
            f'{resource_name}: the copy of {name!r} would be served as {service!r}, past the {_NAME_LIMIT} characters '
            'a name may have, which VolSync then replaces with a hash the source cannot name'
        )
    target = tuple(NodeRequirement(key, value) for key, value in sorted(moving_to.items()))
    return tuple(dict.fromkeys((*binder, *target))), stopped


#: The longest name an object VolSync names after its owner may have before
#: VolSync replaces it with a hash: a Service's, a DNS label.
_NAME_LIMIT = 63


def _pair_name(name: str, generation: int) -> str:
    """The one name of a copy's source and destination."""
    return f'{name}-move-g{generation}'


def _destination_service(pair: str) -> str:
    """The Service VolSync serves an `rsync-tls` destination at, named after it."""
    return f'volsync-rsync-tls-dst-{pair}'


def _claim(
    resource_name: str,
    name: str,
    generation: int,
    size: int,
    storage_class: str,
    *,
    namespace: str,
    opts: pulumi.ResourceOptions | None,
) -> k8s.core.v1.PersistentVolumeClaim:
    return k8s.core.v1.PersistentVolumeClaim(
        f'{resource_name}-g{generation}',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=claim_name(name, generation), namespace=namespace),
        spec=k8s.core.v1.PersistentVolumeClaimSpecArgs(
            access_modes=['ReadWriteOnce'],
            storage_class_name=storage_class,
            resources=k8s.core.v1.VolumeResourceRequirementsArgs(requests={'storage': f'{size}Gi'}),
        ),
        opts=opts,
    )


def _repository_url(namespace: str, name: str) -> str:
    """The repository of a claim's base name: the bucket's S3 endpoint, the bucket, and the path of the one layout."""
    endpoint = s3_endpoint(conventions.B2_ACCOUNT.region)
    return f's3:{endpoint}/{conventions.BUCKET_BACKUP}/{conventions.volsync_repo_path(namespace, name)}'


def _repository(
    resource_name: str,
    name: str,
    *,
    namespace: str,
    password: random.RandomPassword,
    writer_key: Mapping[str, pulumi.Input[str]],
    opts: pulumi.ResourceOptions | None,
) -> crds.bitnami.v1alpha1.SealedSecret:
    """The repository Secret: the namespace's writer key, sealed, with the repository and its password in the template.

    Named for the claim rather than for the value's row, so it is declared
    here rather than through `kluster.lib.k8s.sealed_value`.
    """
    return sealed_secret(
        f'{resource_name}-restic',
        name=name,
        namespace=namespace,
        encrypted_data=writer_key,
        template=SecretTemplate(
            data={'RESTIC_REPOSITORY': _repository_url(namespace, name), 'RESTIC_PASSWORD': password.result}
        ),
        scope=SealingScope.NAMESPACE_WIDE,
        opts=opts,
    )


def _security_context(cls: Any, seccomp: Any, uid: int) -> Any:
    """A mover's pod security context, which `restricted` admits, as the workload's user."""
    return cls(
        run_as_user=uid,
        run_as_group=uid,
        fs_group=uid,
        run_as_non_root=True,
        seccomp_profile=seccomp(type='RuntimeDefault'),
    )


def _resources(cls: Any) -> Any:
    return cls(requests=dict(MOVER_RESOURCES['requests']), limits=dict(MOVER_RESOURCES['limits']))


def _restic(
    resource_name: str,
    name: str,
    generation: int,
    retention: RetentionClass,
    *,
    namespace: str,
    uid: int,
    affinity: crds.volsync.v1alpha1.ReplicationSourceSpecResticMoverAffinityArgs | None,
    opts: pulumi.ResourceOptions | None,
) -> crds.volsync.v1alpha1.ReplicationSource:
    """A claim's restic source, scheduled by its class, to the repository of its base name, its mover held by `affinity`."""
    v = crds.volsync.v1alpha1
    return v.ReplicationSource(
        f'{resource_name}-restic-g{generation}',
        metadata=k8s.meta.v1.ObjectMetaArgs(
            name=claim_name(name, generation), namespace=namespace, labels={RETENTION_LABEL: retention.name}
        ),
        spec=v.ReplicationSourceSpecArgs(
            source_pvc=claim_name(name, generation),
            trigger=v.ReplicationSourceSpecTriggerArgs(schedule=retention.schedule),
            restic=v.ReplicationSourceSpecResticArgs(
                repository=name,
                copy_method='Direct',
                retain=v.ReplicationSourceSpecResticRetainArgs(
                    hourly=retention.hourly,
                    daily=retention.daily,
                    weekly=retention.weekly,
                    monthly=retention.monthly,
                ),
                mover_security_context=_security_context(
                    v.ReplicationSourceSpecResticMoverSecurityContextArgs,
                    v.ReplicationSourceSpecResticMoverSecurityContextSeccompProfileArgs,
                    uid,
                ),
                mover_resources=_resources(v.ReplicationSourceSpecResticMoverResourcesArgs),
                mover_pod_labels={MOVER_LABEL: name},
                mover_affinity=affinity,
            ),
        ),
        opts=opts,
    )


def _mover_affinity(
    requirements: tuple[NodeRequirement, ...],
) -> crds.volsync.v1alpha1.ReplicationSourceSpecResticMoverAffinityArgs | None:
    """The restic mover's affinity: the requirements of a binder in the workload's stead, as one required term; none where there are none."""
    v = crds.volsync.v1alpha1
    if not requirements:
        return None
    return v.ReplicationSourceSpecResticMoverAffinityArgs(
        node_affinity=v.ReplicationSourceSpecResticMoverAffinityNodeAffinityArgs(
            required_during_scheduling_ignored_during_execution=v.ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionArgs(
                node_selector_terms=[
                    v.ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsArgs(
                        match_expressions=[
                            v.ReplicationSourceSpecResticMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressionsArgs(
                                key=it.key, operator='In', values=[it.value]
                            )
                            for it in requirements
                        ]
                    )
                ]
            )
        )
    )


def copy_timeout(size: int) -> int:
    """How long, in seconds, the apply waits for the copy of a claim of `size` GiB."""
    return _COPY_FLOOR + size * COPY_SECONDS_PER_GIB


def _pair(
    resource_name: str,
    name: str,
    generation: int,
    size: int,
    *,
    namespace: str,
    uid: int,
    requirements: tuple[NodeRequirement, ...],
    stopped: pulumi.Resource,
    claims: tuple[pulumi.Resource, ...],
    opts: pulumi.ResourceOptions | None,
) -> tuple[crds.volsync.v1alpha1.ReplicationSource, crds.volsync.v1alpha1.ReplicationDestination]:
    """The copy's pair: the source on the claim before, the destination into the new one, both started by hand.

    The two take one name, so that the source finds the destination's Service
    and its generated key by it. The source is applied after the workload's update,
    and the apply waits for the copy, through the source's `lastManualSync`.
    The destination's mover is held to `requirements`, as one required term.
    """
    v = crds.volsync.v1alpha1
    pair = _pair_name(name, generation)
    trigger = f'copy-g{generation}'
    affinity = v.ReplicationDestinationSpecRsyncTLSMoverAffinityArgs(
        node_affinity=v.ReplicationDestinationSpecRsyncTLSMoverAffinityNodeAffinityArgs(
            required_during_scheduling_ignored_during_execution=v.ReplicationDestinationSpecRsyncTLSMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionArgs(
                node_selector_terms=[
                    v.ReplicationDestinationSpecRsyncTLSMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsArgs(
                        match_expressions=[
                            v.ReplicationDestinationSpecRsyncTLSMoverAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressionsArgs(
                                key=it.key, operator='In', values=[it.value]
                            )
                            for it in requirements
                        ]
                    )
                ]
            )
        )
    )
    destination = v.ReplicationDestination(
        f'{resource_name}-move-g{generation}',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=pair, namespace=namespace),
        spec=v.ReplicationDestinationSpecArgs(
            trigger=v.ReplicationDestinationSpecTriggerArgs(manual=trigger),
            rsync_tls=v.ReplicationDestinationSpecRsyncTLSArgs(
                destination_pvc=claim_name(name, generation),
                copy_method='Direct',
                service_type='ClusterIP',
                mover_affinity=affinity,
                mover_security_context=_security_context(
                    v.ReplicationDestinationSpecRsyncTLSMoverSecurityContextArgs,
                    v.ReplicationDestinationSpecRsyncTLSMoverSecurityContextSeccompProfileArgs,
                    uid,
                ),
                mover_resources=_resources(v.ReplicationDestinationSpecRsyncTLSMoverResourcesArgs),
                mover_pod_labels={MOVER_LABEL: name},
            ),
        ),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=list(claims))),
    )
    source = v.ReplicationSource(
        f'{resource_name}-move-g{generation}',
        metadata=k8s.meta.v1.ObjectMetaArgs(
            name=pair,
            namespace=namespace,
            annotations={
                'pulumi.com/waitFor': f'jsonpath={{.status.lastManualSync}}={trigger}',
                'pulumi.com/timeoutSeconds': str(copy_timeout(size)),
            },
        ),
        spec=v.ReplicationSourceSpecArgs(
            source_pvc=claim_name(name, generation - 1),
            trigger=v.ReplicationSourceSpecTriggerArgs(manual=trigger),
            rsync_tls=v.ReplicationSourceSpecRsyncTLSArgs(
                address=f'{_destination_service(pair)}.{namespace}.svc',
                port=RSYNC_TLS_PORT,
                copy_method='Direct',
                mover_security_context=_security_context(
                    v.ReplicationSourceSpecRsyncTLSMoverSecurityContextArgs,
                    v.ReplicationSourceSpecRsyncTLSMoverSecurityContextSeccompProfileArgs,
                    uid,
                ),
                mover_resources=_resources(v.ReplicationSourceSpecRsyncTLSMoverResourcesArgs),
                mover_pod_labels={MOVER_LABEL: name},
            ),
        ),
        opts=pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[stopped, destination, *claims])),
    )
    return source, destination


def _movers(
    resource_name: str,
    name: str,
    *,
    namespace: str,
    bucket: bool,
    opts: pulumi.ResourceOptions | None,
) -> crds.cilium.v2.CiliumNetworkPolicy:
    """The allow for a claim's movers: inside the namespace, to the cluster's DNS, and for restic, to the bucket.

    Its egress rules switch the movers to default-deny on egress, so this is
    all they reach. The DNS rule goes through Cilium's proxy, which is what
    lets the bucket's rule name the endpoint's host rather than its addresses.
    """
    c = crds.cilium.v2
    egress = [
        c.CiliumNetworkPolicySpecEgressArgs(to_endpoints=[c.CiliumNetworkPolicySpecEgressToEndpointsArgs()]),
        c.CiliumNetworkPolicySpecEgressArgs(
            to_endpoints=[c.CiliumNetworkPolicySpecEgressToEndpointsArgs(match_labels=dict(_CLUSTER_DNS))],
            to_ports=[
                c.CiliumNetworkPolicySpecEgressToPortsArgs(
                    ports=[
                        c.CiliumNetworkPolicySpecEgressToPortsPortsArgs(port='53', protocol=protocol)
                        for protocol in ('UDP', 'TCP')
                    ],
                    rules=c.CiliumNetworkPolicySpecEgressToPortsRulesArgs(
                        dns=[c.CiliumNetworkPolicySpecEgressToPortsRulesDnsArgs(match_pattern='*')]
                    ),
                )
            ],
        ),
    ]
    if bucket:
        host = urlsplit(s3_endpoint(conventions.B2_ACCOUNT.region)).hostname
        egress.append(
            c.CiliumNetworkPolicySpecEgressArgs(
                to_fqdns=[c.CiliumNetworkPolicySpecEgressToFQDNsArgs(match_name=host)],
                to_ports=[
                    c.CiliumNetworkPolicySpecEgressToPortsArgs(
                        ports=[c.CiliumNetworkPolicySpecEgressToPortsPortsArgs(port='443', protocol='TCP')]
                    )
                ],
            )
        )
    return c.CiliumNetworkPolicy(
        f'{resource_name}-movers',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=f'{name}-movers', namespace=namespace),
        spec=c.CiliumNetworkPolicySpecArgs(
            description=(
                f"Let the movers of {name!r} reach the namespace, the cluster's DNS"
                + (' and the backup bucket.' if bucket else '.')
            ),
            endpoint_selector=c.CiliumNetworkPolicySpecEndpointSelectorArgs(match_labels={MOVER_LABEL: name}),
            egress=egress,
        ),
        opts=opts,
    )


#: What a NAS volume and its claim state as their size, which NFS does not
#: enforce: the claim's request has only to fit the volume's capacity.
_NAS_NOMINAL_SIZE = '1Gi'


#: What a pod that mounts the NAS states: the home site, which alone reaches
#: the export. The volume's node affinity already places the pod there; the
#: pod states it too, so that it is held as a pod at home is, and so that a
#: backed claim mounted beside it binds where the pod can run.
_ON_THE_NAS = at_home("mounts the NAS's export, which only the home site reaches (physical/homelab-host.md §5)")


#: How many hex digits of the digest a NAS claim's name carries.
_NAS_DIGEST_DIGITS = 8


def _nas_claim_name(name: str, fixed: Mapping[str, Any]) -> str:
    """The name of a NAS claim: its base name and a digest of the fields its volume and it cannot change."""
    canonical = json.dumps(fixed, sort_keys=True, separators=(',', ':'))
    return f'{name}-{hashlib.sha256(canonical.encode()).hexdigest()[:_NAS_DIGEST_DIGITS]}'


@dataclass(frozen=True)
class NasClaim:
    """What `nas_claim` emitted: the volume, bound to its claim alone, and the claim."""

    volume: k8s.core.v1.PersistentVolume
    claim: k8s.core.v1.PersistentVolumeClaim

    @property
    def constraints(self) -> Constraints:
        """What a pod that mounts it states, and a claim mounted beside it is handed: the home site."""
        return _ON_THE_NAS


def nas_claim(
    resource_name: str,
    name: str,
    path: str,
    *,
    namespace: str,
    read_only: bool,
    opts: pulumi.ResourceOptions | None = None,
) -> NasClaim:
    """A static PersistentVolume over `path` under the host's one export, and its claim, at the home site alone.

    For data the cluster does not own, so it takes no retention: the source's
    own regime is its safety net. The export is reachable from the worker and
    from no other node (physical/homelab-host.md §5), so the volume's node
    affinity is the home site's (`kluster.lib.k8s.HOME_NODE_SELECTOR`), and
    the scheduler places every pod that mounts it there. The volume is bound to its claim by name and to no other, under a
    class no StorageClass answers to, so no provisioner touches either, and it
    reclaims `Retain`: removing the claim never touches the data.

    **Its name carries what neither object can change.** The API server
    refuses a change to a volume's source and node affinity once it exists,
    and to a claim's spec, whose request only a class that allows expansion
    may grow, and no StorageClass answers to the NAS's; so `name` is a base
    name, and the claim and the volume carry it with a digest of those fields
    as declared (`_nas_claim_name`), as a claim moved to a new generation
    carries the generation (`claim_name`). A change to any of them is a volume and a claim
    of a new name: both are created, the workload, which mounts the claim by
    its name, rolls onto the new one, and the old claim and volume are
    deleted after it, unmounted and then unbound.

    A pod writes through it as the export's anonymous user, whatever it runs as
    (`conventions.NAS_EXPORT`).

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param name: The claim's base name in its namespace; the claim is named
        `NasClaim.claim`'s `metadata.name`, which a pod mounts.
    :param namespace: The claim's namespace; the caller's `opts` carries the
        dependency on the Namespace resource, since this is a plain name.
    :param path: The path under the export, relative, the empty string for the
        whole export.
    :param read_only: Whether the volume is mounted read-only.
    """
    parts = [part for part in path.split('/') if part]
    if path.startswith('/') or '..' in parts or '.' in parts:
        raise ValueError(f'{resource_name}: a NAS path is relative to the export and stays under it, not {path!r}')
    access = 'ReadOnlyMany' if read_only else 'ReadWriteMany'
    server = str(conventions.HOMELAB_HOST_LEG_IPV4)
    export_path = '/'.join([conventions.NAS_EXPORT, *parts])
    requests = {'storage': _NAS_NOMINAL_SIZE}
    # Each value below is the one the specs further down declare, as the
    # Kubernetes API spells the field: the volume's source and node affinity,
    # and the claim's spec but its `volumeName`, which is this name itself.
    # `HOME_NODE_SELECTOR_JSON` is the typed selector's JSON, written with it.
    claim_name = _nas_claim_name(
        name,
        {
            'volume': {
                'nfs': {'server': server, 'path': export_path, 'readOnly': read_only},
                'nodeAffinity': {'required': HOME_NODE_SELECTOR_JSON},
            },
            'claim': {
                'accessModes': [access],
                'storageClassName': conventions.SC_NAS,
                'resources': {'requests': requests},
            },
        },
    )
    volume = k8s.core.v1.PersistentVolume(
        f'{resource_name}-nas',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=f'{namespace}-{claim_name}'),
        spec=k8s.core.v1.PersistentVolumeSpecArgs(
            capacity={'storage': _NAS_NOMINAL_SIZE},
            access_modes=[access],
            persistent_volume_reclaim_policy='Retain',
            storage_class_name=conventions.SC_NAS,
            claim_ref=k8s.core.v1.ObjectReferenceArgs(namespace=namespace, name=claim_name),
            nfs=k8s.core.v1.NFSVolumeSourceArgs(server=server, path=export_path, read_only=read_only),
            node_affinity=k8s.core.v1.VolumeNodeAffinityArgs(required=HOME_NODE_SELECTOR),
        ),
        opts=opts,
    )
    volume_claim = k8s.core.v1.PersistentVolumeClaim(
        f'{resource_name}-nas',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=claim_name, namespace=namespace),
        spec=k8s.core.v1.PersistentVolumeClaimSpecArgs(
            access_modes=[access],
            storage_class_name=conventions.SC_NAS,
            volume_name=volume.metadata.name,
            resources=k8s.core.v1.VolumeResourceRequirementsArgs(requests=requests),
        ),
        opts=opts,
    )
    return NasClaim(volume=volume, claim=volume_claim)
