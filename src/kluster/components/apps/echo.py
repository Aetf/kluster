"""The echo server: the front door's exit criterion, and nothing else (rfc-007 §9.2).

A small HTTP server that answers every request with the request's headers and
the peer address it saw, so one request proves the whole chain once: the
public record, the balancer's listener, the node's frontend, the Gateway's
Envoy, the route, the certificate and the pod. Its row,
`conventions.routes.EXPOSURES['echo']`, is published direct, so the request
crosses the balancer with the client's own address rather than the proxy's,
and the echoed headers show which address arrived.

The image is `traefik/whoami`, pinned as `versions:image-echo`. Its image
config names no user, so the pod states a numeric one that is not root, as
the namespace's `restricted` level requires, and the server listens on a port
above 1024, which needs no capability. It writes nothing, so its root
filesystem is read-only. It declares nothing that holds it to a site, so it
is a free pod (`kluster.lib.k8s.UNCONSTRAINED`).

The component, its row, its pin and its tests go at Wave A1's land, when the
first public application takes over as the front door's proof (rfc-008 slice
28): a name that resolves is a promise (declarative/dns.md §2).
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_kubernetes as k8s

from kluster.components.apps import Backend, Exposed, Fronts, declared_constraints, expose
from kluster.conventions.routes import Exposure
from kluster.lib.k8s import ApplicationNamespace, application_namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = ('ECHO_USER', 'LABELS', 'NAME', 'PORT', 'Echo')

#: The application's name: its namespace, its Deployment and its Service.
NAME = 'echo'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the server listens on, which its Service forwards on unchanged.
#: Above 1024, so a user that is not root binds it with no capability.
PORT = 8080

#: The user and group the server runs as: `nobody`. The image is built from
#: scratch and owns no file to anyone, so any user that is not root serves.
ECHO_USER = 65534


def _container(image: ImagePin) -> k8s.core.v1.ContainerArgs:
    # Probed at `/`: the server's `/health` takes a POST that sets the status
    # it answers with, and the route forwards one from anybody.
    probe = k8s.core.v1.ProbeArgs(http_get=k8s.core.v1.HTTPGetActionArgs(path='/', port='http'), period_seconds=10)
    return k8s.core.v1.ContainerArgs(
        name=NAME,
        image=str(image),
        args=['--port', str(PORT)],
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
        readiness_probe=probe,
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '10m', 'memory': '16Mi'},
            limits={'cpu': '50m', 'memory': '32Mi'},
        ),
        security_context=k8s.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
        ),
    )


class Echo(Component, pulumi_type='kluster:apps:Echo'):
    """The echo server's namespace, its Deployment and Service, and what its exposure derives.

    `exposure` is its row of the census, `image` the pin it runs, and
    `fronts` and `zone_ids` what the program read for `expose`. The
    providers arrive in `opts`: the Kubernetes provider for the cluster's
    objects and the Cloudflare one for its record.
    """

    namespace: ApplicationNamespace
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        image: ImagePin,
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAME, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace)
        # Read off the namespace, so each object in it waits for it.
        namespace = self.namespace.namespace.metadata.name

        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(LABELS)),
                    spec=k8s.core.v1.PodSpecArgs(
                        # It speaks HTTP to its clients alone, and never Kubernetes.
                        automount_service_account_token=False,
                        **self.constraints.pod_fields(),
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=ECHO_USER,
                            run_as_group=ECHO_USER,
                            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
                        ),
                        containers=[_container(image)],
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
