"""golinks: `go/<name>` shortlinks, with their editor at `go/edit/<name>` (rfc-008 §5.5).

The service is `kellegous/go`, reached by its route row alone, which for the
home is `go` with its bare label: a browser at home is sent from `go/<name>`
to the name and on to the link. The Gateway terminates TLS for the name and
speaks plain HTTP to the pod.

**Anyone who reaches it may edit a link**, so a row whose `internet` side asks
the portal for no gate is refused: from outside, only a signed-in user reaches
it.

**The links are a LevelDB directory in the claim**, backed up under
`STANDARD`, which one process holds locked, so a rollout stops the old pod
before it starts the new one. The pod declares nothing that holds it to a
site, so the scheduler places it wherever it fits
(`kluster.lib.k8s.UNCONSTRAINED`); its claim keeps it to the claim's node
once bound, and it writes in the claim's blockio class.

**The source URL it shows is the name a client types.** The editor writes a
link's own address with the host it is given (`--host`): the bare label where
the row's `lan` side is `Bare`, and the full name otherwise.

The image is the self-built `golinks` (`docker/golinks.Containerfile`), pinned
as `versions:image-golinks`: one static binary on `scratch`, whose image config
names no user, so the pod states a numeric one, `USER`, which owns the store in
its claim and which the claim's movers run as.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Backend, Exposed, Fronts, declared_constraints, expose
from kluster.components.apps.storage import Claim, claim
from kluster.conventions.backup import STANDARD
from kluster.conventions.routes import Bare, Exposure, Proxied, Route
from kluster.lib.k8s import ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = ('DATA', 'LABELS', 'NAME', 'PORT', 'USER', 'Golinks')

#: The application's name: its namespace, its Deployment and its Service. The
#: namespace is a backed one of `conventions.backup`, whose writer key is
#: sealed under this name.
NAME = 'golinks'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the service listens on, its own default, which its Service
#: forwards on unchanged. Above 1024, so a user that is not root binds it with
#: no capability.
PORT = 8067

#: The user and group the service runs as, which owns the store in its claim.
#: The image holds nothing but the binary, so any user that is not root serves.
USER = 65532

#: Where the claim is mounted, which the service keeps its store in.
DATA = '/data'

#: The claim of the store, in GiB, the least a claim takes: the links run to
#: kilobytes.
_DATA_SIZE = 1


def _route(exposure: Exposure) -> Route:
    """The exposure's one row, a route, refused where its `internet` side would publish the editor ungated.

    A held row is refused with any other kind: the legacy VPS holds no home-only
    name (rfc-008 §3.3).
    """
    if exposure.ports or len(exposure.routes) != 1:
        raise ValueError(
            f'golinks is reached by one route row alone, and its exposure holds {len(exposure.routes)} route rows '
            f'and {len(exposure.ports)} raw ports'
        )
    (row,) = exposure.routes
    if not isinstance(row, Route):
        raise ValueError(f'golinks is reached by a route row, and its exposure holds a {type(row).__name__}')
    if row.internet is not None and not (isinstance(row.internet, Proxied) and row.internet.gate is not None):
        raise ValueError(
            f'the route {row.host!r} reaches golinks from outside with no gate, and anyone who reaches it may edit '
            'a link'
        )
    return row


def _shown_host(row: Route) -> str:
    """The host a link's own address is written with: the bare label where the home types it, the full name otherwise."""
    return row.host if isinstance(row.lan, Bare) else f'{row.host}.{conventions.ZONE_PRIMARY}'


class Golinks(Component, pulumi_type='kluster:apps:Golinks'):
    """golinks's namespace, its claim, its Deployment and Service, and what its exposure derives.

    `exposure` is its row of the census, one route; `image` is the pin it runs,
    `writer_key` the namespace's sealed writer key as the program read it, and
    `fronts` and `zone_ids` what the program read for `expose`. The providers
    arrive in `opts`: the Kubernetes provider for the cluster's objects and the
    Cloudflare one for its records.
    """

    namespace: ApplicationNamespace
    data: Claim
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        image: ImagePin,
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        row = _route(exposure)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace)
        # Read off the namespace, so each object in it waits for it; the claim
        # takes the plain name, and waits for the namespace through opts.
        namespace = self.namespace.namespace.metadata.name

        self.data = claim(
            f'{name}-data',
            'data',
            _DATA_SIZE,
            STANDARD,
            namespace=NAME,
            uid=USER,
            writer_key=writer_key,
            constraints=self.constraints,
            opts=self.child_opts(depends_on=[self.namespace.namespace]),
        )

        health = k8s.core.v1.HTTPGetActionArgs(path='/healthz', port='http')
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # LevelDB locks its directory, so a second pod beside the first
                # would not open the store.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels=dict(LABELS), annotations=dict(self.data.pod_annotations)
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        # It speaks HTTP to its clients alone, and never Kubernetes.
                        automount_service_account_token=False,
                        **self.data.constraints.pod_fields(),
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=USER,
                            run_as_group=USER,
                            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
                        ),
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name=NAME,
                                image=str(image),
                                args=[f'--addr=:{PORT}', f'--data={DATA}', f'--host={_shown_host(row)}'],
                                ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
                                readiness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=10),
                                liveness_probe=k8s.core.v1.ProbeArgs(http_get=health, period_seconds=60),
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={'cpu': '5m', 'memory': '32Mi'},
                                    limits={'cpu': '200m', 'memory': '128Mi'},
                                ),
                                security_context=k8s.core.v1.SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    read_only_root_filesystem=True,
                                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                                ),
                                volume_mounts=[k8s.core.v1.VolumeMountArgs(name='data', mount_path=DATA)],
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name='data',
                                persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                                    claim_name=self.data.claim.metadata.name
                                ),
                            )
                        ],
                    ),
                ),
            ),
            opts=self.child_opts(),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='http', port=PORT, target_port='http', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=Backend(service=self.service.metadata.name, port=PORT),
            selector=None,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})
