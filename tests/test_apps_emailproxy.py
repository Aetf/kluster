"""emailproxy, declared against mocks, and the configuration its ported Secret renders.

Every row here is the test's own: a raw port dialed at a name at the LAN VIP,
under a name and port no census row carries. So what is held is how the row a
component is handed decides what it declares, never what the census happens to
say. The pin and the ciphertext are the test's own too.

The paths the image reads are read from `docker/emailproxy.Containerfile`,
which is what builds the pinned image, and the configuration is read with
Python's `configparser` without interpolation, which is how the proxy reads
it. Literals
written here are the upstream projects' contracts: Kubernetes' Pod Security
label, the sealed-secrets controller's `index` action and its scope
annotation, cert-manager's kinds, and the proxy's section and option names.
"""

from __future__ import annotations

import configparser
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import emailproxy as emailproxy_app
from kluster.components.apps.emailproxy import EmailProxy
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions import CONTROL_PLANE_LABEL
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import REWRITE, Dedicated, Exposure, Port, Route, Vip
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.versions import ImagePin

SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'
SEALED = 'crds:bitnami.com/v1alpha1:SealedSecret'
ISSUER = 'crds:cert-manager.io/v1:Issuer'
CERTIFICATE = 'crds:cert-manager.io/v1:Certificate'

ROOT = Path(__file__).parent.parent

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

#: The raw port, dialed at one name at the LAN VIP.
MAIL = Port('ep-mail', 1994, Transports.TCP, lan=Vip('ep-mail'))
PROXY = Exposure(ports=(MAIL,))

IMAGE = ImagePin('registry.example.invalid/emailproxy', 'v9.9.9', f'sha256:{"4" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'emailproxy-id', 'AWS_SECRET_ACCESS_KEY': 'emailproxy-secret'}
#: Ciphertext of each key of the ported Secret, which names the key.
CIPHERTEXT = {key: f'sealed-{key}' for key in emailproxy_app.PORTED.keys}

#: The controller's action for a decrypted key, the only one the file uses.
INDEX = re.compile(r'\{\{\s*index\s+\.\s+"(?P<key>[^"]+)"\s*\}\}')


def declare(**changed: Any) -> None:
    arguments: dict[str, Any] = {
        'exposure': PROXY,
        'image': IMAGE,
        'config': CIPHERTEXT,
        'writer_key': WRITER_KEY,
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
    } | changed
    _ = EmailProxy('emailproxy', **arguments)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The proxy, as the `apps` program declares it."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        declare()
    return monitor


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [
        it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == emailproxy_app.NAMESPACE
    ]


def pod_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment['spec']['template']


def serving(monitor: Recorder) -> dict[str, Any]:
    (found,) = pod_of(monitor)['spec']['containers']
    return found


def mounted(monitor: Recorder) -> dict[str, dict[str, Any]]:
    """Each volume the container mounts, by the path it is mounted at."""
    volumes = {it['name']: it for it in pod_of(monitor)['spec']['volumes']}
    return {it['mountPath']: volumes[it['name']] for it in serving(monitor)['volumeMounts']}


def file_at(monitor: Recorder, path: str) -> tuple[dict[str, Any], str]:
    """The volume a file of the container is in, and the file's name in it, refused unless the volume holds it."""
    file = PurePosixPath(path)
    volume = mounted(monitor)[str(file.parent)]
    if 'secret' in volume:
        held = {it['path']: it['key'] for it in volume['secret'].get('items', [])}
        assert file.name in held, f'{path} is not among the files of its Secret'
        return volume, held[file.name]
    return volume, file.name


def entrypoint_flag(name: str) -> str:
    """The value the pinned image's entrypoint gives `--<name>`, as its Containerfile builds it."""
    text = (ROOT / 'docker' / 'emailproxy.Containerfile').read_text()
    (found,) = re.findall(r'^ENTRYPOINT\s+(\[.*?\])\s*$', text.replace('\\\n', ' '), re.M)
    arguments: list[str] = json.loads(found)
    return arguments[arguments.index(f'--{name}') + 1]


def sealed_config(monitor: Recorder) -> dict[str, Any]:
    """The SealedSecret of the ported configuration: the namespace's other one is the claim's repository's."""
    (sealed,) = [it for it in in_namespace(monitor, SEALED) if set(it['spec']['encryptedData']) != set(WRITER_KEY)]
    return sealed


def rendered_config(monitor: Recorder) -> configparser.ConfigParser:
    """The configuration file as the controller renders it, each `index` replaced by a stand-in for its key's value."""
    _, key = file_at(monitor, entrypoint_flag('config-file'))
    template = sealed_config(monitor)['spec']['template']['data'][key]
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(INDEX.sub(lambda found: f'decrypted-{found["key"]}', template))
    return parser


# -- the pod -------------------------------------------------------------------


def test_the_pod_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(declared: Recorder) -> None:
    """The image names no user, and `restricted`, which the namespace is held to, refuses a pod that does not."""
    (namespace,) = [it.inputs for it in declared.of_type(NAMESPACE)]
    spec = pod_of(declared)['spec']
    context = spec['securityContext']

    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    for each in spec['containers']:
        assert each['securityContext']['allowPrivilegeEscalation'] is False
        assert each['securityContext']['capabilities'] == {'drop': ['ALL']}


def test_the_container_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    assert serving(declared)['image'] == str(IMAGE)


def test_the_pod_runs_on_the_worker(declared: Recorder) -> None:
    """Its clients are at home, and a cloud node's resources are the control plane's and the internet pool's."""
    affinity = pod_of(declared)['spec']['affinity']['nodeAffinity']
    (term,) = affinity['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']

    assert {'key': CONTROL_PLANE_LABEL, 'operator': 'DoesNotExist'} in term['matchExpressions']


def test_the_cache_never_has_two_writers(declared: Recorder) -> None:
    (deployment,) = in_namespace(declared, DEPLOYMENT)
    strategy = deployment['spec']['strategy']

    assert strategy['type'] == 'Recreate' or strategy.get('rollingUpdate', {}).get('maxSurge') in (0, '0', '0%')


#: The read bits of a file's mode: its owner's, its group's, everyone's.
GROUP_READ, OTHER_READ = 0o040, 0o004


@pytest.mark.parametrize('which', ['config', 'certificate', 'key'])
def test_each_file_the_proxy_reads_is_readable_by_the_pod(declared: Recorder, which: str) -> None:
    """The kubelet writes a Secret's files as root, so the pod's user reads one by its group, with `fsGroup`, or as anyone.

    Unread, the configuration is empty, and the proxy finds no server in it and
    exits; the certificate and the key are what it serves TLS with.
    """
    section = rendered_config(declared)[f'IMAP-{MAIL.port}']
    path = {
        'config': entrypoint_flag('config-file'),
        'certificate': section['local_certificate_path'],
        'key': section['local_key_path'],
    }[which]
    volume, key = file_at(declared, path)
    context = pod_of(declared)['spec']['securityContext']
    modes = {it['key']: it.get('mode') for it in volume['secret']['items']}
    mode = int(modes[key] if modes[key] is not None else volume['secret'].get('defaultMode', 0o644))

    assert mode & OTHER_READ or (mode & GROUP_READ and context.get('fsGroup') is not None), oct(mode)


# -- the raw port --------------------------------------------------------------


def test_the_ports_service_and_allow_reach_the_proxy_on_the_port_it_listens_on(declared: Recorder) -> None:
    """The LAN VIP's Service and the world's allow select the pods, which listen on the row's port, as the configuration says."""
    pod = pod_of(declared)
    labels = pod['metadata']['labels']
    listening = {(it['containerPort'], it['protocol']) for it in serving(declared)['ports']}
    (service,) = in_namespace(declared, SERVICE)
    (allow,) = [it for it in in_namespace(declared, POLICY) if it['metadata']['name'] == f'{MAIL.name}-world']

    assert service['spec']['type'] == 'LoadBalancer'
    assert service['spec']['selector'].items() <= labels.items()
    assert {(it['targetPort'], it['protocol']) for it in service['spec']['ports']} == {(MAIL.port, 'TCP')}
    assert (MAIL.port, 'TCP') in listening
    assert allow['spec']['endpointSelector']['matchLabels'].items() <= labels.items()
    assert f'IMAP-{MAIL.port}' in rendered_config(declared)


# -- the configuration ---------------------------------------------------------


def test_the_configuration_is_the_ported_ciphertext_opened_in_the_namespace_it_was_sealed_to(
    declared: Recorder,
) -> None:
    """Each key as the program read it, sealed namespace-wide, in the legacy proxy's namespace and under the Secret's name."""
    sealed = sealed_config(declared)

    assert sealed['metadata']['name'] == emailproxy_app.PORTED.name
    assert sealed['metadata']['namespace'] == emailproxy_app.NAMESPACE
    assert sealed['spec']['encryptedData'] == CIPHERTEXT
    assert sealed['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}


def test_the_configuration_indexes_each_sealed_key_and_nothing_else(declared: Recorder) -> None:
    """An action naming a key the Secret does not seal renders empty, silently, where the controller finds no value."""
    template = ''.join(sealed_config(declared)['spec']['template']['data'].values())

    assert {found['key'] for found in INDEX.finditer(template)} == set(emailproxy_app.PORTED.keys)


def test_the_proxy_reads_its_configuration_where_the_image_looks_and_keeps_its_cache_in_the_claim(
    declared: Recorder,
) -> None:
    """The entrypoint names both paths; the file is the sealed configuration's, and the cache's directory is the claim."""
    config_volume, _ = file_at(declared, entrypoint_flag('config-file'))
    cache_volume, _ = file_at(declared, entrypoint_flag('cache-store'))
    (claim,) = in_namespace(declared, CLAIM)

    assert config_volume['secret']['secretName'] == sealed_config(declared)['metadata']['name']
    assert cache_volume['persistentVolumeClaim']['claimName'] == claim['metadata']['name']


def test_only_the_rendered_file_of_the_secret_reaches_the_container(declared: Recorder) -> None:
    """The sealed keys are in the rendered file already, and on their own they would be files beside it."""
    config_volume, key = file_at(declared, entrypoint_flag('config-file'))

    assert [it['key'] for it in config_volume['secret']['items']] == [key]


def test_the_proxy_serves_tls_on_the_certificate_issued_for_each_name_it_is_dialed_at(declared: Recorder) -> None:
    """Its section names the certificate and key where the certificate's Secret is mounted, from the namespace's issuer."""
    section = rendered_config(declared)[f'IMAP-{MAIL.port}']
    (certificate,) = in_namespace(declared, CERTIFICATE)
    (issuer,) = in_namespace(declared, ISSUER)
    certificate_volume, certificate_file = file_at(declared, section['local_certificate_path'])
    key_volume, key_file = file_at(declared, section['local_key_path'])

    assert certificate['spec']['dnsNames'] == [f'ep-mail.{conventions.ZONE_PRIMARY}']
    assert certificate['spec']['issuerRef']['kind'] == 'Issuer'
    assert certificate['spec']['issuerRef']['name'] == issuer['metadata']['name']
    assert 'selfSigned' in issuer['spec']
    for volume in (certificate_volume, key_volume):
        assert volume['secret']['secretName'] == certificate['spec']['secretName']
    assert (certificate_file, key_file) == ('tls.crt', 'tls.key')


def test_the_account_is_the_sealed_one_signed_in_with_the_sealed_client(declared: Recorder) -> None:
    """The account's section and its OAuth client are the decrypted values, and its redirect is the port the pod serves it on."""
    parsed = rendered_config(declared)
    section = parsed['decrypted-account']
    redirect = section['redirect_uri']

    assert (section['client_id'], section['client_secret']) == ('decrypted-client_id', 'decrypted-client_secret')
    assert redirect.startswith('http://localhost:')
    assert int(redirect.rsplit(':', 1)[1]) in {it['containerPort'] for it in serving(declared)['ports']}


# -- the claim -------------------------------------------------------------------


def test_the_namespace_is_one_whose_writer_key_is_sealed() -> None:
    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[emailproxy_app.NAMESPACE]


def test_the_cache_is_backed_up_and_read_by_a_mover_as_the_pod(declared: Recorder) -> None:
    pod = pod_of(declared)
    (claim,) = in_namespace(declared, CLAIM)
    (source,) = in_namespace(declared, SOURCE)

    assert source['spec']['sourcePVC'] == claim['metadata']['name']
    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']


# -- refusals --------------------------------------------------------------------


@pytest.mark.parametrize(
    'config',
    [
        pytest.param({key: CIPHERTEXT[key] for key in emailproxy_app.PORTED.keys[1:]}, id='a-key-missing'),
        pytest.param({**CIPHERTEXT, 'password': 'sealed-password'}, id='a-key-extra'),
    ],
)
def test_a_ciphertext_of_other_keys_is_refused(config: dict[str, str]) -> None:
    """The rendered file indexes the ported keys, and one the ciphertext lacks would render empty."""
    with pytest.raises(ValueError, match='the ciphertext handed holds'):
        declare(config=config)


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(Exposure(routes=(Route('ep-web', lan=REWRITE),), ports=(MAIL,)), id='a-route'),
        pytest.param(
            Exposure(ports=(MAIL, Port('ep-other', 1995, Transports.TCP, lan=Vip('ep-other')))), id='two-ports'
        ),
    ],
)
def test_an_exposure_other_than_one_raw_port_is_refused(exposure: Exposure) -> None:
    with pytest.raises(ValueError, match='one raw port alone'):
        declare(exposure=exposure)


def test_a_port_dialed_at_no_name_is_refused() -> None:
    """The certificate the proxy serves names the port's names, and a dedicated front gives it none."""
    with pytest.raises(ValueError, match='dialed at no name'):
        declare(exposure=Exposure(ports=(Port('ep-mail', 1994, Transports.TCP, internet=Dedicated()),)))


@pytest.mark.parametrize('transports', [Transports.UDP, Transports.TCP_AND_UDP])
def test_a_port_on_another_transport_is_refused(transports: Transports) -> None:
    """IMAP is carried on TCP, and a Service and an allow on UDP would stand in front of no listener."""
    with pytest.raises(ValueError, match='over TCP alone'):
        declare(exposure=Exposure(ports=(Port('ep-mail', 1994, transports, lan=Vip('ep-mail')),)))
