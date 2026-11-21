"""The portal, declared against mocks, its configuration read and served by the pinned Authelia, and the canary's run by the pinned curl.

The portal is declared with a route row, image pins and ciphertexts of the
test's own, so what is held is how the component composes what it is handed;
its clients are the census's, since the configuration is rendered from it.
The configuration is the one the declared ConfigMap carries. Read here, it is
YAML once each `secret` expression of Authelia's template filter stands as
the file it names. The cases that need Authelia to read it run the image
`Pulumi.yaml` pins, with podman, with the filter on and a test value in every
file a `secret` and an `AUTHELIA_*_FILE` variable name: Authelia's own
validation, its own evaluation of the access-control rules, and its answers to
requests composed as Envoy's ext_authz and Traefik's `forwardAuth` compose
them, beside the pinned Redis, are what say the configuration holds, not a
reading of the file. The canary's run is run the same way, in the pinned curl
image, against a stand-in for `curl` that answers as the gate would.

Those cases run as `pinned_image` has them: skipped where podman is not on
`PATH`, failed on CI's runner, the image pulled by its digest, and every
container carrying `--rm`, a `--timeout` and the module's label
(framework/testing.md §1).

Literals written here are the upstream projects' contracts: Authelia's
configuration keys, template functions, CLI, endpoints, users file and
messages; the headers Envoy's ext_authz and Traefik's `forwardAuth` send;
curl's options and `--write-out` variables; the Kubernetes, Gateway API and
Cilium fields, the kubelet's layout of a Secret's files, the sealed-secrets
controller's scope annotations, and the label Pod Security Admission reads;
and the addresses kluster-code's forward-auth checks ask at.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import tarfile
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
import yaml
from mock_monitor import Recorder, declaring, run_with
from node_selection import CLOUD, NODES, schedulable
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, pinned, pulled, require_podman
from pulumi import runtime
from section_numbers import sections

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.authelia import (
    ACCESS_RULES,
    AUTH_CANARY,
    CANARY_NAMESPACE,
    CANARY_PATH,
    CONFIG_SECRETS,
    CONFIGURATION,
    NAMESPACE,
    RECEIVE,
    SECRETS,
    AccessRule,
    Authelia,
    Group,
    User,
    gate_endpoints,
)
from kluster.components.apps.exim import HOST as SMTP_HOST
from kluster.components.apps.exim import PORT as SMTP_PORT
from kluster.components.apps.storage import claim_name
from kluster.components.cilium import InternetPoolMembers
from kluster.components.monitoring.operator_objects import type_token
from kluster.conventions.backup import BACKED_NAMESPACES, PRECIOUS, BackupFamily, duration, max_age
from kluster.conventions.routes import PROXIED, REWRITE, Exposure, Gate, Held, Proxied, Route
from kluster.conventions.sso import Factor
from kluster.lib.k8s import PORTAL, Backend, Portal, route, sealed_ciphertext
from kluster.lib.versions import ImagePin

ROOT = Path(__file__).resolve().parents[1]

NAMESPACE_TYPE = 'kubernetes:core/v1:Namespace'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
SERVICE = 'kubernetes:core/v1:Service'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
CRONJOB = 'kubernetes:batch/v1:CronJob'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
GRANT = 'crds:gateway.networking.k8s.io/v1:ReferenceGrant'
VMRULE = type_token('VMRule')
PASSWORD = 'random:index/randomPassword:RandomPassword'

#: The route row the portal is declared with, held as the land declares it.
EXPOSURE = Exposure(routes=(Held(Route('auth', internet=PROXIED, lan=REWRITE)),))
PORTAL_HOST = f'auth.{conventions.ZONE_PRIMARY}'

#: The pins it is declared with here: references that are no published
#: image, so a container carrying one carries that pin.
IMAGE = ImagePin('registry.example.invalid/authelia', 'v9.9.9', f'sha256:{"a" * 64}')
REDIS_IMAGE = ImagePin('registry.example.invalid/redis', 'v9.9.9', f'sha256:{"b" * 64}')
CANARY_IMAGE = ImagePin('registry.example.invalid/curl', 'v9.9.9', f'sha256:{"c" * 64}')
TARGET_IMAGE = ImagePin('registry.example.invalid/nginx', 'v9.9.9', f'sha256:{"d" * 64}')

#: The ciphertexts it is handed, each naming what it stands for.
SECRETS_CIPHERTEXT = {key: f'sealed-{key}' for key in SECRETS.keys}
CONFIG_CIPHERTEXT = {key: f'sealed-{key}' for key in CONFIG_SECRETS.keys}
STORAGE_KEY = {key: f'sealed-{key}' for key in conventions.sealed.AUTHELIA_STORAGE_KEY.keys}
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'sealed-key-id', 'AWS_SECRET_ACCESS_KEY': 'sealed-key-secret'}

#: A namespace whose routes the gate guards.
GATED = 'some-application'

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

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_authelia`.
LABEL = f'kluster-test={Path(__file__).stem}'


class Generating(Recorder):
    """A monitor that answers a password's `result` as the random provider would."""

    def computed(self, args: runtime.MockResourceArgs) -> dict[str, Any]:
        return {'result': 'generated-in-state'} if args.typ == PASSWORD else {}


def declare(*, exposure: Exposure = EXPOSURE, receiving: bool = False) -> Authelia:
    return Authelia(
        'authelia',
        exposure=exposure,
        image=IMAGE,
        redis_image=REDIS_IMAGE,
        canary_image=CANARY_IMAGE,
        target_image=TARGET_IMAGE,
        secrets=SECRETS_CIPHERTEXT,
        config_secrets=CONFIG_CIPHERTEXT,
        storage_key=STORAGE_KEY,
        writer_key=WRITER_KEY,
        gated=(GATED,),
        fronts=FRONTS,
        zone_ids=ZONE_IDS,
        receiving=receiving,
    )


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def portal() -> Recorder:
    """The portal, declared with the test's route, pins and ciphertexts, and beside it a gated row's routes in `GATED`."""
    monitor = await run_with(Generating(), stack='apps')
    async with declaring():
        _ = declare()
        for gate in Gate:
            _ = route(
                f'gated-{gate.value}',
                Route(f'gated-{gate.value}', internet=Proxied(gate=gate)),
                namespace=GATED,
                backend=Backend(service='application', port=80),
            )
    return monitor


def deployment(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(DEPLOYMENT) if it.inputs['metadata']['name'] == name]
    return found


def service(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(SERVICE) if it.inputs['metadata']['name'] == name]
    return found


def pod_of(monitor: Recorder, name: str = 'authelia') -> dict[str, Any]:
    return deployment(monitor, name)['spec']['template']


def container_of(monitor: Recorder, name: str = 'authelia') -> dict[str, Any]:
    (container,) = pod_of(monitor, name)['spec']['containers']
    return container


def canary_pod(monitor: Recorder) -> dict[str, Any]:
    (cronjob,) = [it.inputs for it in monitor.of_type(CRONJOB)]
    return cronjob['spec']['jobTemplate']['spec']['template']


def every_pod(monitor: Recorder) -> list[dict[str, Any]]:
    """Every pod template the component declares: the server's or the receiver's, Redis's, the target's and the canary's."""
    return [*(it.inputs['spec']['template'] for it in monitor.of_type(DEPLOYMENT)), canary_pod(monitor)]


def volume_at(pod: Mapping[str, Any], container: Mapping[str, Any], path: str) -> dict[str, Any]:
    """The pod's volume the container mounts at `path`, or whose mount holds `path`."""
    (mount,) = [
        it for it in container['volumeMounts'] if path == it['mountPath'] or path.startswith(f'{it["mountPath"]}/')
    ]
    (volume,) = [it for it in pod['spec']['volumes'] if it['name'] == mount['name']]
    return volume


def env(container: Mapping[str, Any]) -> dict[str, Any]:
    return {it['name']: it for it in container.get('env', [])}


def config_map(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(CONFIG_MAP) if it.inputs['metadata']['name'] == name]
    return found


def raw_configuration(monitor: Recorder) -> str:
    """The configuration the ConfigMap carries, which the pod mounts at the path the server is started with."""
    container = container_of(monitor)
    assert container['args'] == ['--config', CONFIGURATION]
    (mount,) = [it for it in container['volumeMounts'] if it['mountPath'] == CONFIGURATION]
    volume = volume_at(pod_of(monitor), container, CONFIGURATION)
    return config_map(monitor, volume['configMap']['name'])['data'][mount['subPath']]


#: A `secret` expression of Authelia's template filter, as the configuration
#: writes every one: the file it reads, then `mindent` and `msquote`.
SECRET = re.compile(r'\{\{ secret "([^"]+)" \| mindent \d+ "\|" \| msquote \}\}')


def configuration(monitor: Recorder) -> dict[str, Any]:
    """The configuration as YAML, each `secret` expression standing as `secret:<the file it reads>`."""
    text = raw_configuration(monitor)
    assert '{{' not in SECRET.sub('', text), 'an expression of the template filter this reading does not model'
    return yaml.safe_load(SECRET.sub(lambda found: f'"secret:{found.group(1)}"', text))


def sealed_secret(monitor: Recorder, name: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == name]
    return found


# -- what the gate names ---------------------------------------------------------


def gated_routes(monitor: Recorder) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every route that carries the gate, with the ExternalAuth filter it carries."""
    return [
        (it.inputs, flt['externalAuth'])
        for it in monitor.of_type(HTTP_ROUTE)
        for rule in it.inputs['spec']['rules']
        for flt in rule.get('filters', [])
        if flt['type'] == 'ExternalAuth'
    ]


def test_every_gate_names_the_service_the_component_declares(portal: Recorder) -> None:
    """The filter on a gated row and on the canary's route asks a Service this component declares, at its namespace, name and port."""
    declared = service(portal, PORTAL.service)
    routes = gated_routes(portal)

    assert {found['metadata']['namespace'] for found, _ in routes} == {GATED, CANARY_NAMESPACE}
    for _, ext in routes:
        ref = ext['backendRef']
        assert (ref['namespace'], ref['name']) == (declared['metadata']['namespace'], declared['metadata']['name'])
        assert ref['port'] in {port['port'] for port in declared['spec']['ports']}


def test_the_grant_lets_every_gated_routes_namespace_name_the_service_the_component_declares(
    portal: Recorder,
) -> None:
    """A route in another namespace names the Service only where a grant in the Service's namespace allows it, the canary's among them."""
    declared = service(portal, PORTAL.service)
    (grant,) = [it.inputs for it in portal.of_type(GRANT)]

    for found, ext in gated_routes(portal):
        assert found['metadata']['namespace'] != ext['backendRef']['namespace']
        assert grant['metadata']['namespace'] == ext['backendRef']['namespace']
        assert {
            'group': 'gateway.networking.k8s.io',
            'kind': 'HTTPRoute',
            'namespace': found['metadata']['namespace'],
        } in grant['spec']['from']
    assert {'group': '', 'kind': 'Service', 'name': declared['metadata']['name']} in grant['spec']['to']


def test_the_server_listens_on_the_portals_port_which_its_service_and_probes_reach(portal: Recorder) -> None:
    declared = service(portal, PORTAL.service)
    container = container_of(portal)
    (port,) = declared['spec']['ports']
    named = {it['name']: it['containerPort'] for it in container['ports']}

    assert declared['spec']['selector'].items() <= pod_of(portal)['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port'] == PORTAL.port
    assert configuration(portal)['server']['address'] == f'tcp://:{PORTAL.port}/'
    for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
        assert container[probe]['httpGet'] == {'path': '/api/health', 'port': port['targetPort']}


@pytest.mark.parametrize(
    'paths',
    [
        {Gate.SESSION: '/api/verify/', Gate.BASIC: '/api/authz/ext-authz-basic/'},
        {Gate.SESSION: '/api/authz/ext-authz', Gate.BASIC: '/api/authz/ext-authz-basic/'},
    ],
    ids=['outside-the-prefix', 'no-trailing-slash'],
)
def test_a_portal_path_the_server_serves_no_gate_endpoint_at_is_refused(paths: Mapping[Gate, str]) -> None:
    foreign = Portal(namespace=NAMESPACE, service='authelia', port=9091, paths=paths, identity_headers=('remote-user',))

    with pytest.raises(ValueError, match='serves no authorization endpoint'):
        _ = gate_endpoints(foreign)


# -- the pods --------------------------------------------------------------------


@pytest.fixture(params=['portal', 'receiving'])
def declared(request: pytest.FixtureRequest) -> Recorder:
    """The portal, declared serving and declared receiving: each holds every pod to the same posture."""
    return request.getfixturevalue(request.param)


def test_the_namespaces_enforce_restricted_and_the_portals_is_the_one_the_ported_values_open_in(
    portal: Recorder,
) -> None:
    namespaces = {it.inputs['metadata']['name']: it.inputs for it in portal.of_type(NAMESPACE_TYPE)}

    assert set(namespaces) == {NAMESPACE, CANARY_NAMESPACE}
    for found in namespaces.values():
        assert found['metadata']['labels']['pod-security.kubernetes.io/enforce'] == 'restricted'
    assert PORTAL.namespace == NAMESPACE
    for value in (SECRETS, CONFIG_SECRETS, conventions.sealed.AUTHELIA_STORAGE_KEY):
        assert sealed_secret(portal, value.name)['metadata']['namespace'] == NAMESPACE == value.namespace


def test_every_pod_runs_as_a_user_that_is_not_root_on_a_read_only_root(declared: Recorder) -> None:
    """`restricted` refuses a pod that does not say it runs as a user that is not root."""
    for pod in every_pod(declared):
        context = pod['spec']['securityContext']
        assert context['runAsNonRoot'] is True
        assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
        (container,) = pod['spec']['containers']
        assert container['securityContext']['allowPrivilegeEscalation'] is False
        assert container['securityContext']['capabilities'] == {'drop': ['ALL']}
        assert container['securityContext']['readOnlyRootFilesystem'] is True


def test_a_pod_whose_image_names_no_numeric_user_states_one(declared: Recorder) -> None:
    """The server's image runs as root, Redis's and curl's as a name: each pod states a number (rfc-008 §10.2)."""
    for pod in (pod_of(declared), pod_of(declared, 'authelia-redis'), canary_pod(declared)):
        context = pod['spec']['securityContext']
        # The mock hands numbers back as floats, so the user is held to a whole one.
        assert context['runAsUser'] == int(context['runAsUser']) > 0


def test_every_pod_keeps_working_while_the_uplink_is_down_at_home_with_cpu_and_memory_limits(
    declared: Recorder,
) -> None:
    """The portal declares it: every pod admits no cloud node and tolerates its node's unreachability.

    Every pod carries both limits, since a pod may be placed on a cloud node,
    where etcd shares the cores (declarative/workloads.md §1).
    """
    for pod in every_pod(declared):
        assert schedulable(pod['spec']) == set(NODES) - CLOUD
        assert {it['key'] for it in pod['spec']['tolerations']} == {'node.kubernetes.io/unreachable'}
        (container,) = pod['spec']['containers']
        assert {'cpu', 'memory'} <= container['resources']['limits'].keys()
        assert {'cpu', 'memory'} <= container['resources']['requests'].keys()


def test_no_pod_carries_a_service_account_token(declared: Recorder) -> None:
    for pod in every_pod(declared):
        assert pod['spec']['automountServiceAccountToken'] is False


def test_the_server_is_handed_no_service_link_variables(portal: Recorder) -> None:
    """The kubelet names a Service's link variables after it, `AUTHELIA_…` here, and Authelia reads every such variable as configuration."""
    assert pod_of(portal)['spec']['enableServiceLinks'] is False


def test_one_server_holds_the_database_at_a_time(portal: Recorder) -> None:
    spec = deployment(portal, 'authelia')['spec']

    assert spec['replicas'] == 1
    assert spec['strategy'] == {'type': 'Recreate'}


# -- the claim -------------------------------------------------------------------


def test_the_users_and_the_database_are_on_the_claim_backed_up_as_precious_and_protected(portal: Recorder) -> None:
    name = claim_name('storage', 0)
    config = configuration(portal)
    pod, container = pod_of(portal), container_of(portal)
    (source,) = [it.inputs for it in portal.of_type(SOURCE)]

    for path in (config['authentication_backend']['file']['path'], config['storage']['local']['path']):
        assert volume_at(pod, container, path)['persistentVolumeClaim']['claimName'] == name
    assert source['spec']['sourcePVC'] == name
    assert source['metadata']['labels'][conventions.RETENTION_LABEL] == PRECIOUS.name
    (logical,) = [it.name for it in portal.of_type(CLAIM) if it.inputs['metadata']['name'] == name]
    assert portal.options_of(logical, CLAIM).protect is True


def test_the_claims_movers_are_placed_where_the_server_runs(declared: Recorder) -> None:
    """A mover binds the claim on a node first when it is scheduled first, so it is held to the server's nodes."""
    (source,) = [it.inputs for it in declared.of_type(SOURCE)]

    assert schedulable({'affinity': source['spec']['restic']['moverAffinity']}) == schedulable(pod_of(declared)['spec'])


def test_the_claims_movers_run_as_the_server_so_they_read_and_write_its_files(portal: Recorder) -> None:
    (source,) = [it.inputs for it in portal.of_type(SOURCE)]
    user = pod_of(portal)['spec']['securityContext']['runAsUser']

    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == user


def test_the_namespace_is_one_whose_backups_the_bucket_holds_and_its_writer_key_backs_the_claim(
    portal: Recorder,
) -> None:
    (repository,) = [it.inputs for it in portal.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == 'storage']

    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[NAMESPACE]
    assert repository['spec']['encryptedData'] == WRITER_KEY


def test_the_sessions_redis_holds_no_claim(portal: Recorder) -> None:
    """Losing the sessions costs a sign-in, so they are kept in memory alone (rfc-008 §4.2)."""
    pod = pod_of(portal, 'authelia-redis')

    assert all('persistentVolumeClaim' not in volume for volume in pod['spec']['volumes'])
    assert {'--save', '', '--appendonly', 'no'} <= set(container_of(portal, 'authelia-redis')['args'])


# -- the secrets -----------------------------------------------------------------


def secret_file(monitor: Recorder, path: str) -> tuple[str, str]:
    """The Secret and its data key a file the server's container reads is, by the kubelet's layout: one file per key."""
    pod, container = pod_of(monitor), container_of(monitor)
    (mount,) = [it for it in container['volumeMounts'] if path.startswith(f'{it["mountPath"]}/')]
    volume = volume_at(pod, container, path)
    return volume['secret']['secretName'], path.removeprefix(f'{mount["mountPath"]}/')


def test_every_secret_the_server_reads_from_a_variable_is_a_key_of_a_secret_it_mounts(portal: Recorder) -> None:
    """`AUTHELIA_<key>_FILE` names the file the server reads `<key>` of its configuration from."""
    variables = {name: it['value'] for name, it in env(container_of(portal)).items() if name.endswith('_FILE')}
    read = {secret_file(portal, path) for path in variables.values()}

    assert read == {(SECRETS.name, key) for key in SECRETS.keys} | {
        (conventions.sealed.AUTHELIA_STORAGE_KEY.name, key) for key in conventions.sealed.AUTHELIA_STORAGE_KEY.keys
    }
    for name, path in variables.items():
        assert name == f'AUTHELIA_{path.rsplit("/", 1)[1]}_FILE'


def test_every_secret_the_configuration_reads_is_a_key_of_the_ported_configuration_secret(portal: Recorder) -> None:
    read = {secret_file(portal, path) for path in SECRET.findall(raw_configuration(portal))}

    assert read == {(CONFIG_SECRETS.name, key) for key in CONFIG_SECRETS.keys}


def test_the_ported_secrets_are_declared_namespace_wide_and_the_storage_key_strict(portal: Recorder) -> None:
    for value, ciphertext in ((SECRETS, SECRETS_CIPHERTEXT), (CONFIG_SECRETS, CONFIG_CIPHERTEXT)):
        declared = sealed_secret(portal, value.name)
        assert declared['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}
        assert declared['spec']['encryptedData'] == ciphertext
    storage = sealed_secret(portal, conventions.sealed.AUTHELIA_STORAGE_KEY.name)
    assert storage['metadata']['annotations'] == {'sealedsecrets.bitnami.com/strict': 'true'}
    assert storage['spec']['encryptedData'] == STORAGE_KEY


def test_the_stacks_configuration_holds_every_key_the_ported_secrets_are_read_with() -> None:
    """The `apps` stack's file carries the ported ciphertexts, which the program reads by these keys."""
    configured = yaml.safe_load((ROOT / 'Pulumi.apps.yaml').read_text())['config'][
        f'kluster-py:{conventions.sealed.CONFIG_KEY}'
    ]

    for value in (SECRETS, CONFIG_SECRETS):
        assert set(sealed_ciphertext(value, configured)) == set(value.keys)
        assert set(configured[value.name]) == set(value.keys)


def test_redis_takes_the_password_the_server_reads_for_it(portal: Recorder) -> None:
    redis = container_of(portal, 'authelia-redis')
    password = env(redis)['REDIS_PASSWORD']['valueFrom']['secretKeyRef']
    server_reads = secret_file(portal, env(container_of(portal))['AUTHELIA_SESSION_REDIS_PASSWORD_FILE']['value'])
    session = configuration(portal)['session']['redis']
    redis_service = service(portal, 'authelia-redis')

    assert (password['name'], password['key']) == server_reads
    assert redis['args'][:2] == ['--requirepass', '$(REDIS_PASSWORD)']
    assert session['host'] == f'{redis_service["metadata"]["name"]}.{NAMESPACE}.svc'
    assert session['port'] == redis_service['spec']['ports'][0]['port']
    assert redis_service['spec']['selector'].items() <= pod_of(portal, 'authelia-redis')['metadata']['labels'].items()


# -- the clients -----------------------------------------------------------------


@pytest.mark.parametrize('client', conventions.sso.CLIENTS, ids=lambda client: client.name)
def test_every_client_is_rendered_from_its_census_row(portal: Recorder, client: conventions.sso.Client) -> None:
    oidc = configuration(portal)['identity_providers']['oidc']
    (rendered,) = [it for it in oidc['clients'] if it['client_name'] == client.name]

    for field, part in (('client_id', 'ID'), ('client_secret', 'SECRET')):
        assert secret_file(portal, rendered[field].removeprefix('secret:')) == (
            CONFIG_SECRETS.name,
            f'OIDC_CLIENTS_{client.name.upper()}_{part}',
        )
    assert rendered['redirect_uris'] == list(conventions.sso.redirect_uris(client))
    assert rendered['token_endpoint_auth_method'] == client.token_endpoint_auth.value
    assert rendered['scopes'] == [scope.value for scope in client.scopes]
    assert rendered.get('require_pkce', False) is client.pkce
    if client.pkce:
        assert rendered['pkce_challenge_method'] == 'S256'
    assert ('refresh_token' in rendered['grant_types']) is (conventions.sso.Scope.OFFLINE_ACCESS in client.scopes)
    if client.id_token:
        assert oidc['claims_policies'][rendered['claims_policy']] == {
            'id_token': [claim.value for claim in client.id_token]
        }
    else:
        assert 'claims_policy' not in rendered


@pytest.mark.parametrize('client', conventions.sso.CLIENTS, ids=lambda client: client.name)
def test_every_clients_policy_admits_its_grants_in_order_and_denies_everyone_else(
    portal: Recorder, client: conventions.sso.Client
) -> None:
    """Authelia's own default is `two_factor`, which would admit any subject the portal knows."""
    oidc = configuration(portal)['identity_providers']['oidc']
    (rendered,) = [it for it in oidc['clients'] if it['client_name'] == client.name]
    policy = oidc['authorization_policies'][rendered['authorization_policy']]

    assert policy['default_policy'] == 'deny'
    assert policy['rules'] == [
        {'policy': grant.factor.value, 'subject': f'group:{grant.group}'} for grant in client.policy
    ]


# -- the notifier ----------------------------------------------------------------


def test_the_notifier_speaks_plain_smtp_to_the_relay(portal: Recorder) -> None:
    """The relay offers no STARTTLS, so Authelia is told not to require it."""
    smtp = configuration(portal)['notifier']['smtp']

    assert smtp['address'] == f'smtp://{SMTP_HOST}:{SMTP_PORT}'
    assert smtp['disable_require_tls'] is True


def test_the_servers_pods_carry_the_label_the_relay_admits_senders_by(portal: Recorder) -> None:
    assert conventions.MAIL_SENDER_LABEL.items() <= pod_of(portal)['metadata']['labels'].items()


# -- the access control ----------------------------------------------------------


def test_the_access_control_denies_by_default_names_no_network_and_regulates_by_user(portal: Recorder) -> None:
    """The first element of `X-Forwarded-For` is the client's own word, so neither a rule nor regulation may key on it."""
    config = configuration(portal)
    access = config['access_control']

    assert access['default_policy'] == 'deny'
    assert 'networks' not in access
    assert all('networks' not in rule for rule in access['rules'])
    assert config['regulation']['modes'] == ['user']


@pytest.mark.parametrize(
    'build',
    [
        lambda: AccessRule((), Group('users'), Factor.ONE),
        lambda: Group(''),
        lambda: User(''),
    ],
    ids=['no-host', 'nameless-group', 'nameless-user'],
)
def test_an_access_rule_that_names_no_host_or_no_subject_is_refused(build: Any) -> None:
    with pytest.raises(ValueError, match='names no'):
        _ = build()


# -- the canary ------------------------------------------------------------------


def canary_route(monitor: Recorder) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(HTTP_ROUTE) if it.inputs['metadata']['name'] == 'auth-canary']
    return found


def test_the_canarys_path_is_guarded_by_the_gate_every_session_gated_row_carries(portal: Recorder) -> None:
    """The canary watches the mechanism the gated rows ride: its filter is the one `route` emits for the session form."""
    (rule,) = canary_route(portal)['spec']['rules']
    (session,) = [
        ext
        for found, ext in gated_routes(portal)
        if found['metadata']['namespace'] == GATED and ext['http']['path'] == PORTAL.paths[Gate.SESSION]
    ]

    assert [it['externalAuth'] for it in rule['filters']] == [session]


def test_the_canarys_route_serves_its_path_on_the_portals_name_from_the_target(portal: Recorder) -> None:
    found = canary_route(portal)
    (rule,) = found['spec']['rules']
    (backend,) = rule['backendRefs']
    target = service(portal, backend['name'])

    assert found['spec']['hostnames'] == [PORTAL_HOST]
    assert found['spec']['parentRefs'] == [
        {'name': conventions.GATEWAY_INTERNET, 'namespace': conventions.GATEWAY_NAMESPACE}
    ]
    assert rule['matches'] == [{'path': {'type': 'PathPrefix', 'value': CANARY_PATH}}]
    assert found['metadata']['namespace'] == target['metadata']['namespace'] == CANARY_NAMESPACE
    assert backend['port'] in {port['port'] for port in target['spec']['ports']}
    target_pod = pod_of(portal, target['metadata']['name'])
    assert target['spec']['selector'].items() <= target_pod['metadata']['labels'].items()


def test_the_canary_requests_its_route_at_the_balancers_ipv4_and_is_held_to_the_portal_it_is_sent_to(
    portal: Recorder,
) -> None:
    """A refusal redirects to the portal the configuration names, so that is the one the canary accepts."""
    (container,) = canary_pod(portal)['spec']['containers']
    variables = {name: it['value'] for name, it in env(container).items()}
    (cookie,) = configuration(portal)['session']['cookies']

    assert [variables['CANARY_HOST']] == canary_route(portal)['spec']['hostnames']
    assert variables['CANARY_ADDRESS'] == '203.0.113.10'
    assert variables['CANARY_PATH'] == CANARY_PATH
    assert variables['CANARY_PORTAL'] == cookie['authelia_url']


def test_the_canarys_schedule_and_deadlines_are_its_jobs(portal: Recorder) -> None:
    """The cron line, the run's deadline and its starting deadline are the ones the family's rules are derived from."""
    (cronjob,) = [it.inputs for it in portal.of_type(CRONJOB)]
    minute, *rest = cronjob['spec']['schedule'].split()

    assert cronjob['metadata'] == {'name': AUTH_CANARY.cronjob, 'namespace': AUTH_CANARY.namespace}
    assert rest == ['*', '*', '*', '*']
    assert minute == f'*/{int(AUTH_CANARY.period.total_seconds() // 60)}'
    assert cronjob['spec']['jobTemplate']['spec']['activeDeadlineSeconds'] == AUTH_CANARY.deadline.total_seconds()
    assert AUTH_CANARY.starting_deadline is not None
    assert cronjob['spec']['startingDeadlineSeconds'] == AUTH_CANARY.starting_deadline.total_seconds()


def _threshold(expr: str) -> dt.timedelta:
    """The age a stale alert fires past, in the duration syntax its expression ends in."""
    found = re.search(r'> (\d+)([wdhms])$', expr)
    assert found is not None, expr
    unit = {'w': 'weeks', 'd': 'days', 'h': 'hours', 'm': 'minutes', 's': 'seconds'}[found[2]]
    return dt.timedelta(**{unit: int(found[1])})


def test_the_canarys_rules_watch_the_cronjob_declared_beside_them_and_their_playbook_exists(portal: Recorder) -> None:
    """A canary that is not refused or stops running pages, held to the CronJob declared beside it."""
    (cronjob,) = [it.inputs for it in portal.of_type(CRONJOB)]
    spec = cronjob['spec']['jobTemplate']['spec']
    (rules_object,) = [it.inputs for it in portal.of_type(VMRULE)]
    (group,) = rules_object['spec']['groups']
    selector = f'{{namespace="{cronjob["metadata"]["namespace"]}", cronjob="{cronjob["metadata"]["name"]}"}}'
    rules = {rule['alert']: rule for rule in group['rules']}
    (stale,) = (rule for name, rule in rules.items() if name.endswith('Stale'))
    (failed,) = (rule for name, rule in rules.items() if name.endswith('Failed'))
    period = dt.timedelta(minutes=int(cronjob['spec']['schedule'].split()[0].removeprefix('*/')))

    assert rules_object['metadata']['namespace'] == cronjob['metadata']['namespace']
    assert failed['for'] == duration(dt.timedelta(seconds=spec['activeDeadlineSeconds']))
    assert _threshold(stale['expr']) == max_age(period)
    for rule in group['rules']:
        assert selector in rule['expr'], rule['expr']
        document, section = rule['annotations']['playbook'].split(' §')
        assert section in sections((ROOT / document).read_text())


def test_the_canarys_run_is_the_script_the_cronjob_mounts(portal: Recorder) -> None:
    pod = canary_pod(portal)
    (container,) = pod['spec']['containers']
    script = container['command'][-1]
    volume = volume_at(pod, container, script)

    assert container['command'][0] == 'sh'
    assert config_map(portal, volume['configMap']['name'])['data'][script.rsplit('/', 1)[1]].startswith('#!/bin/sh')


# -- the exposure ----------------------------------------------------------------


def test_the_portals_routes_forward_to_its_service_ungated_on_internet_and_at_home(portal: Recorder) -> None:
    found = [it.inputs for it in portal.of_type(HTTP_ROUTE) if it.inputs['metadata']['namespace'] == NAMESPACE]

    assert {parent['name'] for it in found for parent in it['spec']['parentRefs']} == {
        conventions.GATEWAY_INTERNET,
        conventions.GATEWAY_LAN,
    }
    for each in found:
        (rule,) = each['spec']['rules']
        assert each['spec']['hostnames'] == [PORTAL_HOST]
        assert [it['type'] for it in rule['filters']] == ['RequestHeaderModifier']
        assert rule['backendRefs'] == [{'name': PORTAL.service, 'port': PORTAL.port}]


def test_a_gated_portal_is_refused() -> None:
    with pytest.raises(ValueError, match='gated by itself'):
        _ = declare(exposure=Exposure(routes=(Route('auth', internet=Proxied(gate=Gate.SESSION), lan=REWRITE),)))


@pytest.mark.parametrize(
    'row', [Route('auth', internet=PROXIED), Route('auth', internet=PROXIED, media=REWRITE)], ids=['internet', 'media']
)
def test_a_portal_with_no_home_side_on_lan_is_refused(row: Route) -> None:
    """It declares that it keeps working while the uplink is down, which a portal reached through the uplink alone does not."""
    with pytest.raises(ValueError, match='no home side on `lan`'):
        _ = declare(exposure=Exposure(routes=(row,)))


def test_an_exposure_with_no_route_on_internet_is_refused() -> None:
    with pytest.raises(ValueError, match='one route on `internet`'):
        _ = declare(exposure=Exposure(routes=(Route('auth', lan=REWRITE),)))


# -- the pinned images -----------------------------------------------------------


@dataclass(frozen=True)
class Placed:
    """A file laid into the container before it starts: its bytes and mode, owned by root."""

    content: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class AsPod:
    """Who a container runs as: the user and group a pod's `securityContext` states."""

    user: int
    group: int

    @classmethod
    def of(cls, pod: Mapping[str, Any]) -> AsPod:
        context = pod['spec']['securityContext']
        return cls(int(context['runAsUser']), int(context['runAsGroup']))


def _create(
    name: str,
    image: str,
    who: AsPod,
    script: str,
    files: Mapping[str, Placed],
    *,
    env: Mapping[str, str] | None = None,
    tmpfs: tuple[str, ...] = ('/tmp',),
    network: str = 'none',
    kept: bool = False,
) -> None:
    """A container of the pinned `image`, created as `who` with every capability dropped on a read-only root, its files laid in.

    The files are laid in before the start, owned by root, through a stream
    `podman cp` reads, because the podman a case reaches may run where the
    case's files are not. A container is removed when it exits, unless it is
    `kept` for its logs to be read after it exits; the case's `removing`
    removes it either way.
    """
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for path, placed in files.items():
            member = tarfile.TarInfo(path.lstrip('/'))
            member.size, member.mode, member.uid, member.gid = len(placed.content), placed.mode, 0, 0
            tar.addfile(member, io.BytesIO(placed.content))
    _ = process_sessions.run(
        [
            'podman',
            'create',
            *(() if kept else ('--rm',)),
            f'--timeout={CONTAINER_TIMEOUT}',
            f'--label={LABEL}',
            f'--name={name}',
            f'--network={network}',
            '--read-only',
            '--cap-drop=all',
            '--security-opt=no-new-privileges',
            f'--user={who.user}:{who.group}',
            *(f'--tmpfs={path}:mode=1777' for path in tmpfs),
            *(f'--env={key}={value}' for key, value in (env or {}).items()),
            '--entrypoint=sh',
            pulled(pinned(image)),
            '-c',
            script,
        ],
        timeout=COMMAND_TIMEOUT,
        check=True,
    )
    if files:
        _ = process_sessions.run(
            ['podman', 'cp', '--archive=false', '-', f'{name}:/'],
            input=archive.getvalue(),
            timeout=COMMAND_TIMEOUT,
            check=True,
        )


def _name(role: str) -> str:
    return f'kluster-test-authelia-{role}-{uuid.uuid4().hex[:12]}'


def run_pinned(
    image: str,
    who: AsPod,
    script: str,
    files: Mapping[str, Placed],
    env: Mapping[str, str] | None = None,
    tmpfs: tuple[str, ...] = ('/tmp',),
) -> tuple[int, str]:
    """The status and output of `script` in a container of the pinned `image`, run as `who` on a read-only root."""
    require_podman('the configuration and the canary are run by the pinned images')
    name = _name(image)
    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _create(name, image, who, script, files, env=env, tmpfs=tmpfs)
        done = process_sessions.run(['podman', 'start', '--attach', name], timeout=COMMAND_TIMEOUT * 4)
    return done.returncode, (done.stdout + done.stderr).decode()


#: The one value every secret file the server reads holds here, long enough for
#: every key Authelia holds to a length, and the password Redis is started
#: with, which the server reads from its own file.
TEST_SECRET = 'a-test-value-long-enough-for-every-key-0123456789abcdef'

#: Writes a test value into every file the configuration and the server's
#: variables read, as the pod's Secrets would: a key pair for the provider, a
#: client identifier and a hashed secret for each client, and `TEST_SECRET`
#: for each of the server's own secrets. The configuration's `secret` paths are
#: rewritten to where the values are written, since the root is read-only, and
#: so is the Redis it dials, to `REDIS_AT`.
FILL = """set -eu
mkdir -p /tmp/config-secrets /tmp/secrets
cd /tmp/config-secrets
authelia crypto pair rsa generate --directory . >/dev/null
mv private.pem {jwks}
for key in {ids}; do printf %s "client-$key" > "$key"; done
for key in {secrets}; do
  authelia crypto hash generate pbkdf2 --variant sha512 --password "secret-$key" --no-confirm \\
    | sed 's/^Digest: //' | tr -d '\\n' > "$key"
done
for variable in {variables}; do
  file=/tmp/secrets/$variable
  printf %s "$TEST_SECRET" > "$file"
  export "$variable=$file"
done
sed -e 's#{directory}/#/tmp/config-secrets/#g' -e "s#{redis}#$REDIS_AT#" {configuration} > /tmp/configuration.yml
"""


def _server_script(monitor: Recorder, then: str) -> str:
    variables = env(container_of(monitor))
    keys = CONFIG_SECRETS.keys
    (directory,) = {path.rsplit('/', 1)[0] for path in SECRET.findall(raw_configuration(monitor))}
    return (
        FILL.format(
            jwks='OIDC_JWKS_ISSUER_PRIVATE_KEY',
            ids=' '.join(key for key in keys if key.endswith('_ID')),
            secrets=' '.join(key for key in keys if key.endswith('_SECRET')),
            variables=' '.join(name for name in variables if name.endswith('_FILE')),
            directory=directory,
            redis=configuration(monitor)['session']['redis']['host'],
            configuration=CONFIGURATION,
        )
        + then
    )


def _server_env(monitor: Recorder) -> dict[str, str]:
    variables = env(container_of(monitor))
    return {
        'X_AUTHELIA_CONFIG_FILTERS': variables['X_AUTHELIA_CONFIG_FILTERS']['value'],
        'TEST_SECRET': TEST_SECRET,
        'REDIS_AT': '127.0.0.1',
    }


def as_the_server(monitor: Recorder, then: str) -> tuple[int, str]:
    """`then`, run in the pinned Authelia as the server's pod runs, with the declared configuration and every secret it reads filled."""
    return run_pinned(
        'authelia',
        AsPod.of(pod_of(monitor)),
        _server_script(monitor, then),
        {CONFIGURATION: Placed(raw_configuration(monitor).encode())},
        env=_server_env(monitor),
    )


def test_the_pinned_authelia_loads_the_declared_configuration_without_an_error_or_a_warning(portal: Recorder) -> None:
    status, out = as_the_server(portal, 'exec authelia validate-config --config /tmp/configuration.yml')

    assert status == 0, out
    assert 'Configuration parsed and loaded successfully without errors.' in out, out


def policies(monitor: Recorder, checks: list[tuple[str, str]]) -> list[str]:
    """The policy the pinned Authelia applies to each `(url, flags)` check, by its own evaluation of the rules."""
    script = ''.join(
        f'authelia access-control check-policy --config /tmp/configuration.yml --url {url} {flags}\n'
        for url, flags in checks
    )
    status, out = as_the_server(monitor, script)
    assert status == 0, out
    found = re.findall(r"^The policy '(\w+)'", out, re.MULTILINE)
    assert len(found) == len(checks), out
    return found


def rule_urls() -> list[str]:
    """A URL every access rule matches: each of its names, and the canary's path on the portal's name."""
    urls = [f'https://{host}.{conventions.ZONE_PRIMARY}/' for rule in ACCESS_RULES for host in rule.hosts]
    return [*urls, f'https://{PORTAL_HOST}{CANARY_PATH}']


def test_a_signed_in_subject_with_no_groups_is_refused_wherever_a_rule_applies(portal: Recorder) -> None:
    """Groups are the portal's own record of a subject, so a subject in none is denied wherever a rule names a group."""
    found = policies(portal, [(url, '--username nobody-in-any-group') for url in rule_urls()])

    assert found == ['deny'] * len(found)


def test_no_rule_lets_a_request_through_without_a_sign_in(portal: Recorder) -> None:
    """A `bypass` would answer for no subject, and a client's own identity headers would then pass to the application."""
    found = policies(portal, [(url, '') for url in rule_urls()])

    assert 'bypass' not in found


def test_the_administrators_sign_in_to_the_canarys_path_and_to_nothing_on_the_portal_beside_it(
    portal: Recorder,
) -> None:
    found = policies(
        portal,
        [
            (f'https://{PORTAL_HOST}{CANARY_PATH}', '--username someone --groups admins'),
            (f'https://{PORTAL_HOST}/settings', '--username someone --groups admins'),
        ],
    )

    assert found == ['one_factor', 'deny']


def test_a_rule_on_a_user_admits_that_user_in_no_group_and_no_one_else(portal: Recorder) -> None:
    """`dav`'s rule names the user `webdav`, so a group of that name would not be what it admits."""
    dav = f'https://dav.{conventions.ZONE_PRIMARY}/'
    found = policies(portal, [(dav, '--username webdav'), (dav, '--username someone --groups webdav,users')])

    assert found == ['one_factor', 'deny']


# -- the served portal -----------------------------------------------------------
#
# The pinned server serves the declared configuration beside the pinned
# Redis, in one network namespace with nothing but its loopback, and a client
# of the pinned curl image asks it every request below in one run, each as the
# gateway's or the legacy check's proxy composes it. Four users are in its
# users file: `member` in `users`, `admin` in `admins`, and `nobody` and
# `webdav` in no group, the second the subject of the `dav` rule.

#: The password of every user of the served portal's users file.
USER_PASSWORD = 'the-password-of-every-test-user'

#: Writes the served portal's users file where the configuration reads it.
USERS = """hash=$(authelia crypto hash generate argon2 --password "$USER_PASSWORD" --no-confirm | sed 's/^Digest: //')
cat > {users} <<EOF
users:
  member: {{displayname: Member, email: member@example.com, password: '$hash', groups: [users]}}
  admin: {{displayname: Admin, email: admin@example.com, password: '$hash', groups: [admins]}}
  nobody: {{displayname: Nobody, email: nobody@example.com, password: '$hash', groups: []}}
  webdav: {{displayname: WebDAV, email: webdav@example.com, password: '$hash', groups: []}}
EOF
"""

#: The headers Envoy's ext_authz sends the portal beside the request's own
#: `Host` and path: the scheme and the client's address the filter forwards.
ENVOY_HEADERS = ('X-Forwarded-Proto: https', 'X-Forwarded-For: 192.0.2.1')

#: The headers Traefik's `forwardAuth` sends, its `Host` the check's address,
#: the portal's own name, and the request named in the forwarded headers.
TRAEFIK_HEADERS = (
    'X-Forwarded-Method: GET',
    'X-Forwarded-Proto: https',
    'X-Forwarded-Host: {host}',
    'X-Forwarded-Uri: /',
    'X-Forwarded-For: 192.0.2.1',
)

#: The addresses kluster-code's forward-auth checks ask at, under the portal's
#: name: the session check and the Basic check.
FORWARD_AUTH = '/api/authz/forward-auth'
FORWARD_AUTH_BASIC = '/api/authz/forward-auth-basic'


def _traefik(label: str, *extra: str) -> tuple[str, ...]:
    """Traefik's headers for a request to `label`'s front, and any the client sent of its own, which Traefik copies."""
    host = f'{label}.{conventions.ZONE_PRIMARY}'
    return (*(header.format(host=host) for header in TRAEFIK_HEADERS), *extra)


@dataclass(frozen=True)
class Ask:
    """One request of the client's run: its path, its headers, and the user whose session cookie it carries, if any."""

    path: str
    headers: tuple[str, ...]
    session: str | None = None
    basic: str | None = None


def _asks() -> dict[str, Ask]:
    session, basic = PORTAL.paths[Gate.SESSION], PORTAL.paths[Gate.BASIC]
    return {
        # Envoy asks at the form's path with the request's own path after it.
        'session-none': Ask(session + CANARY_PATH, ENVOY_HEADERS),
        'basic-none': Ask(basic + CANARY_PATH, ENVOY_HEADERS),
        'session-admin': Ask(session + CANARY_PATH, ENVOY_HEADERS, session='admin'),
        'session-forged-groups': Ask(session + CANARY_PATH, (*ENVOY_HEADERS, 'Remote-Groups: admins'), 'nobody'),
        'session-no-slash': Ask(session.rstrip('/') + CANARY_PATH, ENVOY_HEADERS),
        'forward-auth-member': Ask(FORWARD_AUTH, _traefik('spool'), session='member'),
        'forward-auth-nobody': Ask(FORWARD_AUTH, _traefik('spool'), session='nobody'),
        'forward-auth-basic-none': Ask(FORWARD_AUTH_BASIC, _traefik('dav')),
        'forward-auth-basic-webdav': Ask(FORWARD_AUTH_BASIC, _traefik('dav'), basic='webdav'),
        'forward-auth-basic-member': Ask(FORWARD_AUTH_BASIC, _traefik('dav'), basic='member'),
        # A request to `dav` that names, of the client's own, a name `member`
        # may reach.
        'forward-auth-basic-member-elsewhere': Ask(
            FORWARD_AUTH_BASIC,
            _traefik('dav', f'X-Original-URL: https://spool.{conventions.ZONE_PRIMARY}/'),
            basic='member',
        ),
    }


def _client_script() -> str:
    """The client's run: wait for the server, sign each user in, then ask each request and print its answer's head."""
    lines = [
        'set -u',
        'A=http://127.0.0.1:9091',
        'for i in $(seq 60); do curl -fsS -o /dev/null "$A/api/health" && break; sleep 0.5; done',
        'signin() {',
        f'  curl -sS -D - -o /dev/null -H "Host: {PORTAL_HOST}" -H "X-Forwarded-Proto: https" \\',
        '    -H "Content-Type: application/json" \\',
        '    --data "{\\"username\\":\\"$1\\",\\"password\\":\\"$USER_PASSWORD\\",\\"keepMeLoggedIn\\":false}" \\',
        '    "$A/api/firstfactor" | sed -n "s/^[Ss]et-[Cc]ookie: \\(authelia_session=[^;]*\\).*/\\1/p"',
        '}',
        *(f'COOKIE_{user}=$(signin {user})' for user in ('member', 'admin', 'nobody')),
    ]
    for case, ask in _asks().items():
        flags = [f'-H "Host: {PORTAL_HOST}"', *(f'-H "{header}"' for header in ask.headers)]
        if ask.session:
            flags.append(f'-H "Cookie: $COOKIE_{ask.session}"')
        if ask.basic:
            flags.append(f'-u "{ask.basic}:$USER_PASSWORD"')
        lines += [f'echo "== {case}"', f'curl -sS -o /dev/null -D - {" ".join(flags)} "$A{ask.path}"']
    return '\n'.join(lines) + '\n'


@dataclass(frozen=True)
class Answer:
    status: int
    headers: Mapping[str, str]


def _answers(out: str) -> dict[str, Answer]:
    found: dict[str, Answer] = {}
    for block in out.split('== ')[1:]:
        case, *lines = block.strip().splitlines()
        (status,) = [int(line.split()[1]) for line in lines if line.startswith('HTTP/')]
        headers = {
            key.strip().lower(): value.strip() for key, _, value in (line.partition(':') for line in lines[1:]) if value
        }
        found[case] = Answer(status, headers)
    return found


#: Holds the server's start until its Redis answers on its port, which
#: Authelia dials once as it starts, exiting where nothing answers. Any
#: reply to a `PING` is an answer, the `NOAUTH` refusal among them. The wait,
#: and the client's for the server's health, are stop-losses inside the case's
#: bound, so a Redis that does not answer ends the server's run with the line
#: below and fails the case naming it.
REDIS_WAIT = """timeout 15 sh -c 'until printf "PING\\r\\n" | nc -w 1 127.0.0.1 6379 | grep -q .; do sleep 0.2; done' \\
  || {{ echo "Redis did not answer on 127.0.0.1:6379"; exit 1; }}
"""


def _state(name: str) -> str:
    """A kept container's state and its own output, for a failure to name."""
    state = process_sessions.run(
        ['podman', 'inspect', '--format', '{{.State.Status}} {{.State.ExitCode}}', name], timeout=COMMAND_TIMEOUT
    )
    logs = process_sessions.run(['podman', 'logs', name], timeout=COMMAND_TIMEOUT)
    return f'{name}: {state.stdout.decode().strip()}\n{(logs.stdout + logs.stderr).decode()}'


@pytest.fixture(scope='module')
def served(portal: Recorder) -> Iterator[dict[str, Answer]]:
    """The served portal's answer to each request of `_asks`, by its case.

    Redis starts first and holds the network namespace the server and the
    client join, so the namespace stands whatever the server does. The server
    and Redis are kept after they exit, so a run that gets no answer fails
    naming their state and their own output.
    """
    require_podman('the configuration is served by the pinned Authelia')
    redis, server, client = _name('redis'), _name('server'), _name('client')
    users = configuration(portal)['authentication_backend']['file']['path']
    # The containers that join Redis's network are removed ahead of it.
    with process_sessions.removing(client, server, redis, timeout=COMMAND_TIMEOUT):
        _create(
            redis,
            'authelia-redis',
            AsPod.of(pod_of(portal, 'authelia-redis')),
            'exec redis-server --requirepass "$TEST_SECRET" --save "" --appendonly no',
            {},
            env={'TEST_SECRET': TEST_SECRET},
            tmpfs=('/data',),
            kept=True,
        )
        _ = process_sessions.run(['podman', 'start', redis], timeout=COMMAND_TIMEOUT, check=True)
        _create(
            server,
            'authelia',
            AsPod.of(pod_of(portal)),
            _server_script(
                portal,
                USERS.format(users=users) + REDIS_WAIT.format() + 'exec authelia --config /tmp/configuration.yml\n',
            ),
            {CONFIGURATION: Placed(raw_configuration(portal).encode())},
            env={**_server_env(portal), 'USER_PASSWORD': USER_PASSWORD},
            tmpfs=('/tmp', users.rsplit('/', 1)[0]),
            network=f'container:{redis}',
            kept=True,
        )
        _ = process_sessions.run(['podman', 'start', server], timeout=COMMAND_TIMEOUT, check=True)
        _create(
            client,
            'auth-canary',
            AsPod.of(canary_pod(portal)),
            _client_script(),
            {},
            env={'USER_PASSWORD': USER_PASSWORD},
            network=f'container:{redis}',
        )
        done = process_sessions.run(['podman', 'start', '--attach', client], timeout=COMMAND_TIMEOUT * 4)
        out = (done.stdout + done.stderr).decode()
        running = process_sessions.run(
            ['podman', 'inspect', '--format', '{{.State.Running}}', server, redis], timeout=COMMAND_TIMEOUT
        )
        assert running.stdout.decode().split() == ['true', 'true'], f'{out}\n{_state(server)}\n{_state(redis)}'
        answers = _answers(out)
        assert set(answers) == set(_asks()), f'{out}\n{_state(server)}\n{_state(redis)}'
        yield answers


def test_the_session_form_redirects_a_request_with_no_session_to_the_portal_naming_it(
    served: dict[str, Answer],
) -> None:
    """Envoy asks at the form's path with the request's own after it; the portal reads the request back out of it."""
    answer = served['session-none']

    assert answer.status == 302
    assert answer.headers['location'].startswith(f'https://{PORTAL_HOST}/?rd=')
    assert f'rd=https%3A%2F%2F{PORTAL_HOST}%2Fcanary' in answer.headers['location']


def test_the_basic_form_challenges_a_request_with_no_credential(served: dict[str, Answer]) -> None:
    assert served['basic-none'].status == 401


def test_the_session_form_passes_an_administrator_to_the_canarys_path_with_the_identity_headers(
    served: dict[str, Answer],
) -> None:
    answer = served['session-admin']

    assert answer.status == 200
    assert answer.headers['remote-user'] == 'admin'
    assert answer.headers['remote-groups'] == 'admins'
    assert set(PORTAL.identity_headers) <= set(answer.headers)


def test_a_subject_with_no_groups_who_sends_its_own_groups_is_refused(served: dict[str, Answer]) -> None:
    """The portal reads a subject's groups from its own record: `Remote-Groups` from the client is not read (Aetf/kluster-ops#78)."""
    answer = served['session-forged-groups']

    assert answer.status == 403
    assert 'remote-groups' not in answer.headers


def test_the_trailing_slash_of_a_form_path_is_what_keeps_the_requests_path(served: dict[str, Answer]) -> None:
    """Without it, the request's path runs into the endpoint's name and the portal authorizes no request at all."""
    assert all(path.endswith('/') for path in PORTAL.paths.values())
    assert served['session-no-slash'].status == 400


# -- the forward-auth layer ------------------------------------------------------


def test_the_legacy_session_check_passes_a_member_with_the_identity_headers(served: dict[str, Answer]) -> None:
    """The check asks as Traefik does: the portal's own `Host`, the request in the forwarded headers."""
    answer = served['forward-auth-member']

    assert answer.status == 200
    assert answer.headers['remote-user'] == 'member'
    assert answer.headers['remote-groups'] == 'users'
    assert set(PORTAL.identity_headers) <= set(answer.headers)


def test_the_legacy_session_check_refuses_a_subject_with_no_groups(served: dict[str, Answer]) -> None:
    assert served['forward-auth-nobody'].status == 403


def test_the_legacy_basic_check_challenges_a_request_with_no_credential(served: dict[str, Answer]) -> None:
    answer = served['forward-auth-basic-none']

    assert answer.status == 401
    assert answer.headers['www-authenticate'].startswith('Basic ')


def test_the_legacy_basic_check_passes_the_user_a_rule_names_and_refuses_another(served: dict[str, Answer]) -> None:
    """`dav` is `webdav`'s alone, a user in no group: a member of `users` with a good password is refused there."""
    assert served['forward-auth-basic-webdav'].status == 200
    assert served['forward-auth-basic-webdav'].headers['remote-user'] == 'webdav'
    assert served['forward-auth-basic-member'].status == 403


def test_the_legacy_basic_check_authorizes_the_request_the_proxy_names_not_one_the_client_names(
    served: dict[str, Answer],
) -> None:
    """A client's own `X-Original-URL`, which Traefik copies, naming a front the member may reach, changes nothing."""
    assert served['forward-auth-basic-member-elsewhere'].status == 403


# -- the canary's run ------------------------------------------------------------


#: A stand-in for `curl` laid ahead of the image's on `PATH`: it prints the
#: arguments it was called with, then answers `--write-out` as the gate would,
#: from `FAKE_ANSWER`, exiting with `FAKE_STATUS`.
FAKE_CURL = b"""#!/bin/sh
echo "called: $*" >&2
printf %s "$FAKE_ANSWER"
exit "$FAKE_STATUS"
"""


def canary_run(monitor: Recorder, answer: str, status: int = 0) -> tuple[int, str]:
    """The canary's run, in the pinned curl image as the CronJob's pod, against a gate that answers `answer`."""
    pod = canary_pod(monitor)
    (container,) = pod['spec']['containers']
    script_path = container['command'][-1]
    volume = volume_at(pod, container, script_path)
    script = config_map(monitor, volume['configMap']['name'])['data'][script_path.rsplit('/', 1)[1]]
    return run_pinned(
        'auth-canary',
        AsPod.of(pod),
        f'PATH=/fake:$PATH exec {" ".join(container["command"])}',
        {script_path: Placed(script.encode()), '/fake/curl': Placed(FAKE_CURL, 0o755)},
        env={
            **{name: it['value'] for name, it in env(container).items()},
            'FAKE_ANSWER': answer,
            'FAKE_STATUS': str(status),
        },
    )


@pytest.mark.parametrize(
    'answer',
    ['401 ', f'302 https://{PORTAL_HOST}/?rd=https%3A%2F%2F{PORTAL_HOST}%2Fcanary&rm=GET'],
    ids=['unauthorized', 'redirect-to-the-portal'],
)
def test_the_canary_passes_on_a_refusal(portal: Recorder, answer: str) -> None:
    status, out = canary_run(portal, answer)

    assert status == 0, out
    assert f'--resolve {PORTAL_HOST}:443:203.0.113.10' in out
    assert f'https://{PORTAL_HOST}{CANARY_PATH}' in out


@pytest.mark.parametrize(
    'answer',
    ['200 ', '404 ', '403 ', '500 ', '302 https://elsewhere.example/', f'302 https://{PORTAL_HOST}.elsewhere.example/'],
    ids=['served', 'not-found', 'forbidden', 'error', 'redirect-elsewhere', 'redirect-to-a-lookalike'],
)
def test_the_canary_fails_on_anything_but_a_refusal(portal: Recorder, answer: str) -> None:
    """The target's own answer is the gate failing open; a 403 or a 5xx is the gateway's, without the portal's answer."""
    status, out = canary_run(portal, answer)

    assert status == 1, out
    assert 'auth-canary: FAILED' in out


def test_the_canary_fails_when_nothing_answers(portal: Recorder) -> None:
    status, out = canary_run(portal, '000 ', status=7)

    assert status == 1, out
    assert 'auth-canary: FAILED — no answer' in out


@pytest.mark.parametrize(
    'image, pod, user', [('authelia-redis', 'authelia-redis', 'redis'), ('auth-canary', None, 'curl_user')]
)
def test_a_pod_runs_as_the_user_its_pinned_image_creates(
    portal: Recorder, image: str, pod: str | None, user: str
) -> None:
    """The numbers a pod states are the ones the image's own user has, which owns what the image wrote for it."""
    who = AsPod.of(pod_of(portal, pod) if pod else canary_pod(portal))

    status, out = run_pinned(image, who, f'id -u {user} && id -g {user}', {})

    assert status == 0, out
    assert out.split() == [str(who.user), str(who.group)]


# -- the receiving layer ---------------------------------------------------------


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def receiving() -> Recorder:
    """The portal declared while the land streams its claim in."""
    monitor = await run_with(Generating(), stack='apps')
    async with declaring():
        _ = declare(receiving=True)
    return monitor


def test_while_receiving_no_container_runs_the_server(portal: Recorder, receiving: Recorder) -> None:
    """No container of the receiving pod starts the server or reads its configuration."""
    server = container_of(portal)
    pod = pod_of(receiving)

    for container in pod['spec']['containers']:
        assert container.get('command') != server['command']
        assert all(mount['mountPath'] != CONFIGURATION for mount in container.get('volumeMounts', []))


def test_the_receiver_mounts_the_claim_where_the_server_reads_its_data(portal: Recorder, receiving: Recorder) -> None:
    users = configuration(portal)['authentication_backend']['file']['path']
    (receiver,) = pod_of(receiving)['spec']['containers']

    assert volume_at(pod_of(receiving), receiver, users) == volume_at(pod_of(portal), container_of(portal), users)


def test_the_receiver_runs_as_the_server_and_is_placed_as_the_server(portal: Recorder, receiving: Recorder) -> None:
    for key in ('securityContext', 'affinity', 'tolerations', 'priorityClassName'):
        assert pod_of(receiving)['spec'][key] == pod_of(portal)['spec'][key]


def test_the_pinned_server_image_extracts_the_lands_stream_as_the_servers_user(
    portal: Recorder, receiving: Recorder
) -> None:
    """The receiver's `tar` reads the stream the land pipes into it, which carries the claim's root, root's, as `./`."""
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        root = tarfile.TarInfo('.')
        root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o755, 0, 0
        tar.addfile(root)
        users = tarfile.TarInfo('./users.yaml')
        users.size, users.mode, users.uid, users.gid = 6, 0o600, 0, 0
        tar.addfile(users, io.BytesIO(b'users\n'))
    who = AsPod.of(pod_of(receiving))
    path = configuration(portal)['authentication_backend']['file']['path']

    status, out = run_pinned(
        'authelia',
        who,
        f'{" ".join(RECEIVE)} < /stream.tar && stat -c "%u %a" {path}',
        {'/stream.tar': Placed(archive.getvalue())},
        tmpfs=('/tmp', path.rsplit('/', 1)[0]),
    )

    assert status == 0, out
    assert out.split() == [str(who.user), '600']
