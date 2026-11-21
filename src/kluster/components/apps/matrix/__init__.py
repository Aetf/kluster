"""The Matrix homeserver: continuwuity, signing its users in through the portal (rfc-008 §5.3).

One server, with its database and media store on one claim, `PRECIOUS` and
protected. It declares nothing that holds it to a site, so the scheduler
places it (`kluster.lib.k8s.UNCONSTRAINED`); the claim keeps it to the claim's
node once bound, and it writes in the claim's blockio class. An account comes into being only through the
portal's sign-in: continuwuity provisions one for whoever the portal lets
through, so the client's policy in `conventions.sso.CLIENTS` is the one gate on
who has one. Its route carries no gate, since federation and the sign-in flow
reach the server without the portal's session, and the component refuses an
exposure that gates it.

**The server name is the apex** (`SERVER_NAME`), and the homeserver answers at
its client's host. The apex's `/.well-known/matrix/server` and
`/.well-known/matrix/client` delegate to it; they are the blog's to serve
(declarative/workloads.md §4), and say what `delegation` returns. Federation
therefore arrives on 443, through the same route as a Matrix client.

**The client is the census's row**, handed in by the program. The scopes the
server asks for are the row's. What the row has to agree with is the server's
own: the address it sends a browser back to, which continuwuity builds from
its base URL and holds no setting for; how it authenticates at the token
endpoint; and the claim it takes the localpart from. A row that disagrees on
any of them is refused. The client's identifier and secret are the legacy
Secret's ciphertext, ported as it is (`OIDC`), sealed namespace-wide to
`NAMESPACE`, which is why the namespace is the legacy one: the identifier
reaches the server as an environment variable, and the secret as a file
(`client_secret_file`). Nothing secret is in the configuration, which is a
ConfigMap.

**The server does not start while the portal's discovery document is out of
reach.** It fetches it from `issuer` at every start and exits when the fetch
fails, so a restart during a portal outage keeps the homeserver, and its
federation, down until the portal answers and the kubelet's back-off next
retries it.

The image is built from scratch and names no user, so the pod states a
numeric one, as `restricted` requires (rfc-008 §10.2). It writes nothing
outside its claim, so its root filesystem is read-only. It sends no mail.

**The land streams the legacy claim in through a receiver** (`receiver`), a
migration layer of the component's own, described where it is declared below.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Backend, Exposed, Fronts, declared_constraints, expose, issuer_aliases
from kluster.components.apps.storage import Claim, backed, claim
from kluster.conventions.backup import PRECIOUS
from kluster.conventions.routes import Exposure, Held, Proxied, Route
from kluster.lib import templates
from kluster.lib.k8s import ApplicationNamespace, PortedSecret, application_namespace, sealed_value
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'ADMINS',
    'CONFIGURATION',
    'DATA',
    'LABELS',
    'MATRIX_USER',
    'NAME',
    'NAMESPACE',
    'OIDC',
    'PORT',
    'RECEIVE',
    'REDIRECT_PATH',
    'SERVER_NAME',
    'Matrix',
    'delegation',
    'server_configuration',
)

#: The application's name: its Deployment, its Service and its configuration.
NAME = 'matrix'

#: The legacy homeserver's namespace, which the ported client Secret is sealed
#: to (declarative/workloads.md §5) and whose backups
#: `conventions.backup.BACKED_NAMESPACES` names.
NAMESPACE = 'matrix'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the server listens on, which its Service forwards on unchanged.
#: Above 1024, so a user that is not root binds it with no capability.
PORT = 8008

#: The user and group the server runs as, and so the owner of everything on
#: its claim, which the claim's movers read as. The image is built from
#: scratch and owns no file to anyone, so any user that is not root serves.
MATRIX_USER = 1000

#: The domain in every user ID and every event the server has signed, which
#: federated servers have stored: changing it is a new server, not a rename.
SERVER_NAME = conventions.ZONE_PRIMARY

#: The localparts of the server's administrators, each the username the
#: portal knows, since the localpart is read from its claim.
ADMINS = ('aetf',)

#: The legacy Secret whose ciphertext is ported as it is: the client's
#: identifier, then its secret. The program reads it with
#: `kluster.lib.k8s.sealed_ciphertext` (declarative/workloads.md §5).
OIDC = PortedSecret(name='matrix', namespace=NAMESPACE, keys=('oidc_client_id', 'oidc_client_secret'))
_OIDC_ID, _OIDC_SECRET_KEY = OIDC.keys

#: The path continuwuity sends a browser back to after the portal's sign-in,
#: under its base URL (`src/service/oidc/mod.rs` upstream, the route prefix
#: `/_continuwuity` and `/oidc/complete`). The server holds no setting for it.
REDIRECT_PATH = '/_continuwuity/oidc/complete'

#: How continuwuity authenticates at the token endpoint: HTTP Basic, the
#: default of its OpenID Connect library, which `src/service/oidc/mod.rs`
#: upstream never overrides.
_TOKEN_ENDPOINT_AUTH = conventions.sso.TokenEndpointAuth.BASIC

#: The claims the server reads from the ID token. The localpart fails a
#: sign-in where it is missing, since the server asks for none; the email and
#: the display name are taken where they are present.
_LOCALPART_CLAIM = conventions.sso.Claim.PREFERRED_USERNAME
_EMAIL_CLAIM = conventions.sso.Claim.EMAIL
_DISPLAYNAME_CLAIM = conventions.sso.Claim.NAME

#: Where the configuration is mounted, which `CONTINUWUITY_CONFIG` names.
CONFIGURATION = '/etc/continuwuity/continuwuity.toml'

#: Where the client's secret is mounted, the file the configuration names.
_SECRET_DIRECTORY = '/etc/continuwuity/oidc'

#: Where the claim is mounted: the database and the media store.
DATA = '/data'

#: The claim's base name, and its size in GiB. The database and the media
#: store share it, and the media store keeps a copy of what federated rooms
#: post, which is what grows.
_CLAIM = 'data'
_CLAIM_GIB = 20

#: The file the configuration lands under in its ConfigMap.
_CONFIG_FILE = 'continuwuity.toml'

#: The environment variable the server reads the client's identifier from, as
#: its configuration's `global.oauth.oidc.client_id` (figment's `Env`, which
#: continuwuity reads `CONTINUWUITY_` variables with, splits a name at `__`).
#: The value is quoted, so figment reads it as a string whatever it holds.
_CLIENT_ID_VARIABLE = 'CONTINUWUITY_OAUTH__OIDC__CLIENT_ID'
_CLIENT_ID_RAW = 'OIDC_CLIENT_ID'


@dataclass(frozen=True)
class _Configuration:
    """What `templates/continuwuity.toml.j2` reads."""

    server_name: str
    port: int
    database: str
    admins: tuple[str, ...]
    base_url: str
    federation: str
    issuer: str
    secret_file: str
    scopes: tuple[str, ...]
    localpart_claim: str
    email_claim: str
    displayname_claim: str


def _homeserver(client: conventions.sso.Client) -> str:
    """The name the homeserver answers at: its client's host, in the client's zone."""
    return f'{client.host}.{client.zone}'


def delegation(client: conventions.sso.Client) -> Mapping[str, str]:
    """The apex's two delegation documents, by their path under the apex, as the blog serves them.

    `server` names the homeserver on 443, where federation arrives through
    the same route a Matrix client takes, and `client` names it to a Matrix
    client. Both are JSON, which a browser's Matrix client reads across
    origins, so they are served with `Access-Control-Allow-Origin: *` (the
    Matrix client-server API, "Server Discovery").
    """
    homeserver = _homeserver(client)
    return {
        '/.well-known/matrix/server': json.dumps({'m.server': f'{homeserver}:443'}),
        '/.well-known/matrix/client': json.dumps({'m.homeserver': {'base_url': f'https://{homeserver}'}}),
    }


def server_configuration(client: conventions.sso.Client, *, issuer: str) -> str:
    """continuwuity's configuration, rendered for `client`, signing in at the portal whose issuer is `issuer`.

    The scopes it asks for beside `openid`, which it always asks for, are the
    row's; the claims it reads are its own. A row the server cannot sign in
    through is refused (`_refuse_client`).
    """
    _refuse_client(client)
    homeserver = _homeserver(client)
    return templates.render(
        'kluster.components.apps.matrix',
        'templates/continuwuity.toml.j2',
        _Configuration(
            server_name=SERVER_NAME,
            port=PORT,
            database=DATA,
            admins=tuple(f'@{localpart}:{SERVER_NAME}' for localpart in ADMINS),
            base_url=f'https://{homeserver}',
            federation=f'{homeserver}:443',
            issuer=issuer,
            secret_file=f'{_SECRET_DIRECTORY}/{_OIDC_SECRET_KEY}',
            scopes=tuple(scope.value for scope in client.scopes if scope is not conventions.sso.Scope.OPENID),
            localpart_claim=_LOCALPART_CLAIM.value,
            email_claim=_EMAIL_CLAIM.value,
            displayname_claim=_DISPLAYNAME_CLAIM.value,
        ),
    )


def _refuse_client(client: conventions.sso.Client) -> None:
    """Refuse a client the server cannot sign in through.

    One that does not register the address the server sends a browser back
    to, one registered for another way of authenticating at the token
    endpoint than the server's, and one whose ID token lacks the localpart.
    """
    redirect = f'https://{_homeserver(client)}{REDIRECT_PATH}'
    if redirect not in conventions.sso.redirect_uris(client):
        raise ValueError(
            f'the client {client.name!r} does not register {redirect}, where the homeserver sends a browser back to'
        )
    if client.token_endpoint_auth is not _TOKEN_ENDPOINT_AUTH:
        raise ValueError(
            f'the client {client.name!r} authenticates by {client.token_endpoint_auth.value}, '
            f'and the homeserver sends {_TOKEN_ENDPOINT_AUTH.value}'
        )
    if _LOCALPART_CLAIM not in client.id_token:
        raise ValueError(
            f"the client {client.name!r}'s ID token does not carry {_LOCALPART_CLAIM.value}, "
            'which the homeserver takes the localpart from'
        )


def _refuse_exposure(exposure: Exposure, client: conventions.sso.Client) -> None:
    """Refuse an exposure with no ungated route at the client's host on `internet`.

    The portal sends a browser back to the client's host, and federation
    arrives there through the delegation, so the server is reached at that
    name; a gate would stop both, since neither carries the portal's session.
    """
    rows = [held.row if isinstance(held, Held) else held for held in exposure.routes]
    served = [
        row
        for row in rows
        if isinstance(row, Route) and row.host == client.host and client.zone in row.zones and row.internet is not None
    ]
    if not served:
        raise ValueError(f'the exposure has no route at {_homeserver(client)} on `internet`, where the server answers')
    if gated := [row for row in served if isinstance(row.internet, Proxied) and row.internet.gate is not None]:
        raise ValueError(
            f'the route at {_homeserver(client)} is gated by {gated[0].internet}; federation and the sign-in flow '
            'reach the server without the portal session a gate asks for'
        )


#: Who a pod of the homeserver's runs as: the server's user, which owns the
#: claim's files, and the group the client secret's file is readable by.
_POD_SECURITY = k8s.core.v1.PodSecurityContextArgs(
    run_as_non_root=True,
    run_as_user=MATRIX_USER,
    run_as_group=MATRIX_USER,
    fs_group=MATRIX_USER,
    seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
)

#: What every container of the homeserver's is held to.
_CONTAINER_SECURITY = k8s.core.v1.SecurityContextArgs(
    allow_privilege_escalation=False,
    read_only_root_filesystem=True,
    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
)


def _data_volume(claim_name: pulumi.Input[str]) -> k8s.core.v1.VolumeArgs:
    return k8s.core.v1.VolumeArgs(
        name='data',
        persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(claim_name=claim_name),
    )


def _server(
    image: ImagePin, oidc_secret: pulumi.Input[str], data: Claim, issuer: conventions.sso.Issuer
) -> k8s.core.v1.PodSpecArgs:
    """The server's pod: continuwuity, reading its configuration, the client's secret and its claim, placed as the claim records, reaching the issuer at its alias."""
    probe = k8s.core.v1.HTTPGetActionArgs(path='/_matrix/federation/v1/version', port='http')
    container = k8s.core.v1.ContainerArgs(
        name=NAME,
        image=str(image),
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
        env=[
            k8s.core.v1.EnvVarArgs(name='CONTINUWUITY_CONFIG', value=CONFIGURATION),
            k8s.core.v1.EnvVarArgs(
                name=_CLIENT_ID_RAW,
                value_from=k8s.core.v1.EnvVarSourceArgs(
                    secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(name=oidc_secret, key=_OIDC_ID)
                ),
            ),
            # The kubelet expands a variable defined above it in the list.
            k8s.core.v1.EnvVarArgs(name=_CLIENT_ID_VARIABLE, value=f'"$({_CLIENT_ID_RAW})"'),
        ],
        # A first start after an upgrade can migrate and compact the database
        # for minutes before the server answers.
        startup_probe=k8s.core.v1.ProbeArgs(http_get=probe, period_seconds=5, failure_threshold=120),
        readiness_probe=k8s.core.v1.ProbeArgs(http_get=probe, period_seconds=10),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            # Its working set runs at 0.13 GiB and peaks at 0.19 over 30 days
            # (nodes.md §1.1); the limit leaves the database's caches room to
            # grow with it. Its CPU is a few millicores, and a start that
            # migrates the database is what the limit slows.
            requests={'cpu': '10m', 'memory': '160Mi'},
            limits={'cpu': '100m', 'memory': '512Mi'},
        ),
        security_context=_CONTAINER_SECURITY,
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name='config', mount_path=CONFIGURATION, sub_path=_CONFIG_FILE, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='oidc', mount_path=_SECRET_DIRECTORY, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='data', mount_path=DATA),
        ],
    )
    return k8s.core.v1.PodSpecArgs(
        # It speaks Matrix and OpenID Connect, and never Kubernetes.
        automount_service_account_token=False,
        enable_service_links=False,
        **data.constraints.pod_fields(),
        host_aliases=issuer_aliases(issuer),
        security_context=_POD_SECURITY,
        containers=[container],
        volumes=[
            k8s.core.v1.VolumeArgs(name='config', config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=NAME)),
            k8s.core.v1.VolumeArgs(
                name='oidc',
                secret=k8s.core.v1.SecretVolumeSourceArgs(
                    secret_name=oidc_secret,
                    items=[k8s.core.v1.KeyToPathArgs(key=_OIDC_SECRET_KEY, path=_OIDC_SECRET_KEY)],
                    # The file is the pod's group's to read alone.
                    default_mode=0o440,
                ),
            ),
            _data_volume(data.claim.metadata.name),
        ],
    )


# -- the receiving layer ---------------------------------------------------------
#
# The land streams the legacy server's claim into this one, after the legacy
# server has stopped (rfc-008 §5.3), through a pod that mounts the claim. The
# stream's two ends, from the legacy claim's directory to this claim's mount:
#
#     tar -c -C <legacy claim> . |
#         kubectl exec -i -n matrix deploy/matrix -- tar -x --strip-components=1 -C /data
#
# The receiving end is `RECEIVE`. `--strip-components=1` drops the stream's `./`, which is root's: the claim's
# own directory is root's too, and the receiver, which is not root, can set
# neither its time nor its mode, so extracting `./` fails the stream. The
# server's image has no shell and no `tar`, and the server started on an empty
# claim creates a database of its own there, so under `receiver` the
# Deployment runs a receiver in the server's place: an idle container of an
# image that has `tar`, mounting the claim where the server does. A second
# merge, without `receiver`, starts the server on what was streamed. The
# receiver is a running pod with the server's constraints, never zero replicas,
# since the land execs the stream into it. It runs as the server's user, so
# what it extracts is the server's to write, whoever owned it before.
#
# The layer is everything in this module and in `tests/test_apps_matrix.py`
# that names the receiver. Today that is the `receiver` parameter of `Matrix`,
# `_receiver`, `_RECEIVER_COMMAND` and `RECEIVE` here, with its entry in
# `__all__`, and the cases, fixture and
# constants under "the receiving layer" there. It goes with Wave A2's cut, once
# the server has started on the streamed claim, since nothing streams into it
# after that (rfc-008 §10.3, slice 32).

#: What the receiver runs: nothing, until the land execs `tar` beside it.
_RECEIVER_COMMAND = ('sleep', 'infinity')

#: What the land execs in the receiver, reading the stream on its standard
#: input: `tar -c -C <legacy claim> .` at the other end.
RECEIVE = ('tar', '-x', '--strip-components=1', '-C', DATA)


def _receiver(image: ImagePin, data: Claim) -> k8s.core.v1.PodSpecArgs:
    """The receiver's pod: an idle container of `image`, mounting the claim at `DATA`, placed and run as the server is."""
    container = k8s.core.v1.ContainerArgs(
        name='receiver',
        image=str(image),
        command=list(_RECEIVER_COMMAND),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '10m', 'memory': '32Mi'},
            limits={'cpu': '100m', 'memory': '128Mi'},
        ),
        security_context=_CONTAINER_SECURITY,
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='data', mount_path=DATA)],
    )
    return k8s.core.v1.PodSpecArgs(
        automount_service_account_token=False,
        enable_service_links=False,
        **data.constraints.pod_fields(),
        security_context=_POD_SECURITY,
        containers=[container],
        volumes=[_data_volume(data.claim.metadata.name)],
    )


# -- the component ---------------------------------------------------------------


class Matrix(Component, pulumi_type='kluster:apps:Matrix'):
    """The homeserver's namespace, its client Secret, its configuration and claim, its Deployment and Service, and what its exposure derives.

    `exposure` is its row of the route census and `client` its row of the
    single sign-on census; `issuer` is the portal's issuer, whose URL the
    server discovers the portal's endpoints from and whose name its pod
    reaches at the issuer's addresses (`issuer_aliases`). `image` is the pin it runs.
    `oidc` is the ciphertext `kluster.lib.k8s.sealed_ciphertext` reads for the
    ported client Secret, `OIDC`, and `writer_key` the ciphertext of the
    namespace's sealed writer key, which the claim's backups carry. `fronts`
    and `zone_ids` are what the program read for `expose`. `receiver`, while
    the land streams the claim in, is the pin of an image with `tar` that runs
    in the server's place (the receiving layer, above). The providers arrive
    in `opts`: the Kubernetes provider for the cluster's objects and the
    Cloudflare one for its record.
    """

    namespace: ApplicationNamespace
    oidc: crds.bitnami.v1alpha1.SealedSecret
    config: k8s.core.v1.ConfigMap
    data: Claim
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        client: conventions.sso.Client,
        issuer: conventions.sso.Issuer,
        image: ImagePin,
        oidc: Mapping[str, pulumi.Input[str]],
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        receiver: ImagePin | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        _refuse_exposure(exposure, client)
        configuration = server_configuration(client, issuer=issuer.url)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAMESPACE, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace)
        # Read off the namespace, so each object in it waits for it.
        namespace = self.namespace.namespace.metadata.name
        # A value or a claim names its namespace as a string, so its object
        # waits for it here.
        behind = self.child_opts(depends_on=[self.namespace.namespace])

        self.oidc = sealed_value(f'{name}-oidc', OIDC, ciphertext=oidc, opts=behind)
        self.config = k8s.core.v1.ConfigMap(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            data={_CONFIG_FILE: configuration},
            opts=self.child_opts(),
        )
        self.data = claim(
            f'{name}-data',
            _CLAIM,
            _CLAIM_GIB,
            backed(PRECIOUS, writer_key),
            namespace=NAMESPACE,
            uid=MATRIX_USER,
            constraints=self.constraints,
            protect=True,
            opts=behind,
        )
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=NAME,
                namespace=namespace,
                # Reloader rolls the pod when its configuration or the client
                # Secret changes; the server reads both only at its start.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # One server owns the database: a second pod beside the old
                # one would open it too.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels=dict(LABELS), annotations=dict(self.data.pod_annotations)
                    ),
                    spec=_server(image, self.oidc.metadata.name, self.data, issuer)
                    if receiver is None
                    else _receiver(receiver, self.data),
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
