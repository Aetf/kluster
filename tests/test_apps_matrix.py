"""The Matrix homeserver, declared against mocks from its row of the single sign-on census.

The homeserver is declared with the census's `matrix` client, since the server
is configured from it, and with a route row, an image pin, ciphertexts and an
issuer of the test's own, so what is held is how the component composes what
it is handed. Its configuration is the one the declared ConfigMap carries,
read as TOML, as continuwuity reads it.

Literals written here are the upstream projects' contracts: continuwuity's
configuration keys, the path it sends a browser back to after a sign-in and
its health endpoint, figment's reading of an environment variable's value, the
Matrix delegation documents' keys, the Kubernetes and Cilium fields, the
kubelet's expansion of `$(VAR)` and its layout of a Secret's files, the
sealed-secrets controller's scope annotations, and the label Pod Security
Admission reads.
"""

from __future__ import annotations

import dataclasses
import html
import io
import json
import re
import tarfile
import tomllib
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, pinned, pulled, require_podman
from pulumi import runtime

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.matrix import NAMESPACE, OIDC, RECEIVE, Matrix, delegation, server_configuration
from kluster.components.apps.storage import claim_name
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.backup import BACKED_NAMESPACES, PRECIOUS, BackupFamily
from kluster.conventions.routes import PROXIED, REWRITE, Exposure, Gate, Held, Proxied, Route
from kluster.lib.versions import ImagePin

NAMESPACE_TYPE = 'kubernetes:core/v1:Namespace'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
SERVICE = 'kubernetes:core/v1:Service'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'
PASSWORD = 'random:index/randomPassword:RandomPassword'

#: The path continuwuity sends a browser back to after the portal's sign-in,
#: under its base URL: its route prefix and the OIDC completion route
#: (`src/service/oidc/mod.rs` at v26.9.1).
UPSTREAM_REDIRECT_PATH = '/_continuwuity/oidc/complete'

#: The census's client, which the program hands the homeserver.
(CLIENT,) = [client for client in conventions.sso.CLIENTS if client.name == 'matrix']

#: The route row the homeserver is declared with: its client's host, behind
#: the proxy, held as the land declares it.
EXPOSURE = Exposure(routes=(Held(Route(CLIENT.host, internet=PROXIED)),))

#: The pin it is declared with here: a reference that is no published image,
#: so a container carrying it carries the pin.
IMAGE = ImagePin('registry.example.invalid/continuwuity', 'v9.9.9', f'sha256:{"e" * 64}')

#: The portal's issuer, as the program would hand it: where the stub issuer
#: listens beside the server when a case runs the pinned image.
ISSUER = 'http://127.0.0.1:8080'

#: The ciphertexts it is handed, each naming what it stands for.
CIPHERTEXT = {'oidc_client_id': 'sealed-client-id', 'oidc_client_secret': 'sealed-client-secret'}
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-key-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-key-secret'}

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


class Generating(Recorder):
    """A monitor that answers a password's `result` as the random provider would."""

    def computed(self, args: runtime.MockResourceArgs) -> dict[str, Any]:
        return {'result': 'generated-in-state'} if args.typ == PASSWORD else {}


def declare(
    *, exposure: Exposure = EXPOSURE, client: conventions.sso.Client = CLIENT, receiver: ImagePin | None = None
) -> Matrix:
    return Matrix(
        'matrix',
        exposure=exposure,
        client=client,
        issuer=conventions.sso.Issuer(ISSUER),
        image=IMAGE,
        oidc=CIPHERTEXT,
        writer_key=WRITER_KEY,
        fronts=FRONTS,
        zone_ids=ZONE_IDS,
        receiver=receiver,
    )


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def homeserver() -> Recorder:
    """The homeserver, declared from the census's client with the test's route, pin and ciphertexts."""
    monitor = await run_with(Generating(), stack='apps')
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


def volume_at(monitor: Recorder, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The container's mount at `path`, and the pod's volume it mounts."""
    (mount,) = [it for it in container_of(monitor)['volumeMounts'] if it['mountPath'] == path]
    (volume,) = [it for it in pod_of(monitor)['spec']['volumes'] if it['name'] == mount['name']]
    return mount, volume


def env(monitor: Recorder) -> list[dict[str, Any]]:
    return container_of(monitor)['env']


def configuration(monitor: Recorder) -> dict[str, Any]:
    """The configuration continuwuity reads: the file `CONTINUWUITY_CONFIG` names, as its ConfigMap carries it."""
    (path,) = [it['value'] for it in env(monitor) if it['name'] == 'CONTINUWUITY_CONFIG']
    mount, volume = volume_at(monitor, path)
    config_map = the_one(monitor, CONFIG_MAP)
    assert volume['configMap']['name'] == config_map['metadata']['name']
    return tomllib.loads(config_map['data'][mount['subPath']])['global']


def oidc_config(monitor: Recorder) -> dict[str, Any]:
    return configuration(monitor)['oauth']['oidc']


def sealed_secret(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == name]
    return found


# -- the pod ---------------------------------------------------------------------


def test_the_server_runs_as_a_numeric_user_that_is_not_root_on_a_read_only_root(homeserver: Recorder) -> None:
    """Its image names no user, and `restricted` refuses a pod that does not say it runs as one that is not root."""
    spec = pod_of(homeserver)['spec']
    context = spec['securityContext']
    container = container_of(homeserver)

    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities'] == {'drop': ['ALL']}
    assert container['securityContext']['readOnlyRootFilesystem'] is True
    assert container['image'] == str(IMAGE)


def test_the_namespace_enforces_restricted(homeserver: Recorder) -> None:
    namespace = the_one(homeserver, NAMESPACE_TYPE)

    assert namespace['metadata']['labels']['pod-security.kubernetes.io/enforce'] == 'restricted'


def test_the_server_carries_cpu_and_memory_limits(homeserver: Recorder) -> None:
    """The scheduler may place it on a cloud node, where etcd shares the cores, so it is held to both (declarative/workloads.md §1)."""
    resources = container_of(homeserver)['resources']

    assert {'cpu', 'memory'} <= resources['limits'].keys()
    assert {'cpu', 'memory'} <= resources['requests'].keys()


def test_one_server_holds_the_database_at_a_time(homeserver: Recorder) -> None:
    """The old pod stops before the new one starts, so two servers never open the one database."""
    spec = the_one(homeserver, DEPLOYMENT)['spec']

    assert spec['replicas'] == 1
    assert spec['strategy'] == {'type': 'Recreate'}


def test_the_server_listens_on_the_port_its_service_and_probes_reach(homeserver: Recorder) -> None:
    service = the_one(homeserver, SERVICE)
    container = container_of(homeserver)
    (port,) = service['spec']['ports']
    named = {it['name']: it['containerPort'] for it in container['ports']}

    assert service['spec']['selector'].items() <= pod_of(homeserver)['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port'] == configuration(homeserver)['port']
    assert configuration(homeserver)['address'] == '0.0.0.0'
    for probe in ('startupProbe', 'readinessProbe'):
        assert container[probe]['httpGet'] == {'path': '/_matrix/federation/v1/version', 'port': port['targetPort']}


# -- the claim -------------------------------------------------------------------


def test_the_database_is_on_the_claim_backed_up_as_precious_and_protected(homeserver: Recorder) -> None:
    """The database path is the claim's mount, and the claim is backed up under `PRECIOUS` and protected (rfc-008 §4.2)."""
    name = claim_name('data', 0)
    _, volume = volume_at(homeserver, configuration(homeserver)['database_path'])
    (source,) = [it.inputs for it in homeserver.of_type(SOURCE)]

    assert volume['persistentVolumeClaim']['claimName'] == name
    assert source['spec']['sourcePVC'] == name
    assert source['metadata']['labels'][conventions.RETENTION_LABEL] == PRECIOUS.name
    (logical,) = [it.name for it in homeserver.of_type(CLAIM) if it.inputs['metadata']['name'] == name]
    assert homeserver.options_of(logical, CLAIM).protect is True


def test_the_claims_movers_run_as_the_server_so_they_read_and_write_its_files(homeserver: Recorder) -> None:
    (source,) = [it.inputs for it in homeserver.of_type(SOURCE)]
    user = pod_of(homeserver)['spec']['securityContext']['runAsUser']

    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == user


def test_the_namespace_is_one_whose_backups_the_bucket_holds_and_its_writer_key_backs_the_claim(
    homeserver: Recorder,
) -> None:
    """The writer key `physical` mints is per backed namespace, so the claim's repository is in one."""
    namespace = the_one(homeserver, NAMESPACE_TYPE)['metadata']['name']
    (repository,) = [it.inputs for it in homeserver.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == 'data']

    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[namespace]
    assert repository['metadata']['namespace'] == namespace
    assert repository['spec']['encryptedData'] == WRITER_KEY


# -- the client ------------------------------------------------------------------


def test_the_client_secret_is_ported_namespace_wide_into_the_namespace_it_was_sealed_to(homeserver: Recorder) -> None:
    secret = sealed_secret(homeserver, OIDC.name)

    assert secret['metadata']['namespace'] == NAMESPACE == the_one(homeserver, NAMESPACE_TYPE)['metadata']['name']
    assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}
    assert secret['spec']['encryptedData'] == CIPHERTEXT


def test_the_client_secret_is_the_file_the_configuration_names_readable_by_the_pods_group_alone(
    homeserver: Recorder,
) -> None:
    """continuwuity reads the secret from `client_secret_file`; the kubelet writes it as root, in the pod's `fsGroup`."""
    path = oidc_config(homeserver)['client_secret_file']
    directory, file = path.rsplit('/', 1)
    _, volume = volume_at(homeserver, directory)
    context = pod_of(homeserver)['spec']['securityContext']
    mode = int(volume['secret']['defaultMode'])

    assert 'client_secret' not in oidc_config(homeserver)
    assert volume['secret']['secretName'] == OIDC.name
    assert volume['secret']['items'] == [{'key': 'oidc_client_secret', 'path': file}]
    assert int(context['fsGroup']) == int(context['runAsGroup'])
    assert mode & 0o040, f'mode {mode:o}'
    assert not mode & 0o004, f'mode {mode:o} lets every user read the secret'


def test_the_client_identifier_reaches_the_server_as_a_string_from_the_ported_secret(homeserver: Recorder) -> None:
    """figment reads `CONTINUWUITY_OAUTH__OIDC__CLIENT_ID` as `oauth.oidc.client_id`, and as a string only when quoted.

    Unquoted, an identifier of digits would be read as a number and refused
    where the configuration wants a string. The kubelet expands `$(NAME)` from
    a variable defined above it in the list.
    """
    variables = env(homeserver)
    names = [it['name'] for it in variables]
    (identifier,) = [it for it in variables if it['name'] == 'CONTINUWUITY_OAUTH__OIDC__CLIENT_ID']
    quoted = re.fullmatch(r'"\$\((\w+)\)"', identifier['value'])
    assert quoted is not None, identifier['value']
    (referenced,) = quoted.groups()
    (source,) = [it for it in variables if it['name'] == referenced]

    assert names.index(referenced) < names.index(identifier['name'])
    assert source['valueFrom']['secretKeyRef'] == {'name': OIDC.name, 'key': 'oidc_client_id'}
    assert 'client_id' not in oidc_config(homeserver)


def test_the_server_asks_for_the_clients_scopes(homeserver: Recorder) -> None:
    """continuwuity always asks for `openid` and adds `additional_scopes`, which together are the client's."""
    assert {conventions.sso.Scope.OPENID.value, *oidc_config(homeserver)['additional_scopes']} == {
        s.value for s in CLIENT.scopes
    }


def test_every_claim_the_server_reads_is_one_the_portal_puts_in_its_id_token(homeserver: Recorder) -> None:
    oidc = oidc_config(homeserver)
    read = {oidc['preferred_username_claim'], oidc['email_claim'], *oidc['profile_key_map'].values()}

    assert oidc['prompt_for_localpart'] is False
    assert read <= {claim.value for claim in CLIENT.id_token}


def test_the_server_returns_a_browser_to_an_address_the_portal_registers(homeserver: Recorder) -> None:
    """continuwuity builds the address from its base URL and the path upstream fixes."""
    base_url = configuration(homeserver)['well_known']['client']

    assert f'{base_url.rstrip("/")}{UPSTREAM_REDIRECT_PATH}' in conventions.sso.redirect_uris(CLIENT)


def test_the_server_discovers_the_portal_at_the_issuer_it_is_handed(homeserver: Recorder) -> None:
    assert oidc_config(homeserver)['discovery_url'] == ISSUER


def test_a_client_that_does_not_register_the_servers_address_is_refused() -> None:
    client = dataclasses.replace(CLIENT, paths=('/elsewhere',))

    with pytest.raises(ValueError, match='does not register'):
        _ = declare(client=client)


def test_a_client_whose_id_token_lacks_the_localpart_is_refused() -> None:
    """Without the claim, continuwuity fails every sign-in, since it asks for no localpart."""
    client = dataclasses.replace(
        CLIENT, id_token=tuple(it for it in CLIENT.id_token if it is not conventions.sso.Claim.PREFERRED_USERNAME)
    )

    with pytest.raises(ValueError, match='preferred_username'):
        _ = declare(client=client)


def test_a_client_that_authenticates_in_the_form_body_is_refused() -> None:
    """continuwuity's OpenID Connect library sends HTTP Basic, and the portal refuses a method it did not register."""
    client = dataclasses.replace(CLIENT, token_endpoint_auth=conventions.sso.TokenEndpointAuth.POST)

    with pytest.raises(ValueError, match='authenticates by'):
        _ = declare(client=client)


def test_a_client_whose_id_token_lacks_the_email_is_accepted() -> None:
    """continuwuity attaches an address where the claim is present, and signs in without it."""
    client = dataclasses.replace(
        CLIENT, id_token=tuple(it for it in CLIENT.id_token if it is not conventions.sso.Claim.EMAIL)
    )

    _ = server_configuration(client, issuer=ISSUER)


def test_every_administrator_is_a_user_of_the_server(homeserver: Recorder) -> None:
    """An administrator is named by a Matrix user ID, whose server part is the server's own name."""
    config = configuration(homeserver)

    assert config['admins_list']
    for user in config['admins_list']:
        localpart, _, server = user.removeprefix('@').partition(':')
        assert user.startswith('@') and localpart, user
        assert server == config['server_name'], user


# -- the exposure ----------------------------------------------------------------


def test_the_route_at_the_clients_host_forwards_to_the_server_ungated(homeserver: Recorder) -> None:
    """Federation and the sign-in flow reach the server with no portal session, so its route carries no gate."""
    (route,) = [it.inputs for it in homeserver.of_type(HTTP_ROUTE)]
    service = the_one(homeserver, SERVICE)
    (port,) = service['spec']['ports']
    filters = [item for rule in route['spec']['rules'] for item in rule.get('filters', [])]

    assert route['spec']['hostnames'] == [f'{CLIENT.host}.{CLIENT.zone}']
    assert route['spec']['rules'][0]['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
    assert not [item for item in filters if item['type'] == 'ExtensionRef']


def test_a_held_row_publishes_no_record(homeserver: Recorder) -> None:
    assert not homeserver.of_type(RECORD)


def test_the_delegation_and_the_servers_own_well_known_name_the_host_its_route_serves(homeserver: Recorder) -> None:
    """The apex's documents and the server's agree, and name the host the route answers at, on 443."""
    (route,) = [it.inputs for it in homeserver.of_type(HTTP_ROUTE)]
    (host,) = route['spec']['hostnames']
    documents = {path: json.loads(body) for path, body in delegation(CLIENT).items()}
    well_known = configuration(homeserver)['well_known']

    assert documents['/.well-known/matrix/server'] == {'m.server': f'{host}:443'} == {'m.server': well_known['server']}
    assert documents['/.well-known/matrix/client'] == {'m.homeserver': {'base_url': well_known['client']}}
    assert well_known['client'] == f'https://{host}'


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
    with pytest.raises(ValueError, match='no route at'):
        _ = declare(exposure=exposure)


def test_a_gated_route_at_the_clients_host_is_refused() -> None:
    exposure = Exposure(routes=(Route(CLIENT.host, internet=Proxied(Gate.SESSION)),))

    with pytest.raises(ValueError, match='gated'):
        _ = declare(exposure=exposure)


# -- the pinned images, run as the pod runs them ----------------------------------

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_matrix`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why these cases need podman.
WHY = 'the server is run from the image it is pinned to with it'

#: The plaintext the stub hands the server in the ported Secret's place: an
#: identifier of digits, which figment reads as a number unless it is quoted.
PLAINTEXT = {'oidc_client_id': '1234567890', 'oidc_client_secret': 'stub-client-secret'}

#: The stub issuer's static server, which serves the discovery document and the
#: key set as JSON at `ISSUER`.
STUB_CONFIGURATION = """server {
    listen 8080;
    root /srv/issuer;
    default_type application/json;
}
"""

#: The stub issuer's image: the static site's stock server.
STATIC_SERVER = 'static-site'


@dataclass(frozen=True)
class Placed:
    """A file laid into a container before it starts: its bytes, mode and group, owned by root."""

    content: bytes
    mode: int = 0o644
    gid: int = 0


def archive(files: Mapping[str, Placed]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for path, placed in files.items():
            member = tarfile.TarInfo(path.lstrip('/'))
            member.size, member.mode, member.uid, member.gid = len(placed.content), placed.mode, 0, placed.gid
            tar.addfile(member, io.BytesIO(placed.content))
    return stream.getvalue()


def podman(*argv: str, input: bytes | None = None, timeout: float = COMMAND_TIMEOUT) -> str:  # noqa: A002 -- `subprocess.run`'s own name
    done = process_sessions.run(['podman', *argv], input=input, timeout=timeout, check=True)
    return done.stdout.decode()


def expanded(variables: list[dict[str, Any]], secret: Mapping[str, str]) -> dict[str, str]:
    """The container's environment as the kubelet builds it: each Secret key's value, then `$(NAME)` from above."""
    built: dict[str, str] = {}
    for variable in variables:
        if 'valueFrom' in variable:
            built[variable['name']] = secret[variable['valueFrom']['secretKeyRef']['key']]
        else:
            built[variable['name']] = re.sub(
                r'\$\((\w+)\)', lambda found: built.get(found[1], found[0]), variable['value']
            )
    return built


def server_files(monitor: Recorder) -> dict[str, Placed]:
    """The configuration and the client's secret, where the pod mounts them and as the kubelet writes them."""
    (path,) = [it['value'] for it in env(monitor) if it['name'] == 'CONTINUWUITY_CONFIG']
    mount, volume = volume_at(monitor, path)
    config_map = the_one(monitor, CONFIG_MAP)
    assert volume['configMap']['name'] == config_map['metadata']['name']
    secret_file = oidc_config(monitor)['client_secret_file']
    _, secret_volume = volume_at(monitor, secret_file.rsplit('/', 1)[0])
    context = pod_of(monitor)['spec']['securityContext']
    return {
        path: Placed(config_map['data'][mount['subPath']].encode()),
        secret_file: Placed(
            PLAINTEXT['oidc_client_secret'].encode(),
            mode=int(secret_volume['secret']['defaultMode']),
            gid=int(context['fsGroup']),
        ),
        # The kubelet writes one into every pod; the server reads it at its start.
        '/etc/resolv.conf': Placed(b'nameserver 127.0.0.1\n'),
    }


def issuer_files() -> dict[str, Placed]:
    discovery = {
        'issuer': ISSUER,
        'authorization_endpoint': f'{ISSUER}/authorize',
        'token_endpoint': f'{ISSUER}/token',
        'jwks_uri': f'{ISSUER}/jwks',
        'response_types_supported': ['code'],
        'subject_types_supported': ['public'],
        'id_token_signing_alg_values_supported': ['RS256'],
    }
    return {
        '/etc/nginx/conf.d/default.conf': Placed(STUB_CONFIGURATION.encode()),
        '/srv/issuer/.well-known/openid-configuration': Placed(json.dumps(discovery).encode()),
        '/srv/issuer/jwks': Placed(json.dumps({'keys': []}).encode()),
    }


def as_the_pod(monitor: Recorder, name: str, reference: str, *, network: str) -> list[str]:
    """`podman create` options that run a container of `reference` as the declared pod runs its one container."""
    context = pod_of(monitor)['spec']['securityContext']
    container = container_of(monitor)
    data = data_mount(monitor)
    return [
        'create',
        '--rm',
        f'--timeout={CONTAINER_TIMEOUT}',
        f'--label={LABEL}',
        f'--name={name}',
        f'--network={network}',
        '--read-only',
        '--read-only-tmpfs=false',
        '--cap-drop=all',
        '--security-opt=no-new-privileges',
        f'--user={int(context["runAsUser"])}:{int(context["runAsGroup"])}',
        f'--group-add={int(context["fsGroup"])}',
        # The claim: a directory the provisioner makes every user's to write,
        # mounted as the container's declaration mounts it.
        f'--tmpfs={data["mountPath"]}:mode=1777' + (',ro' if data.get('readOnly') else ''),
        *(f'--env={key}={value}' for key, value in expanded(container.get('env', []), PLAINTEXT).items()),
        *([f'--entrypoint={json.dumps(container["command"])}'] if 'command' in container else []),
        reference,
    ]


def data_mount(monitor: Recorder) -> dict[str, Any]:
    """The pod's one container's mount of the claim."""
    (mount,) = [it for it in container_of(monitor)['volumeMounts'] if it['name'] == 'data']
    return mount


def test_the_pinned_server_starts_and_signs_in_through_the_issuer_as_the_census_registers_it(
    homeserver: Recorder,
) -> None:
    """The pinned image, run as the pod runs it beside a stub issuer, answers, and sends a browser to the portal as the row says.

    The authorization URL its login page links to carries the identifier as
    the ported Secret holds it, unquoted, and the row's redirect address and
    scopes, which holds the quoting, `client_secret_file`, the read-only root
    and the redirect against continuwuity itself.
    """
    require_podman(WHY)
    server_image = pulled(pinned('matrix'))
    stub_image = pulled(pinned(STATIC_SERVER))
    run = uuid.uuid4().hex[:12]
    stub, server = (f'kluster-test-matrix-{role}-{run}' for role in ('issuer', 'server'))
    # The stub's `curl` asks, from the network the server shares: retried
    # while the server is not yet listening, a stop-loss on its start.
    ask = ('exec', stub, 'curl', '--retry', '20', '--retry-connrefused', '--retry-delay', '1', '--silent', '--fail')

    with process_sessions.removing(server, stub, timeout=COMMAND_TIMEOUT):
        _ = podman(
            'create', '--rm', f'--timeout={CONTAINER_TIMEOUT}', f'--label={LABEL}', f'--name={stub}', '--network=none',
            stub_image,
        )  # fmt: skip
        _ = podman('cp', '--archive=false', '-', f'{stub}:/', input=archive(issuer_files()))
        _ = podman('start', stub)
        _ = podman(*as_the_pod(homeserver, server, server_image, network=f'container:{stub}'))
        _ = podman('cp', '--archive=false', '-', f'{server}:/', input=archive(server_files(homeserver)))
        _ = podman('start', server)
        version = podman(*ask, 'http://127.0.0.1:8008/_matrix/federation/v1/version')
        page = podman(*ask, 'http://127.0.0.1:8008/_continuwuity/account/login')

    links = [html.unescape(it) for it in re.findall(r'href="([^"]*)"', page) if html.unescape(it).startswith(ISSUER)]
    (authorization,) = links
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization).query)
    assert 'server' in json.loads(version)
    assert authorization.startswith(f'{ISSUER}/authorize?')
    assert query['client_id'] == [PLAINTEXT['oidc_client_id']]
    assert query['redirect_uri'][0] in conventions.sso.redirect_uris(CLIENT)
    assert set(query['scope'][0].split()) == {scope.value for scope in CLIENT.scopes}


# -- the receiving layer ---------------------------------------------------------

#: The pin the receiver is declared with here, unlike the server's.
RECEIVER = ImagePin('registry.example.invalid/receiver', 'v9.9.9', f'sha256:{"f" * 64}')


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def receiving() -> Recorder:
    """The homeserver declared with a receiver in the server's place, as the land declares it while it streams."""
    monitor = await run_with(Generating(), stack='apps')
    async with declaring():
        _ = declare(receiver=RECEIVER)
    return monitor


def claim_at(monitor: Recorder, path: str) -> str:
    """The claim the pod's one container mounts at `path`."""
    _, volume = volume_at(monitor, path)
    return volume['persistentVolumeClaim']['claimName']


def test_while_receiving_no_container_runs_the_server(receiving: Recorder) -> None:
    """The server started on the claim would create a database of its own there, beside what is streamed in."""
    spec = pod_of(receiving)['spec']
    images = [it['image'] for it in (*spec['containers'], *spec.get('initContainers', []))]

    assert images == [str(RECEIVER)]


def test_the_receiver_mounts_the_claim_where_the_server_reads_its_database(
    homeserver: Recorder, receiving: Recorder
) -> None:
    database = configuration(homeserver)['database_path']

    assert claim_at(receiving, database) == claim_at(homeserver, database)


def test_the_receiver_runs_as_the_server_and_is_placed_as_the_server(homeserver: Recorder, receiving: Recorder) -> None:
    """What it extracts is the server's to write, and the claim binds where the server is placed.

    The claim binds to the node its first consumer is scheduled on, so the
    receiver is a running pod with the server's constraints, never zero
    replicas.
    """
    server = pod_of(homeserver)['spec']
    receiver = pod_of(receiving)['spec']

    for field in ('runAsUser', 'runAsGroup'):
        assert receiver['securityContext'][field] == server['securityContext'][field]
    for key in ('affinity', 'tolerations', 'priorityClassName'):
        assert receiver.get(key) == server.get(key), key
    assert the_one(receiving, DEPLOYMENT)['spec']['replicas'] == 1


def test_the_receiver_meets_restricted_and_carries_limits(receiving: Recorder) -> None:
    spec = pod_of(receiving)['spec']
    context = spec['securityContext']
    container = container_of(receiving)

    assert context['runAsNonRoot'] is True
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities'] == {'drop': ['ALL']}
    assert {'cpu', 'memory'} <= container['resources']['limits'].keys()


#: The pin the land hands the receiver: the static site's stock server, which
#: has `tar` and `sleep` and is pulled in the same wave.
RECEIVER_PIN = 'static-site'

#: Why the receiver's case needs podman.
RECEIVER_WHY = 'the receiver is run from the image the land hands it with it'


def legacy_stream() -> bytes:
    """What `tar -c -C <legacy claim> .` writes for a claim holding one file in one directory, every member root's."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for path in ('./', './db/'):
            member = tarfile.TarInfo(path)
            member.type, member.mode, member.uid, member.gid = tarfile.DIRTYPE, 0o755, 0, 0
            tar.addfile(member)
        content = b'MANIFEST-000001\n'
        member = tarfile.TarInfo('./db/CURRENT')
        member.size, member.mode, member.uid, member.gid = len(content), 0o600, 0, 0
        tar.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def test_the_pinned_receiver_idles_and_extracts_the_lands_stream_as_the_servers_user(receiving: Recorder) -> None:
    """The land's stream, root's files from the legacy claim, lands on the claim as the server's to read and write.

    The stream is the whole legacy directory, `./` first, read by the command
    the land execs (`RECEIVE`); it lands under the claim's mount.
    """
    require_podman(RECEIVER_WHY)
    reference = pulled(pinned(RECEIVER_PIN))
    name = f'kluster-test-matrix-receiver-{uuid.uuid4().hex[:12]}'
    user = int(pod_of(receiving)['spec']['securityContext']['runAsUser'])
    data = data_mount(receiving)['mountPath']

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _ = podman(*as_the_pod(receiving, name, reference, network='none'))
        _ = podman('start', name)
        _ = podman('exec', '-i', name, *RECEIVE, input=legacy_stream())
        owned = podman('exec', name, 'stat', '-c', '%u %a', f'{data}/db/CURRENT')
        running = podman('inspect', '--format', '{{.State.Running}}', name)

    assert owned.split() == [str(user), '600']
    assert running.strip() == 'true'
