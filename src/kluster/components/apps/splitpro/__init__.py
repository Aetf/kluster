"""SplitPro, the expense splitter, signing its users in through the portal (rfc-008 §5.3).

One server, and beside it its database, one CNPG instance
(`kluster.components.apps.database`), which archives to the backup bucket.
Receipts are kept on a claim of their own, backed up as `PRECIOUS`
(Aetf/kluster-ops#883). It declares nothing that holds it to a site, so the
scheduler places the server and the instance (`kluster.lib.k8s.UNCONSTRAINED`);
each is kept to its claim's node once bound, and writes in the claim's blockio
class.

**The portal is the only way in.** The server signs in through the client the
program hands it, its row of `conventions.sso.CLIENTS`, and registers no other
provider: with no mail server configured, NextAuth registers no sign-in by
mail link, so the client's policy is the one gate on who signs in. A person
added to an expense by address gets a placeholder account, which their first
sign-in takes over (`OIDC_ALLOW_DANGEROUS_EMAIL_LINKING`); the portal is the
only provider and the addresses are its own, so no one can claim another's.
Its route carries no gate, since the sign-in flow returns to the server without
the portal's session, and the component refuses an exposure that gates it.

**What the row has to agree with is the server's own**, and a row that
disagrees is refused (`SplitProRefused`): the address it sends a browser back
to, which NextAuth derives from the provider's name; the scopes it asks for,
which are fixed; how it authenticates at the token endpoint; no PKCE
challenge, which it never sends; and the email in the ID token, since NextAuth
builds the profile from the ID token alone and the email is the only key it
matches people by. The client's identifier and secret, with the session
secret, are the legacy Secret's ciphertext, ported as it is (`AUTH`), sealed
namespace-wide to `NAMESPACE`, which is why the namespace is the legacy one.

The image names no user, so the pod states a numeric one, as `restricted`
requires (rfc-008 §10.2): the image's own `node`. Its root filesystem is
read-only, and the server writes to its receipts' claim and to two empty
directories: Next.js's image cache, and `/tmp`, where each upload is staged
before it is resized into the receipts. It sends no mail.

**The land recovers the database from the legacy store** (`RECOVERY_KEY`), a
migration layer of the component's own, described where it is declared below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Backend, Exposed, Fronts, declared_constraints, expose, issuer_aliases
from kluster.components.apps.database import API_CLIENTS, Database, Drill, Recovery, Resources, database
from kluster.components.apps.storage import Claim, backed, claim
from kluster.conventions.backup import PRECIOUS
from kluster.conventions.routes import Exposure, Held, Proxied, Route
from kluster.lib.k8s import (
    ApplicationNamespace,
    PortedSecret,
    application_namespace,
    sealed_value,
)
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'APPLICATION_SQL',
    'AUTH',
    'DATABASE',
    'DBNAME',
    'LABELS',
    'NAME',
    'NAMESPACE',
    'OIDC_NAME',
    'PORT',
    'RECOVERY_KEY',
    'REDIRECT_PATH',
    'SPLITPRO_USER',
    'Refusal',
    'SplitPro',
    'SplitProRefused',
)

#: The application's name: its Deployment and its Service.
NAME = 'splitpro'

#: The legacy namespace, which the ported Secrets are sealed to
#: (declarative/workloads.md §5) and whose backups
#: `conventions.backup.BACKED_NAMESPACES` names.
NAMESPACE = 'splitpro'

#: The labels its pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The port the server listens on, which its Service forwards on unchanged.
PORT = 3000

#: The user and group the server runs as: the image's `node`, and so the
#: owner of every receipt, which the claim's movers read as.
SPLITPRO_USER = 1000

#: The legacy Secret whose ciphertext is ported as it is: the session secret,
#: then the client's identifier and its secret. The program reads it with
#: `kluster.lib.k8s.sealed_ciphertext` (declarative/workloads.md §5).
AUTH = PortedSecret(
    name='splitpro-auth', namespace=NAMESPACE, keys=('nextauth_secret', 'oidc_client_id', 'oidc_client_secret')
)
_SESSION_SECRET, _CLIENT_ID, _CLIENT_SECRET = AUTH.keys

#: The provider's name, which NextAuth lowercases into its identifier and so
#: into the path it sends a browser back to.
OIDC_NAME = 'authelia'

#: The path NextAuth sends a browser back to after the portal's sign-in,
#: under the server's own URL, derived from the provider's identifier.
REDIRECT_PATH = f'/api/auth/callback/{OIDC_NAME.lower()}'

#: The scopes the server asks for, which SplitPro writes into its provider
#: (`src/server/auth.ts` upstream) and holds no setting for.
_SCOPES = (conventions.sso.Scope.OPENID, conventions.sso.Scope.EMAIL, conventions.sso.Scope.PROFILE)

#: How the server authenticates at the token endpoint: HTTP Basic, the default
#: of `openid-client`, which NextAuth's provider leaves alone.
_TOKEN_ENDPOINT_AUTH = conventions.sso.TokenEndpointAuth.BASIC

#: The claim NextAuth matches people by, read from the ID token alone, since
#: the provider sets `idToken: true` and never asks the userinfo endpoint.
_EMAIL_CLAIM = conventions.sso.Claim.EMAIL

#: The database's `Cluster`, which its Services and the Secret CloudNativePG
#: generates for its owner are named after.
DATABASE = 'splitpro-db'

#: The application's database and its owner, under the names the legacy
#: cluster's carry.
DBNAME = 'app'

#: The claim's size in GiB. The database is 16 MB; the claim holds its WAL
#: beside it, which the archive drains.
_DATABASE_GIB = 2

#: The archive's generation, raised by a land retried after a revert
#: (rfc-008 §3.6).
_GENERATION = 0

#: The table the restore drill counts, which SplitPro's first migration
#: creates and every database in use holds rows in.
_DRILL_TABLE = 'Expense'

#: `pg_cron`, which SplitPro's recurring expenses and its cache cleanup are
#: jobs of, with a foreign key onto its job table. It installs into one
#: database, which has to be the application's.
_PG_CRON = 'pg_cron'

#: PostgreSQL's settings for `pg_cron`. A job runs in a background worker: run
#: the default way, `pg_cron` opens a connection to the server as the job's
#: owner, which CloudNativePG's `pg_hba` admits only with a password the job
#: does not have, so every run would fail as `connection failed`.
_PARAMETERS: Mapping[str, str] = {'cron.database_name': DBNAME, 'cron.use_background_workers': 'on'}

#: What a database initialized empty runs as the superuser in the
#: application's database, before the server first migrates it: `pg_cron`
#: created, which SplitPro's migration otherwise creates as the owner, and
#: `pg_cron` refuses to anyone else; and the owner allowed the foreign key its
#: schema puts on the jobs, and to read and clear their runs.
APPLICATION_SQL: Sequence[str] = (
    f'CREATE EXTENSION IF NOT EXISTS {_PG_CRON};',
    f'GRANT USAGE ON SCHEMA cron TO {DBNAME};',
    f'GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON cron.job TO {DBNAME};',
    f'GRANT SELECT, DELETE ON cron.job_run_details TO {DBNAME};',
)

#: Where the image's working directory is, which the server resolves its
#: receipts and Next.js its cache under, and where Node.js stages uploads.
_WORKDIR = '/app'
_UPLOADS = f'{_WORKDIR}/uploads'
_NEXT_CACHE = f'{_WORKDIR}/.next/cache'
_TMP = '/tmp'

#: What the database's instance asks for and is held to. With the server it
#: runs at 0.37 GiB and peaks at 0.82 over 30 days, under 0.04 core
#: (nodes.md §1.1), the server the larger part: the two requests sum to the
#: first, and the two limits clear the second.
_DATABASE_RESOURCES = Resources(
    requests={'cpu': '25m', 'memory': '128Mi'},
    limits={'cpu': '100m', 'memory': '384Mi'},
)


class Refusal(StrEnum):
    """What a declaration hands the server that it cannot run on, which `SplitPro` refuses before it declares anything."""

    REDIRECT = 'redirect'
    """A client that does not register the address the server sends a browser back to."""
    SCOPE = 'scope'
    """A client that does not grant a scope the server asks for."""
    TOKEN_ENDPOINT_AUTH = 'token-endpoint-auth'
    """A client registered for another way of authenticating at the token endpoint."""
    PKCE = 'pkce'
    """A client that requires a PKCE challenge, which the server never sends."""
    EMAIL_CLAIM = 'email-claim'
    """A client whose ID token lacks the email, which the server matches people by."""
    NO_ROUTE = 'no-route'
    """An exposure with no `internet` route at the client's host."""
    GATED = 'gated'
    """An exposure that gates the route at the client's host."""
    PORTED_KEYS = 'ported-keys'
    """A ported ciphertext whose keys are not its Secret's."""


class SplitProRefused(ValueError):
    """A declaration refused, and which of its arguments refused it."""

    def __init__(self, refusal: Refusal, message: str) -> None:
        super().__init__(message)
        self.refusal = refusal


def _server_url(client: conventions.sso.Client) -> str:
    """The URL the server answers at, `NEXTAUTH_URL`: its client's host, in the client's zone."""
    return f'https://{client.host}.{client.zone}'


def _refuse_client(client: conventions.sso.Client) -> None:
    """Refuse a client the server cannot sign in through, by the `Refusal` it fails."""
    what = f'the client {client.name!r}'
    redirect = f'{_server_url(client)}{REDIRECT_PATH}'
    if redirect not in conventions.sso.redirect_uris(client):
        raise SplitProRefused(
            Refusal.REDIRECT, f'{what} does not register {redirect}, where the server sends a browser back to'
        )
    if missing := [scope.value for scope in _SCOPES if scope not in client.scopes]:
        raise SplitProRefused(Refusal.SCOPE, f'{what} does not grant {missing}, which the server asks for')
    if client.token_endpoint_auth is not _TOKEN_ENDPOINT_AUTH:
        raise SplitProRefused(
            Refusal.TOKEN_ENDPOINT_AUTH,
            f'{what} authenticates by {client.token_endpoint_auth.value}, and the server sends '
            f'{_TOKEN_ENDPOINT_AUTH.value}',
        )
    if client.pkce:
        raise SplitProRefused(Refusal.PKCE, f'{what} requires a PKCE challenge, which the server never sends')
    if _EMAIL_CLAIM not in client.id_token:
        raise SplitProRefused(
            Refusal.EMAIL_CLAIM,
            f"{what}'s ID token does not carry {_EMAIL_CLAIM.value}, which the server matches people by",
        )


def _refuse_exposure(exposure: Exposure, client: conventions.sso.Client) -> None:
    """Refuse an exposure with no ungated route at the client's host on `internet`.

    The portal sends a browser back to the client's host, and the server is
    reached at that name; a gate would stop the return, which carries no
    portal session.
    """
    host = f'{client.host}.{client.zone}'
    rows = [held.row if isinstance(held, Held) else held for held in exposure.routes]
    served = [
        row
        for row in rows
        if isinstance(row, Route) and row.host == client.host and client.zone in row.zones and row.internet is not None
    ]
    if not served:
        raise SplitProRefused(
            Refusal.NO_ROUTE, f'the exposure has no route at {host} on `internet`, where the server answers'
        )
    if gated := [row for row in served if isinstance(row.internet, Proxied) and row.internet.gate is not None]:
        raise SplitProRefused(
            Refusal.GATED,
            f'the route at {host} is gated by {gated[0].internet}; the sign-in flow returns to the server without '
            'the portal session a gate asks for',
        )


def _refuse_ported(ported: PortedSecret, ciphertext: Mapping[str, pulumi.Input[str]]) -> None:
    """Refuse a ciphertext that does not hold each key `ported` carries and no other: a pod would read a missing one."""
    if sorted(ciphertext) != sorted(ported.keys):
        raise SplitProRefused(
            Refusal.PORTED_KEYS,
            f'the ported {ported.name!r} Secret carries {sorted(ported.keys)}, and the ciphertext handed '
            f'holds {sorted(ciphertext)}',
        )


def _from_secret(variable: str, secret: pulumi.Input[str], key: str) -> k8s.core.v1.EnvVarArgs:
    return k8s.core.v1.EnvVarArgs(
        name=variable,
        value_from=k8s.core.v1.EnvVarSourceArgs(secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(name=secret, key=key)),
    )


#: The receipts' claim's base name, and its size in GiB: a receipt is
#: resized to a WebP of at most 1200 pixels across, a few hundred kB with
#: its thumbnail.
_RECEIPTS = 'uploads'
_RECEIPTS_GIB = 2

#: Each empty directory the server writes to: its volume's name, where it is
#: mounted, and its size limit.
_EMPTY_DIRECTORIES: Sequence[tuple[str, str, str]] = (
    ('cache', _NEXT_CACHE, '256Mi'),
    # One upload at a time, each under `UPLOAD_MAX_FILE_SIZE_MB`, 10 MB by
    # default.
    ('tmp', _TMP, '64Mi'),
)


def _probe(*, period_seconds: int, failure_threshold: int | None = None) -> k8s.core.v1.ProbeArgs:
    """A probe of the server's root.

    The first minute after a start runs the jobs the server schedules beside
    its answers, under the CPU limit, and an answer then takes up to half a
    minute, so the timeout clears it.
    """
    return k8s.core.v1.ProbeArgs(
        http_get=k8s.core.v1.HTTPGetActionArgs(path='/', port='http'),
        timeout_seconds=30,
        period_seconds=period_seconds,
        failure_threshold=failure_threshold,
    )


def _server(
    image: ImagePin,
    client: conventions.sso.Client,
    *,
    issuer: conventions.sso.Issuer,
    auth_secret: pulumi.Input[str],
    database_secret: str,
    receipts: Claim,
) -> k8s.core.v1.PodSpecArgs:
    """The server's pod: SplitPro, reading the ported Secret and the database owner's, on its receipts and empty directories, reaching the issuer at its alias."""
    plain = {
        # The runtime sets `HOSTNAME` to the pod's name, which Next.js binds
        # otherwise, and a port-forward reaches the pod on its loopback.
        'HOSTNAME': '0.0.0.0',
        'PORT': str(PORT),
        'NEXTAUTH_URL': _server_url(client),
        'OIDC_NAME': OIDC_NAME,
        'OIDC_WELL_KNOWN_URL': f'{issuer.url.rstrip("/")}/.well-known/openid-configuration',
        # Read as `!!value`, so any value turns it on and only its absence
        # turns it off.
        'OIDC_ALLOW_DANGEROUS_EMAIL_LINKING': 'true',
        # No `EMAIL_SERVER_HOST`, so NextAuth registers no sign-in by mail
        # link and invitations have no mail to send; this refuses the
        # accounts such a sign-in would make, should one ever be configured.
        # `INVITE_ONLY` stays unset: SplitPro refuses every new account under
        # it, the first one included.
        'DISABLE_EMAIL_SIGNUP': 'true',
        'ENABLE_SENDING_INVITES': 'false',
        # The default, `/home`, is the project's landing page.
        'DEFAULT_HOMEPAGE': '/balances',
        # The rates provider that needs no key.
        'CURRENCY_RATE_PROVIDER': 'frankfurter',
        # The `pg_cron` job that clears cached rates and bank data.
        'CLEAR_CACHE_CRON_RULE': '0 2 * * 0',
        'CACHE_RETENTION_INTERVAL': '2 days',
    }
    container = k8s.core.v1.ContainerArgs(
        name=NAME,
        image=str(image),
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORT, protocol='TCP')],
        env=[
            *(k8s.core.v1.EnvVarArgs(name=key, value=value) for key, value in plain.items()),
            # CloudNativePG's Secret for the owner carries a ready connection
            # string, its password escaped.
            _from_secret('DATABASE_URL', database_secret, 'uri'),
            _from_secret('NEXTAUTH_SECRET', auth_secret, _SESSION_SECRET),
            _from_secret('OIDC_CLIENT_ID', auth_secret, _CLIENT_ID),
            _from_secret('OIDC_CLIENT_SECRET', auth_secret, _CLIENT_SECRET),
        ],
        # The schema's migrations run in the server at every start, so a
        # first start after an upgrade can take minutes before it answers.
        startup_probe=_probe(period_seconds=5, failure_threshold=60),
        readiness_probe=_probe(period_seconds=10),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            # Sized with the database's (`_DATABASE_RESOURCES`). Its CPU is a
            # few millicores at rest. Resizing a receipt is what the limit
            # slows: one of a few megapixels takes over a minute at it, past
            # the 15 s Envoy gives a route that sets no timeout, so such an
            # upload fails through the Gateway. Whether the limit grows for
            # receipts is Aetf/kluster-ops#882.
            requests={'cpu': '25m', 'memory': '256Mi'},
            limits={'cpu': '100m', 'memory': '640Mi'},
        ),
        security_context=k8s.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
        ),
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name='receipts', mount_path=_UPLOADS),
            *(k8s.core.v1.VolumeMountArgs(name=volume, mount_path=path) for volume, path, _ in _EMPTY_DIRECTORIES),
        ],
    )
    return k8s.core.v1.PodSpecArgs(
        # It speaks HTTP, PostgreSQL and OpenID Connect, and never Kubernetes.
        automount_service_account_token=False,
        # SplitPro validates its environment at its start; a variable per
        # Service in the namespace is nothing it reads.
        enable_service_links=False,
        # The claim binds where its first consumer is placed, so the pod
        # states the constraints the claim and its movers were given.
        **receipts.constraints.pod_fields(),
        host_aliases=issuer_aliases(issuer),
        security_context=k8s.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=SPLITPRO_USER,
            run_as_group=SPLITPRO_USER,
            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
        ),
        containers=[container],
        volumes=[
            k8s.core.v1.VolumeArgs(
                name='receipts',
                persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(claim_name=receipts.name),
            ),
            *(
                k8s.core.v1.VolumeArgs(name=volume, empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(size_limit=limit))
                for volume, _, limit in _EMPTY_DIRECTORIES
            ),
        ],
    )


# -- the recovery layer ----------------------------------------------------------
#
# The land recovers the database once from the legacy store, where the legacy
# cluster archived under its `Cluster`'s name, with the legacy Secret's
# service-account key ported as it is (rfc-008 §4.3). After the recovery the
# database archives to the backup bucket, and nothing reads the legacy store.
#
# The layer is everything in this module and in `tests/test_apps_splitpro.py`
# that names the recovery. Today that is `RECOVERY_KEY` with its entry in
# `__all__`, the import of `Recovery`, `_RECOVERY_DATA_KEY`, `_LEGACY_STORE`,
# `_LEGACY_SERVER` and
# `_recovery` here, the module docstring's last paragraph, and the
# `recovery_key` parameter of `SplitPro` with its sentence in the class's
# docstring, its refusal and the `recovery` it hands `database`; and there
# the import of `RECOVERY_KEY`, `OBJECT_STORE`, `RECOVERY_CIPHERTEXT`, the
# `recovery_key` its `declare` hands and the cases under "the recovery layer". It goes with Wave A2's
# cut, slice 32 (rfc-008 §10.3), once the database has archived a base backup
# of its own. The `Cluster`'s `bootstrap` and `externalClusters` change with
# it, which that slice's preview shows.

#: The legacy Secret holding the service account's key that reads the legacy
#: store, ported as it is.
RECOVERY_KEY = PortedSecret(name='splitpro-db', namespace=NAMESPACE, keys=('gcs_credentials',))
(_RECOVERY_DATA_KEY,) = RECOVERY_KEY.keys

#: The store the legacy database archives to, and the legacy `Cluster`'s
#: name, which Pulumi suffixed and which the legacy archive is kept under as
#: its server name.
_LEGACY_STORE = 'gs://splitpro-postgresql-backup'
_LEGACY_SERVER = 'splitpro-db-61dcba6a'


def _recovery(recovery_key: Mapping[str, pulumi.Input[str]]) -> Recovery:
    """The legacy store the database recovers from once, read with the ported key."""
    return Recovery(
        destination_path=_LEGACY_STORE,
        server_name=_LEGACY_SERVER,
        key=_RECOVERY_DATA_KEY,
        ciphertext=recovery_key[_RECOVERY_DATA_KEY],
    )


# -- the component ---------------------------------------------------------------


class SplitPro(Component, pulumi_type='kluster:apps:SplitPro'):
    """SplitPro's namespace, its ported Secret, its database, its Deployment and Service, and what its exposure derives.

    `exposure` is its row of the route census and `client` its row of the
    single sign-on census; `issuer` is the portal's issuer, whose URL the
    server discovers the portal's endpoints from and whose name its pod
    reaches at the issuer's addresses (`issuer_aliases`). `image` is the pin the
    server runs, `database_image` the `pg_cron` operand its database runs and
    `drill_image` the restore drill's. `auth` is the ciphertext
    `kluster.lib.k8s.sealed_ciphertext` reads for the ported Secret, `AUTH`;
    `recovery_key` the one it reads for `RECOVERY_KEY`, which the recovery
    layer reads (above). `database_writer_key`
    and `receipts_writer_key` are the ciphertexts of the namespace's sealed
    writer keys of the barman family, which the database's archive is written
    with, and of the VolSync family, which the receipts' backups are.
    `fronts` and `zone_ids` are
    what the program read for `expose`. The providers arrive in `opts`: the
    Kubernetes provider for the cluster's objects and the Cloudflare one for
    its record.
    """

    namespace: ApplicationNamespace
    auth: crds.bitnami.v1alpha1.SealedSecret
    database: Database
    receipts: Claim
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
        database_image: ImagePin,
        drill_image: ImagePin,
        auth: Mapping[str, pulumi.Input[str]],
        recovery_key: Mapping[str, pulumi.Input[str]],
        database_writer_key: Mapping[str, pulumi.Input[str]],
        receipts_writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        _refuse_client(client)
        _refuse_exposure(exposure, client)
        _refuse_ported(AUTH, auth)
        _refuse_ported(RECOVERY_KEY, recovery_key)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(
            f'{name}-namespace', name=NAMESPACE, api_clients=API_CLIENTS, opts=self.child_opts()
        )
        self.constraints = declared_constraints(exposure, self.namespace)
        # Read off the namespace, so each object in it waits for it.
        namespace = self.namespace.namespace.metadata.name
        # A value or a database names its namespace as a string, so its
        # objects wait for it here.
        behind = self.child_opts(depends_on=[self.namespace.namespace])

        self.auth = sealed_value(f'{name}-auth', AUTH, ciphertext=auth, opts=behind)
        self.database = database(
            f'{name}-database',
            DATABASE,
            _DATABASE_GIB,
            PRECIOUS,
            namespace=NAMESPACE,
            image=database_image,
            dbname=DBNAME,
            resources=_DATABASE_RESOURCES,
            constraints=self.constraints,
            writer_key=database_writer_key,
            drill=Drill(table=_DRILL_TABLE, extensions=(_PG_CRON,)),
            drill_image=drill_image,
            generation=_GENERATION,
            recovery=_recovery(recovery_key),
            shared_preload_libraries=(_PG_CRON,),
            parameters=_PARAMETERS,
            post_init_application_sql=APPLICATION_SQL,
            opts=behind,
        )
        # What a receipt annotates is an expense, which the database keeps as
        # `PRECIOUS`, and a receipt is a photo of a paper nobody keeps: lost,
        # it is not taken again, so it is kept as long as the expense's row.
        self.receipts = claim(
            f'{name}-receipts',
            _RECEIPTS,
            _RECEIPTS_GIB,
            backed(PRECIOUS, receipts_writer_key),
            namespace=NAMESPACE,
            uid=SPLITPRO_USER,
            constraints=self.constraints,
            protect=True,
            opts=behind,
        )
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=NAME,
                namespace=namespace,
                # Reloader rolls the pod when a Secret it reads changes; the
                # server reads them only at its start.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # The server migrates the schema at its start: a second pod
                # beside the old one would migrate under it.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels=dict(LABELS), annotations=dict(self.receipts.pod_annotations)
                    ),
                    spec=_server(
                        image,
                        client,
                        issuer=issuer,
                        auth_secret=self.auth.metadata.name,
                        # CloudNativePG names the owner's Secret after the
                        # cluster: `<cluster>-app`.
                        database_secret=f'{self.database.name}-app',
                        receipts=self.receipts,
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
