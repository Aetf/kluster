"""spoolman, declared against mocks.

The row here is the test's own, under a name no census row carries: a route
gated from outside and rewritten at the media VIP for the home. So what is held
is how the row a component is handed decides what it declares, never what the
census happens to say. The pin is the test's own too.

The user the entrypoint needs is read from the image `Pulumi.yaml` pins: its
`/etc/passwd`, copied out of it as `image_busybox` has it, which runs where
podman is on `PATH`.

Literals written here are the upstream projects' contracts: Kubernetes' Pod
Security label, the Gateway API's spellings, the user and variables spoolman's
image reads (`entrypoint.sh` and `spoolman/env.py` at the pinned release), and
the fields of `/etc/passwd`.
"""

from __future__ import annotations

from typing import Any

import image_busybox
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import spoolman as spoolman_app
from kluster.components.apps.spoolman import Spoolman
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.routes import REWRITE, Exposure, Gate, Proxied, Route
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PORTAL, PodSecurity
from kluster.lib.versions import ImagePin

HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
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

#: Gated from outside, and ungated at home through `media`, where the printer is.
SPOOLS = Exposure(routes=(Route('sp-spools', internet=Proxied(Gate.SESSION), media=REWRITE),))

IMAGE = ImagePin('registry.example.invalid/spoolman', 'v9.9.9', f'sha256:{"5" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'spoolman-id', 'AWS_SECRET_ACCESS_KEY': 'spoolman-secret'}


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """spoolman, as the `apps` program declares it."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Spoolman('spoolman', exposure=SPOOLS, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS)
    return monitor


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == spoolman_app.NAME]


def pod_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment['spec']['template']


def serving(monitor: Recorder) -> dict[str, Any]:
    (found,) = pod_of(monitor)['spec']['containers']
    return found


def environment(monitor: Recorder) -> dict[str, str]:
    return {it['name']: it['value'] for it in serving(monitor)['env']}


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


#: The user the pinned image's entrypoint runs the server as.
APP_USER = 'app'


def passwd_entry(text: str, user: str) -> tuple[int, int]:
    """The uid and gid `/etc/passwd` gives `user`."""
    (entry,) = [line.split(':') for line in text.splitlines() if line.split(':')[0] == user]
    return int(entry[2]), int(entry[3])


def test_the_pod_runs_as_the_app_user_the_entrypoint_is_asked_for(
    declared: Recorder, request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The entrypoint moves `app` to `PUID` and `PGID` wherever they differ from it, which only root can.

    So the pod runs as `app`'s own uid and gid, and asks for exactly those:
    anything else stops it at its start.
    """
    context = pod_of(declared)['spec']['securityContext']
    found = environment(declared)
    cache = image_busybox.cache_directory(request.config, tmp_path_factory)
    passwd = image_busybox.copied(image_busybox.pinned('spoolman'), '/etc/passwd', cache).read_text()
    uid, gid = passwd_entry(passwd, APP_USER)

    assert (context['runAsUser'], context['runAsGroup']) == (uid, gid)
    assert (int(found['PUID']), int(found['PGID'])) == (uid, gid)


def test_the_container_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    assert serving(declared)['image'] == str(IMAGE)


def test_the_server_listens_on_the_port_the_container_declares(declared: Recorder) -> None:
    (port,) = serving(declared)['ports']

    assert int(environment(declared)['SPOOLMAN_PORT']) == port['containerPort']


def test_each_probe_asks_a_port_the_container_listens_on(declared: Recorder) -> None:
    container = serving(declared)
    named = {it['name'] for it in container['ports']}

    for probe in ('readinessProbe', 'livenessProbe'):
        assert container[probe]['httpGet']['port'] in named, probe


def test_the_pod_runs_on_the_worker(declared: Recorder) -> None:
    """Its clients are at home, and a cloud node's resources are the control plane's and the internet pool's."""
    affinity = pod_of(declared)['spec']['affinity']['nodeAffinity']
    (term,) = affinity['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']

    assert {'key': CONTROL_PLANE_LABEL, 'operator': 'DoesNotExist'} in term['matchExpressions']


def test_the_database_never_has_two_writers(declared: Recorder) -> None:
    (deployment,) = in_namespace(declared, DEPLOYMENT)
    strategy = deployment['spec']['strategy']

    assert strategy['type'] == 'Recreate' or strategy.get('rollingUpdate', {}).get('maxSurge') in (0, '0', '0%')


# -- the exposure ----------------------------------------------------------------


def test_the_route_is_gated_from_outside_and_open_at_home(declared: Recorder) -> None:
    """`internet-gw` asks the portal for its session; `media-gw`, where the printer reaches it, asks for nothing.

    Both forward to a port the pods listen on.
    """
    routes = {it['spec']['parentRefs'][0]['name']: it for it in in_namespace(declared, HTTP_ROUTE)}
    (service,) = in_namespace(declared, SERVICE)
    pod = pod_of(declared)
    named = {it['name']: it['containerPort'] for it in serving(declared)['ports']}
    (port,) = service['spec']['ports']

    assert sorted(routes) == sorted([conventions.GATEWAY_INTERNET, conventions.GATEWAY_MEDIA])
    for gateway, route in routes.items():
        (rule,) = route['spec']['rules']
        gates = [it for it in rule['filters'] if it['type'] == 'ExternalAuth']
        assert route['spec']['hostnames'] == [f'sp-spools.{conventions.ZONE_PRIMARY}']
        assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
        if gateway == conventions.GATEWAY_INTERNET:
            assert [it['externalAuth']['http']['path'] for it in gates] == [PORTAL.paths[Gate.SESSION]]
        else:
            assert gates == []
    assert service['spec']['selector'].items() <= pod['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port']


# -- the claim -------------------------------------------------------------------


def test_the_namespace_is_one_whose_writer_key_is_sealed() -> None:
    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[spoolman_app.NAME]


def test_the_database_is_in_the_backed_up_claim_read_by_a_mover_as_the_pod(declared: Recorder) -> None:
    """spoolman keeps its database in the directory it is told, where the claim is mounted, backed up as the user that wrote it."""
    pod = pod_of(declared)
    (claim,) = in_namespace(declared, CLAIM)
    (source,) = in_namespace(declared, SOURCE)
    volumes = {it['name']: it['persistentVolumeClaim']['claimName'] for it in pod['spec']['volumes']}
    mounted = {it['mountPath']: volumes[it['name']] for it in serving(declared)['volumeMounts']}

    assert mounted[environment(declared)['SPOOLMAN_DIR_DATA']] == claim['metadata']['name']
    assert source['spec']['sourcePVC'] == claim['metadata']['name']
    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']
