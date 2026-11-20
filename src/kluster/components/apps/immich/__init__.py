"""immich, the photo library, on the worker (rfc-008 §5.6).

Four workloads in one namespace, `NAMESPACE`, which is the legacy one because
the ported ciphertexts are sealed to it (declarative/workloads.md §5):

-   **the server**, which serves the web interface, the mobile apps and the
    API, and runs the jobs that derive thumbnails, transcodes and metadata;
-   **its machine-learning service**, which the server asks for search
    embeddings, faces and text in images, at `ML_SERVICE`;
-   **its queue store**, a Redis of its own, which BullMQ keeps the jobs in,
    holding nothing a restart should keep: a lost queue re-runs. Its metrics
    exporter runs beside it, on `REDIS_METRICS`;
-   **its database**, the application base's shape
    (`kluster.components.apps.database`), on the `vchord` operand.

**It runs on the worker.** The library is on the NAS, which the worker alone
reaches, and the GPU both the server and the machine-learning service request
is the worker's: each asks for one `kluster.components.gpu.GPU_RESOURCE`, the
server for ffmpeg's QSV transcodes and the service for OpenVINO. Every pod
takes `kluster.lib.k8s.WORKER_POD_AFFINITY`, the cache's through its claim's
placement, and the database the node affinity of `WORKER_AFFINITY_JSON`. Each
rollout is `Recreate`, since the GPU is one device a second pod would wait
for.

**What it keeps, and where** (rfc-008 §4.2):

-   the library, originals, thumbnails and encoded video together under one
    media root, `LIBRARY` on the export, writable, mounted at `MEDIA`, where the
    legacy pod mounted it: every path the database records is under it. The
    server keeps the root it last ran with, and started under another it
    rewrites every recorded path to it;
-   the models, 11 GiB that the service downloads again, in a claim nothing
    backs up (`off`), so the namespace has no restic repository;
-   the database, archived by barman under `PRECIOUS`, which is the
    namespace's one backup family (`conventions.backup.BACKED_NAMESPACES`).

**The database is the one an empty `initdb` and `APPLICATION_SQL` make.**
On the operand, creating `vchord`, with pgvector's `vector` it depends on,
and `earthdistance` takes a superuser. The server, which connects as the
database's owner, creates `vchord` at its start and stops where it cannot,
then creates `earthdistance` in its first migration. So the application's
SQL, which the superuser runs, creates both with what they depend on; every
other extension the server creates is one the owner may. The instance preloads
VectorChord's library and pgvecto.rs's, as the legacy instance does. Its
restore drill checks the assets' table and the two vector extensions immich
searches with, `vchord` and `vector`. The server reaches the database at the
`Cluster`'s read-write Service, as the owner whose credential the operator
generates in `<cluster>-app`.

**The configuration is the component's, whole**, a file the server reads at
`CONFIG_FILE` (`IMMICH_CONFIG_FILE`), which makes the admin interface's
settings read-only. It is JSON, which the server reads as YAML, around the
one secret it holds: the client secret, which immich reads from that file
alone (immich-app/immich#14815 was declined). So the file is the template of
the ported ciphertext's Secret (`CONFIG`), which the sealed-secrets controller
renders with the decrypted secret in its place, written by `toJson` as the
legacy file wrote it, so whatever the secret holds stays one JSON string.
What the file says beyond immich's defaults:

-   the server's external address, the route's name over HTTPS;
-   mail through the cluster's relay, `exim.HOST` on `exim.PORT`, in the
    clear, since the relay serves no certificate, from a send-as alias of the
    relay's account, which Gmail keeps as From; the server's pods carry
    `conventions.MAIL_SENDER_LABEL`, which is how the relay admits them;
-   sign-in through the portal alone, as the census's client (`client`), with
    the password login off: every account is linked to a portal identity.
    The issuer is the portal's, and the scopes and the token endpoint's
    authentication are the client's. A browser comes back to the route's
    name, and the mobile app to `MOBILE_REDIRECT`, so a client that does not
    register each of them, or a route under another name, is refused;
-   the job concurrency, the transcoding and the storage template the
    legacy file states, each with its reason beside it in
    `server_configuration`.

**It is reached at one name**, its route row, which the Gateways terminate TLS
for, with no gate: immich signs its users in itself, and its mobile app sends
no portal session. A row that gates its `internet` side is refused. The row is
`Direct` on `internet`, since the proxy refuses an upload over its body limit
and immich sends an asset whole (rfc-008 §4.1); that is the row's to say.

**Its queue watchdog is a layer of its own** (`watchdog`): a CronJob that
restarts the server when a job slot has leaked, watched by the scheduled-job
family's rules, and the server's connection setting that keeps the leak from
starting on the client's side. It works
around an upstream bug and goes with it.

**The land recovers the database from the legacy store** (`RECOVERY`), a
migration layer of the component's own, described where it is declared below.

The images are the legacy pins: `immich-server`, `immich-machine-learning` in
its OpenVINO flavour, the official `redis` with `redis_exporter` beside it,
and the `vchord` operand built in `docker/`. None of the first three states a
numeric user, so each pod states one, as the namespace's `restricted` level
requires (rfc-008 §10.2): the server and the service run as `USER`, which
writes the NAS as the export's anonymous user whatever it is, and Redis's pod
as the image's `redis`, by number.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Backend, Exposed, Fronts, exim, expose
from kluster.components.apps.database import Database, Drill, Recovery, Resources, database
from kluster.components.apps.immich import watchdog
from kluster.components.apps.storage import Claim, NasClaim, claim, nas_claim, off
from kluster.components.gpu import GPU_RESOURCE
from kluster.conventions.backup import PRECIOUS
from kluster.conventions.routes import Exposure, Held, Proxied, Route
from kluster.conventions.sso import Client, redirect_uris
from kluster.lib.k8s import (
    WORKER_AFFINITY_JSON,
    WORKER_POD_AFFINITY,
    ApiClients,
    ApplicationNamespace,
    PortedSecret,
    SecretTemplate,
    application_namespace,
    sealed_secret,
    sealed_value,
)
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'APPLICATION_SQL',
    'CACHE',
    'CLIENT_ID',
    'CONFIG',
    'CONFIG_FILE',
    'DATABASE',
    'DBNAME',
    'LIBRARY',
    'MEDIA',
    'ML_LABELS',
    'ML_PORT',
    'ML_SERVICE',
    'MOBILE_REDIRECT',
    'NAMESPACE',
    'RECOVERY',
    'REDIS',
    'REDIS_LABELS',
    'REDIS_METRICS',
    'REDIS_PORT',
    'REDIS_SECRET',
    'REDIS_SERVICE',
    'REDIS_USER',
    'SERVER_LABELS',
    'SERVER_PORT',
    'SERVER_SERVICE',
    'USER',
    'WEB_REDIRECT_PATHS',
    'Immich',
    'ImmichRefused',
    'Refusal',
    'server_configuration',
)

#: The legacy namespace, which the ported ciphertexts are sealed to and whose
#: barman writer key `conventions.backup.BACKED_NAMESPACES` names.
NAMESPACE = 'immich'

#: The server's Deployment and Service, its labels, and the port it serves on,
#: which the Service forwards on unchanged.
SERVER_SERVICE = 'immich-server'
SERVER_LABELS: Mapping[str, str] = {'app.kubernetes.io/name': SERVER_SERVICE}
SERVER_PORT = 2283

#: The machine-learning service's Deployment and Service, its labels, and the
#: port it serves on.
ML_SERVICE = 'immich-machine-learning'
ML_LABELS: Mapping[str, str] = {'app.kubernetes.io/name': ML_SERVICE}
ML_PORT = 3003

#: The queues' Redis: its Deployment and Service, its labels, its port, and
#: its user and group, the official image's `redis`, by number.
REDIS_SERVICE = 'immich-redis'
REDIS_LABELS: Mapping[str, str] = {'app.kubernetes.io/name': REDIS_SERVICE}
REDIS_PORT = 6379
REDIS_USER = 999

#: The port Redis's exporter serves its metrics on, its default, named
#: `metrics` on its container.
REDIS_METRICS = 9121

#: The user and group the server, its watchdog and the machine-learning
#: service run as. Neither image names one; any user that is not root serves,
#: since the NAS takes every write as its anonymous user.
USER = 1000

#: The path under the export that holds the library, where its files already
#: are, and where it is mounted: the media root the server is told and the
#: legacy pod mounted it at.
LIBRARY = 'Immich'
MEDIA = '/data'

#: Where the machine-learning service keeps its models: the image's
#: `MACHINE_LEARNING_CACHE_FOLDER`, under which it also points the caches of
#: the libraries it loads.
CACHE = '/cache'

#: The ported Secret of the queues' Redis password, which the server, the
#: watchdog and Redis read.
REDIS = PortedSecret(name='immich', namespace=NAMESPACE, keys=('redis_pass',))
(_REDIS_KEY,) = REDIS.keys
REDIS_SECRET = k8s.core.v1.SecretKeySelectorArgs(name=REDIS.name, key=_REDIS_KEY)

#: The ported Secret of the client secret, which the configuration file is
#: rendered around, and the file's name in it.
CONFIG = PortedSecret(name='immich-config', namespace=NAMESPACE, keys=('oauth_client_secret',))
(_CLIENT_SECRET_KEY,) = CONFIG.keys
_CONFIG_KEY = 'immich-config.json'
_CONFIG_DIRECTORY = '/config'
CONFIG_FILE = f'{_CONFIG_DIRECTORY}/{_CONFIG_KEY}'

#: The `Cluster`'s name, which its Services and the operator's Secret of the
#: owner's credential are named after, and the application's database and its
#: owner, under the names the legacy cluster's carry.
DATABASE = 'immich-db'
DBNAME = 'app'

#: The client's identifier, as the legacy configuration states it in the
#: clear: a browser carries it in every authorization request, and the portal
#: holds its own copy in its ported ciphertext.
CLIENT_ID = 'sJ5K_PnRwwN9KxZAcWvVcvz3C4BqiL_1E4DNiTKpo2YMdO3q2U7IhcXI1QcVlbAiZyHWQxfP'

#: The paths under the server's address that immich sends a browser back to
#: after the portal's sign-in: the login page, and the settings page that
#: links an account (immich's OAuth guide, "Prerequisites").
WEB_REDIRECT_PATHS = ('/auth/login', '/user-settings')

#: Where the mobile app is sent back to: its own scheme, which the server
#: hands the portal as it is while `oauth.mobileOverrideEnabled` is off.
MOBILE_REDIRECT = 'app.immich:///oauth-callback'

#: The sign-in button's text, the legacy one.
_BUTTON = 'Login with UnlimitedCodeWorks'

#: The relay's account keeps a send-as alias at this address, which Gmail
#: leaves as From.
_MAIL_FROM = f'"kluster immich" <kluster@{conventions.ZONE_PRIMARY}>'

#: The claim of the models, in GiB: 11 GiB of them, with room to download a
#: changed model beside the old one.
_CACHE_GIB = 16

#: The database's claim, in GiB: 1.5 GiB of data, with room for its WAL.
_DATABASE_GIB = 5

#: What each workload asks for and is held to (rfc-008 §6.1): the server's and
#: the service's memory from what each runs at and its 30-day peak, the
#: database's and Redis's together 1.1 GiB and 2 GiB.
_SERVER_RESOURCES = k8s.core.v1.ResourceRequirementsArgs(
    requests={'cpu': '1', 'memory': '2Gi', GPU_RESOURCE: '1'},
    limits={'cpu': '8', 'memory': '5Gi', GPU_RESOURCE: '1'},
)
_ML_RESOURCES = k8s.core.v1.ResourceRequirementsArgs(
    requests={'cpu': '500m', 'memory': '1Gi', GPU_RESOURCE: '1'},
    limits={'cpu': '6', 'memory': '5Gi', GPU_RESOURCE: '1'},
)
_DATABASE_RESOURCES = Resources(
    requests={'cpu': '250m', 'memory': '1Gi'},
    limits={'cpu': '2', 'memory': '1792Mi'},
)
# A CPU limit of a tenth of a core throttled the legacy Redis into failed
# probes whenever the queues got busy.
_REDIS_RESOURCES = k8s.core.v1.ResourceRequirementsArgs(
    requests={'cpu': '100m', 'memory': '128Mi'},
    limits={'cpu': '1', 'memory': '256Mi'},
)
_EXPORTER_RESOURCES = k8s.core.v1.ResourceRequirementsArgs(
    requests={'cpu': '10m', 'memory': '16Mi'},
    limits={'cpu': '100m', 'memory': '64Mi'},
)

#: The libraries the instance preloads: pgvecto.rs's and VectorChord's, as the
#: legacy instance does.
_PRELOAD = ('vectors', 'vchord.so')

#: What a database initialized empty runs as the superuser in the
#: application's database, before the server first starts on it: the
#: extensions the server creates as the owner that the operand lets a
#: superuser alone create, each with what it depends on. `vchord` brings
#: pgvector's `vector`, and `earthdistance` brings `cube`.
APPLICATION_SQL: Sequence[str] = (
    'CREATE EXTENSION IF NOT EXISTS vchord CASCADE;',
    'CREATE EXTENSION IF NOT EXISTS earthdistance CASCADE;',
)

#: What the restore drill checks on the recovered database: immich's assets,
#: and the vector extensions it indexes and searches with.
_DRILL = Drill(table='asset', extensions=('vchord', 'vector'))

#: Who in the namespace calls the API, and why.
_API_CLIENTS = ApiClients(
    reason=(
        "the database's instances keep their cluster's status, its restore drill recovers a cluster of its own, "
        "and the job-slot watchdog restarts the server's pods"
    )
)

#: Where the configuration holds the client secret before the controller
#: renders it: one JSON string, which `_configuration_template` replaces with
#: the action that writes the decrypted secret.
_SECRET_STANDIN = 'the client secret, which the controller renders in'


class Refusal(StrEnum):
    """What a declaration hands immich that it cannot run on, which `Immich` refuses before it declares anything."""

    ROUTES = 'routes'
    """An exposure other than one route row and no raw port."""
    ROUTE_KIND = 'route-kind'
    """A route row that is no `Route`, which the Gateways terminate TLS for."""
    ZONES = 'zones'
    """A route published in more than one zone, which is more than one address."""
    GATED = 'gated'
    """A route gated on `internet`, which the mobile app has no portal session for."""
    CLIENT_HOST = 'client-host'
    """A client whose redirect addresses are under another name than the route's."""
    REDIRECT = 'redirect'
    """A client that does not register an address immich comes back to."""
    PORTED_KEYS = 'ported-keys'
    """A ported ciphertext whose keys are not its Secret's."""


class ImmichRefused(ValueError):
    """A declaration refused, and which of its arguments refused it."""

    def __init__(self, refusal: Refusal, message: str) -> None:
        super().__init__(message)
        self.refusal = refusal


# -- the configuration -------------------------------------------------------------


@dataclass(frozen=True)
class _Served:
    """The one name the server is reached at."""

    host: str
    zone: str

    @property
    def url(self) -> str:
        return f'https://{self.host}.{self.zone}'


def server_configuration(served: str, client: Client, *, issuer: str, client_secret: str) -> dict[str, Any]:
    """immich's settings beyond its defaults, for the server reached at `served` and signing in as `client`.

    `served` is the server's address, `https://<name>`; `client_secret` is what
    the file holds where the client secret goes.
    """
    return {
        'server': {'externalDomain': served},
        'notifications': {
            'smtp': {
                'enabled': True,
                'from': _MAIL_FROM,
                # The relay serves no certificate and offers no STARTTLS.
                'transport': {'host': exim.HOST, 'port': exim.PORT, 'secure': False, 'ignoreCert': False},
            }
        },
        # Each job's concurrency, for the worker the server runs on.
        'job': {
            # Cheap, and a long backlog follows any bulk import.
            'migration': {'concurrency': 10},
            'thumbnailGeneration': {'concurrency': 5},
            'ocr': {'concurrency': 2},
            'sidecar': {'concurrency': 2},
            # Each one is a full ffmpeg transcode.
            'videoConversion': {'concurrency': 2},
            # A scan of the library on the NAS is bound by its IO, not its CPU.
            'library': {'concurrency': 1},
        },
        'ffmpeg': {
            # QSV on the A380, which the server's pod requests.
            'accel': 'qsv',
            # Software decode: the iGPU's HEVC decoder hung the video engine on
            # 4K60 HEVC from Android phones (`GPU HANG ... in dec0:0:hevc_qsv`),
            # and ffmpeg never recovered from the reset, wedging the queue.
            # Software decode of the same file is fast, so the decoder was never
            # the expensive half; the encoder runs on the GPU. The A380's media
            # engine is a newer generation, on which hardware decode is
            # untried (Aetf/kluster-code#196).
            'accelDecode': False,
            # A 1080p h264 streaming copy, not a second master: the GPU's VP9
            # encoder spent an order of magnitude more bitrate for a quarter of a
            # percent of SSIM, and h264 plays everywhere. A transcode is the only
            # thing a client streams once it exists (immich-app/immich#22727), so
            # 4K stays available by downloading the original.
            'targetResolution': '1080',
            'targetVideoCodec': 'h264',
            # Only h264 originals are left alone.
            'acceptedVideoCodecs': ['h264'],
        },
        'image': {'preview': {'format': 'webp'}, 'extractEmbedded': True},
        'machineLearning': {'clip': {'modelName': 'XLM-Roberta-Large-Vit-B-16Plus'}},
        # immich's own dumps are a convenience copy; the database's backups are
        # barman's, and its restore drill proves them.
        'backup': {'database': {'keepLastAmount': 5}},
        'storageTemplate': {'enabled': True, 'template': '{{y}}/{{y}}-{{MM}}/{{filename}}'},
        'oauth': {
            'enabled': True,
            'issuerUrl': issuer,
            'buttonText': _BUTTON,
            # Accounts are made by hand; the client's policy gates who may reach
            # the callback at all.
            'autoRegister': False,
            'clientId': CLIENT_ID,
            'clientSecret': client_secret,
            'scope': ' '.join(scope.value for scope in client.scopes),
            'tokenEndpointAuthMethod': client.token_endpoint_auth.value,
        },
        # Every account is linked to a portal identity, so a local password is a
        # second, weaker way in. With the configuration a file, the recovery
        # from a broken sign-in is turning this on in it: `immich-admin
        # enable-password-login` writes the database's copy, which the server
        # ignores while it reads a file.
        'passwordLogin': {'enabled': False},
    }


def _configuration_template(served: str, client: Client, *, issuer: str) -> str:
    """The configuration as the controller's template: JSON, with the client secret written in by `toJson`.

    This and `_constant` are a private copy of
    `kluster.lib.k8s.rendered_template`, which renders a field of a sealed key
    as its value alone and has no conversion that writes it as a JSON string.
    They go once it has one, and the configuration then moves to a template
    file and `sealed_value(rendered=...)` (Aetf/kluster-ops#875).

    The controller renders the template as Go's `text/template`, with sprig's
    functions, and a decrypted key as `index . "<key>"`. `toJson` writes the
    secret as a JSON string, its quotes and escapes with it. Every other part
    of the file is a Go string constant, quoted as JSON, whose escapes Go
    reads: unquoted, a brace of the JSON beside an action, or the storage
    template's own double braces, would be read as part of one.
    """
    text = json.dumps(server_configuration(served, client, issuer=issuer, client_secret=_SECRET_STANDIN), indent=2)
    before, after = text.split(json.dumps(_SECRET_STANDIN))
    secret = f'{{{{ toJson (index . "{_CLIENT_SECRET_KEY}") }}}}'
    return ''.join([_constant(before), secret, _constant(after)])


def _constant(text: str) -> str:
    """`text` as a Go template that renders it as it is: `rendered_template`'s quoting of a literal chunk, copied (above)."""
    return f'{{{{ {json.dumps(text, ensure_ascii=False)} }}}}' if text else ''


# -- refusals ------------------------------------------------------------------------


def _served(exposure: Exposure) -> _Served:
    """The exposure's one route row, held or not, served in one zone and gated nowhere; no raw port."""
    if len(exposure.routes) != 1 or exposure.ports:
        raise ImmichRefused(
            Refusal.ROUTES,
            f'immich is served at one name and no raw port, and its exposure holds {len(exposure.routes)} '
            f'route rows and {len(exposure.ports)} raw ports',
        )
    (held,) = exposure.routes
    row = held.row if isinstance(held, Held) else held
    if not isinstance(row, Route):
        raise ImmichRefused(Refusal.ROUTE_KIND, 'immich is served at a route row, which the Gateways terminate TLS for')
    if len(row.zones) != 1:
        raise ImmichRefused(
            Refusal.ZONES, f'the route {row.host!r} is published in {len(row.zones)} zones, and immich has one address'
        )
    if isinstance(row.internet, Proxied) and row.internet.gate is not None:
        raise ImmichRefused(
            Refusal.GATED,
            f'the route {row.host!r} is gated by {row.internet.gate}; immich signs its users in itself, and its '
            'mobile app sends no portal session',
        )
    (zone,) = row.zones
    return _Served(row.host, zone)


def _refuse_client(served: _Served, client: Client) -> None:
    """Refuse a client under another name than the route's, or one that does not register each address immich comes back to."""
    if (client.host, client.zone) != (served.host, served.zone):
        raise ImmichRefused(
            Refusal.CLIENT_HOST,
            f'the client {client.name!r} redirects to {client.host}.{client.zone}, and immich is served at '
            f'{served.host}.{served.zone}',
        )
    registered = redirect_uris(client)
    needed = (*(f'{served.url}{path}' for path in WEB_REDIRECT_PATHS), MOBILE_REDIRECT)
    if missing := [uri for uri in needed if uri not in registered]:
        raise ImmichRefused(
            Refusal.REDIRECT,
            f'the client {client.name!r} does not register {", ".join(missing)}, where immich comes back to',
        )


def _refuse_ported(ported: PortedSecret, ciphertext: Mapping[str, pulumi.Input[str]]) -> None:
    """Refuse a ciphertext that does not hold each key `ported` carries and no other: a pod would read a missing one."""
    if sorted(ciphertext) != sorted(ported.keys):
        raise ImmichRefused(
            Refusal.PORTED_KEYS,
            f'the ported {ported.name!r} Secret carries {sorted(ported.keys)}, and the ciphertext handed '
            f'holds {sorted(ciphertext)}',
        )


# -- the pods --------------------------------------------------------------------------


def _pod_security(user: int) -> k8s.core.v1.PodSecurityContextArgs:
    return k8s.core.v1.PodSecurityContextArgs(
        run_as_non_root=True,
        run_as_user=user,
        run_as_group=user,
        fs_group=user,
        seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
    )


def _container_security(*, read_only: bool = False) -> k8s.core.v1.SecurityContextArgs:
    return k8s.core.v1.SecurityContextArgs(
        allow_privilege_escalation=False,
        read_only_root_filesystem=read_only or None,
        capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
    )


def _claim_volume(name: str, claim_name: pulumi.Input[str]) -> k8s.core.v1.VolumeArgs:
    return k8s.core.v1.VolumeArgs(
        name=name,
        persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(claim_name=claim_name),
    )


def _env(name: str, value: str) -> k8s.core.v1.EnvVarArgs:
    return k8s.core.v1.EnvVarArgs(name=name, value=value)


def _secret_env(name: str, selector: k8s.core.v1.SecretKeySelectorArgs) -> k8s.core.v1.EnvVarArgs:
    return k8s.core.v1.EnvVarArgs(name=name, value_from=k8s.core.v1.EnvVarSourceArgs(secret_key_ref=selector))


def _server(image: ImagePin, *, library: pulumi.Input[str], config: pulumi.Input[str]) -> k8s.core.v1.PodSpecArgs:
    """The server's pod: the library at `MEDIA`, the configuration at `CONFIG_FILE`, the database, Redis and the service by name."""
    # The operator's Secret of the owner's credential (CloudNativePG's
    # `<cluster>-app`).
    owner = f'{DATABASE}-app'
    ping = k8s.core.v1.HTTPGetActionArgs(path='/api/server/ping', port='http')
    container = k8s.core.v1.ContainerArgs(
        name='server',
        image=str(image),
        env=[
            _env('IMMICH_CONFIG_FILE', CONFIG_FILE),
            _env('IMMICH_MEDIA_LOCATION', MEDIA),
            _env('IMMICH_MACHINE_LEARNING_URL', f'http://{ML_SERVICE}:{ML_PORT}'),
            _env('DB_HOSTNAME', f'{DATABASE}-rw'),
            _env('DB_PORT', '5432'),
            _env('DB_DATABASE_NAME', DBNAME),
            _secret_env('DB_USERNAME', k8s.core.v1.SecretKeySelectorArgs(name=owner, key='username')),
            _secret_env('DB_PASSWORD', k8s.core.v1.SecretKeySelectorArgs(name=owner, key='password')),
            _env('REDIS_HOSTNAME', REDIS_SERVICE),
            _env('REDIS_PORT', str(REDIS_PORT)),
            _secret_env('REDIS_PASSWORD', REDIS_SECRET),
            *(_env(variable, value) for variable, value in watchdog.SERVER_ENV.items()),
        ],
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=SERVER_PORT, protocol='TCP')],
        # The ping answers at once while idle and times out only while the job
        # queue saturates the event loop, so liveness tolerates a slow reply
        # rather than restart the server, and readiness sheds traffic.
        startup_probe=k8s.core.v1.ProbeArgs(http_get=ping, period_seconds=10, timeout_seconds=10, failure_threshold=30),
        readiness_probe=k8s.core.v1.ProbeArgs(http_get=ping, period_seconds=10, timeout_seconds=5, failure_threshold=3),
        liveness_probe=k8s.core.v1.ProbeArgs(http_get=ping, period_seconds=30, timeout_seconds=10, failure_threshold=6),
        resources=_SERVER_RESOURCES,
        security_context=_container_security(),
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name='library', mount_path=MEDIA),
            k8s.core.v1.VolumeMountArgs(name='config', mount_path=_CONFIG_DIRECTORY, read_only=True),
        ],
    )
    return k8s.core.v1.PodSpecArgs(
        automount_service_account_token=False,
        enable_service_links=False,
        affinity=WORKER_POD_AFFINITY,
        security_context=_pod_security(USER),
        containers=[container],
        volumes=[
            _claim_volume('library', library),
            k8s.core.v1.VolumeArgs(
                name='config',
                secret=k8s.core.v1.SecretVolumeSourceArgs(
                    secret_name=config,
                    # The rendered file alone: the sealed key stays out of the
                    # container's filesystem.
                    items=[k8s.core.v1.KeyToPathArgs(key=_CONFIG_KEY, path=_CONFIG_KEY)],
                    default_mode=0o440,
                ),
            ),
        ],
    )


def _machine_learning(image: ImagePin, cache: Claim) -> k8s.core.v1.PodSpecArgs:
    """The machine-learning service's pod: its models on their claim, placed as the claim records."""
    ping = k8s.core.v1.HTTPGetActionArgs(path='/ping', port='http')
    container = k8s.core.v1.ContainerArgs(
        name='machine-learning',
        image=str(image),
        env=[
            _env('MACHINE_LEARNING_CACHE_FOLDER', CACHE),
            _env('TRANSFORMERS_CACHE', CACHE),
            _env('HF_XET_CACHE', f'{CACHE}/huggingface-xet'),
            # Matplotlib's default is under a home the pod's user does not have.
            _env('MPLCONFIGDIR', f'{CACHE}/matplotlib-config'),
        ],
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=ML_PORT, protocol='TCP')],
        # No model is preloaded, so the service answers once its server is up
        # and downloads each model on the first request that needs it.
        startup_probe=k8s.core.v1.ProbeArgs(http_get=ping, period_seconds=10, failure_threshold=60),
        readiness_probe=k8s.core.v1.ProbeArgs(http_get=ping, period_seconds=10),
        liveness_probe=k8s.core.v1.ProbeArgs(http_get=ping, period_seconds=30, timeout_seconds=10),
        resources=_ML_RESOURCES,
        security_context=_container_security(),
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='cache', mount_path=CACHE)],
    )
    return k8s.core.v1.PodSpecArgs(
        automount_service_account_token=False,
        enable_service_links=False,
        affinity=cache.placement,
        security_context=_pod_security(USER),
        containers=[container],
        volumes=[_claim_volume('cache', cache.claim.metadata.name)],
    )


def _redis(image: ImagePin, exporter_image: ImagePin) -> k8s.core.v1.PodSpecArgs:
    """Redis's pod: the queues in memory alone, behind the ported password, with its metrics exporter beside it."""
    container = k8s.core.v1.ContainerArgs(
        name='redis',
        image=str(image),
        command=['redis-server'],
        # The kubelet expands a variable defined in the container's `env`. No
        # snapshot and no append-only file: a lost queue re-runs.
        args=['--requirepass', '$(REDIS_PASSWORD)', '--save', '', '--appendonly', 'no'],
        env=[_secret_env('REDIS_PASSWORD', REDIS_SECRET)],
        ports=[k8s.core.v1.ContainerPortArgs(name='redis', container_port=REDIS_PORT, protocol='TCP')],
        readiness_probe=k8s.core.v1.ProbeArgs(
            tcp_socket=k8s.core.v1.TCPSocketActionArgs(port='redis'), period_seconds=10
        ),
        resources=_REDIS_RESOURCES,
        security_context=_container_security(read_only=True),
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='data', mount_path='/data')],
    )
    exporter = k8s.core.v1.ContainerArgs(
        name='exporter',
        image=str(exporter_image),
        env=[
            _env('REDIS_ADDR', f'redis://localhost:{REDIS_PORT}'),
            _secret_env('REDIS_PASSWORD', REDIS_SECRET),
        ],
        ports=[k8s.core.v1.ContainerPortArgs(name='metrics', container_port=REDIS_METRICS, protocol='TCP')],
        readiness_probe=k8s.core.v1.ProbeArgs(
            http_get=k8s.core.v1.HTTPGetActionArgs(path='/health', port='metrics'), period_seconds=30
        ),
        resources=_EXPORTER_RESOURCES,
        security_context=_container_security(read_only=True),
    )
    return k8s.core.v1.PodSpecArgs(
        automount_service_account_token=False,
        enable_service_links=False,
        affinity=WORKER_POD_AFFINITY,
        security_context=_pod_security(REDIS_USER),
        containers=[container, exporter],
        # Its working directory, which it keeps nothing in.
        volumes=[k8s.core.v1.VolumeArgs(name='data', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs())],
    )


# -- the recovery layer ----------------------------------------------------------
#
# The land recovers the database once from the legacy store, where the legacy
# cluster archived under its `Cluster`'s name, with the legacy Secret's
# service-account key ported as it is (rfc-008 §4.3). After the recovery the
# database archives to the backup bucket, and nothing reads the legacy store.
#
# The layer is everything in this module and in `tests/test_apps_immich.py`
# that names the recovery. Today that is `RECOVERY` with its entry in
# `__all__`, the import of `Recovery`, `_RECOVERY_KEY`, `_LEGACY_STORE`,
# `_LEGACY_SERVER` and `_recovery` here, the module docstring's paragraph on
# the land's recovery, and the `recovery` parameter of `Immich` with its
# sentence in the class's docstring, its refusal and the `recovery` it hands
# `database`; and there the import of `RECOVERY`, `RECOVERY_CIPHERTEXT`, the
# `recovery` its `arguments` hands and the cases under "the recovery layer".
# Every other case reads the declared bootstrap whichever way it bootstraps,
# so it holds once the layer is gone. The layer goes with Wave C's cut, slice
# 41 (rfc-008 §10.3), once the database has archived a base backup of its
# own. The `Cluster`'s `bootstrap` and `externalClusters` change with it,
# which that slice's preview shows.

#: The legacy Secret holding the service account's key that reads the legacy
#: store, ported as it is.
RECOVERY = PortedSecret(name='immich-db', namespace=NAMESPACE, keys=('gcs_credentials',))
(_RECOVERY_KEY,) = RECOVERY.keys

#: The store the legacy database archives to, and its server name there: the
#: legacy `Cluster`'s name, which Pulumi suffixed. Both are the legacy
#: cluster's own, read from its `Cluster` `immich/immich-db-b925d327`, whose
#: `backup.barmanObjectStore` names this destination and no server name, so
#: barman archives under the cluster's name.
_LEGACY_STORE = 'gs://immich-postgresql-backup'
_LEGACY_SERVER = 'immich-db-b925d327'


def _recovery(recovery: Mapping[str, pulumi.Input[str]]) -> Recovery:
    """The legacy store the database recovers from once, read with the ported key."""
    return Recovery(
        destination_path=_LEGACY_STORE,
        server_name=_LEGACY_SERVER,
        key=_RECOVERY_KEY,
        ciphertext=recovery[_RECOVERY_KEY],
    )


# -- the component -----------------------------------------------------------------------


class Immich(Component, pulumi_type='kluster:apps:Immich'):
    """The photo library: its namespace, database, configuration and storage, its Deployments, its watchdog and its exposure.

    `exposure` is its row of the route census and `client` its row of the
    single sign-on census; `issuer` is the portal's issuer URL. `server_image`,
    `ml_image`, `redis_image`, `exporter_image`, `database_image` and
    `drill_image` are the pins it runs. `redis` and `config` are the
    ciphertexts `kluster.lib.k8s.sealed_ciphertext` reads for the ported
    Secrets `REDIS` and `CONFIG`; `recovery` the one it reads for `RECOVERY`,
    which the recovery layer reads (above). `writer_key` is the namespace's
    sealed writer key
    of the barman family, which the database's archive is written with.
    `fronts` and `zone_ids` are what the program read for `expose`. The
    providers arrive in `opts`: the Kubernetes provider for the cluster's
    objects and the Cloudflare one for its record.
    """

    namespace: ApplicationNamespace
    redis_password: crds.bitnami.v1alpha1.SealedSecret
    config: crds.bitnami.v1alpha1.SealedSecret
    library: NasClaim
    cache: Claim
    database: Database
    redis: k8s.apps.v1.Deployment
    redis_service: k8s.core.v1.Service
    machine_learning: k8s.apps.v1.Deployment
    ml_service: k8s.core.v1.Service
    server: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    watchdog: watchdog.Watchdog
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        client: Client,
        issuer: str,
        server_image: ImagePin,
        ml_image: ImagePin,
        redis_image: ImagePin,
        exporter_image: ImagePin,
        database_image: ImagePin,
        drill_image: ImagePin,
        redis: Mapping[str, pulumi.Input[str]],
        config: Mapping[str, pulumi.Input[str]],
        recovery: Mapping[str, pulumi.Input[str]],
        writer_key: Mapping[str, pulumi.Input[str]],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # Refused before anything is registered.
        served = _served(exposure)
        _refuse_client(served, client)
        _refuse_ported(REDIS, redis)
        _refuse_ported(CONFIG, config)
        _refuse_ported(RECOVERY, recovery)
        template = _configuration_template(served.url, client, issuer=issuer)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(
            f'{name}-namespace', name=NAMESPACE, api_clients=_API_CLIENTS, opts=self.child_opts()
        )
        # Read off the namespace, so each object in it waits for it; a value,
        # a claim or the database names its namespace as a string, so its
        # objects wait for it here.
        namespace = self.namespace.namespace.metadata.name
        behind = self.child_opts(depends_on=[self.namespace.namespace])

        self.redis_password = sealed_value(f'{name}-redis-password', REDIS, ciphertext=redis, opts=behind)
        self.config = sealed_secret(
            f'{name}-config',
            name=CONFIG.name,
            namespace=CONFIG.namespace,
            encrypted_data=config,
            template=SecretTemplate(data={_CONFIG_KEY: template}),
            scope=CONFIG.scope,
            opts=behind,
        )
        self.library = nas_claim(
            f'{name}-library', 'library', LIBRARY, namespace=NAMESPACE, read_only=False, opts=behind
        )
        self.cache = claim(
            f'{name}-cache',
            'cache',
            _CACHE_GIB,
            off('the models, which the service downloads again'),
            namespace=NAMESPACE,
            uid=USER,
            # Read by no mover under `off`; the namespace's one writer key is
            # the barman family's.
            writer_key=writer_key,
            placement=WORKER_POD_AFFINITY,
            opts=behind,
        )
        self.database = database(
            f'{name}-database',
            DATABASE,
            _DATABASE_GIB,
            PRECIOUS,
            namespace=NAMESPACE,
            image=database_image,
            dbname=DBNAME,
            resources=_DATABASE_RESOURCES,
            node_affinity=WORKER_AFFINITY_JSON['nodeAffinity'],
            writer_key=writer_key,
            drill=_DRILL,
            drill_image=drill_image,
            recovery=_recovery(recovery),
            shared_preload_libraries=_PRELOAD,
            post_init_application_sql=APPLICATION_SQL,
            opts=behind,
        )

        self.redis = self._deployment(
            f'{name}-redis',
            REDIS_SERVICE,
            REDIS_LABELS,
            _redis(redis_image, exporter_image),
            namespace=namespace,
            reload=True,
        )
        self.redis_service = self._service(f'{name}-redis', REDIS_SERVICE, REDIS_LABELS, REDIS_PORT, 'redis', namespace)
        self.machine_learning = self._deployment(
            f'{name}-machine-learning',
            ML_SERVICE,
            ML_LABELS,
            _machine_learning(ml_image, self.cache),
            namespace=namespace,
        )
        self.ml_service = self._service(f'{name}-machine-learning', ML_SERVICE, ML_LABELS, ML_PORT, 'http', namespace)
        self.server = self._deployment(
            f'{name}-server',
            SERVER_SERVICE,
            # The relay admits a sender by this label.
            {**SERVER_LABELS, **conventions.MAIL_SENDER_LABEL},
            _server(server_image, library=self.library.claim.metadata.name, config=self.config.metadata.name),
            namespace=namespace,
            selector=SERVER_LABELS,
            reload=True,
        )
        self.service = self._service(f'{name}-server', SERVER_SERVICE, SERVER_LABELS, SERVER_PORT, 'http', namespace)
        self.watchdog = watchdog.job_slot_watchdog(
            f'{name}-watchdog',
            namespace=NAMESPACE,
            image=server_image,
            server_labels=SERVER_LABELS,
            redis_host=REDIS_SERVICE,
            redis_port=REDIS_PORT,
            redis_password=REDIS_SECRET,
            user=USER,
            affinity=WORKER_POD_AFFINITY,
            opts=behind,
        )
        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=Backend(service=self.service.metadata.name, port=SERVER_PORT),
            selector=None,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    def _deployment(
        self,
        resource_name: str,
        name: str,
        labels: Mapping[str, str],
        pod: k8s.core.v1.PodSpecArgs,
        *,
        namespace: pulumi.Input[str],
        selector: Mapping[str, str] | None = None,
        reload: bool = False,
    ) -> k8s.apps.v1.Deployment:
        """One replica of `pod`, stopped before its successor starts, rolled by Reloader where it reads a Secret at its start."""
        return k8s.apps.v1.Deployment(
            resource_name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=name,
                namespace=namespace,
                annotations={'reloader.stakater.com/auto': 'true'} if reload else None,
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(selector or labels)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(labels)), spec=pod
                ),
            ),
            opts=self.child_opts(),
        )

    def _service(
        self,
        resource_name: str,
        name: str,
        selector: Mapping[str, str],
        port: int,
        target: str,
        namespace: pulumi.Input[str],
    ) -> k8s.core.v1.Service:
        return k8s.core.v1.Service(
            resource_name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=name, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(selector),
                ports=[k8s.core.v1.ServicePortArgs(name=target, port=port, target_port=target, protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
