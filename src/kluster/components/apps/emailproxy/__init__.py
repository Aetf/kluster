"""emailproxy: IMAP for a client with no OAuth, over Gmail's XOAUTH2 (rfc-008 §5.5).

`email-oauth2-proxy` takes a client's IMAP login by password and signs in
to Gmail's IMAP for it with an OAuth token, so Home Assistant's mail integration
and the host's jobs watch a mailbox with no app password. It is reached at a
raw port on TCP, the `imap` row at the LAN VIP, whose allow admits clients on
that port alone.

**It serves TLS itself**, since Home Assistant's integration speaks IMAP over
TLS and nothing else, on a certificate from a self-signed issuer of its own
namespace for each name the port is dialed at (`dialed_names`). Its clients do
not verify the certificate, and a name of the home's is not one to publish in
certificate logs, so no public issuer is asked.

**The configuration is the ported Secret's**, `PORTED`: the legacy Secret's
ciphertext, sealed namespace-wide to the legacy proxy's namespace, which the
stack's configuration holds under the Secret's name and its data keys
(declarative/workloads.md §5). The account and the OAuth client are sealed; the
rest of the file is the program's, rendered from `templates/emailproxy.config.j2`
around the sealed keys (`kluster.lib.k8s.sealed_value`). That is why the
namespace is the legacy proxy's.

**The token cache is the claim's**, backed up under `STANDARD`, so the account
needs no fresh consent after a move or a restore. The proxy encrypts each token
with the password the client presents. One process writes it, so a rollout
stops the old pod before it starts the new one. The pod runs on the worker: its
clients are at home, and a cloud node's resources are budgeted to the control
plane and the internet-pool workloads (cluster/nodes.md §1.1).

**Authorizing the account afresh**, when its refresh token is revoked, is a
browser's. The proxy serves its authorization page in the pod on
`AUTHORIZATION_PORT`, at the loopback address the OAuth client allows, and the
browser is redirected to `localhost` on that same port: a port-forward to the
pod binds that port locally too, or the redirect reaches nothing. A client then
signs in once, and the pod's log gives the URL to follow.

**It has no probe.** The proxy opens a connection to Gmail for every connection
it accepts, a probe's included, and it exits when it cannot serve its
configuration.

The image is the self-built `emailproxy` (`docker/emailproxy.Containerfile`),
pinned as `versions:image-emailproxy`, whose entrypoint reads the configuration
at `CONFIG` and keeps its cache at `CACHE`. Its image config names no user, so
the pod states a numeric one, `USER`, which owns the cache in its claim and
which the claim's movers run as.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster.components.apps import Exposed, Fronts, dialed_names, expose
from kluster.components.apps.storage import Claim, claim
from kluster.conventions.backup import STANDARD
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Exposure, Port
from kluster.lib import templates
from kluster.lib.k8s import WORKER_POD_AFFINITY, ApplicationNamespace, PortedSecret, application_namespace, sealed_value
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'AUTHORIZATION_PORT',
    'CACHE',
    'CONFIG',
    'LABELS',
    'NAME',
    'NAMESPACE',
    'PORTED',
    'TLS',
    'TLS_SECRET',
    'USER',
    'EmailProxy',
)

#: The application's name: its Deployment.
NAME = 'emailproxy'

#: The legacy proxy's namespace, which the ported configuration is sealed to,
#: and a backed one of `conventions.backup`, whose writer key is sealed under
#: this name.
NAMESPACE = 'emailproxy'

#: The labels its pods carry and its raw port's Services select.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The legacy Secret whose ciphertext is ported as it is: the mailbox's
#: address, and the OAuth client's identifier and secret. The program reads it
#: with `kluster.lib.k8s.sealed_ciphertext`.
PORTED = PortedSecret(name='emailproxy', namespace=NAMESPACE, keys=('account', 'client_id', 'client_secret'))

#: The configuration file and the token cache, where the image's entrypoint
#: names them.
CONFIG = '/config/emailproxy.config'
CACHE = '/data/credstore.config'

#: The directory the certificate's Secret is mounted at.
TLS = '/tls'

#: The Secret the certificate is issued into.
TLS_SECRET = 'emailproxy-tls'

#: The port the authorization page is served on in the pod, which the OAuth
#: client's redirect names on the loopback address.
AUTHORIZATION_PORT = 8080

#: The user and group the proxy runs as, which owns the cache in its claim. The
#: proxy writes nothing of its image's, so any user that is not root serves.
USER = 65532

#: The self-signed issuer the certificate is from, in the namespace.
_ISSUER = 'self-signed'

#: The claim of the cache, in GiB, the least a claim takes: the cache runs to
#: kilobytes.
_CACHE_SIZE = 1

_TEMPLATE = 'templates/emailproxy.config.j2'


@dataclass(frozen=True)
class _ConfigParams:
    """What the configuration's program-held part is rendered with."""

    port: int
    certificate: str
    key: str
    authorization_port: int


def _port(exposure: Exposure) -> Port:
    """The exposure's one row, a raw port on TCP, which IMAP is carried on."""
    if exposure.routes or len(exposure.ports) != 1:
        raise ValueError(
            f'emailproxy is reached at one raw port alone, and its exposure holds {len(exposure.ports)} raw ports '
            f'and {len(exposure.routes)} route rows'
        )
    (row,) = exposure.ports
    if row.transports is not Transports.TCP:
        raise ValueError(
            f'emailproxy serves IMAP over TCP alone, and its port {row.name!r} is on {row.transports.value}'
        )
    return row


def _certified(row: Port) -> tuple[str, ...]:
    """The names the certificate is issued for: each name the port is dialed at, refused where there is none."""
    names = dialed_names(row)
    if not names:
        raise ValueError(
            f'the port {row.name!r} is dialed at no name, and the certificate emailproxy serves on it names one'
        )
    return names


def _ported(config: Mapping[str, pulumi.Input[str]]) -> Mapping[str, pulumi.Input[str]]:
    """The ciphertext, refused unless it holds each key `PORTED` seals and no other: a missing one would render empty."""
    if sorted(config) != sorted(PORTED.keys):
        raise ValueError(
            f'the ported {PORTED.name!r} Secret carries {sorted(PORTED.keys)}, and the ciphertext handed '
            f'holds {sorted(config)}'
        )
    return config


class EmailProxy(Component, pulumi_type='kluster:apps:EmailProxy'):
    """The proxy's namespace, its configuration, its certificate, its claim, its Deployment, and its raw port.

    `exposure` is its row of the census, one raw port; `image` is the pin it
    runs; `config` the ciphertext of each key of `PORTED`, as the program read
    it with `kluster.lib.k8s.sealed_ciphertext`; `writer_key` the namespace's
    sealed writer key as the program read it; and `fronts` and `zone_ids` what
    the program read for `expose`. The providers arrive in `opts`: the
    Kubernetes provider for the cluster's objects and the Cloudflare one for
    its records.
    """

    namespace: ApplicationNamespace
    config: crds.bitnami.v1alpha1.SealedSecret
    issuer: crds.cert_manager.v1.Issuer
    certificate: crds.cert_manager.v1.Certificate
    cache: Claim
    deployment: k8s.apps.v1.Deployment
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        image: ImagePin,
        config: Mapping[str, pulumi.Input[str]],
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        row = _port(exposure)
        names = _certified(row)
        ciphertext = _ported(config)
        config_file = PurePosixPath(CONFIG)
        rendered = templates.render(
            __name__,
            _TEMPLATE,
            _ConfigParams(
                port=row.port,
                certificate=f'{TLS}/tls.crt',
                key=f'{TLS}/tls.key',
                authorization_port=AUTHORIZATION_PORT,
            ),
        )
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAMESPACE, opts=self.child_opts())
        # Read off the namespace, so each object in it waits for it; the claim
        # and the sealed configuration take the plain name, and wait for the
        # namespace through opts.
        namespace = self.namespace.namespace.metadata.name
        in_namespace = self.child_opts(depends_on=[self.namespace.namespace])

        self.config = sealed_value(
            f'{name}-config', PORTED, ciphertext=ciphertext, rendered={config_file.name: rendered}, opts=in_namespace
        )
        self.issuer = crds.cert_manager.v1.Issuer(
            f'{name}-issuer',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=_ISSUER, namespace=namespace),
            spec=crds.cert_manager.v1.IssuerSpecArgs(self_signed=crds.cert_manager.v1.IssuerSpecSelfSignedArgs()),
            opts=self.child_opts(),
        )
        self.certificate = crds.cert_manager.v1.Certificate(
            f'{name}-certificate',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            spec=crds.cert_manager.v1.CertificateSpecArgs(
                # A self-signed certificate's issuer is its own subject, which
                # the names alone leave empty.
                common_name=names[0],
                dns_names=list(names),
                secret_name=TLS_SECRET,
                issuer_ref=crds.cert_manager.v1.CertificateSpecIssuerRefArgs(
                    name=_ISSUER, kind='Issuer', group='cert-manager.io'
                ),
            ),
            opts=self.child_opts(depends_on=[self.issuer]),
        )
        self.cache = claim(
            f'{name}-cache',
            'cache',
            _CACHE_SIZE,
            STANDARD,
            namespace=NAMESPACE,
            uid=USER,
            writer_key=writer_key,
            placement=WORKER_POD_AFFINITY,
            opts=in_namespace,
        )

        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=NAME,
                namespace=namespace,
                # Reloader rolls the pod when its configuration or its
                # certificate changes, a renewal included; the proxy reads
                # both only at its start.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # The cache is one process's file, rewritten whole.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(LABELS)),
                    spec=self._pod(row, image=image, config_file=config_file),
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

    def _pod(self, row: Port, *, image: ImagePin, config_file: PurePosixPath) -> k8s.core.v1.PodSpecArgs:
        cache_file = PurePosixPath(CACHE)
        return k8s.core.v1.PodSpecArgs(
            # It speaks IMAP to its clients and to Gmail alone, and never Kubernetes.
            automount_service_account_token=False,
            affinity=self.cache.placement,
            security_context=k8s.core.v1.PodSecurityContextArgs(
                run_as_non_root=True,
                run_as_user=USER,
                run_as_group=USER,
                # The Secrets' files are the group's to read alone.
                fs_group=USER,
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
            ),
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=NAME,
                    image=str(image),
                    ports=[
                        k8s.core.v1.ContainerPortArgs(name='imap', container_port=row.port, protocol='TCP'),
                        k8s.core.v1.ContainerPortArgs(
                            name='authorization', container_port=AUTHORIZATION_PORT, protocol='TCP'
                        ),
                    ],
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={'cpu': '5m', 'memory': '48Mi'},
                        limits={'cpu': '200m', 'memory': '128Mi'},
                    ),
                    security_context=k8s.core.v1.SecurityContextArgs(
                        allow_privilege_escalation=False,
                        read_only_root_filesystem=True,
                        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
                    ),
                    volume_mounts=[
                        k8s.core.v1.VolumeMountArgs(name='config', mount_path=str(config_file.parent), read_only=True),
                        k8s.core.v1.VolumeMountArgs(name='cache', mount_path=str(cache_file.parent)),
                        k8s.core.v1.VolumeMountArgs(name='tls', mount_path=TLS, read_only=True),
                    ],
                )
            ],
            volumes=[
                # The rendered file alone: the sealed keys stay out of the
                # container's files.
                k8s.core.v1.VolumeArgs(
                    name='config',
                    secret=k8s.core.v1.SecretVolumeSourceArgs(
                        secret_name=PORTED.name,
                        items=[k8s.core.v1.KeyToPathArgs(key=config_file.name, path=config_file.name)],
                        default_mode=0o440,
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='cache',
                    persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                        claim_name=self.cache.claim.metadata.name
                    ),
                ),
                k8s.core.v1.VolumeArgs(
                    name='tls',
                    secret=k8s.core.v1.SecretVolumeSourceArgs(
                        secret_name=TLS_SECRET,
                        items=[k8s.core.v1.KeyToPathArgs(key=key, path=key) for key in ('tls.crt', 'tls.key')],
                        default_mode=0o440,
                    ),
                ),
            ],
        )
