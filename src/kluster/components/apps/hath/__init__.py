"""hath, the Hentai@Home client (rfc-008 §5.4).

The client serves a slice of the H@H network's archive from its cache, and
takes commands from the network over the same port. **It is reached and
leaves at one address**: the network takes the address the client's calls
arrive from as the client's identity and tests the client's port there, so the
port is the census's one raw port on TCP at the `Dedicated` front, which is
the dedicated node's own address. How the client leaves is the node's:
rfc-008 §5.4 puts the reserved address on that node's primary private address,
so every pod on the node leaves through it by the node's own masquerading, and
nothing here declares egress.

**Its cache holds it to that node.** The cache and the client's state beside it
are one claim of the node volume `CACHE_VOLUME`'s class
(`conventions.node_volume_class`), whose allowed topology is the label of the
node carrying the volume, so the scheduler places the client there and the pod
selects no node. The volume's row follows the dedicated VIP
(`conventions.FOLLOWS_DEDICATED_VIP`), so the node that holds the cache is the
node that holds the address; a row that does not follow it is refused, since
the client would be reached at one node and run on another. The client's
port is answered on its own node, and the proxy replacement translates a
connection to a local backend without rewriting its source (Cilium's
`kubeproxy-free.rst`, "Direct Server Return (DSR)"), so the client sees each
caller's address and keeps the network's origin check on its commands.

**The cache is preserved, and nothing backs it up**: a slice of a distributed
archive whose redundancy is the network it came from, so the claim takes `off`
and is protected, and the class retains its data (cluster/storage.md §3.3;
declarative/workloads.md §2). The claim is mounted a directory at a time at
each directory the client's start script names, `DIRECTORIES`, which are the
image's volumes too: a volume of the image that no mount covers would hide
what the claim holds there.

**Its login is the ported Secret's**, `PORTED`: the legacy ciphertext, sealed
namespace-wide to the legacy client's namespace, which is why the namespace is
that one (declarative/workloads.md §5). It is mounted as the one file
`LOGIN`, which the start script writes only where it is absent and the client
reads at its start, so Reloader rolls the pod when the Secret changes.

**One client at a time.** The network refuses two clients online under one
login, so a rollout stops the old pod before it starts the new one. The start
script `exec`s the client, so the kubelet's stop reaches its shutdown hook,
which signs off at the network and lets the requests in flight finish, about
30 seconds by the client's own account; five minutes bounds it. `started` is the
migration's layer of this component, deleted at the cut: while the legacy
client serves, the component is wired with its client scaled to zero
(rfc-008 §5.4).

**It has no probe.** The network tests the port as soon as the client tells it
the client is up, and an endpoint a readiness probe held back for its period
would fail that test.

**A closed binary serves the public here, beside etcd**, so it runs under the
namespace's `restricted` level, with a CPU limit and a read-only root
filesystem (cluster/architecture.md, workload-origin risk). The image is
`frosty5689/hath`, pinned as `versions:image-hath`. Its image config names no
user, so the pod states one, `USER`, which owns what the client writes in its
claim and which any mover of the claim runs as.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Exposed, Fronts, declared_constraints, expose
from kluster.components.apps.storage import Claim, claim, off
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Dedicated, Exposure, Port
from kluster.lib.k8s import ApplicationNamespace, PortedSecret, application_namespace, sealed_value
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CACHE_VOLUME',
    'DIRECTORIES',
    'LABELS',
    'LOGIN',
    'NAME',
    'NAMESPACE',
    'PORTED',
    'ROOT',
    'USER',
    'Hath',
)

#: The application's name: its Deployment.
NAME = 'hath'

#: The legacy client's namespace, which the ported login is sealed to.
NAMESPACE = 'hath'

#: The labels its pods carry, which its raw port's Services and allow select.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The row of `conventions.NODE_VOLUMES` whose class the cache is claimed under.
CACHE_VOLUME = 'hath-cache'

#: The legacy Secret whose ciphertext is ported as it is: the client's ID and
#: key, as the one line the client reads. The program reads it with
#: `kluster.lib.k8s.sealed_ciphertext`.
PORTED = PortedSecret(name='hath', namespace=NAMESPACE, keys=('client_login',))

#: The directory the image's start script names every directory of the client
#: under.
ROOT = '/hath'

#: The directories the start script hands the client, each a volume of the
#: image, and each a directory of the claim: the cache, the client's state, its
#: downloads, its log and its scratch.
DIRECTORIES = ('cache', 'data', 'download', 'log', 'tmp')

#: The login file the client reads, in its state directory.
LOGIN = f'{ROOT}/data/client_login'

#: The user and group the client runs as, which owns the claim's files.
USER = 1000

#: The cache's claim, in GiB: 49.3 GiB of data, on a volume that keeps a few
#: GiB beside it (rfc-008 §4.2). `local-path` enforces no size; how much the
#: client caches is its setting at the network.
_CACHE_GIB = 50

#: How long the client is given to stop: it signs off at the network, which
#: otherwise counts it as gone uncleanly, in about 30 seconds by its own
#: account, and this bounds it.
_GRACE_SECONDS = 300


def _port(exposure: Exposure) -> Port:
    """The exposure's one row: a raw port on TCP at the `Dedicated` front, refused otherwise."""
    if exposure.routes or len(exposure.ports) != 1:
        raise ValueError(
            f'hath is reached at one raw port alone, and its exposure holds {len(exposure.ports)} raw ports '
            f'and {len(exposure.routes)} route rows'
        )
    (row,) = exposure.ports
    if row.transports is not Transports.TCP:
        raise ValueError(f'hath serves over TCP alone, and its port {row.name!r} is on {row.transports.value}')
    if not isinstance(row.internet, Dedicated):
        raise ValueError(
            f"the network tests hath's port at the address its calls leave from, which is the dedicated node's own, "
            f'and the port {row.name!r} is at {type(row.internet).__name__} on `internet`'
        )
    return row


def _cache_class() -> str:
    """The class of the cache's node volume, refused unless the volume is on the node that holds the dedicated address."""
    entry = conventions.NODE_VOLUMES.get(CACHE_VOLUME)
    if entry is not None and entry.node is not conventions.FOLLOWS_DEDICATED_VIP:
        raise ValueError(
            f'the node volume {CACHE_VOLUME!r} is attached to {entry.node!r}, and the client it holds there is '
            "reached at the dedicated node's address: the volume must follow the dedicated VIP (`FOLLOWS_DEDICATED_VIP`)"
        )
    return conventions.node_volume_class(CACHE_VOLUME)


def _ported(login: Mapping[str, pulumi.Input[str]]) -> Mapping[str, pulumi.Input[str]]:
    """The ciphertext, refused unless it holds each key `PORTED` seals and no other."""
    if sorted(login) != sorted(PORTED.keys):
        raise ValueError(
            f'the ported {PORTED.name!r} Secret carries {sorted(PORTED.keys)}, and the ciphertext handed '
            f'holds {sorted(login)}'
        )
    return login


class Hath(Component, pulumi_type='kluster:apps:Hath'):
    """The client's namespace, its login, its cache's claim, its Deployment, and its raw port.

    `exposure` is its row of the census, one raw port at the `Dedicated`
    front; `image` is the pin it runs; `login` the ciphertext of each key of
    `PORTED`, as the program read it with
    `kluster.lib.k8s.sealed_ciphertext`; `writer_key` the namespace's sealed
    writer key, which `claim` takes whatever the retention and no mover of an
    `off` claim reads; `started` whether the client runs, the migration's
    layer, false while the legacy client serves; and `fronts` and `zone_ids`
    what the program read for `expose`. The providers arrive in `opts`: the
    Kubernetes provider for the cluster's objects and the Cloudflare one for
    its records.
    """

    namespace: ApplicationNamespace
    login: crds.bitnami.v1alpha1.SealedSecret
    cache: Claim
    deployment: k8s.apps.v1.Deployment
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        image: ImagePin,
        login: Mapping[str, pulumi.Input[str]],
        writer_key: Mapping[str, pulumi.Input[str]],
        started: bool,
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        row = _port(exposure)
        cache_class = _cache_class()
        ciphertext = _ported(login)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAMESPACE, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace)
        # Read off the namespace, so each object in it waits for it; the claim
        # and the sealed login take the plain name, and wait for the namespace
        # through opts.
        namespace = self.namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[self.namespace.namespace])

        self.login = sealed_value(f'{name}-login', PORTED, ciphertext=ciphertext, opts=in_namespace)
        self.cache = claim(
            f'{name}-cache',
            'cache',
            _CACHE_GIB,
            off("a slice of the H@H network's archive, whose redundancy is the network it came from"),
            namespace=NAMESPACE,
            uid=USER,
            writer_key=writer_key,
            constraints=self.constraints,
            storage_class=cache_class,
            protect=True,
            opts=in_namespace,
        )

        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=NAME,
                namespace=namespace,
                # Reloader rolls the pod when the login changes: the client
                # reads it at its start alone.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1 if started else 0,
                # Two clients online under one login are refused by the network.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels=dict(LABELS), annotations=dict(self.cache.pod_annotations)
                    ),
                    spec=self._pod(row, image=image),
                ),
            ),
            opts=self.child_opts(),
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=None,
            selector=LABELS,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    def _pod(self, row: Port, *, image: ImagePin) -> k8s.core.v1.PodSpecArgs:
        return k8s.core.v1.PodSpecArgs(
            # It speaks to the network and its clients alone, and never to
            # Kubernetes.
            automount_service_account_token=False,
            **self.cache.constraints.pod_fields(),
            termination_grace_period_seconds=_GRACE_SECONDS,
            security_context=k8s.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=USER,
                run_as_group=USER,
                # The login's file is the group's to read alone. The claim's
                # root is the group's after the first start, so the walk over
                # the cache's files is not repeated at every start.
                fs_group=USER,
                fs_group_change_policy='OnRootMismatch',
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
            ),
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=NAME,
                    image=str(image),
                    # The image's start script hands the client every directory
                    # and appends this to its arguments: the port it listens on,
                    # which its Services forward to unchanged.
                    env=[k8s.core.v1.EnvVarArgs(name='EXTRA_ARGS', value=f'--port={row.port}')],
                    ports=[k8s.core.v1.ContainerPortArgs(name=row.name, container_port=row.port, protocol='TCP')],
                    # The legacy client's figures. The CPU limit holds a closed
                    # binary that serves the public beside etcd to a fifth of a
                    # core (declarative/workloads.md §1).
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '20m', 'memory': '196Mi'},
                        limits={'cpu': '200m', 'memory': '512Mi'},
                    ),
                    security_context=k8s.core.v1.SecurityContextArgs(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                    ),
                    volume_mounts=[
                        *(
                            k8s.core.v1.VolumeMountArgs(name='cache', mount_path=f'{ROOT}/{it}', sub_path=it)
                            for it in DIRECTORIES
                        ),
                        k8s.core.v1.VolumeMountArgs(
                            name='login', mount_path=LOGIN, sub_path=PORTED.keys[0], read_only=True
                        ),
                    ],
                )
            ],
            volumes=[
                k8s.core.v1.VolumeArgs(
                    name='cache',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.cache.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='login',
                    secret=k8s.core.v1.SecretVolumeSourceArgs(secret_name=PORTED.name, default_mode=0o440),
                ),
            ],
        )
