"""immich, declared against mocks.

Every row here is the test's own: a route held while the legacy VPS keeps its
name, direct on `internet` and rewritten at home, under a name no census row
carries, with a single sign-on client under the route's name that
authenticates at the token endpoint by a method the census's immich row does
not use. The pins and the ported ciphertexts are the test's own too. So what is
held is how what a component is handed decides what it declares, never what
the census or the pins happen to say.

Literals written here are the upstream projects' contracts: the environment
the server reads its settings and its peers from and the keys of its
configuration file (immich's `config.repository.ts` and `config.dto.ts` at
v3.3.3), its mobile app's redirect scheme, the cache folder the
machine-learning image sets (`MACHINE_LEARNING_CACHE_FOLDER` in its
`Dockerfile`), Redis's command line, CloudNativePG's fields and its `-app`
Secret, the libraries VectorChord and pgvecto.rs ask to be preloaded,
Kubernetes' Pod Security label, and the Gateway API's spellings.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pinned_image import (
    COMMAND_TIMEOUT,
    CONTAINER_TIMEOUT,
    LARGE_PULL_TIMEOUT,
    PULL_TIMEOUT,
    pinned,
    pulled,
    require_podman,
)

from kluster import conventions
from kluster.components.apps import Fronts, exim
from kluster.components.apps import immich as immich_app
from kluster.components.apps.immich import APPLICATION_SQL, CONFIG, RECOVERY, REDIS, Immich, ImmichRefused, Refusal
from kluster.components.apps.immich import watchdog as watchdog_layer
from kluster.components.cilium import InternetPoolMembers
from kluster.components.gpu import GPU_RESOURCE
from kluster.components.monitoring.operator_objects import type_token
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    PROXIED,
    REWRITE,
    Apex,
    Direct,
    Exposure,
    Gate,
    Held,
    Port,
    Proxied,
    Route,
    RouteRow,
    Vip,
)
from kluster.conventions.sso import Client, Factor, Grant, Issuer, Scope, TokenEndpointAuth
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.versions import ImagePin

HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
CRONJOB = 'kubernetes:batch/v1:CronJob'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
ROLE = 'kubernetes:rbac.authorization.k8s.io/v1:Role'
ROLE_BINDING = 'kubernetes:rbac.authorization.k8s.io/v1:RoleBinding'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
SEALED = 'crds:bitnami.com/v1alpha1:SealedSecret'
CLUSTER = 'crds:postgresql.cnpg.io/v1:Cluster'
OBJECT_STORE = 'crds:barmancloud.cnpg.io/v1:ObjectStore'
VMRULE = type_token('VMRule')

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

ROUTE = Route('im-photos', internet=Direct('uploads larger than the proxy accepts'), lan=REWRITE)
#: The route held, as it is while the legacy VPS keeps the name.
PHOTOS = Exposure(routes=(Held(ROUTE),))
#: The mobile app's own redirect, which immich hands the portal as it is.
MOBILE = 'app.immich:///oauth-callback'
CLIENT = Client(
    'im',
    'im-photos',
    ('/auth/login', '/user-settings'),
    policy=(Grant('im-users', Factor.TWO),),
    token_endpoint_auth=TokenEndpointAuth.BASIC,
    scopes=(Scope.OPENID, Scope.PROFILE),
    native=(MOBILE,),
)
ISSUER = Issuer('https://portal.example.invalid')

SERVER_IMAGE = ImagePin('registry.example.invalid/server', 'v9.9.9', f'sha256:{"1" * 64}')
ML_IMAGE = ImagePin('registry.example.invalid/ml', 'v9.9.9-openvino', f'sha256:{"2" * 64}')
REDIS_IMAGE = ImagePin('registry.example.invalid/redis', '9.9.9', f'sha256:{"3" * 64}')
EXPORTER_IMAGE = ImagePin('registry.example.invalid/exporter', 'v9.9.9', f'sha256:{"6" * 64}')
DATABASE_IMAGE = ImagePin('registry.example.invalid/vchord', '15.99', f'sha256:{"4" * 64}')
DRILL_IMAGE = ImagePin('registry.example.invalid/drill', '1.0.0', f'sha256:{"5" * 64}')

REDIS_CIPHERTEXT = {key: f'AgB-ported-{key}' for key in REDIS.keys}
CONFIG_CIPHERTEXT = {key: f'AgB-ported-{key}' for key in CONFIG.keys}
RECOVERY_CIPHERTEXT = {key: f'AgB-ported-{key}' for key in RECOVERY.keys}

#: One action of the controller's template: a Go string constant, a sealed
#: key's decrypted value, or that value written by sprig's `toJson`.
ACTION = re.compile(
    r'\{\{ (?:(?P<constant>"(?:[^"\\]|\\.)*")|index \. "(?P<key>[^"]+)"|toJson \(index \. "(?P<json>[^"]+)"\)) \}\}'
)


def decrypted(key: str) -> str:
    """What stands in for a sealed key's decrypted value: a quote and a backslash in it, which a JSON string escapes."""
    return f'decrypted "{key}" \\ value'


#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_immich`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: The watchdog's script, which reads its settings with `env('<NAME>')`.
SCRIPT = Path(immich_app.__file__).parent / 'templates' / watchdog_layer.SCRIPT
#: What the kubelet sets in every container, beside what the pod states.
KUBELET_SET = frozenset({'KUBERNETES_SERVICE_HOST', 'KUBERNETES_SERVICE_PORT'})


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


def arguments(**changed: Any) -> dict[str, Any]:
    return {
        'exposure': PHOTOS,
        'client': CLIENT,
        'issuer': ISSUER,
        'server_image': SERVER_IMAGE,
        'ml_image': ML_IMAGE,
        'redis_image': REDIS_IMAGE,
        'exporter_image': EXPORTER_IMAGE,
        'database_image': DATABASE_IMAGE,
        'drill_image': DRILL_IMAGE,
        'redis': REDIS_CIPHERTEXT,
        'config': CONFIG_CIPHERTEXT,
        'recovery': RECOVERY_CIPHERTEXT,
        'writer_key': writer_key(immich_app.NAMESPACE),
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
    } | changed


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """immich, as the `apps` program declares it."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Immich('immich', **arguments())
    return monitor


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == immich_app.NAMESPACE]


def named(monitor: Recorder, typ: str, name: str) -> dict[str, Any]:
    (found,) = [it for it in in_namespace(monitor, typ) if it['metadata'].get('name') == name]
    return found


def pod_of(monitor: Recorder, deployment: str) -> dict[str, Any]:
    return named(monitor, DEPLOYMENT, deployment)['spec']['template']


def watchdog_pod(monitor: Recorder) -> dict[str, Any]:
    return named(monitor, CRONJOB, watchdog_layer.NAME)['spec']['jobTemplate']['spec']['template']


def every_pod(monitor: Recorder) -> list[dict[str, Any]]:
    """The pods the component states itself: its Deployments' and its watchdog's."""
    return [*(it['spec']['template'] for it in in_namespace(monitor, DEPLOYMENT)), watchdog_pod(monitor)]


def only(pod: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """The pod's one container, or the one of its containers named `name`."""
    (container,) = [it for it in pod['spec']['containers'] if name is None or it['name'] == name]
    return container


def environment(container: dict[str, Any]) -> dict[str, Any]:
    """Each variable a container states, by name: its value, or the source it is read from."""
    return {it['name']: it.get('value', it.get('valueFrom')) for it in container['env']}


def mounts(pod: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each volume the pod's one container mounts, by the path it is mounted at, with the mount itself."""
    volumes = {volume['name']: volume for volume in pod['spec']['volumes']}
    return {
        mount['mountPath']: {'volume': volumes[mount['name']], 'mount': mount} for mount in only(pod)['volumeMounts']
    }


def nas_volume(monitor: Recorder, claim_name: str) -> dict[str, Any]:
    (volume,) = [
        it.inputs
        for it in monitor.of_type(VOLUME)
        if it.inputs['spec']['claimRef'] == {'namespace': immich_app.NAMESPACE, 'name': claim_name}
    ]
    return volume


def server(monitor: Recorder) -> dict[str, Any]:
    return pod_of(monitor, immich_app.SERVER_SERVICE)


def server_env(monitor: Recorder) -> dict[str, Any]:
    return environment(only(server(monitor)))


def served_by(service: dict[str, Any], pod: dict[str, Any]) -> int:
    """The port a Service forwards to a pod it selects, refused unless it selects it and the pod listens there."""
    assert service['spec']['selector'].items() <= pod['metadata']['labels'].items()
    (port,) = service['spec']['ports']
    listening = {it['name']: int(it['containerPort']) for each in pod['spec']['containers'] for it in each['ports']}
    assert listening[port['targetPort']] == int(port['port'])
    return int(port['port'])


def go_template(template: str) -> str:
    """The text the controller renders a template into, each sealed key's value standing in as `decrypted(<key>)`.

    Go's templates open an action at every `{{` and read a lone `}}` as text, so
    a `{{` that opens no action the controller reads fails the rendering here,
    as it fails the controller's.
    """
    rendered: list[str] = []
    position = 0
    for action in ACTION.finditer(template):
        literal = template[position : action.start()]
        assert '{{' not in literal, literal
        rendered.append(literal)
        constant, key, as_json = action['constant'], action['key'], action['json']
        if constant is not None:
            rendered.append(json.loads(constant))
        elif key is not None:
            rendered.append(decrypted(key))
        else:
            rendered.append(json.dumps(decrypted(as_json)))
        position = action.end()
    tail = template[position:]
    assert '{{' not in tail, tail
    return ''.join([*rendered, tail])


def config_secret(monitor: Recorder) -> dict[str, Any]:
    """The SealedSecret the server's configuration volume names."""
    (volume,) = [it for it in server(monitor)['spec']['volumes'] if 'secret' in it]
    return named(monitor, SEALED, volume['secret']['secretName'])


def configuration(monitor: Recorder) -> dict[str, Any]:
    """The file the server reads as `IMMICH_CONFIG_FILE`, as the controller renders it."""
    path = Path(server_env(monitor)['IMMICH_CONFIG_FILE'])
    secret = mounts(server(monitor))[str(path.parent)]['volume']['secret']
    (item,) = [it for it in secret['items'] if it['path'] == path.name]
    template = config_secret(monitor)['spec']['template']['data'][item['key']]
    loaded: dict[str, Any] = json.loads(go_template(template))
    return loaded


def selected(selector: str, labels: dict[str, str]) -> bool:
    """Whether a `key=value,…` label selector selects a pod carrying `labels`."""
    return all(labels.get(key) == value for key, value in (term.split('=', 1) for term in selector.split(',')))


# -- the pods --------------------------------------------------------------------


def test_every_pod_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(declared: Recorder) -> None:
    """None of the images names a numeric user, and `restricted`, which the namespace is held to, refuses a pod that states none."""
    (namespace,) = [it.inputs for it in declared.of_type(NAMESPACE)]

    assert namespace['metadata']['name'] == immich_app.NAMESPACE
    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    for pod in every_pod(declared):
        context = pod['spec']['securityContext']
        assert context['runAsNonRoot'] is True
        # The mock hands numbers back as floats, so the user is held to a whole one.
        assert context['runAsUser'] == int(context['runAsUser']) > 0
        assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
        for each in pod['spec']['containers']:
            assert each['securityContext']['allowPrivilegeEscalation'] is False, each['name']
            assert each['securityContext']['capabilities'] == {'drop': ['ALL']}, each['name']


def test_each_workload_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    """The watchdog runs the server's image, whose Redis client and dependency versions it reads."""
    (cluster,) = in_namespace(declared, CLUSTER)
    (drill,) = [it for it in in_namespace(declared, CRONJOB) if it['metadata']['name'] != watchdog_layer.NAME]
    drill_pod = drill['spec']['jobTemplate']['spec']['template']

    assert only(server(declared))['image'] == str(SERVER_IMAGE)
    assert only(watchdog_pod(declared))['image'] == str(SERVER_IMAGE)
    assert only(pod_of(declared, immich_app.ML_SERVICE))['image'] == str(ML_IMAGE)
    assert only(pod_of(declared, immich_app.REDIS_SERVICE), 'redis')['image'] == str(REDIS_IMAGE)
    assert only(pod_of(declared, immich_app.REDIS_SERVICE), 'exporter')['image'] == str(EXPORTER_IMAGE)
    assert cluster['spec']['imageName'] == str(DATABASE_IMAGE)
    assert only(drill_pod)['image'] == str(DRILL_IMAGE)


def test_each_probe_asks_a_port_the_container_listens_on(declared: Recorder) -> None:
    containers = [
        each for it in in_namespace(declared, DEPLOYMENT) for each in it['spec']['template']['spec']['containers']
    ]
    for container in containers:
        listening = {it['name'] for it in container['ports']}
        probes = [container[it] for it in ('startupProbe', 'readinessProbe', 'livenessProbe') if it in container]
        assert probes, container['name']
        for probe in probes:
            action = probe.get('httpGet') or probe['tcpSocket']
            assert action['port'] in listening, container['name']


def test_the_server_and_the_machine_learning_service_are_each_given_one_gpu(declared: Recorder) -> None:
    for deployment in (immich_app.SERVER_SERVICE, immich_app.ML_SERVICE):
        resources = only(pod_of(declared, deployment))['resources']
        assert resources['requests'][GPU_RESOURCE] == resources['limits'][GPU_RESOURCE], deployment
        assert int(resources['limits'][GPU_RESOURCE]) == 1, deployment


def test_each_workload_stops_before_its_successor_starts(declared: Recorder) -> None:
    """The GPU is one device, which a second pod beside the first would wait for."""
    for deployment in in_namespace(declared, DEPLOYMENT):
        assert deployment['spec']['strategy']['type'] == 'Recreate', deployment['metadata']['name']


def test_each_pod_that_requests_the_gpu_is_held_where_the_device_keeps_it(declared: Recorder) -> None:
    """The request keeps it to the node offering the device, so it outranks free pods there."""
    requesting = [
        pod
        for pod in every_pod(declared)
        if any(GPU_RESOURCE in container['resources'].get('requests', {}) for container in pod['spec']['containers'])
    ]

    assert requesting
    for pod in requesting:
        assert pod['spec']['priorityClassName'] == conventions.HELD_PRIORITY_CLASS


# -- the server's peers --------------------------------------------------------------


def owner_and_database(monitor: Recorder) -> tuple[str, str]:
    """The owner and the database the declared cluster's bootstrap names, whichever way it bootstraps."""
    (cluster,) = in_namespace(monitor, CLUSTER)
    (bootstrap,) = cluster['spec']['bootstrap'].values()
    return bootstrap['owner'], bootstrap['database']


def test_the_server_reaches_the_database_as_its_owner_at_the_read_write_service(declared: Recorder) -> None:
    """CloudNativePG serves a cluster's primary at `<cluster>-rw` and keeps the owner's credential in `<cluster>-app`."""
    (cluster,) = in_namespace(declared, CLUSTER)
    name = cluster['metadata']['name']
    owner, database = owner_and_database(declared)
    env = server_env(declared)

    assert env['DB_HOSTNAME'] == f'{name}-rw'
    assert env['DB_DATABASE_NAME'] == database
    assert env['DB_USERNAME'] == {'secretKeyRef': {'name': f'{name}-app', 'key': 'username'}}
    assert env['DB_PASSWORD'] == {'secretKeyRef': {'name': f'{name}-app', 'key': 'password'}}
    assert owner == database


def test_the_server_reaches_redis_with_the_password_redis_requires(declared: Recorder) -> None:
    """The password both read is the ported ciphertext's, which opens in the namespace under the name they read."""
    env = server_env(declared)
    redis_pod = pod_of(declared, immich_app.REDIS_SERVICE)
    redis = only(redis_pod, 'redis')
    service = named(declared, SERVICE, env['REDIS_HOSTNAME'])
    secret = env['REDIS_PASSWORD']['secretKeyRef']
    sealed = named(declared, SEALED, secret['name'])

    assert int(env['REDIS_PORT']) == served_by(service, redis_pod)
    assert environment(redis)['REDIS_PASSWORD'] == env['REDIS_PASSWORD']
    assert redis['args'][redis['args'].index('--requirepass') + 1] == '$(REDIS_PASSWORD)'
    assert sealed['spec']['encryptedData'] == {secret['key']: REDIS_CIPHERTEXT[secret['key']]}


def test_redis_keeps_nothing_across_a_restart(declared: Recorder) -> None:
    """A lost queue re-runs, so Redis writes no snapshot and no append-only file, on no claim."""
    pod = pod_of(declared, immich_app.REDIS_SERVICE)
    args = only(pod, 'redis')['args']

    assert args[args.index('--save') + 1] == ''
    assert args[args.index('--appendonly') + 1] == 'no'
    assert all('emptyDir' in it for it in pod['spec']['volumes'])


def test_the_exporter_reads_the_redis_beside_it_with_its_password(declared: Recorder) -> None:
    pod = pod_of(declared, immich_app.REDIS_SERVICE)
    redis, exporter = only(pod, 'redis'), only(pod, 'exporter')
    address = re.fullmatch(r'redis://localhost:(?P<port>\d+)', environment(exporter)['REDIS_ADDR'])
    assert address is not None

    assert int(address['port']) in {int(it['containerPort']) for it in redis['ports']}
    assert environment(exporter)['REDIS_PASSWORD'] == environment(redis)['REDIS_PASSWORD']


def test_the_server_reaches_the_machine_learning_service_by_its_service(declared: Recorder) -> None:
    url = re.fullmatch(r'http://(?P<host>[^:/]+):(?P<port>\d+)', server_env(declared)['IMMICH_MACHINE_LEARNING_URL'])
    assert url is not None
    service = named(declared, SERVICE, url['host'])

    assert int(url['port']) == served_by(service, pod_of(declared, immich_app.ML_SERVICE))


# -- the storage -----------------------------------------------------------------


def test_the_namespace_backs_up_by_the_families_its_census_row_names(declared: Recorder) -> None:
    """A family the row names has its writer key sealed into the namespace, and one it does not name has none to write with."""
    families = BACKED_NAMESPACES[immich_app.NAMESPACE]

    assert (in_namespace(declared, SOURCE) != []) == (BackupFamily.VOLSYNC in families)
    assert (in_namespace(declared, CLUSTER) != []) == (BackupFamily.BARMAN in families)


def test_the_library_is_the_exports_media_root_writable_where_the_server_is_told_it_is(declared: Recorder) -> None:
    """Every path the database records is under the media root, which the server is told and the library is mounted at."""
    pod = server(declared)
    library = mounts(pod)[server_env(declared)['IMMICH_MEDIA_LOCATION']]
    volume = nas_volume(declared, library['volume']['persistentVolumeClaim']['claimName'])

    assert not library['mount'].get('readOnly', False)
    assert volume['spec']['nfs']['path'] == f'{conventions.NAS_EXPORT}/{immich_app.LIBRARY}'
    assert volume['spec']['accessModes'] == ['ReadWriteMany']


def test_the_models_are_on_a_node_claim_where_the_image_keeps_them(declared: Recorder) -> None:
    """The image's cache folder is `/cache`, and the claim is mounted there and named for it in the environment."""
    pod = pod_of(declared, immich_app.ML_SERVICE)
    cache = mounts(pod)['/cache']['volume']['persistentVolumeClaim']['claimName']
    nas = {it.inputs['spec']['claimRef']['name'] for it in declared.of_type(VOLUME)}

    assert cache in {it['metadata']['name'] for it in in_namespace(declared, CLAIM)} - nas
    assert environment(only(pod))['MACHINE_LEARNING_CACHE_FOLDER'] == '/cache'


# -- the database ------------------------------------------------------------------


def test_the_instance_preloads_vectorchord(declared: Recorder) -> None:
    """VectorChord's library is loaded only at the server's start, and `vchord` refuses to be created without it."""
    (cluster,) = in_namespace(declared, CLUSTER)

    assert 'vchord.so' in cluster['spec']['postgresql']['shared_preload_libraries']


# -- the configuration ---------------------------------------------------------------


def test_the_server_reads_the_rendered_file_of_the_secret_alone(declared: Recorder) -> None:
    """Its configuration volume carries one item, so the sealed key is never a file in the container."""
    path = Path(server_env(declared)['IMMICH_CONFIG_FILE'])
    secret = mounts(server(declared))[str(path.parent)]['volume']['secret']
    sealed = config_secret(declared)

    assert [it['path'] for it in secret['items']] == [path.name]
    assert set(sealed['spec']['template']['data']) == {it['key'] for it in secret['items']}
    assert sealed['spec']['encryptedData'] == CONFIG_CIPHERTEXT


def test_the_configuration_holds_the_decrypted_client_secret_and_parses(declared: Recorder) -> None:
    """The secret is written as a JSON string, so a quote or a backslash in it leaves the file whole."""
    (key,) = CONFIG.keys

    assert configuration(declared)['oauth']['clientSecret'] == decrypted(key)


def test_the_storage_template_reaches_the_server_with_its_braces(declared: Recorder) -> None:
    """immich's storage template is written in double braces, which the controller would read as its own actions."""
    template = configuration(declared)['storageTemplate']['template']

    assert re.search(r'\{\{y\}\}', template)


def test_the_external_address_is_the_routes_name_over_https(declared: Recorder) -> None:
    domain = configuration(declared)['server']['externalDomain']
    hostnames = {name for route in in_namespace(declared, HTTP_ROUTE) for name in route['spec']['hostnames']}

    assert domain.startswith('https://')
    assert domain.removeprefix('https://') in hostnames


def test_mail_goes_to_the_relay_in_the_clear_from_the_one_pod_it_admits(declared: Recorder) -> None:
    """The relay serves no certificate, and admits a pod of another namespace by its label alone."""
    transport = configuration(declared)['notifications']['smtp']['transport']
    sender = conventions.MAIL_SENDER_LABEL.items()

    assert (transport['host'], transport['port']) == (exim.HOST, exim.PORT)
    assert transport['secure'] is False
    assert transport['ignoreCert'] is False
    assert sender <= server(declared)['metadata']['labels'].items()
    for pod in every_pod(declared):
        labels = pod['metadata'].get('labels', {}).items()
        if not immich_app.SERVER_LABELS.items() <= labels:
            assert not sender <= labels


def test_sign_in_is_the_portals_alone_as_the_client_asks_for_it(declared: Recorder) -> None:
    oauth = configuration(declared)['oauth']

    assert oauth['enabled'] is True
    assert oauth['issuerUrl'] == ISSUER.url
    assert oauth['scope'].split() == [scope.value for scope in CLIENT.scopes]
    assert oauth['tokenEndpointAuthMethod'] == CLIENT.token_endpoint_auth.value
    assert configuration(declared)['passwordLogin']['enabled'] is False


# -- the exposure ------------------------------------------------------------------


def test_the_route_attaches_at_each_gateway_its_row_sets_and_reaches_the_server_ungated(declared: Recorder) -> None:
    """immich signs its users in itself, so neither route asks the portal, and each forwards to the server's port."""
    routes = in_namespace(declared, HTTP_ROUTE)
    service = named(declared, SERVICE, immich_app.SERVER_SERVICE)
    port = served_by(service, server(declared))

    assert sorted(ref['name'] for route in routes for ref in route['spec']['parentRefs']) == sorted(
        [conventions.GATEWAY_INTERNET, conventions.GATEWAY_LAN]
    )
    for route in routes:
        (rule,) = route['spec']['rules']
        assert route['spec']['hostnames'] == [f'{ROUTE.host}.{conventions.ZONE_PRIMARY}']
        assert [it for it in rule['filters'] if it['type'] == 'ExternalAuth'] == []
        assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port}]


# -- the watchdog ----------------------------------------------------------------------


def called_verbs() -> set[str]:
    """The verbs of the calls the script makes to the API, each on the namespace's pods, as RBAC names them.

    A call is `kube('<method>', `<path>`)`: a `GET` of the collection is
    `list`, a `GET` of one pod `get`, and a `DELETE` of one pod `delete`.
    """
    verbs: set[str] = set()
    for method, path in re.findall(r"kube\('([A-Z]+)',\s*`([^`]*)`", SCRIPT.read_text()):
        resource = path.split('?')[0].split('/namespaces/${NAMESPACE}/')[1]
        assert resource.split('/')[0] == 'pods', path
        one = '/' in resource
        verbs.add({('GET', False): 'list', ('GET', True): 'get', ('DELETE', True): 'delete'}[method, one])
    return verbs


def test_the_watchdog_deletes_the_servers_pods_and_nothing_else(declared: Recorder) -> None:
    """Its role allows on pods exactly the calls its script makes, and its selector picks the server's pods alone."""
    pod = watchdog_pod(declared)
    role = named(declared, ROLE, watchdog_layer.NAME)
    (binding,) = [
        it for it in in_namespace(declared, ROLE_BINDING) if it['roleRef']['name'] == role['metadata']['name']
    ]
    selector = environment(only(pod))['SERVER_SELECTOR']

    (rule,) = role['rules']
    assert (rule['apiGroups'], rule['resources']) == ([''], ['pods'])
    assert set(rule['verbs']) == called_verbs()
    assert [subject['name'] for subject in binding['subjects']] == [pod['spec']['serviceAccountName']]
    assert selected(selector, server(declared)['metadata']['labels'])
    for other in (immich_app.ML_SERVICE, immich_app.REDIS_SERVICE):
        assert not selected(selector, pod_of(declared, other)['metadata']['labels'])


def test_the_watchdog_calls_the_api_from_a_pod_the_namespace_lets_through(declared: Recorder) -> None:
    pod = watchdog_pod(declared)

    assert conventions.API_CLIENT_LABEL.items() <= pod['metadata']['labels'].items()
    assert pod['spec']['automountServiceAccountToken'] is True


def test_the_watchdog_reads_the_queues_the_server_writes(declared: Recorder) -> None:
    watched = environment(only(watchdog_pod(declared)))
    env = server_env(declared)

    for variable in ('REDIS_HOSTNAME', 'REDIS_PORT', 'REDIS_PASSWORD'):
        assert watched[variable] == env[variable], variable


def test_the_watchdogs_script_is_given_every_setting_it_reads(declared: Recorder) -> None:
    """The script stops at its first unset variable, so each it reads is set on its container or by the kubelet."""
    read = set(re.findall(r"""env\('([A-Z_]+)'\)""", SCRIPT.read_text()))
    container = only(watchdog_pod(declared))
    (script,) = [it for it in in_namespace(declared, CONFIG_MAP) if it['metadata']['name'] == watchdog_layer.NAME]

    assert read
    assert read <= set(environment(container)) | KUBELET_SET
    assert script['data'] == {watchdog_layer.SCRIPT: SCRIPT.read_text()}


@pytest.mark.timeout(LARGE_PULL_TIMEOUT + 3 * COMMAND_TIMEOUT)
def test_the_pinned_server_still_ships_the_postgres_js_that_leaks_slots() -> None:
    """The layer's tripwire: red on the bump of the server's pin that ships the fix, and where the script's path moved.

    The version is read where the watchdog's script reads it, from the pinned
    server image, pulled where it is not local, so the pull request that bumps
    the pin runs it on CI's runner.
    """
    require_podman("the watchdog's tripwire reads the pinned server image")
    # The server image's download is over half a gigabyte.
    reference = pulled(pinned('immich-server'), timeout=LARGE_PULL_TIMEOUT)
    container = f'kluster-test-immich-server-{uuid.uuid4().hex[:12]}'
    with process_sessions.removing(container, timeout=COMMAND_TIMEOUT):
        done = process_sessions.run(
            [
                'podman', 'run', '--rm', f'--label={LABEL}', f'--name={container}', '--entrypoint', 'cat',
                reference, watchdog_layer.POSTGRES_PACKAGE,
            ],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )  # fmt: skip
    version = json.loads(done.stdout)['version']

    assert int(version.split('.')[0]) < watchdog_layer.FIXED_POSTGRES_MAJOR, (
        f'the pinned server ships postgres.js {version}, which carries the fix: remove the layer that '
        '`kluster.components.apps.immich.watchdog` describes'
    )


def test_the_server_never_recycles_a_database_connection(declared: Recorder) -> None:
    """A recycled connection is what leaks a slot on the client's side.

    postgres.js reads `PG<OPTION>` from the environment for each option its
    caller leaves unset, and a `max_lifetime` of 0 recycles no connection.
    """
    assert server_env(declared)['PGMAX_LIFETIME'] == '0'


def test_the_watchdog_is_watched_by_the_scheduled_job_familys_rules_on_its_cronjob(declared: Recorder) -> None:
    """A run that restarts the server fails, so the failed alert is what reports a leak; the stale one, a watchdog that stopped."""
    job = watchdog_layer.watchdog_job(immich_app.NAMESPACE)
    cronjob = named(declared, CRONJOB, job.cronjob)
    (rules,) = [it for it in in_namespace(declared, VMRULE) if it['metadata']['name'] == job.cronjob]
    (group,) = rules['spec']['groups']

    assert {rule['alert'] for rule in group['rules']} == {f'{job.alert}Stale', f'{job.alert}Failed'}
    for rule in group['rules']:
        assert f'cronjob="{job.cronjob}"' in rule['expr']
        assert f'namespace="{job.namespace}"' in rule['expr']
        assert rule['annotations']['playbook'] == str(job.playbook)
    assert int(cronjob['spec']['jobTemplate']['spec']['activeDeadlineSeconds']) == job.deadline.total_seconds()
    assert job.starting_deadline is not None
    assert int(cronjob['spec']['startingDeadlineSeconds']) == job.starting_deadline.total_seconds()


def test_the_watchdogs_playbook_is_a_section_of_its_document() -> None:
    """An alert whose response is not written down is not shipped (operations.md §5)."""
    playbook = watchdog_layer.PLAYBOOK
    text = (Path(__file__).resolve().parents[1] / playbook.document).read_text()

    assert re.search(rf'^#+ {re.escape(playbook.section)} ', text, re.M)


def test_the_watchdog_runs_on_the_cadence_its_rules_read(declared: Recorder) -> None:
    """The stale alert is derived from the job's period, so the schedule fires once in each period."""
    job = watchdog_layer.watchdog_job(immich_app.NAMESPACE)
    schedule = named(declared, CRONJOB, job.cronjob)['spec']['schedule']
    minute = re.fullmatch(r'\*/(?P<every>\d+) \* \* \* \*', schedule)

    assert minute is not None
    assert int(minute['every']) * 60 == job.period.total_seconds()


# -- the database on its pinned operand ---------------------------------------------

#: Why these cases need podman.
WHY = "the database's application SQL is run on the operand it is pinned to"

#: The bound on each case that runs the operand: its pull, and one container
#: that makes a database and runs the server's statements in it. A stop-loss,
#: which nothing asserts on.
OPERAND_CASE_TIMEOUT = PULL_TIMEOUT + 2 * COMMAND_TIMEOUT

#: The client rules of the `pg_hba.conf` CloudNativePG generates for every
#: instance (its documentation, `postgresql_conf.md`, "The `pg_hba` section"),
#: less the certificate rules: a local connection as the user the operating
#: system names, and any other with a password.
CNPG_PG_HBA = 'local all all peer\nhost all all all scram-sha-256\n'

#: The owner's password the stand-in database is made with.
PASSWORD = 'stub-owner-password'

#: What immich v3.3.3 runs as the owner at its start that creates an
#: extension, in order: its database service's creation of the vector extension
#: it finds, VectorChord, whose failure it logs as fatal and stops on
#: (`database.service.ts`, `database.repository.ts`), then its first
#: migration's (`1744910873969-InitialMigration.ts`).
BOOT_EXTENSIONS = (
    'CREATE EXTENSION IF NOT EXISTS vchord CASCADE',
    'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"',
    'CREATE EXTENSION IF NOT EXISTS "unaccent"',
    'CREATE EXTENSION IF NOT EXISTS "cube"',
    'CREATE EXTENSION IF NOT EXISTS "earthdistance"',
    'CREATE EXTENSION IF NOT EXISTS "pg_trgm"',
    'CREATE EXTENSION IF NOT EXISTS vchord CASCADE',
)


def boot_on_an_empty_database(monitor: Recorder, application_sql: Sequence[str]) -> str:
    """A script that makes the declared database empty, runs `application_sql` as the superuser, then the server's statements as the owner.

    The instance preloads the declared libraries, under CloudNativePG's
    client rules, and the owner connects over TCP with its password as the
    server does. The script stops at the first statement that fails, whose
    error `psql` prints with its SQLSTATE.
    """
    (cluster,) = in_namespace(monitor, CLUSTER)
    owner, name = owner_and_database(monitor)
    preload = ','.join(cluster['spec']['postgresql']['shared_preload_libraries'])
    options = shlex.quote(
        f'-c shared_preload_libraries={preload} -c unix_socket_directories=/tmp -c listen_addresses=127.0.0.1'
    )
    psql = 'psql -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -q'
    superuser = f'{psql} -h /tmp -U postgres'
    as_owner = f'PGPASSWORD={PASSWORD} {psql} -h 127.0.0.1 -U {owner} -d {name}'
    return '\n'.join([
        'set -e',
        'export PATH="$(echo /usr/lib/postgresql/*/bin):$PATH"',
        'initdb -D /tmp/data -U postgres --auth=trust >/dev/null',
        f'printf %s {shlex.quote(CNPG_PG_HBA)} > /tmp/data/pg_hba.conf',
        f'pg_ctl -D /tmp/data -w -o {options} start >/dev/null',
        f'{superuser} -c {shlex.quote(f"CREATE ROLE {owner} LOGIN PASSWORD {PASSWORD!r}")}',
        f'{superuser} -c {shlex.quote(f"CREATE DATABASE {name} OWNER {owner}")}',
        *(f'{superuser} -d {name} -c {shlex.quote(statement)}' for statement in application_sql),
        *(f'{as_owner} -c {shlex.quote(statement)}' for statement in BOOT_EXTENSIONS),
        'echo booted',
    ])  # fmt: skip


#: The SQLSTATE PostgreSQL refuses a statement with for want of a privilege,
#: `insufficient_privilege` (its documentation, "PostgreSQL Error Codes").
INSUFFICIENT_PRIVILEGE = '42501'


def refused_extensions(output: str) -> list[str]:
    """Each extension whose creation `psql` reports refused for want of a privilege, by its name in the error."""
    return re.findall(rf'ERROR:\s+{INSUFFICIENT_PRIVILEGE}: [^\n]*extension "([^"]+)"', output)


def run_on_the_operand(script: str) -> tuple[int, str]:
    """`script` under `bash` in a container of the pinned operand: its exit status, and what it printed."""
    require_podman(WHY)
    image = pulled(pinned('immich-database'))
    container = f'kluster-test-immich-database-{uuid.uuid4().hex[:12]}'
    with process_sessions.removing(container, timeout=COMMAND_TIMEOUT):
        done = process_sessions.run(
            [
                'podman', 'run', '--rm', f'--timeout={CONTAINER_TIMEOUT}', f'--label={LABEL}', f'--name={container}',
                image, 'bash', '-c', script,
            ],
            timeout=2 * COMMAND_TIMEOUT,
        )  # fmt: skip
    return done.returncode, (done.stdout + done.stderr).decode(errors='replace')


@pytest.mark.timeout(OPERAND_CASE_TIMEOUT)
def test_an_empty_database_with_the_application_sql_lets_the_server_create_its_extensions(declared: Recorder) -> None:
    status, output = run_on_the_operand(boot_on_an_empty_database(declared, APPLICATION_SQL))

    assert status == 0, output
    assert 'booted' in output.splitlines(), output


@pytest.mark.timeout(OPERAND_CASE_TIMEOUT)
def test_without_the_application_sql_the_server_cannot_create_vectorchord(declared: Recorder) -> None:
    """The operand does not trust `vchord`'s dependency to the owner, so the server's first statement fails, and the server stops."""
    status, output = run_on_the_operand(boot_on_an_empty_database(declared, ()))

    assert status != 0
    assert refused_extensions(output) == ['vector'], output
    assert 'booted' not in output.splitlines()


@pytest.mark.timeout(OPERAND_CASE_TIMEOUT)
@pytest.mark.parametrize('left_out', range(len(APPLICATION_SQL)))
def test_each_statement_of_the_application_sql_is_one_the_server_needs(declared: Recorder, left_out: int) -> None:
    """Without any one of them, a statement the server runs as the owner is refused for want of a superuser."""
    kept = [statement for index, statement in enumerate(APPLICATION_SQL) if index != left_out]
    status, output = run_on_the_operand(boot_on_an_empty_database(declared, kept))

    assert status != 0
    assert refused_extensions(output), output


# -- refusals ----------------------------------------------------------------------


def refused(**changed: Any) -> Refusal:
    """The refusal of a declaration with `changed` in place of the test's arguments."""
    with pytest.raises(ImmichRefused) as raised:
        _ = Immich('immich', **arguments(**changed))
    return raised.value.refusal


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(Exposure(routes=(ROUTE, Route('im-other', internet=PROXIED))), id='two-routes'),
        pytest.param(
            Exposure(routes=(ROUTE,), ports=(Port('im-raw', 2283, Transports.TCP, lan=Vip('im-photos')),)),
            id='a-raw-port',
        ),
        pytest.param(Exposure(ports=(Port('im-raw', 2283, Transports.TCP, lan=Vip('im-photos')),)), id='no-route'),
    ],
)
def test_an_exposure_other_than_one_route_is_refused(exposure: Exposure) -> None:
    assert refused(exposure=exposure) is Refusal.ROUTES


def test_an_apex_in_place_of_the_route_is_refused() -> None:
    assert refused(exposure=Exposure(routes=(Apex((conventions.ZONE_PRIMARY,)),))) is Refusal.ROUTE_KIND


def test_a_route_published_in_two_zones_is_refused() -> None:
    route = Route('im-photos', internet=PROXIED, zones=(conventions.ZONE_PRIMARY, conventions.ZONE_SHORT))

    assert refused(exposure=Exposure(routes=(route,))) is Refusal.ZONES


@pytest.mark.parametrize('gate', list(Gate))
def test_a_gated_route_is_refused(gate: Gate) -> None:
    """The mobile app sends no portal session, so a gate would stop every phone."""
    route: RouteRow = Held(Route('im-photos', internet=Proxied(gate), lan=REWRITE))

    assert refused(exposure=Exposure(routes=(route,))) is Refusal.GATED


def _client(**changed: Any) -> Client:
    fields: dict[str, Any] = {
        'name': CLIENT.name,
        'host': CLIENT.host,
        'paths': CLIENT.paths,
        'policy': CLIENT.policy,
        'token_endpoint_auth': CLIENT.token_endpoint_auth,
        'scopes': CLIENT.scopes,
        'zone': CLIENT.zone,
        'native': CLIENT.native,
    } | changed
    return Client(**fields)


@pytest.mark.parametrize(
    'client',
    [
        pytest.param(_client(host='im-elsewhere'), id='another-host'),
        pytest.param(_client(zone=conventions.ZONE_SHORT), id='another-zone'),
    ],
)
def test_a_client_whose_redirects_are_under_another_name_is_refused(client: Client) -> None:
    """The portal sends a browser back to its client's host alone, so a sign-in through any other name fails."""
    assert refused(client=client) is Refusal.CLIENT_HOST


@pytest.mark.parametrize(
    'client',
    [
        pytest.param(_client(native=()), id='no-mobile-redirect'),
        pytest.param(_client(paths=('/auth/login',)), id='no-settings-page'),
    ],
)
def test_a_client_that_does_not_register_where_immich_comes_back_is_refused(client: Client) -> None:
    """The portal refuses a redirect it does not register, so the mobile app's sign-in or an account's linking would fail."""
    assert refused(client=client) is Refusal.REDIRECT


@pytest.mark.parametrize(
    ('argument', 'ciphertext'),
    [
        pytest.param('redis', {}, id='redis-short'),
        pytest.param('redis', {**REDIS_CIPHERTEXT, 'another_key': 'AgB-another'}, id='redis-another-key'),
        pytest.param('config', {'client_secret': 'AgB-misnamed'}, id='config-misnamed'),
    ],
)
def test_a_ported_ciphertext_whose_keys_are_not_its_secrets_is_refused(
    argument: str, ciphertext: dict[str, str]
) -> None:
    """A pod reading a key the ported Secret lacks would never start, which is found only live."""
    assert refused(**{argument: ciphertext}) is Refusal.PORTED_KEYS


# -- the recovery layer ----------------------------------------------------------
#
# What `kluster.components.apps.immich`'s recovery layer declares, which goes
# with it: these cases, `RECOVERY_CIPHERTEXT` and the `recovery` `arguments`
# hands.


def test_the_database_recovers_from_the_legacy_server_with_the_ported_credential(declared: Recorder) -> None:
    """The source is a server other than the one the database archives as, read through the ported ciphertext."""
    (cluster,) = in_namespace(declared, CLUSTER)
    spec = cluster['spec']
    (archiver,) = spec['plugins']
    (source,) = spec['externalClusters']
    parameters = source['plugin']['parameters']
    store = named(declared, OBJECT_STORE, parameters['barmanObjectName'])['spec']['configuration']
    reference = store['googleCredentials']['applicationCredentials']
    credential = named(declared, SEALED, reference['name'])

    assert spec['bootstrap']['recovery']['source'] == source['name']
    assert parameters['serverName'] != archiver['parameters']['serverName']
    assert store['destinationPath'].startswith('gs://')
    assert credential['spec']['encryptedData'] == {reference['key']: RECOVERY_CIPHERTEXT[reference['key']]}
    assert credential['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}


@pytest.mark.parametrize(
    'ciphertext',
    [pytest.param({}, id='short'), pytest.param({'gcs_key': 'AgB-misnamed'}, id='misnamed')],
)
def test_a_recovery_ciphertext_whose_keys_are_not_its_secrets_is_refused(ciphertext: dict[str, str]) -> None:
    assert refused(recovery=ciphertext) is Refusal.PORTED_KEYS
