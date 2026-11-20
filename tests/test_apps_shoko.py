"""shoko, declared against mocks, and its pod run as declared from the pinned image.

Every row here is the test's own, under a name no census row carries, and so
are the pin and the writer key: what is held is how the component composes
what it is handed, never what the census happens to say.

The case that starts the server runs the image `Pulumi.yaml` pins, with podman,
as the pod runs it: the declared command, user, group and environment, with
every capability dropped, on a read-only root that podman adds no scratch
filesystem and no password-file entry to, as the kubelet adds none, with a
scratch filesystem at each writable mount and at `/dev/shm`, and no network. It runs as `pinned_image` has it: skipped
where podman is not on `PATH`, as in a cloud session, and failed on CI's
runner, which has podman; the image pulled by its digest; and every container
carrying `--rm`, a `--timeout` and the module's label (framework/testing.md
§1).

Literals written here are the upstream projects' contracts: Kubernetes' Pod
Security label, the Gateway API's spellings, and the file Shoko Server keeps
its settings in.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, PULL_TIMEOUT, pinned, pulled, require_podman

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import shoko as shoko_app
from kluster.components.apps.shoko import Shoko
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    PROXIED,
    REWRITE,
    Direct,
    Exposure,
    Gate,
    Held,
    Passthrough,
    Port,
    Proxied,
    Route,
    Vip,
)
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.versions import ImagePin

HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'

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

#: A home route, as shoko is reached.
LIBRARY = Exposure(routes=(Route('sk-library', lan=REWRITE),))
#: The same, reached from outside too, behind the portal's session.
GATED = Exposure(routes=(Route('sk-library', internet=Proxied(Gate.SESSION), lan=REWRITE),))

IMAGE = ImagePin('registry.example.invalid/shoko', 'v9.9.9', f'sha256:{"5" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'shoko-id', 'AWS_SECRET_ACCESS_KEY': 'shoko-secret'}

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_shoko`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why the case that starts the server needs podman.
WHY = 'the pod is run from the pinned image with it'

#: The file the server keeps its settings in, in its application directory.
SETTINGS = 'settings-server.json'

#: The bound on the case that starts the server: the pull, and the server's
#: start, which takes seconds. A stop-loss, which nothing asserts on.
SERVER_CASE_TIMEOUT = PULL_TIMEOUT + 2 * COMMAND_TIMEOUT + 60


async def _declared(exposure: Exposure) -> Recorder:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Shoko(
            'shoko',
            exposure=exposure,
            image=IMAGE,
            writer_key=WRITER_KEY,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """shoko on its home route, as the `apps` program declares it."""
    return await _declared(LIBRARY)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def gated() -> Recorder:
    """shoko reached from outside behind the portal's session, and at home."""
    return await _declared(GATED)


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == shoko_app.NAME]


def pod_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment['spec']['template']


def serving(monitor: Recorder) -> dict[str, Any]:
    (found,) = pod_of(monitor)['spec']['containers']
    return found


def volume_claims(monitor: Recorder) -> dict[str, str]:
    """The claim each of the pod's volumes names, by volume."""
    return {
        it['name']: it['persistentVolumeClaim']['claimName']
        for it in pod_of(monitor)['spec']['volumes']
        if 'persistentVolumeClaim' in it
    }


def mounted(monitor: Recorder, claim_name: str) -> dict[str, Any]:
    """The container's one mount of the volume over `claim_name`."""
    by_volume = volume_claims(monitor)
    (mount,) = [it for it in serving(monitor)['volumeMounts'] if by_volume.get(it['name']) == claim_name]
    return mount


def configuration_mount(monitor: Recorder) -> str:
    """Where the container mounts the claim the restic source backs up."""
    (source,) = in_namespace(monitor, SOURCE)
    return mounted(monitor, source['spec']['sourcePVC'])['mountPath']


def plain_env(monitor: Recorder) -> dict[str, str]:
    """The container's variables that carry a value of their own, by name."""
    return {it['name']: it['value'] for it in serving(monitor)['env'] if 'value' in it}


# -- the pod -------------------------------------------------------------------


def test_the_pod_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(declared: Recorder) -> None:
    """The image's entrypoint needs root, and `restricted`, which the namespace is held to, refuses a pod that runs as it."""
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
        assert each['securityContext']['readOnlyRootFilesystem'] is True


def test_the_container_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    assert serving(declared)['image'] == str(IMAGE)


def test_each_probe_asks_a_port_the_container_listens_on(declared: Recorder) -> None:
    container = serving(declared)
    named = {it['name'] for it in container['ports']}

    for probe in ('readinessProbe', 'livenessProbe'):
        assert container[probe]['httpGet']['port'] in named, probe


def test_the_pod_runs_on_the_worker(declared: Recorder) -> None:
    """The worker is the one node that reaches the export the library is on."""
    affinity = pod_of(declared)['spec']['affinity']['nodeAffinity']
    (term,) = affinity['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']

    assert {'key': CONTROL_PLANE_LABEL, 'operator': 'DoesNotExist'} in term['matchExpressions']


def test_the_database_never_has_two_writers(declared: Recorder) -> None:
    """The SQLite database is one process's, so a rollout stops the old pod before it starts the new one."""
    (deployment,) = in_namespace(declared, DEPLOYMENT)
    strategy = deployment['spec']['strategy']

    assert strategy['type'] == 'Recreate' or strategy.get('rollingUpdate', {}).get('maxSurge') in (0, '0', '0%')


# -- the exposure ----------------------------------------------------------------


def test_the_route_reaches_the_pods_at_home_ungated(declared: Recorder) -> None:
    """The row's one Gateway, `lan-gw`, forwards the name to the port the pods listen on, with no gate."""
    (route,) = in_namespace(declared, HTTP_ROUTE)
    (service,) = in_namespace(declared, SERVICE)
    pod = pod_of(declared)
    named = {it['name']: it['containerPort'] for it in serving(declared)['ports']}
    (port,) = service['spec']['ports']
    (rule,) = route['spec']['rules']

    assert [ref['name'] for ref in route['spec']['parentRefs']] == [conventions.GATEWAY_LAN]
    assert route['spec']['hostnames'] == [f'sk-library.{conventions.ZONE_PRIMARY}']
    assert [it for it in rule['filters'] if it['type'] == 'ExternalAuth'] == []
    assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
    assert service['spec']['selector'].items() <= pod['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port']


def test_a_gated_internet_side_reaches_the_interface_behind_the_portal(gated: Recorder) -> None:
    """From outside, only a signed-in user reaches the interface."""
    (route,) = [
        it
        for it in in_namespace(gated, HTTP_ROUTE)
        if [ref['name'] for ref in it['spec']['parentRefs']] == [conventions.GATEWAY_INTERNET]
    ]
    (rule,) = route['spec']['rules']

    assert [it['type'] for it in rule['filters'] if it['type'] == 'ExternalAuth'] == ['ExternalAuth']


# -- the claims ------------------------------------------------------------------


def test_the_namespace_is_one_whose_writer_key_is_sealed() -> None:
    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[shoko_app.NAME]


def test_the_configuration_is_the_backed_up_claim_under_the_servers_home(declared: Recorder) -> None:
    """The server keeps its configuration in `.shoko` under its home, and a mover reads it as the user that wrote it."""
    pod = pod_of(declared)
    (source,) = in_namespace(declared, SOURCE)

    assert configuration_mount(declared) == f'{plain_env(declared)["HOME"]}/.shoko'
    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']


def test_the_library_is_the_exports_media_mounted_read_only(declared: Recorder) -> None:
    """The library is the NAS's, which the server reads and never writes."""
    (volume,) = [it.inputs for it in declared.of_type(VOLUME)]
    (library,) = [
        it for it in in_namespace(declared, CLAIM) if it['metadata']['name'] == volume['spec']['claimRef']['name']
    ]
    (pod_volume,) = [
        it
        for it in pod_of(declared)['spec']['volumes']
        if it.get('persistentVolumeClaim', {}).get('claimName') == library['metadata']['name']
    ]

    assert volume['spec']['nfs']['path'] == f'{conventions.NAS_EXPORT}/Media'
    assert volume['spec']['nfs']['readOnly'] is True
    assert volume['spec']['claimRef'] == {'namespace': shoko_app.NAME, 'name': library['metadata']['name']}
    assert pod_volume['persistentVolumeClaim']['readOnly'] is True
    assert mounted(declared, library['metadata']['name'])['readOnly'] is True


# -- the server, as the pod runs it -------------------------------------------------


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_the_server_answers_its_probe_and_keeps_its_settings_in_the_claim(declared: Recorder) -> None:
    """Run as the pod runs it, the server answers its readiness probe and writes its settings under the claim's mount.

    The root is read-only, so a server that looked for its configuration
    anywhere but the claim's mount would not start.
    """
    require_podman(WHY)
    reference = pulled(pinned('shoko'))
    pod = pod_of(declared)
    container = serving(declared)
    context = pod['spec']['securityContext']
    probe = container['readinessProbe']['httpGet']
    (port,) = [it['containerPort'] for it in container['ports'] if it['name'] == probe['port']]
    configuration = configuration_mount(declared)
    writable = [it['mountPath'] for it in container['volumeMounts'] if not it.get('readOnly')]
    read_only = [it['mountPath'] for it in container['volumeMounts'] if it.get('readOnly')]
    name = f'kluster-test-shoko-{uuid.uuid4().hex[:12]}'

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        _ = process_sessions.run(
            [
                'podman',
                'run',
                '--detach',
                '--rm',
                f'--timeout={CONTAINER_TIMEOUT}',
                f'--label={LABEL}',
                f'--name={name}',
                '--network=none',
                '--read-only',
                # The kubelet adds no scratch filesystem to a read-only root
                # but `/dev/shm`, and no entry to the image's password file.
                '--read-only-tmpfs=false',
                '--passwd=false',
                '--mount=type=tmpfs,destination=/dev/shm,tmpfs-mode=1777',
                '--cap-drop=all',
                '--security-opt=no-new-privileges',
                f'--user={int(context["runAsUser"])}:{int(context["runAsGroup"])}',
                *(f'--mount=type=tmpfs,destination={path},tmpfs-mode=1777' for path in writable),
                *(f'--mount=type=tmpfs,destination={path},ro=true' for path in read_only),
                *(f'--env={key}={value}' for key, value in plain_env(declared).items()),
                f'--entrypoint={container["command"][0]}',
                reference,
                *container['command'][1:],
            ],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )
        url = f'http://127.0.0.1:{int(port)}{probe["path"]}'
        answered = process_sessions.run(
            [
                'podman',
                'exec',
                name,
                'sh',
                '-c',
                f'until code=$(curl -s -o /dev/null -w "%{{http_code}}" {url}) && [ "$code" != 000 ]; do sleep 0.2; done; '
                f'echo "$code"; find {configuration} -name {SETTINGS}',
            ],
            timeout=COMMAND_TIMEOUT + 30,
            check=True,
        )

    code, *settings = answered.stdout.decode().split()
    assert code == '200'
    assert settings == [f'{configuration}/Shoko.CLI/{SETTINGS}']


# -- refusals --------------------------------------------------------------------


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(Exposure(routes=(Route('sk-a', lan=REWRITE), Route('sk-b', lan=REWRITE))), id='two-routes'),
        pytest.param(
            Exposure(routes=LIBRARY.routes, ports=(Port('sk-p', 1, Transports.TCP, lan=Vip('sk-p')),)), id='a-port'
        ),
    ],
)
def test_an_exposure_other_than_one_route_is_refused(exposure: Exposure) -> None:
    with pytest.raises(ValueError, match='one route row alone'):
        _ = Shoko(
            'shoko',
            exposure=exposure,
            image=IMAGE,
            writer_key=WRITER_KEY,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )


@pytest.mark.parametrize(
    'internet',
    [pytest.param(PROXIED, id='proxied'), pytest.param(Direct('a test of the refusal'), id='direct')],
)
def test_an_ungated_internet_side_is_refused(internet: Proxied | Direct) -> None:
    """The server's own sign-in is all that guards its interface, so from outside it is reached behind the gate or not at all."""
    exposure = Exposure(routes=(Route('sk-library', internet=internet, lan=REWRITE),))

    with pytest.raises(ValueError, match='with no gate'):
        _ = Shoko('shoko', exposure=exposure, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS)


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(Exposure(routes=(Held(Route('sk-library', lan=REWRITE)),)), id='held'),
        pytest.param(Exposure(routes=(Passthrough('sk-library', lan=REWRITE),)), id='passthrough'),
    ],
)
def test_a_row_that_is_no_route_of_its_own_is_refused(exposure: Exposure) -> None:
    """A passthrough is TLS the pod would end, which the server serves none of, and the name is new, so no legacy record holds it."""
    with pytest.raises(ValueError, match='by a route row'):
        _ = Shoko(
            'shoko',
            exposure=exposure,
            image=IMAGE,
            writer_key=WRITER_KEY,
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
