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
    worker alone, since no other node can reach the export.

**A backed claim** carries a VolSync `ReplicationSource` with the restic mover,
`copyMethod: Direct` since `local-path` has no snapshots, to the repository at
`conventions.volsync_repo_path` of its namespace and its base name. The
repository's password is generated into state, and its Secret is a SealedSecret
under the base name that carries the namespace's sealed writer key, with the
password and the repository in its template: in state, and in clear in the
SealedSecret object, which is the residual the security audit records.

**A move is asked for, and it backs nothing up.** A claim's name and its restic
source's carry a generation, while its repository, its password and its Secret
are keyed by the base name and survive every move. Raising the generation with
`moving_to` set is the copy: the claim of the generation before stays where it
is, the claim of the new one is added, and an `rsync-tls` source and
destination copy the one into the other over a Service in the namespace,
started by hand and waited for by the apply. Restic has no retention of zero,
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
from kluster.components.monitoring import WORKER_AFFINITY
from kluster.conventions.backup import RetentionClass
from kluster.conventions.cluster import RETENTION_LABEL, WRITER_KEY_FIELDS
from kluster.conventions.sealed import SealingScope
from kluster.lib.k8s import SecretTemplate, sealed_secret

__all__ = (
    'COPY_SECONDS_PER_GIB',
    'MOVER_LABEL',
    'MOVER_RESOURCES',
    'RSYNC_TLS_PORT',
    'Claim',
    'Move',
    'NasClaim',
    'Off',
    'claim',
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


def claim(
    resource_name: str,
    name: str,
    size: int,
    retention: RetentionClass | Off,
    *,
    namespace: str,
    uid: int,
    writer_key: Mapping[str, pulumi.Input[str]] | None,
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
        repository Secret's name; `None` while it is absent, in which case no
        Secret is declared until it is sealed in (physical/bring-up.md §1).
    :param generation: The claim's generation, raised by one for each move.
    :param moving_to: The node labels the new generation's claim is made on,
        which the destination's mover selects; set for the copy alone.
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
    copy = _copy(resource_name, name, generation, moving_to, stopped)
    if writer_key is not None and frozenset(writer_key) != WRITER_KEY_FIELDS:
        raise ValueError(
            f'{resource_name}: the writer key of {namespace!r} holds {sorted(writer_key)}, '
            f'and a repository Secret carries {sorted(WRITER_KEY_FIELDS)}'
        )

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
        labels, workload = copy
        # The claim that holds the data, as it was made: this call's size and
        # class are the new claim's, and a class changed here would otherwise
        # replace this one, which empties it.
        held = pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(ignore_changes=['spec']))
        copied = _claim(resource_name, name, generation - 1, size, storage_class, namespace=namespace, opts=held)
        source, destination = _pair(
            resource_name,
            name,
            generation,
            size,
            namespace=namespace,
            uid=uid,
            moving_to=labels,
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
                _restic(resource_name, name, generation - 1, retention, namespace=namespace, uid=uid, opts=opts)
            )
    elif isinstance(retention, RetentionClass):
        sources.append(_restic(resource_name, name, generation, retention, namespace=namespace, uid=uid, opts=opts))

    backed = isinstance(retention, RetentionClass)
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
    )


def _copy(
    resource_name: str,
    name: str,
    generation: int,
    moving_to: Mapping[str, str] | None,
    stopped: pulumi.Resource | None,
) -> tuple[Mapping[str, str], pulumi.Resource] | None:
    """The labels a copy moves the claim to and the workload it is ordered after, refused where a copy cannot be made."""
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
    service = _destination_service(_pair_name(name, generation))
    if len(service) > _NAME_LIMIT:
        raise ValueError(
            f'{resource_name}: the copy of {name!r} would be served as {service!r}, past the {_NAME_LIMIT} characters '
            'a name may have, which VolSync then replaces with a hash the source cannot name'
        )
    return moving_to, stopped


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
    writer_key: Mapping[str, pulumi.Input[str]] | None,
    opts: pulumi.ResourceOptions | None,
) -> crds.bitnami.v1alpha1.SealedSecret | None:
    """The repository Secret: the namespace's writer key, sealed, with the repository and its password in the template."""
    if writer_key is None:
        # The tolerance `kluster.lib.k8s.sealed_value` keeps for an absent
        # sealed value, which retires with it at the bring-up's gate: this
        # Secret is named for the claim rather than for the value's row, so it
        # is declared here rather than through that helper, and warns the same.
        pulumi.log.warn(
            f'the sealed writer key of {namespace!r} is absent from this stack, so no repository Secret is '
            f'declared for {name!r} and its source stays pending until it is sealed in (physical/bring-up.md §1)'
        )
        return None
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
    opts: pulumi.ResourceOptions | None,
) -> crds.volsync.v1alpha1.ReplicationSource:
    """A claim's restic source, scheduled by its class, to the repository of its base name."""
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
            ),
        ),
        opts=opts,
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
    moving_to: Mapping[str, str],
    stopped: pulumi.Resource,
    claims: tuple[pulumi.Resource, ...],
    opts: pulumi.ResourceOptions | None,
) -> tuple[crds.volsync.v1alpha1.ReplicationSource, crds.volsync.v1alpha1.ReplicationDestination]:
    """The copy's pair: the source on the claim before, the destination into the new one, both started by hand.

    The two take one name, so that the source finds the destination's Service
    and its generated key by it. The source is applied after the workload's update,
    and the apply waits for the copy, through the source's `lastManualSync`.
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
                                key=key, operator='In', values=[value]
                            )
                            for key, value in sorted(moving_to.items())
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


@dataclass(frozen=True)
class NasClaim:
    """What `nas_claim` emitted: the volume, bound to its claim alone, and the claim."""

    volume: k8s.core.v1.PersistentVolume
    claim: k8s.core.v1.PersistentVolumeClaim


def nas_claim(
    resource_name: str,
    name: str,
    path: str,
    *,
    namespace: str,
    read_only: bool,
    opts: pulumi.ResourceOptions | None = None,
) -> NasClaim:
    """A static PersistentVolume over `path` under the host's one export, and its claim, on the worker alone.

    For data the cluster does not own, so it takes no retention: the source's
    own regime is its safety net. The export is reachable from the worker and
    from no other node (physical/homelab-host.md §5), so the volume's node
    affinity is the worker's, and the scheduler places every pod that mounts it
    there. The volume is bound to its claim by name and to no other, under a
    class no StorageClass answers to, so no provisioner touches either, and it
    reclaims `Retain`: removing the claim never touches the data.

    A pod writes through it as the export's anonymous user, whatever it runs as
    (`conventions.NAS_EXPORT`).

    :param resource_name: The logical name, which carries the declaring
        component's name.
    :param name: The claim's name in its namespace.
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
    volume_name = f'{namespace}-{name}'
    worker = WORKER_AFFINITY['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
    volume = k8s.core.v1.PersistentVolume(
        f'{resource_name}-nas',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=volume_name),
        spec=k8s.core.v1.PersistentVolumeSpecArgs(
            capacity={'storage': _NAS_NOMINAL_SIZE},
            access_modes=[access],
            persistent_volume_reclaim_policy='Retain',
            storage_class_name=conventions.SC_NAS,
            claim_ref=k8s.core.v1.ObjectReferenceArgs(namespace=namespace, name=name),
            nfs=k8s.core.v1.NFSVolumeSourceArgs(
                server=str(conventions.HOMELAB_HOST_LEG_IPV4),
                path='/'.join([conventions.NAS_EXPORT, *parts]),
                read_only=read_only,
            ),
            node_affinity=k8s.core.v1.VolumeNodeAffinityArgs(required=worker),
        ),
        opts=opts,
    )
    volume_claim = k8s.core.v1.PersistentVolumeClaim(
        f'{resource_name}-nas',
        metadata=k8s.meta.v1.ObjectMetaArgs(name=name, namespace=namespace),
        spec=k8s.core.v1.PersistentVolumeClaimSpecArgs(
            access_modes=[access],
            storage_class_name=conventions.SC_NAS,
            volume_name=volume.metadata.name,
            resources=k8s.core.v1.VolumeResourceRequirementsArgs(requests={'storage': _NAS_NOMINAL_SIZE}),
        ),
        opts=opts,
    )
    return NasClaim(volume=volume, claim=volume_claim)
