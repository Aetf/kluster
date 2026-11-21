"""hath, declared against mocks, and its client run from the pinned image as the pod runs it.

Every row here is the test's own: a raw port on TCP at the `Dedicated` front,
under a name and port no census row carries. So what is held is how the row a
component is handed decides what it declares, never what the census happens to
say. The pin in the declared cases and the ciphertexts are the test's own too.

The cases that read the image or run the client use the image `Pulumi.yaml`
pins, with podman, as the pod runs it: the declared user, group, environment
and command, with every capability dropped, a read-only root filesystem and no
network, and each directory the pod mounts a scratch filesystem. They run as
`pinned_image` has it: skipped where podman is not on `PATH`, failed on CI's
runner, which has podman, the image pulled by its digest, and every container
carrying `--rm`, a `--timeout` and the module's label (framework/testing.md
§1).

Literals written here are the upstream projects' contracts: the lines the
client logs as it reads its login, the `<id>-<key>` form of that login,
Kubernetes' Pod Security label and its field names, and LB IPAM's annotation.
"""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, PULL_TIMEOUT, pinned, pulled, require_podman

from kluster import conventions
from kluster.components.apps import LB_IPAM_IPS, WORLD_ENTITY, Fronts
from kluster.components.apps import hath as hath_app
from kluster.components.apps.hath import Hath, Stage
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.backup import BACKED_NAMESPACES
from kluster.conventions.cloud import NodeVolumeEntry
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import REWRITE, Balancer, Dedicated, Exposure, Host, Port, Route, SiteForward, Vip
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.versions import ImagePin

SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
DESTINATION = 'crds:volsync.backube/v1alpha1:ReplicationDestination'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'
SEALED = 'crds:bitnami.com/v1alpha1:SealedSecret'

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

#: The raw port, at the dedicated node's own address.
SERVE = Port('hh-serve', 60123, Transports.TCP, internet=Dedicated())
CLIENT = Exposure(ports=(SERVE,))

#: The two stages, each under a tag of the test's own.
WARM = Stage('a-warm-pass', final=False)
FINAL = Stage('a-final-pass', final=True)

IMAGE = ImagePin('registry.example.invalid/hath', 'v9.9.9', f'sha256:{"5" * 64}')
#: Ciphertext of each key of the ported Secret, which names the key.
CIPHERTEXT = {key: f'sealed-{key}' for key in hath_app.PORTED.keys}


def declare(**changed: Any) -> None:
    arguments: dict[str, Any] = {
        'exposure': CLIENT,
        'image': IMAGE,
        'login': CIPHERTEXT,
        'stage': FINAL,
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
    } | changed
    _ = Hath('hath', **arguments)


async def declared_with(**changed: Any) -> Recorder:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        declare(**changed)
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The client, started behind the final pass, as the `apps` program declares it."""
    return await declared_with()


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == hath_app.NAMESPACE]


def deployment_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment


def pod_of(monitor: Recorder) -> dict[str, Any]:
    return deployment_of(monitor)['spec']['template']


def client(monitor: Recorder) -> dict[str, Any]:
    (found,) = pod_of(monitor)['spec']['containers']
    return found


def environment(monitor: Recorder) -> dict[str, str]:
    return {it['name']: it['value'] for it in client(monitor).get('env', [])}


def mounts(monitor: Recorder) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Each mount of the client, by the path it is mounted at, with its volume."""
    volumes = {it['name']: it for it in pod_of(monitor)['spec']['volumes']}
    return {it['mountPath']: (it, volumes[it['name']]) for it in client(monitor)['volumeMounts']}


def cache_claim(monitor: Recorder) -> dict[str, Any]:
    (found,) = [it for it in in_namespace(monitor, CLAIM) if it['spec']['storageClassName'] != conventions.SC_NAS]
    return found


def legacy_claim(monitor: Recorder) -> dict[str, Any]:
    (found,) = [it for it in in_namespace(monitor, CLAIM) if it['spec']['storageClassName'] == conventions.SC_NAS]
    return found


def seed_source(monitor: Recorder) -> tuple[str, dict[str, Any]]:
    """The seed's source, by its logical name, with its inputs."""
    (found,) = [(it.name, it.inputs) for it in monitor.of_type(SOURCE)]
    return found


# -- the pod -------------------------------------------------------------------


def test_the_pod_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(declared: Recorder) -> None:
    """The image names no user, and `restricted`, which the namespace is held to, refuses a pod that does not."""
    (namespace,) = [it.inputs for it in declared.of_type(NAMESPACE)]
    spec = pod_of(declared)['spec']
    context = spec['securityContext']

    assert namespace['metadata']['name'] == hath_app.NAMESPACE
    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    for each in spec['containers']:
        assert each['securityContext']['allowPrivilegeEscalation'] is False
        assert each['securityContext']['capabilities'] == {'drop': ['ALL']}
        assert each['securityContext']['readOnlyRootFilesystem'] is True


def test_the_container_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    assert client(declared)['image'] == str(IMAGE)


def test_every_container_is_held_to_a_cpu_and_a_memory_limit(declared: Recorder) -> None:
    """It runs beside etcd, where a closed binary serving the public takes no more than its limit."""
    for each in pod_of(declared)['spec']['containers']:
        assert {'cpu', 'memory'} <= each['resources']['limits'].keys()


def test_there_is_never_a_second_client_under_the_login(declared: Recorder) -> None:
    """The network refuses two clients online under one login, so a rollout stops the old pod before the new one starts."""
    deployment = deployment_of(declared)

    assert deployment['spec']['replicas'] == 1
    assert deployment['spec']['strategy']['type'] == 'Recreate'


@pytest.mark.asyncio
async def test_before_the_final_pass_the_client_is_at_zero_and_nothing_waits_for_the_pass() -> None:
    """While the legacy client serves under the login, its own is scaled to zero, and the warm pass runs on unwatched."""
    monitor = await declared_with(stage=WARM)
    _, source = seed_source(monitor)

    assert deployment_of(monitor)['spec']['replicas'] == 0
    assert source['spec']['trigger'] == {'manual': WARM.tag}
    assert 'pulumi.com/waitFor' not in source['metadata'].get('annotations', {})


def test_the_final_pass_is_waited_for_and_the_client_starts_behind_it(declared: Recorder) -> None:
    """The client's update is issued once the source's, whose await is the pass, has finished, so it never starts on a partial cache."""
    name, source = seed_source(declared)

    assert deployment_of(declared)['spec']['replicas'] == 1
    assert source['spec']['trigger'] == {'manual': FINAL.tag}
    assert source['metadata']['annotations']['pulumi.com/waitFor'] == f'jsonpath={{.status.lastManualSync}}={FINAL.tag}'
    assert [it for it in declared.depends_on(hath_app.NAME, DEPLOYMENT) if it.endswith(f'{SOURCE}::{name}')]


# -- the cache -------------------------------------------------------------------


def test_the_cache_is_claimed_under_its_node_volumes_class_protected(declared: Recorder) -> None:
    """The class's allowed topology is the node carrying the volume, and the class retains a claim's data; the claim is protected too."""
    found = cache_claim(declared)
    logical = next(it.name for it in declared.of_type(CLAIM))

    assert found['spec']['storageClassName'] == conventions.node_volume_class(hath_app.CACHE_VOLUME)
    assert declared.options_of(logical, CLAIM).protect is True


def test_nothing_backs_the_cache_up_and_the_namespace_is_no_backed_one(declared: Recorder) -> None:
    """The cache's redundancy is the network's, so the claim takes `off`: no restic source, no repository, and no writer key to mint."""
    assert [it for it in in_namespace(declared, SOURCE) if 'restic' in it['spec']] == []
    assert [it['metadata']['name'] for it in in_namespace(declared, SEALED)] == [hath_app.PORTED.name]
    assert hath_app.NAMESPACE not in BACKED_NAMESPACES


def test_the_cache_is_seeded_from_the_legacy_clients_directory_read_only(declared: Recorder) -> None:
    """The seed's source reads the legacy cache on the NAS, and its destination writes the cache's claim, which the pod mounts."""
    _, source = seed_source(declared)
    (destination,) = in_namespace(declared, DESTINATION)
    (volume,) = [it.inputs for it in declared.of_type(VOLUME)]

    assert source['spec']['sourcePVC'] == legacy_claim(declared)['metadata']['name']
    assert destination['spec']['rsyncTLS']['destinationPVC'] == cache_claim(declared)['metadata']['name']
    assert volume['spec']['nfs']['path'] == f'{conventions.NAS_EXPORT}/{hath_app.LEGACY}'
    assert volume['spec']['nfs']['readOnly'] is True


def test_the_pod_mounts_no_nas_claim_so_it_states_no_home_site(declared: Recorder) -> None:
    """The legacy cache belongs to the seed's source alone; the client stays as free as its claim leaves it."""
    claims = {
        it['persistentVolumeClaim']['claimName']
        for it in pod_of(declared)['spec']['volumes']
        if 'persistentVolumeClaim' in it
    }

    assert legacy_claim(declared)['metadata']['name'] not in claims
    assert 'affinity' not in pod_of(declared)['spec']


def test_the_pod_selects_no_node_and_runs_at_the_held_priority(declared: Recorder) -> None:
    """Its claim's class places it on the volume's node, which is all that holds it there."""
    spec = pod_of(declared)['spec']

    assert 'nodeSelector' not in spec
    assert 'affinity' not in spec
    assert spec['priorityClassName'] == conventions.HELD_PRIORITY_CLASS


def test_every_directory_the_client_is_handed_is_a_directory_of_the_cache_claim(declared: Recorder) -> None:
    """The client's state sits beside its cache, so the claim holds all of what a move carries."""
    found = mounts(declared)

    for directory in hath_app.DIRECTORIES:
        mount, volume = found[f'{hath_app.ROOT}/{directory}']
        assert mount['subPath'] == directory
        assert volume['persistentVolumeClaim']['claimName'] == cache_claim(declared)['metadata']['name']


def test_a_cache_volume_off_the_dedicated_node_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client is reached at the dedicated node's address, and would run where its volume is."""
    monkeypatch.setattr(conventions, 'NODE_VOLUMES', {hath_app.CACHE_VOLUME: NodeVolumeEntry(node='cp2', size_gb=55)})

    with pytest.raises(ValueError, match=r'must follow the dedicated VIP \(`FOLLOWS_DEDICATED_VIP`\)'):
        declare()


# -- the login -------------------------------------------------------------------

#: The read bits of a file's mode: its group's, everyone's.
GROUP_READ, OTHER_READ = 0o040, 0o004


def test_the_login_is_the_ported_ciphertext_opened_in_the_namespace_it_was_sealed_to(declared: Recorder) -> None:
    (sealed,) = in_namespace(declared, SEALED)

    assert sealed['metadata']['name'] == hath_app.PORTED.name
    assert sealed['spec']['encryptedData'] == CIPHERTEXT
    assert sealed['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}


def test_the_login_is_the_one_file_the_client_reads_and_the_pod_can_read_it(declared: Recorder) -> None:
    """The kubelet writes a Secret's file as root, so the pod's user reads it by its group, with `fsGroup`, or as anyone."""
    mount, volume = mounts(declared)[hath_app.LOGIN]
    (sealed,) = in_namespace(declared, SEALED)
    context = pod_of(declared)['spec']['securityContext']
    mode = int(volume['secret'].get('defaultMode', 0o644))

    assert volume['secret']['secretName'] == sealed['metadata']['name']
    assert mount['subPath'] in sealed['spec']['encryptedData']
    assert mount['readOnly'] is True
    assert mode & OTHER_READ or (mode & GROUP_READ and context.get('fsGroup') is not None), oct(mode)


@pytest.mark.parametrize(
    'login',
    [
        pytest.param({}, id='the-key-missing'),
        pytest.param({**CIPHERTEXT, 'client_key': 'sealed-client_key'}, id='a-key-extra'),
    ],
)
def test_a_ciphertext_of_other_keys_is_refused(login: dict[str, str]) -> None:
    with pytest.raises(ValueError, match='the ciphertext handed holds'):
        declare(login=login)


# -- the raw port --------------------------------------------------------------


def test_the_client_listens_on_the_port_its_service_at_the_dedicated_address_forwards_to(declared: Recorder) -> None:
    """The port is the client's argument, and the one Service, at the dedicated address, and the world's allow select the pod."""
    labels = pod_of(declared)['metadata']['labels']
    (service,) = in_namespace(declared, SERVICE)
    (allow,) = [
        it
        for it in in_namespace(declared, POLICY)
        if any(WORLD_ENTITY in rule.get('fromEntities', []) for rule in it.get('spec', {}).get('ingress', []))
    ]

    assert environment(declared)['EXTRA_ARGS'].split() == [f'--port={SERVE.port}']
    assert {(it['containerPort'], it['protocol']) for it in client(declared)['ports']} == {(SERVE.port, 'TCP')}
    assert service['metadata']['annotations'][LB_IPAM_IPS] == FRONTS.internet.dedicated_vip
    assert service['spec']['selector'].items() <= labels.items()
    assert {(it['targetPort'], it['protocol']) for it in service['spec']['ports']} == {(SERVE.port, 'TCP')}
    assert allow['spec']['endpointSelector']['matchLabels'].items() <= labels.items()


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(Exposure(routes=(Route('hh-web', lan=REWRITE),), ports=(SERVE,)), id='a-route'),
        pytest.param(
            Exposure(ports=(SERVE, Port('hh-other', 60124, Transports.TCP, lan=Vip('hh-other')))), id='two-ports'
        ),
    ],
)
def test_an_exposure_other_than_one_raw_port_is_refused(exposure: Exposure) -> None:
    with pytest.raises(ValueError, match='one raw port alone'):
        declare(exposure=exposure)


@pytest.mark.parametrize(
    'internet',
    [
        pytest.param(Balancer((Host('hh-serve'),)), id='the-balancer'),
        pytest.param(SiteForward(), id='the-site-forward'),
    ],
)
def test_a_port_at_any_front_but_the_dedicated_one_is_refused(internet: Balancer | SiteForward) -> None:
    """The network tests the port at the address the client's calls leave from, which only the dedicated front is."""
    with pytest.raises(ValueError, match="the dedicated node's own"):
        declare(exposure=Exposure(ports=(Port('hh-serve', 60123, Transports.TCP, internet=internet),)))


@pytest.mark.parametrize('transports', [Transports.UDP, Transports.TCP_AND_UDP])
def test_a_port_on_another_transport_is_refused(transports: Transports) -> None:
    with pytest.raises(ValueError, match='over TCP alone'):
        declare(exposure=Exposure(ports=(Port('hh-serve', 60123, transports, internet=SERVE.internet),)))


# -- the client, as the pod runs it ----------------------------------------------

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_hath`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why the cases that read or run the pinned image need podman.
WHY = 'the pinned image is read and its client run with it'

#: The bound on a case that runs the client: the pull, the check before it, the
#: inspection, and the client's start, which with no network gives up within
#: seconds. A stop-loss, which nothing asserts on.
CLIENT_CASE_TIMEOUT = PULL_TIMEOUT + 4 * COMMAND_TIMEOUT

#: A stand-in login, in the `<id>-<key>` form the client reads.
STAND_IN_ID = '424242'
STAND_IN_LOGIN = f'{STAND_IN_ID}-abcdefghijklmnopqrst'


def image_config(reference: str) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(
        process_sessions.run(
            ['podman', 'image', 'inspect', '--format', '{{json .Config}}', reference],
            timeout=COMMAND_TIMEOUT,
            check=True,
        ).stdout
    )
    return found


@pytest.mark.timeout(PULL_TIMEOUT + 2 * COMMAND_TIMEOUT)
def test_each_volume_of_the_image_is_a_directory_the_pod_mounts_from_the_claim(declared: Recorder) -> None:
    """A volume of the image that no mount covers would hide what the claim holds at that path."""
    require_podman(WHY)
    volumes = image_config(pulled(pinned('hath')))['Volumes']
    found = mounts(declared)
    claim_name = cache_claim(declared)['metadata']['name']

    assert volumes
    for path in volumes:
        assert path in found, path
        assert found[path][1]['persistentVolumeClaim']['claimName'] == claim_name


@pytest.mark.timeout(CLIENT_CASE_TIMEOUT)
def test_the_client_run_as_the_pod_runs_it_reads_the_login_where_the_pod_mounts_it(declared: Recorder) -> None:
    """Run as the pod's user, with its environment, its read-only root and its mounts, the client signs in with the login at `LOGIN`.

    The case has no network, so the client stops when it cannot reach the
    network's server; by then it has read the login and named its ID.
    """
    require_podman(WHY)
    reference = pulled(pinned('hath'))
    config = image_config(reference)
    entrypoint: list[str] = config.get('Entrypoint') or []
    cmd: list[str] = config['Cmd']
    command = [*entrypoint, *cmd]
    context = pod_of(declared)['spec']['securityContext']
    login = PurePosixPath(hath_app.LOGIN)
    # The login's directory is a mount of its own, so the scratch filesystem
    # at it holds the stand-in the way the Secret's file sits in the claim's
    # directory.
    scratch = [path for path in mounts(declared) if path != hath_app.LOGIN]
    assert str(login.parent) in scratch
    name = f'kluster-test-hath-{uuid.uuid4().hex[:12]}'
    script = f'printf %s "$LOGIN" >{shlex.quote(str(login))} && exec "$@"'

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        ran = process_sessions.run(
            [
                'podman',
                'run',
                '--rm',
                f'--timeout={CONTAINER_TIMEOUT}',
                f'--label={LABEL}',
                process_sessions.LOG_TO_FILE,
                f'--name={name}',
                '--network=none',
                '--read-only',
                # The pod's read-only root has no writable tmpfs beside it.
                '--read-only-tmpfs=false',
                '--cap-drop=all',
                '--security-opt=no-new-privileges',
                f'--user={int(context["runAsUser"])}:{int(context["runAsGroup"])}',
                *(f'--mount=type=tmpfs,destination={path},tmpfs-mode=1777' for path in scratch),
                *(f'--env={key}={value}' for key, value in environment(declared).items()),
                f'--env=LOGIN={STAND_IN_LOGIN}',
                '--entrypoint=/bin/sh',
                reference,
                '-c',
                script,
                'sh',
                *command,
            ],
            timeout=COMMAND_TIMEOUT,
            text=True,
        )

    output = ran.stdout + ran.stderr
    assert 'Loaded login settings from client_login' in output, output
    assert f'client with ID {STAND_IN_ID}' in output, output
