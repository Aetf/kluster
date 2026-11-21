"""SplitPro, declared against mocks from its row of the single sign-on census, and its pinned image run as the pod runs it.

The server is declared with the census's `splitpro` client, since the server
is configured from it, and with a route row, image pins, ciphertexts and an
issuer of the test's own, so what is held is how the
component composes what it is handed.

Literals written here are the upstream projects' contracts: SplitPro's
environment variables and the path NextAuth sends a browser back to, which
NextAuth derives from the provider's name; the directory SplitPro writes
receipts to, `uploads` under its working directory; Next.js's response header
for its image cache; CloudNativePG's name for the Secret it generates for a
database's owner and that Secret's `uri` key; the Kubernetes, Cilium and
CloudNativePG fields; the sealed-secrets controller's scope annotation; and the
label Pod Security Admission reads.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess
import uuid
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from node_selection import schedulable
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, PULL_TIMEOUT, pinned, pulled, require_podman

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.splitpro import (
    APPLICATION_SQL,
    AUTH,
    NAMESPACE,
    RECOVERY_KEY,
    Refusal,
    SplitPro,
    SplitProRefused,
)
from kluster.components.apps.storage import claim_name
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.backup import BACKED_NAMESPACES, PRECIOUS, BackupFamily
from kluster.conventions.routes import PROXIED, REWRITE, Exposure, Gate, Held, Proxied, Route
from kluster.lib.versions import ImagePin

NAMESPACE_TYPE = 'kubernetes:core/v1:Namespace'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
SERVICE = 'kubernetes:core/v1:Service'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
CLUSTER = 'crds:postgresql.cnpg.io/v1:Cluster'
OBJECT_STORE = 'crds:barmancloud.cnpg.io/v1:ObjectStore'
SCHEDULED_BACKUP = 'crds:postgresql.cnpg.io/v1:ScheduledBackup'
CRONJOB = 'kubernetes:batch/v1:CronJob'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'

#: The census's client, which the program hands the server.
(CLIENT,) = [client for client in conventions.sso.CLIENTS if client.name == 'splitpro']

#: The route row the server is declared with: its client's host, behind the
#: proxy, held as the land declares it.
EXPOSURE = Exposure(routes=(Held(Route(CLIENT.host, internet=PROXIED)),))

#: The pins it is declared with here: references that are no published image,
#: so a container carrying one carries that pin.
IMAGE = ImagePin('registry.example.invalid/splitpro', 'v9.9.9', f'sha256:{"a" * 64}')
DATABASE_IMAGE = ImagePin('registry.example.invalid/pgcron', '17.9', f'sha256:{"b" * 64}')
DRILL_IMAGE = ImagePin('registry.example.invalid/drill', '1.0', f'sha256:{"c" * 64}')

#: The portal's issuer, as the program would hand it.
ISSUER = conventions.sso.Issuer('https://auth.example.invalid')

#: The ciphertexts it is handed, each naming what it stands for.
CIPHERTEXT = {
    'nextauth_secret': 'sealed-session-secret',
    'oidc_client_id': 'sealed-client-id',
    'oidc_client_secret': 'sealed-client-secret',
}
RECOVERY_CIPHERTEXT = {'gcs_credentials': 'sealed-service-account-key'}
DATABASE_WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-barman-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-barman-secret'}
RECEIPTS_WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-volsync-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-volsync-secret'}

FRONTS = Fronts(
    internet=InternetPoolMembers(
        node_private_ips={'cp-1': '10.0.1.11'},
        node_guas={'cp-1': '2001:db8:1::11'},
        dedicated_vip='10.0.1.99',
        balancer_v4='203.0.113.10',
        balancer_v6='2001:db8::10',
    ),
    worker_gua='2001:db8:70::10',
)
ZONE_IDS = {conventions.ZONE_PRIMARY: 'the-primary-zones-id'}

#: The path NextAuth sends a browser back to, under its own URL: its callback
#: route and the provider's identifier, the provider's name lowercased.
UPSTREAM_CALLBACK = '/api/auth/callback/'


def declare(
    *,
    exposure: Exposure = EXPOSURE,
    client: conventions.sso.Client = CLIENT,
    auth: Mapping[str, str] = CIPHERTEXT,
    recovery_key: Mapping[str, str] = RECOVERY_CIPHERTEXT,
) -> SplitPro:
    return SplitPro(
        'splitpro',
        exposure=exposure,
        client=client,
        issuer=ISSUER,
        image=IMAGE,
        database_image=DATABASE_IMAGE,
        drill_image=DRILL_IMAGE,
        auth=auth,
        recovery_key=recovery_key,
        database_writer_key=DATABASE_WRITER_KEY,
        receipts_writer_key=RECEIPTS_WRITER_KEY,
        fronts=FRONTS,
        zone_ids=ZONE_IDS,
    )


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The server, declared from the census's client with the test's route, pins and ciphertexts."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = declare()
    return monitor


def the_one(monitor: Recorder, typ: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(typ)]
    return found


def pod_of(monitor: Recorder) -> dict[str, Any]:
    return the_one(monitor, DEPLOYMENT)['spec']['template']


def container_of(monitor: Recorder) -> dict[str, Any]:
    (container,) = pod_of(monitor)['spec']['containers']
    return container


def env(monitor: Recorder) -> dict[str, dict[str, Any]]:
    """The container's environment, by variable."""
    return {it['name']: it for it in container_of(monitor)['env']}


def value(monitor: Recorder, variable: str) -> str:
    return env(monitor)[variable]['value']


def secret_of(monitor: Recorder, variable: str) -> dict[str, str]:
    return env(monitor)[variable]['valueFrom']['secretKeyRef']


def sealed_secret(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == name]
    return found


def holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether `labels` satisfy one requirement of a Kubernetes or Cilium selector."""
    present = expression['key'] in labels
    match expression['operator']:
        case 'Exists':
            return present
        case 'DoesNotExist':
            return not present
        case operator:
            raise AssertionError(f'no reading of {operator!r} here')


# -- the pod ---------------------------------------------------------------------


def test_the_server_runs_as_a_numeric_user_that_is_not_root(declared: Recorder) -> None:
    """Its image names no user, and `restricted` refuses a pod that does not say it runs as one that is not root."""
    context = pod_of(declared)['spec']['securityContext']
    container = container_of(declared)

    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities'] == {'drop': ['ALL']}


def test_the_namespace_enforces_restricted(declared: Recorder) -> None:
    namespace = the_one(declared, NAMESPACE_TYPE)

    assert namespace['metadata']['labels']['pod-security.kubernetes.io/enforce'] == 'restricted'


def test_the_server_and_its_database_carry_cpu_and_memory_limits(declared: Recorder) -> None:
    """The scheduler may place either on a cloud node, where etcd shares the cores, so each is held to both (declarative/workloads.md §1)."""
    server = container_of(declared)['resources']
    database = the_one(declared, CLUSTER)['spec']['resources']

    for resources in (server, database):
        assert {'cpu', 'memory'} <= resources['limits'].keys()
        assert {'cpu', 'memory'} <= resources['requests'].keys()


def test_the_server_listens_on_the_port_its_service_and_probes_reach(declared: Recorder) -> None:
    service = the_one(declared, SERVICE)
    container = container_of(declared)
    (port,) = service['spec']['ports']
    named = {it['name']: it['containerPort'] for it in container['ports']}

    assert service['spec']['selector'].items() <= pod_of(declared)['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port'] == int(value(declared, 'PORT'))
    for probe in ('startupProbe', 'readinessProbe'):
        assert container[probe]['httpGet']['port'] == port['targetPort']


def volume_at(monitor: Recorder, path: str) -> dict[str, Any]:
    """The pod's volume its one container mounts at `path`."""
    (mount,) = [it for it in container_of(monitor)['volumeMounts'] if it['mountPath'] == path]
    (volume,) = [it for it in pod_of(monitor)['spec']['volumes'] if it['name'] == mount['name']]
    return volume


#: Where SplitPro writes receipts, `uploads` under its working directory,
#: which the pinned image's case holds to the image's.
RECEIPTS = '/app/uploads'


def test_the_receipts_are_on_a_claim_backed_up_as_precious_and_protected(declared: Recorder) -> None:
    """A receipt annotates an expense the database keeps as `PRECIOUS`, and is no more replaceable than the row."""
    name = claim_name('uploads', 0)
    (source,) = [it.inputs for it in declared.of_type(SOURCE)]
    (logical,) = [it.name for it in declared.of_type(CLAIM) if it.inputs['metadata']['name'] == name]

    assert volume_at(declared, RECEIPTS)['persistentVolumeClaim']['claimName'] == name
    assert source['spec']['sourcePVC'] == name
    assert source['metadata']['labels'][conventions.RETENTION_LABEL] == PRECIOUS.name
    assert declared.options_of(logical, CLAIM).protect is True


def test_the_receipts_movers_run_as_the_server_so_they_read_and_write_its_files(declared: Recorder) -> None:
    (source,) = [it.inputs for it in declared.of_type(SOURCE)]

    assert (
        source['spec']['restic']['moverSecurityContext']['runAsUser']
        == pod_of(declared)['spec']['securityContext']['runAsUser']
    )


def test_the_receipts_are_backed_up_from_a_backed_namespace_with_its_writer_key(declared: Recorder) -> None:
    """The writer key `physical` mints is per backed namespace and family, so the receipts' repository is in one."""
    namespace = the_one(declared, NAMESPACE_TYPE)['metadata']['name']
    keys = [
        it.inputs for it in declared.of_type(SEALED_SECRET) if it.inputs['spec']['encryptedData'] == RECEIPTS_WRITER_KEY
    ]

    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[namespace]
    assert [key['metadata']['namespace'] for key in keys] == [namespace]


def test_the_claims_movers_are_placed_where_the_server_is(declared: Recorder) -> None:
    """The claim binds on whichever of the server and a mover is scheduled first, so both admit the same nodes."""
    (source,) = [it.inputs for it in declared.of_type(SOURCE)]

    assert schedulable({'affinity': source['spec']['restic'].get('moverAffinity')}) == schedulable(
        pod_of(declared)['spec']
    )


def test_the_server_writes_nothing_else_it_keeps(declared: Recorder) -> None:
    """Next.js's cache and the uploads staged in `/tmp` are rebuilt or consumed, so each is an empty directory."""
    others = [it['mountPath'] for it in container_of(declared)['volumeMounts'] if it['mountPath'] != RECEIPTS]

    assert others
    assert all('emptyDir' in volume_at(declared, path) for path in others), others


# -- the database ----------------------------------------------------------------


def database(monitor: Recorder) -> dict[str, Any]:
    return the_one(monitor, CLUSTER)


def owner_and_database(monitor: Recorder) -> tuple[str, str]:
    """The owner and the database the declared cluster's bootstrap names, whichever way it bootstraps."""
    (bootstrap,) = database(monitor)['spec']['bootstrap'].values()
    return bootstrap['owner'], bootstrap['database']


def test_the_server_connects_with_the_secret_the_operator_generates_for_the_databases_owner(
    declared: Recorder,
) -> None:
    """CloudNativePG writes `<cluster>-app` for the owner the bootstrap names, with a ready connection string at `uri`."""
    cluster = database(declared)

    assert secret_of(declared, 'DATABASE_URL') == {'name': f'{cluster["metadata"]["name"]}-app', 'key': 'uri'}


def test_pg_cron_is_preloaded_into_the_database_the_server_uses_and_drilled_for(declared: Recorder) -> None:
    """`pg_cron` keeps its jobs in one database, which has to be the application's, and the drill checks it is there."""
    cluster = database(declared)
    postgresql = cluster['spec']['postgresql']
    (drill,) = [it.inputs for it in declared.of_type(CRONJOB)]
    (container,) = drill['spec']['jobTemplate']['spec']['template']['spec']['containers']
    drilled = {it['name']: it['value'] for it in container['env']}

    assert 'pg_cron' in postgresql['shared_preload_libraries']
    assert postgresql['parameters']['cron.database_name'] == owner_and_database(declared)[1]
    assert drilled['DATABASE'] == postgresql['parameters']['cron.database_name']
    assert 'pg_cron' in drilled['EXTENSIONS'].split()


def test_the_database_is_archived_as_precious_from_a_backed_namespace_with_its_writer_key(declared: Recorder) -> None:
    """The writer key `physical` mints is per backed namespace and family, so the archive is written from one."""
    namespace = the_one(declared, NAMESPACE_TYPE)['metadata']['name']
    backup = the_one(declared, SCHEDULED_BACKUP)
    keys = [
        it.inputs for it in declared.of_type(SEALED_SECRET) if it.inputs['spec']['encryptedData'] == DATABASE_WRITER_KEY
    ]

    assert BackupFamily.BARMAN in BACKED_NAMESPACES[namespace]
    assert database(declared)['metadata']['namespace'] == namespace
    assert backup['metadata']['labels'][conventions.RETENTION_LABEL] == PRECIOUS.name
    assert [key['metadata']['namespace'] for key in keys] == [namespace]


def test_the_database_reaches_the_api_and_the_server_does_not(declared: Recorder) -> None:
    """The instances keep their cluster's status through the API; the server speaks no Kubernetes and is denied it."""
    (block,) = [it.inputs for it in declared.of_type(POLICY) if it.inputs['metadata']['name'] == 'egress-block']
    (api,) = [spec for spec in block['specs'] if denied_ports(spec) == [str(conventions.MANAGEMENT_PORTS.kubernetes)]]
    selector = api['endpointSelector'].get('matchExpressions', [])
    instance = database(declared)['spec']['inheritedMetadata']['labels']
    server = pod_of(declared)['metadata']['labels']

    assert not all(holds(expression, instance) for expression in selector)
    assert all(holds(expression, server) for expression in selector)
    assert pod_of(declared)['spec']['automountServiceAccountToken'] is False


def denied_ports(spec: Mapping[str, Any]) -> list[str]:
    """Every port a policy's egress deny names, in order."""
    return [port['port'] for deny in spec['egressDeny'] for to in deny.get('toPorts', []) for port in to['ports']]


# -- the client ------------------------------------------------------------------


def test_the_ported_secret_is_sealed_namespace_wide_into_the_namespace_it_was_sealed_to(declared: Recorder) -> None:
    secret = sealed_secret(declared, AUTH.name)

    assert secret['metadata']['namespace'] == NAMESPACE == the_one(declared, NAMESPACE_TYPE)['metadata']['name']
    assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}
    assert secret['spec']['encryptedData'] == CIPHERTEXT


def test_the_session_secret_and_the_client_reach_the_server_from_the_ported_secret(declared: Recorder) -> None:
    for variable, key in (
        ('NEXTAUTH_SECRET', 'nextauth_secret'),
        ('OIDC_CLIENT_ID', 'oidc_client_id'),
        ('OIDC_CLIENT_SECRET', 'oidc_client_secret'),
    ):
        assert secret_of(declared, variable) == {'name': AUTH.name, 'key': key}, variable


def test_the_server_returns_a_browser_to_an_address_the_portal_registers(declared: Recorder) -> None:
    """NextAuth builds the address from its URL and the provider's identifier, its name lowercased."""
    address = f'{value(declared, "NEXTAUTH_URL")}{UPSTREAM_CALLBACK}{value(declared, "OIDC_NAME").lower()}'

    assert address in conventions.sso.redirect_uris(CLIENT)


def test_the_server_discovers_the_portal_at_the_issuer_it_is_handed(declared: Recorder) -> None:
    assert value(declared, 'OIDC_WELL_KNOWN_URL') == f'{ISSUER.url}/.well-known/openid-configuration'


def test_a_client_that_does_not_register_the_servers_address_is_refused() -> None:
    client = dataclasses.replace(CLIENT, paths=('/elsewhere',))

    with pytest.raises(SplitProRefused) as refused:
        _ = declare(client=client)

    assert refused.value.refusal is Refusal.REDIRECT


def test_a_client_that_does_not_grant_a_scope_the_server_asks_for_is_refused() -> None:
    """SplitPro asks for `openid email profile` whatever the row says, and the portal refuses a scope it did not grant."""
    client = dataclasses.replace(
        CLIENT,
        scopes=(conventions.sso.Scope.OPENID, conventions.sso.Scope.EMAIL),
        id_token=(conventions.sso.Claim.EMAIL,),
    )

    with pytest.raises(SplitProRefused) as refused:
        _ = declare(client=client)

    assert refused.value.refusal is Refusal.SCOPE


def test_a_client_that_authenticates_in_the_form_body_is_refused() -> None:
    """NextAuth's OpenID Connect library sends HTTP Basic, and the portal refuses a method it did not register."""
    client = dataclasses.replace(CLIENT, token_endpoint_auth=conventions.sso.TokenEndpointAuth.POST)

    with pytest.raises(SplitProRefused) as refused:
        _ = declare(client=client)

    assert refused.value.refusal is Refusal.TOKEN_ENDPOINT_AUTH


def test_a_client_that_requires_pkce_is_refused() -> None:
    """NextAuth's provider sends a `state` and no code challenge, so the portal would refuse every sign-in."""
    client = dataclasses.replace(CLIENT, pkce=True)

    with pytest.raises(SplitProRefused) as refused:
        _ = declare(client=client)

    assert refused.value.refusal is Refusal.PKCE


def test_a_client_whose_id_token_lacks_the_email_is_refused() -> None:
    """NextAuth builds the profile from the ID token alone, and the email is the only key SplitPro matches people by."""
    client = dataclasses.replace(
        CLIENT, id_token=tuple(it for it in CLIENT.id_token if it is not conventions.sso.Claim.EMAIL)
    )

    with pytest.raises(SplitProRefused) as refused:
        _ = declare(client=client)

    assert refused.value.refusal is Refusal.EMAIL_CLAIM


def test_a_ported_secret_short_of_its_keys_is_refused() -> None:
    """A pod reading a key the ported Secret lacks would never start, which is found only live."""
    with pytest.raises(SplitProRefused) as refused:
        _ = declare(auth={key: CIPHERTEXT[key] for key in AUTH.keys[:1]})

    assert refused.value.refusal is Refusal.PORTED_KEYS


# -- the recovery layer ----------------------------------------------------------
#
# What `kluster.components.apps.splitpro`'s recovery layer declares, which goes
# with it: these cases, `RECOVERY_CIPHERTEXT` and the `recovery_key` `declare`
# hands.


def test_the_database_recovers_from_the_legacy_server_with_the_ported_credential(declared: Recorder) -> None:
    """The source is a server other than the one the database archives as, read through the ported ciphertext."""
    cluster = database(declared)
    (archiver,) = cluster['spec']['plugins']
    (source,) = cluster['spec']['externalClusters']
    parameters = source['plugin']['parameters']
    (store,) = [
        it.inputs
        for it in declared.of_type(OBJECT_STORE)
        if it.inputs['metadata']['name'] == parameters['barmanObjectName']
    ]
    reference = store['spec']['configuration']['googleCredentials']['applicationCredentials']
    credential = sealed_secret(declared, reference['name'])

    assert cluster['spec']['bootstrap']['recovery']['source'] == source['name']
    assert parameters['serverName'] != archiver['parameters']['serverName']
    assert store['spec']['configuration']['destinationPath'].startswith('gs://')
    assert credential['spec']['encryptedData'] == {reference['key']: RECOVERY_CIPHERTEXT[RECOVERY_KEY.keys[0]]}
    assert credential['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}
    assert credential['metadata']['namespace'] == the_one(declared, NAMESPACE_TYPE)['metadata']['name']


def test_a_recovery_key_short_of_its_key_is_refused() -> None:
    with pytest.raises(SplitProRefused) as refused:
        _ = declare(recovery_key={})

    assert refused.value.refusal is Refusal.PORTED_KEYS


# -- the exposure ----------------------------------------------------------------


def test_the_route_at_the_clients_host_forwards_to_the_server_ungated(declared: Recorder) -> None:
    """The sign-in flow returns to the server with no portal session, so its route carries no gate."""
    (route,) = [it.inputs for it in declared.of_type(HTTP_ROUTE)]
    service = the_one(declared, SERVICE)
    (port,) = service['spec']['ports']
    filters = [item for rule in route['spec']['rules'] for item in rule.get('filters', [])]

    assert route['spec']['hostnames'] == [f'{CLIENT.host}.{CLIENT.zone}']
    assert route['spec']['rules'][0]['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
    assert not [item for item in filters if item['type'] == 'ExtensionRef']


def test_a_held_row_publishes_no_record(declared: Recorder) -> None:
    assert not declared.of_type(RECORD)


@pytest.mark.parametrize(
    'exposure',
    [
        Exposure(routes=(Route('elsewhere', internet=PROXIED),)),
        Exposure(routes=(Route(CLIENT.host, internet=PROXIED, zones=('other.example',)),)),
        Exposure(routes=(Route(CLIENT.host, lan=REWRITE),)),
    ],
    ids=['another host', 'another zone', 'home alone'],
)
def test_an_exposure_with_no_route_at_the_clients_host_is_refused(exposure: Exposure) -> None:
    with pytest.raises(SplitProRefused) as refused:
        _ = declare(exposure=exposure)

    assert refused.value.refusal is Refusal.NO_ROUTE


def test_a_gated_route_at_the_clients_host_is_refused() -> None:
    exposure = Exposure(routes=(Route(CLIENT.host, internet=Proxied(Gate.SESSION)),))

    with pytest.raises(SplitProRefused) as refused:
        _ = declare(exposure=exposure)

    assert refused.value.refusal is Refusal.GATED


# -- the pinned images, run as the pod runs them ----------------------------------

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_splitpro`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why these cases need podman.
WHY = 'the server is run from the image it is pinned to, beside the database image it is pinned with'

#: The stop-loss on each wait for a container to start answering: the stand-in
#: database's listener, and the server's first answer after its migrations.
READY_TIMEOUT = 60

#: The bound on each case that runs the pinned images. The first one pays for
#: both pulls and both starts, which the module's fixture makes; every case
#: then makes a few commands of its own, and the last one removes the two
#: containers. A stop-loss, which nothing asserts on.
SERVER_CASE_TIMEOUT = 2 * PULL_TIMEOUT + 2 * READY_TIMEOUT + 8 * COMMAND_TIMEOUT

#: The owner's password the stand-in database is made with, and so the
#: connection string the operator's Secret would carry for it.
PASSWORD = 'stub-owner-password'

#: The client rules of the `pg_hba.conf` CloudNativePG generates for every
#: instance (its documentation, `postgresql_conf.md`, "The `pg_hba` section"),
#: less the certificate rules, which no connection here presents a certificate
#: for: a local connection as the user the operating system names, and any
#: other with a password.
CNPG_PG_HBA = 'local all all peer\nhost all all all scram-sha-256\n'

#: What the stand-in for the stub portal's plaintext is: the ported Secret's
#: keys, each a value naming itself.
PLAINTEXT = {key: f'stub-{key}' for key in AUTH.keys}

#: A file the image serves, which Next.js optimizes on request, and which
#: stands in for a receipt's photo.
IMAGE_PATH = '/logo.png'

#: The session cookie NextAuth reads where its URL is `https`, and the
#: session the stand-in database holds under it.
SESSION_COOKIE = '__Secure-next-auth.session-token'
SESSION_TOKEN = 'stub-session-token'

#: Asks the server, from its own container, as `argv[1]` says in JSON: a
#: `url`, with a `cookie`, and an `upload` of a file of the container's under
#: the form field SplitPro's upload route reads. A refused connection, which
#: is the server not listening yet, is tried again up to `tries` times. Prints
#: the status, the content type, the cache header, the body's length and a
#: text body.
ASK = """
const spec = JSON.parse(process.argv[1]);
(async () => {
  const init = {headers: spec.cookie ? {cookie: spec.cookie} : {}};
  if (spec.upload) {
    const form = new FormData();
    form.append('file', new Blob([require('fs').readFileSync(spec.upload)], {type: 'image/png'}), 'receipt.png');
    Object.assign(init, {method: 'POST', body: form});
  }
  for (let tried = 0; tried < spec.tries; tried++) {
    let response;
    try { response = await fetch(spec.url, init); }
    catch { await new Promise((resume) => setTimeout(resume, 500)); continue; }
    const type = response.headers.get('content-type') ?? '';
    const body = Buffer.from(await response.arrayBuffer());
    console.log(JSON.stringify({
      status: response.status, type, cache: response.headers.get('x-nextjs-cache'), length: body.length,
      body: type.startsWith('image/') ? '' : body.toString(),
    }));
    return;
  }
  process.exit(1);
})();
"""


def podman(*argv: str, timeout: float = COMMAND_TIMEOUT) -> str:
    done = process_sessions.run(['podman', *argv], timeout=timeout, check=True)
    return done.stdout.decode()


def stand_in_database(monitor: Recorder) -> str:
    """The database the server is pointed at: the declared cluster's settings, from an empty `initdb` and `APPLICATION_SQL`.

    What a database initialized empty runs is what the recovered one carries
    from the legacy cluster's `initdb`, so the stand-in runs it, and without
    it the server's migration cannot create `pg_cron` and the server never
    answers. `pg_cron`'s preload and its settings are the declared cluster's, under
    CloudNativePG's client rules, so a setting that puts the extension's jobs
    in another database stops `CREATE EXTENSION` here, and one that leaves a
    job to connect fails the job. It listens on the loopback only once it is
    made.
    """
    owner, name = owner_and_database(monitor)
    postgresql = database(monitor)['spec']['postgresql']
    settings = [
        f'shared_preload_libraries={",".join(postgresql["shared_preload_libraries"])}',
        *(f'{key}={setting}' for key, setting in postgresql['parameters'].items()),
        'unix_socket_directories=/tmp',
    ]
    options = ' '.join(f'-c {shlex.quote(setting)}' for setting in settings)
    psql = 'psql -h /tmp -U postgres -v ON_ERROR_STOP=1 -q'
    return '\n'.join([
        'set -e',
        'export PATH="$(echo /usr/lib/postgresql/*/bin):$PATH"',
        'initdb -D /tmp/data -U postgres --auth=trust >/dev/null',
        f'printf %s {shlex.quote(CNPG_PG_HBA)} > /tmp/data/pg_hba.conf',
        f'pg_ctl -D /tmp/data -w -o {shlex.quote(f"{options} -c listen_addresses=")} start >/dev/null',
        f'{psql} -c {shlex.quote(f"CREATE ROLE {owner} LOGIN PASSWORD {PASSWORD!r}")}',
        f'{psql} -c {shlex.quote(f"CREATE DATABASE {name} OWNER {owner}")}',
        *(f'{psql} -d {name} -c {shlex.quote(statement)}' for statement in APPLICATION_SQL),
        'pg_ctl -D /tmp/data -w stop >/dev/null',
        f'exec postgres -D /tmp/data {options} -c listen_addresses=127.0.0.1',
    ])  # fmt: skip


def secrets(monitor: Recorder) -> dict[tuple[str, str], str]:
    """The plaintext of each Secret key the server reads, by Secret and key: the stub's, and the stand-in database's."""
    owner, name = owner_and_database(monitor)
    return {
        **{(AUTH.name, key): plain for key, plain in PLAINTEXT.items()},
        (f'{database(monitor)["metadata"]["name"]}-app', 'uri'): (
            f'postgresql://{owner}:{PASSWORD}@127.0.0.1:5432/{name}'
        ),
    }


def as_the_pod(monitor: Recorder, name: str, reference: str, *, network: str) -> list[str]:
    """`podman create` options that run a container of `reference` as the declared pod runs its one container.

    The user, the root's mode, the capabilities, privilege escalation, each
    empty directory and the environment are the declaration's.
    """
    context = pod_of(monitor)['spec']['securityContext']
    container = container_of(monitor)
    security = container['securityContext']
    plaintext = secrets(monitor)
    variables = [
        f'--env={it["name"]}={plaintext[(ref["name"], ref["key"])] if (ref := it.get("valueFrom", {}).get("secretKeyRef")) else it["value"]}'
        for it in container['env']
    ]
    return [
        'create',
        '--rm',
        f'--timeout={CONTAINER_TIMEOUT}',
        f'--label={LABEL}',
        f'--name={name}',
        f'--network={network}',
        *(['--read-only', '--read-only-tmpfs=false'] if security.get('readOnlyRootFilesystem') else []),
        *(f'--cap-drop={capability.lower()}' for capability in security.get('capabilities', {}).get('drop', [])),
        *(['--security-opt=no-new-privileges'] if security.get('allowPrivilegeEscalation') is False else []),
        f'--user={int(context["runAsUser"])}:{int(context["runAsGroup"])}',
        # Each empty directory: one the kubelet makes every user's to write.
        *(f'--tmpfs={mount["mountPath"]}:mode=1777' for mount in container['volumeMounts']),
        *variables,
        reference,
    ]


@dataclass(frozen=True)
class Running:
    """The pinned server as it runs beside its stand-in database: what asks it, what runs SQL there, the working directory."""

    ask: Callable[..., dict[str, Any]]
    sql: Callable[..., str]
    workdir: str


@pytest.fixture(scope='module')
def running(declared: Recorder) -> Generator[Running]:
    """The pinned server, run as the pod runs it beside a database of the pinned database image."""
    require_podman(WHY)
    server_image = pulled(pinned('splitpro'))
    database_image = pulled(pinned('splitpro-database'))
    run = uuid.uuid4().hex[:12]
    stand_in, server = (f'kluster-test-splitpro-{role}-{run}' for role in ('database', 'server'))
    owner, name = owner_and_database(declared)

    def ask(path: str, *, cookie: str = '', upload: str = '', tries: int = 1) -> dict[str, Any]:
        spec = {'url': f'http://127.0.0.1:3000{path}', 'cookie': cookie, 'upload': upload, 'tries': tries}
        try:
            answer = podman(
                'exec', server, 'node', '-e', ASK, json.dumps(spec), timeout=READY_TIMEOUT + COMMAND_TIMEOUT
            )
        except subprocess.CalledProcessError as failed:
            # The server's last words, where it is still there to give them:
            # a server that exited is gone with its `--rm`.
            logs = process_sessions.run(['podman', 'logs', server], timeout=COMMAND_TIMEOUT)
            failed.add_note((logs.stdout + logs.stderr).decode(errors='replace')[-4000:])
            raise
        return json.loads(answer)

    def sql(statement: str, *, as_owner: bool = False) -> str:
        """`statement`'s rows in the application's database, as the superuser or, over TCP, as the owner."""
        who = ['--env', f'PGPASSWORD={PASSWORD}'] if as_owner else []
        where = ['-h', '127.0.0.1', '-U', owner] if as_owner else ['-h', '/tmp', '-U', 'postgres']
        return podman(
            'exec', *who, stand_in, 'psql', *where, '-d', name, '-v', 'ON_ERROR_STOP=1', '-qAt', '-c', statement
        )

    with process_sessions.removing(server, stand_in, timeout=COMMAND_TIMEOUT):
        _ = podman(
            'create', '--rm', f'--timeout={CONTAINER_TIMEOUT}', f'--label={LABEL}', f'--name={stand_in}',
            database_image, 'bash', '-c', stand_in_database(declared),
        )  # fmt: skip
        _ = podman('start', stand_in)
        # Waits on the stand-in's listener, which opens once it is made.
        _ = podman(
            'exec', stand_in, 'bash', '-c', 'until pg_isready -q -h 127.0.0.1; do sleep 0.2; done',
            timeout=READY_TIMEOUT,
        )  # fmt: skip
        _ = podman(*as_the_pod(declared, server, server_image, network=f'container:{stand_in}'))
        _ = podman('start', server)
        # The server answers once its migrations have run.
        _ = ask('/api/auth/providers', tries=2 * READY_TIMEOUT)
        workdir = podman('image', 'inspect', '--format', '{{.Config.WorkingDir}}', server_image).strip()
        yield Running(ask, sql, workdir)


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_the_pinned_server_signs_in_through_the_portal_alone_returning_where_the_census_registers(
    running: Running,
) -> None:
    """The server, run on its pod's environment, answers on its pod's loopback and registers one provider, the portal.

    No other provider means no sign-in that skips the portal's policy, and
    the address it sends a browser back to is one the row registers.
    """
    answer = running.ask('/api/auth/providers')
    providers = json.loads(answer['body'])

    assert answer['status'] == 200
    (provider,) = providers.values()
    assert provider['callbackUrl'] in conventions.sso.redirect_uris(CLIENT)


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_the_pinned_server_keeps_its_image_cache_on_a_read_only_root(running: Running) -> None:
    """Next.js writes optimized images under its cache, so a repeated request is answered from it."""
    path = f'/_next/image?url={IMAGE_PATH.replace("/", "%2F")}&w=64&q=75'
    # Next.js writes the cache after it answers, so a request soon after the
    # first one can still miss; a handful gives the write time to land.
    answers = [running.ask(path) for _ in range(5)]

    assert [answer['status'] for answer in answers] == [200] * len(answers)
    assert answers[-1]['cache'] == 'HIT'


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_a_receipt_uploaded_to_the_pinned_server_is_read_back(running: Running) -> None:
    """A signed-in user's receipt is staged in `/tmp`, resized into the receipts directory and served back from it."""
    user = running.sql("INSERT INTO \"User\" (name, email) VALUES ('Receipt', 'receipt@example.invalid') RETURNING id")
    _ = running.sql(
        'INSERT INTO "Session" (id, "sessionToken", "userId", expires) '
        f"VALUES ('stub-session', '{SESSION_TOKEN}', {int(user)}, now() + interval '1 day')"
    )
    cookie = f'{SESSION_COOKIE}={SESSION_TOKEN}'

    uploaded = running.ask('/api/upload', cookie=cookie, upload=f'{running.workdir}/public{IMAGE_PATH}')
    assert uploaded['status'] == 200, uploaded['body']
    served = running.ask(f'/api/files/{json.loads(uploaded["body"])["key"]}', cookie=cookie)

    assert served['status'] == 200
    assert served['type'] == 'image/webp'
    assert served['length'] > 0


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_the_receipts_directory_is_the_one_the_pinned_server_writes_to(declared: Recorder, running: Running) -> None:
    """SplitPro writes receipts to `uploads` under its working directory, which is the image's, and the claim is there."""
    assert f'{running.workdir}/uploads' == RECEIPTS
    assert 'persistentVolumeClaim' in volume_at(declared, RECEIPTS)


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_a_job_the_owner_schedules_runs_under_the_operators_client_rules(running: Running) -> None:
    """`pg_cron` runs the owner's job, where CloudNativePG's `pg_hba` admits a job's own connection only with a password.

    SplitPro's recurring expenses are such jobs. The job runs every second,
    and the wait is for its first run to finish, either way.
    """
    _ = running.sql('CREATE TABLE cron_probe (at timestamptz)', as_owner=True)
    job = running.sql(
        "SELECT cron.schedule('kluster-probe', '1 seconds', 'INSERT INTO cron_probe VALUES (now())')", as_owner=True
    )
    finished = f"SELECT status, return_message FROM cron.job_run_details WHERE jobid = {int(job)} AND status IN ('succeeded', 'failed') ORDER BY runid LIMIT 1"
    try:
        _ = running.sql(
            # Bounded inside the server as well, so the wait ends there too
            # when the exec's stop-loss ends the client.
            f"SET statement_timeout = '{COMMAND_TIMEOUT - 5}s'; "
            f'DO $$ BEGIN WHILE NOT EXISTS ({finished}) LOOP PERFORM pg_sleep(0.2); END LOOP; END $$',
            as_owner=True,
        )
        status = running.sql(finished, as_owner=True)
    finally:
        _ = running.sql("SELECT cron.unschedule('kluster-probe')", as_owner=True)

    assert status.split('|')[0] == 'succeeded', status
