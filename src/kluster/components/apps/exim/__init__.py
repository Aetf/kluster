"""The mail relay: exim, taking the cluster's mail and handing it to Gmail signed (rfc-008 §5.2).

The cluster's applications send their notifications through it: Authelia's
password resets and enrollments, and later immich's and Grafana's. A
sending component puts `conventions.MAIL_SENDER_LABEL` on its own pods, and
the pods carrying it are the only pods of another namespace that reach the
relay; exim itself relays for the cluster's pod ranges, in both
families, and for no other address. It relays every message to Gmail's
submission port under the relay's account. It serves no certificate, so it
offers no STARTTLS, and its senders speak to it in the clear (`HOST`,
`PORT`).

**The DKIM key is carried, not minted** (declarative/workloads.md §5). Every
mail zone publishes the public half under the selector `k8s`
(`conventions.DKIM_K8S`), so the relay signs with the private half the legacy
relay signs with, the sealed value `conventions.sealed.DKIM_EXIM`, mounted
where the configuration reads `tls.key`. Nothing here issues a key: a
certificate request would mint a fresh one that no zone publishes. Gmail
rewrites a From it does not recognize as the account or one of its send-as
aliases, and From is a signed header, so a sender sends as such an alias or
its signature fails all the same.

**The SMTP credential is ported as it is**: the legacy Secret's ciphertext,
sealed namespace-wide to `mail-system`, which the stack's configuration holds
under the legacy Secret's name (`SMTP`). That is why the namespace is the
legacy relay's, which the DKIM key's row names too.

The configuration is `templates/exim.conf.j2`, rendered with the pod ranges,
the primary zone, the smarthost and the DKIM selector and key path. It runs on
the worker, the architecture the pinned image is built for (rfc-008 §5.2),
and holds no data: a queued message lives in the spool, an `emptyDir`, until
it is delivered. The image's user is a name, `exim`, so the pod states its
number (rfc-008 §10.2); exim starts its daemon for an administrative user
alone, and that one is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib import templates
from kluster.lib.k8s import WORKER_POD_AFFINITY, ApplicationNamespace, PortedSecret, application_namespace, sealed_value
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'CONFIGURATION',
    'DKIM_SELECTOR',
    'EXIM_GROUP',
    'EXIM_USER',
    'HOST',
    'LABELS',
    'NAME',
    'NAMESPACE',
    'PORT',
    'SERVICE',
    'SMTP',
    'Exim',
    'relay_configuration',
)

#: The application's name: its Deployment and its configuration.
NAME = 'exim'

#: The legacy relay's namespace, which the ported credential is sealed to and
#: the DKIM key's row names (declarative/workloads.md §5).
NAMESPACE = conventions.sealed.DKIM_EXIM.namespace

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the daemon listens on, which the Service forwards on unchanged.
#: Above 1024, so a user that is not root binds it with no capability.
PORT = 8025

#: The Service the cluster's senders name, and the name they reach it at.
SERVICE = 'smtp'
HOST = f'{SERVICE}.{NAMESPACE}.svc'

#: The user and group the image's packages create for exim, by number: an
#: administrative user, which is who exim starts its daemon for.
EXIM_USER = 100
EXIM_GROUP = 101

#: Where the relay hands every message: Gmail's submission port, in exim's
#: `host::port` form.
SMARTHOST = 'smtp.gmail.com::587'

#: The selector every mail zone publishes the relay's public key under, as
#: `<selector>._domainkey` (`components.dns.base`).
DKIM_SELECTOR = 'k8s'

#: Each data key of the ported credential, and the environment variable it
#: reaches exim under, which the configuration's authenticators read: the
#: relay's Gmail account, then its app password.
_SMTP_VARIABLES: Mapping[str, str] = {'smtp_user': 'SMTP_USERNAME', 'smtp_pass': 'SMTP_PASSWORD'}

#: The legacy Secret whose ciphertext is ported as it is: the program reads
#: it with `kluster.lib.k8s.sealed_ciphertext` (declarative/workloads.md §5).
SMTP = PortedSecret(name='exim', namespace=NAMESPACE, keys=tuple(_SMTP_VARIABLES))

#: Where the configuration is mounted: the path exim reads by default.
CONFIGURATION = '/etc/exim/exim.conf'

#: Where the DKIM key's Secret is mounted, and the file in it the
#: configuration names.
_DKIM_DIRECTORY = '/etc/exim/dkim'
(_DKIM_FILE,) = conventions.sealed.DKIM_EXIM.keys

#: The queue, which exim writes; the root filesystem is read-only.
_SPOOL = '/var/spool/exim'

#: The file the configuration lands under in its ConfigMap.
_CONFIG_FILE = 'exim.conf'


@dataclass(frozen=True)
class _Configuration:
    """What `templates/exim.conf.j2` reads."""

    domain: str
    relay_from: tuple[str, ...]
    port: int
    smarthost: str
    selector: str
    dkim_key: str
    account: str
    password: str


def relay_configuration() -> str:
    """exim's configuration, rendered.

    It relays for the cluster's pod ranges, both families, and refuses every
    other client. It signs as the primary zone, which is the name it greets
    the smarthost with too, under `DKIM_SELECTOR`, with the key mounted from
    the sealed value.
    """
    return templates.render(
        'kluster.components.apps.exim',
        'templates/exim.conf.j2',
        _Configuration(
            domain=conventions.ZONE_PRIMARY,
            relay_from=(str(conventions.POD_CIDR_V4), str(conventions.POD_CIDR_V6)),
            port=PORT,
            smarthost=SMARTHOST,
            selector=DKIM_SELECTOR,
            dkim_key=f'{_DKIM_DIRECTORY}/{_DKIM_FILE}',
            account=_SMTP_VARIABLES['smtp_user'],
            password=_SMTP_VARIABLES['smtp_pass'],
        ),
    )


def _container(image: ImagePin, smtp_secret: pulumi.Input[str]) -> k8s.core.v1.ContainerArgs:
    # Probed at the port: a connection is accepted once the daemon listens.
    probe = k8s.core.v1.ProbeArgs(tcp_socket=k8s.core.v1.TCPSocketActionArgs(port='smtp'), period_seconds=10)
    return k8s.core.v1.ContainerArgs(
        name=NAME,
        image=str(image),
        ports=[k8s.core.v1.ContainerPortArgs(name='smtp', container_port=PORT, protocol='TCP')],
        env=[
            k8s.core.v1.EnvVarArgs(
                name=variable,
                value_from=k8s.core.v1.EnvVarSourceArgs(
                    secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(name=smtp_secret, key=key)
                ),
            )
            for key, variable in _SMTP_VARIABLES.items()
        ],
        readiness_probe=probe,
        resources=k8s.core.v1.ResourceRequirementsArgs(
            # exim forks a process per connection, and the spool's page cache
            # counts against the memory limit, so the limit is sized for a
            # handful of concurrent deliveries; a cap of a few millicores
            # delays its greeting past a client's connect timeout.
            requests={'cpu': '5m', 'memory': '32Mi'},
            limits={'cpu': '500m', 'memory': '64Mi'},
        ),
        security_context=k8s.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
        ),
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name='config', mount_path=CONFIGURATION, sub_path=_CONFIG_FILE, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='dkim', mount_path=_DKIM_DIRECTORY, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='spool', mount_path=_SPOOL),
        ],
    )


class Exim(Component, pulumi_type='kluster:apps:Exim'):
    """The relay's namespace, its two Secrets, its configuration, its Deployment and Service, and who may send.

    `image` is the pin it runs; `smtp` and `dkim` are the ciphertexts
    `kluster.lib.k8s.sealed_ciphertext` reads for the ported credential,
    `SMTP`, and for the DKIM key, `conventions.sealed.DKIM_EXIM`. Who may
    send is the pods carrying `conventions.MAIL_SENDER_LABEL`, which each
    sending component puts on its own. The Kubernetes provider arrives in `opts`. It has no exposure:
    its senders are in the cluster.
    """

    namespace: ApplicationNamespace
    smtp: crds.bitnami.v1alpha1.SealedSecret
    dkim: crds.bitnami.v1alpha1.SealedSecret
    config: k8s.core.v1.ConfigMap
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    senders: crds.cilium.v2.CiliumNetworkPolicy

    def __init__(
        self,
        name: str,
        *,
        image: ImagePin,
        smtp: Mapping[str, pulumi.Input[str]],
        dkim: Mapping[str, pulumi.Input[str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAMESPACE, opts=self.child_opts())
        # Read off the namespace, so each object in it waits for it.
        namespace = self.namespace.namespace.metadata.name

        # Each value names its namespace, so its object waits for it here.
        behind = self.child_opts(depends_on=[self.namespace.namespace])
        self.smtp = sealed_value(f'{name}-smtp', SMTP, ciphertext=smtp, opts=behind)
        self.dkim = sealed_value(f'{name}-dkim', conventions.sealed.DKIM_EXIM, ciphertext=dkim, opts=behind)
        self.config = k8s.core.v1.ConfigMap(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            data={_CONFIG_FILE: relay_configuration()},
            opts=self.child_opts(),
        )
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=NAME,
                namespace=namespace,
                # Reloader rolls the pods when the configuration or a Secret
                # they read changes; exim reads both only at its start.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(LABELS)),
                    spec=k8s.core.v1.PodSpecArgs(
                        # It speaks SMTP alone, and never Kubernetes.
                        automount_service_account_token=False,
                        affinity=WORKER_POD_AFFINITY,
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=EXIM_USER,
                            run_as_group=EXIM_GROUP,
                            # The DKIM key's file is the group's to read alone.
                            fs_group=EXIM_GROUP,
                            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
                        ),
                        containers=[_container(image, self.smtp.metadata.name)],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name='config', config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=NAME)
                            ),
                            k8s.core.v1.VolumeArgs(
                                name='dkim',
                                secret=k8s.core.v1.SecretVolumeSourceArgs(
                                    secret_name=conventions.sealed.DKIM_EXIM.name, default_mode=0o440
                                ),
                            ),
                            k8s.core.v1.VolumeArgs(name='spool', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs()),
                        ],
                    ),
                ),
            ),
            opts=self.child_opts(),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=SERVICE, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='smtp', port=PORT, target_port='smtp', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        self.senders = _senders(name, namespace=namespace, opts=self.child_opts())

        self.register_outputs({})


def _senders(
    name: str, *, namespace: pulumi.Input[str], opts: pulumi.ResourceOptions
) -> crds.cilium.v2.CiliumNetworkPolicy:
    """The policy admitting the pods carrying `conventions.MAIL_SENDER_LABEL`, from any namespace, to SMTP alone.

    The namespace's default-deny admits no other namespace's pods. A peer
    selector that names no namespace selects in the policy's own, so the
    peer asks for the namespace label to exist, which selects in every
    namespace
    ([`kubernetes.rst` L216-219](https://github.com/cilium/cilium/blob/v1.20.1/Documentation/security/policy/kubernetes.rst#L216-L219)).
    exim relays for any address of the pod ranges; this is what keeps a pod
    that is not a sender from sending as the relay's account.
    """
    return crds.cilium.v2.CiliumNetworkPolicy(
        f'{name}-senders',
        metadata=k8s.meta.v1.ObjectMetaArgs(name='senders', namespace=namespace),
        spec=crds.cilium.v2.CiliumNetworkPolicySpecArgs(
            description="Admit the pods labelled as mail senders, from any namespace, to the relay's SMTP port alone.",
            endpoint_selector=crds.cilium.v2.CiliumNetworkPolicySpecEndpointSelectorArgs(match_labels=dict(LABELS)),
            ingress=[
                crds.cilium.v2.CiliumNetworkPolicySpecIngressArgs(
                    from_endpoints=[
                        crds.cilium.v2.CiliumNetworkPolicySpecIngressFromEndpointsArgs(
                            match_labels=dict(conventions.MAIL_SENDER_LABEL),
                            match_expressions=[
                                crds.cilium.v2.CiliumNetworkPolicySpecIngressFromEndpointsMatchExpressionsArgs(
                                    key='k8s:io.kubernetes.pod.namespace', operator='Exists'
                                )
                            ],
                        )
                    ],
                    to_ports=[
                        crds.cilium.v2.CiliumNetworkPolicySpecIngressToPortsArgs(
                            ports=[
                                crds.cilium.v2.CiliumNetworkPolicySpecIngressToPortsPortsArgs(
                                    port=str(PORT), protocol='TCP'
                                )
                            ]
                        )
                    ],
                )
            ],
        ),
        opts=opts,
    )
