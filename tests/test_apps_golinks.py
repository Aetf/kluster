"""golinks, declared against mocks.

Every row here is the test's own, under a name no census row carries: a home
route with its bare label, and one rewritten without it. So what is held is how
the row a component is handed decides what it declares, never what the census
happens to say. The pin is the test's own too.

Literals written here are the upstream projects' contracts: Kubernetes' Pod
Security label and the Gateway API's spellings.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import golinks as golinks_app
from kluster.components.apps.golinks import Golinks
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    BARE,
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
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity, bare_listener
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

#: A home route with its bare label, as golinks is used.
LINKS = Exposure(routes=(Route('gl-links', lan=BARE),))
#: The same, rewritten without a bare label.
FULL = Exposure(routes=(Route('gl-links', lan=REWRITE),))
#: The same, reached from outside too, behind the portal's session.
GATED = Exposure(routes=(Route('gl-links', internet=Proxied(Gate.SESSION), lan=BARE),))

IMAGE = ImagePin('registry.example.invalid/golinks', 'v9.9.9', f'sha256:{"3" * 64}')
WRITER_KEY = {'AWS_ACCESS_KEY_ID': 'golinks-id', 'AWS_SECRET_ACCESS_KEY': 'golinks-secret'}


async def _declared(exposure: Exposure) -> Recorder:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Golinks('golinks', exposure=exposure, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS)
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """golinks on its bare home route, as the `apps` program declares it."""
    return await _declared(LINKS)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def full_name() -> Recorder:
    """golinks on a home route with no bare label."""
    return await _declared(FULL)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def gated() -> Recorder:
    """golinks reached from outside behind the portal's session, and at home."""
    return await _declared(GATED)


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == golinks_app.NAME]


def pod_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment['spec']['template']


def serving(monitor: Recorder) -> dict[str, Any]:
    (found,) = pod_of(monitor)['spec']['containers']
    return found


def flag(arguments: list[str], name: str) -> str:
    """The value of the one `--<name>=` among a container's arguments."""
    (value,) = [it.removeprefix(f'--{name}=') for it in arguments if it.startswith(f'--{name}=')]
    return value


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


def test_each_probe_asks_a_port_the_container_listens_on(declared: Recorder) -> None:
    container = serving(declared)
    named = {it['name'] for it in container['ports']}

    for probe in ('readinessProbe', 'livenessProbe'):
        assert container[probe]['httpGet']['port'] in named, probe


def test_the_service_binds_the_port_it_declares(declared: Recorder) -> None:
    """The bind is the flag's, and the port the pod names is the one it binds."""
    container = serving(declared)
    (port,) = container['ports']

    assert flag(container['args'], 'addr') == f':{int(port["containerPort"])}'


def test_the_pod_runs_on_the_worker(declared: Recorder) -> None:
    """Its clients are at home, and a cloud node's resources are the control plane's and the internet pool's."""
    affinity = pod_of(declared)['spec']['affinity']['nodeAffinity']
    (term,) = affinity['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']

    assert {'key': CONTROL_PLANE_LABEL, 'operator': 'DoesNotExist'} in term['matchExpressions']


def test_the_store_never_has_two_writers(declared: Recorder) -> None:
    """LevelDB locks its directory, so a rollout stops the old pod before it starts the new one."""
    (deployment,) = in_namespace(declared, DEPLOYMENT)
    strategy = deployment['spec']['strategy']

    assert strategy['type'] == 'Recreate' or strategy.get('rollingUpdate', {}).get('maxSurge') in (0, '0', '0%')


# -- the exposure ----------------------------------------------------------------


def test_the_route_reaches_the_pods_at_home_ungated(declared: Recorder) -> None:
    """The row's one Gateway, `lan-gw`, forwards the name to a port the pods listen on, with no gate."""
    routes = in_namespace(declared, HTTP_ROUTE)
    (route,) = [it for it in routes if it['spec']['parentRefs'][0].get('sectionName') is None]
    (service,) = in_namespace(declared, SERVICE)
    pod = pod_of(declared)
    named = {it['name']: it['containerPort'] for it in serving(declared)['ports']}
    (port,) = service['spec']['ports']
    (rule,) = route['spec']['rules']

    assert [ref['name'] for ref in route['spec']['parentRefs']] == [conventions.GATEWAY_LAN]
    assert route['spec']['hostnames'] == [f'gl-links.{conventions.ZONE_PRIMARY}']
    assert [it for it in rule['filters'] if it['type'] == 'ExternalAuth'] == []
    assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
    assert service['spec']['selector'].items() <= pod['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port']


def test_the_bare_label_redirects_to_the_name(declared: Recorder) -> None:
    (bare,) = [
        it
        for it in in_namespace(declared, HTTP_ROUTE)
        if it['spec']['parentRefs'][0].get('sectionName') == bare_listener('gl-links')
    ]
    ((redirect,),) = [rule['filters'] for rule in bare['spec']['rules']]

    assert bare['spec']['hostnames'] == ['gl-links']
    assert redirect['requestRedirect']['hostname'] == f'gl-links.{conventions.ZONE_PRIMARY}'


def test_a_link_is_shown_at_the_bare_label_where_the_home_types_it(declared: Recorder) -> None:
    assert flag(serving(declared)['args'], 'host') == 'gl-links'


def test_a_link_is_shown_at_the_full_name_where_the_row_has_no_bare_label(full_name: Recorder) -> None:
    """A bare label nothing redirects would be a link no client follows."""
    assert flag(serving(full_name)['args'], 'host') == f'gl-links.{conventions.ZONE_PRIMARY}'


def test_a_gated_internet_side_reaches_the_editor_behind_the_portal(gated: Recorder) -> None:
    """From outside, only a signed-in user may edit a link."""
    (route,) = [
        it
        for it in in_namespace(gated, HTTP_ROUTE)
        if [ref['name'] for ref in it['spec']['parentRefs']] == [conventions.GATEWAY_INTERNET]
    ]
    (rule,) = route['spec']['rules']

    assert [it['type'] for it in rule['filters'] if it['type'] == 'ExternalAuth'] == ['ExternalAuth']


# -- the claim -------------------------------------------------------------------


def test_the_namespace_is_one_whose_writer_key_is_sealed() -> None:
    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[golinks_app.NAME]


def test_the_store_is_the_backed_up_claim_read_by_a_mover_as_the_pod(declared: Recorder) -> None:
    """The service keeps its store where the claim is mounted, and the claim is backed up as the user that wrote it."""
    pod = pod_of(declared)
    (claim,) = in_namespace(declared, CLAIM)
    (source,) = in_namespace(declared, SOURCE)
    volumes = {it['name']: it['persistentVolumeClaim']['claimName'] for it in pod['spec']['volumes']}
    mounted = {it['mountPath']: volumes[it['name']] for it in serving(declared)['volumeMounts']}

    assert mounted[flag(serving(declared)['args'], 'data')] == claim['metadata']['name']
    assert source['spec']['sourcePVC'] == claim['metadata']['name']
    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']


# -- refusals --------------------------------------------------------------------


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(Exposure(routes=(Route('gl-a', lan=BARE), Route('gl-b', lan=REWRITE))), id='two-routes'),
        pytest.param(
            Exposure(routes=LINKS.routes, ports=(Port('gl-p', 1, Transports.TCP, lan=Vip('gl-p')),)), id='a-port'
        ),
    ],
)
def test_an_exposure_other_than_one_route_is_refused(exposure: Exposure) -> None:
    with pytest.raises(ValueError, match='one route row alone'):
        _ = Golinks('golinks', exposure=exposure, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS)


@pytest.mark.parametrize(
    'internet',
    [pytest.param(PROXIED, id='proxied'), pytest.param(Direct('a test of the refusal'), id='direct')],
)
def test_an_ungated_internet_side_is_refused(internet: Proxied | Direct) -> None:
    """Anyone who reaches the editor may change a link, so from outside it is reached behind the gate or not at all."""
    exposure = Exposure(routes=(Route('gl-links', internet=internet, lan=BARE),))

    with pytest.raises(ValueError, match='with no gate'):
        _ = Golinks('golinks', exposure=exposure, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS)


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(Exposure(routes=(Held(Route('gl-links', lan=BARE)),)), id='held'),
        pytest.param(Exposure(routes=(Passthrough('gl-links', lan=REWRITE),)), id='passthrough'),
    ],
)
def test_a_row_that_is_no_route_of_its_own_is_refused(exposure: Exposure) -> None:
    """A passthrough is TLS the pod would end, which golinks serves none of, and no home-only name is held."""
    with pytest.raises(ValueError, match='by a route row'):
        _ = Golinks('golinks', exposure=exposure, image=IMAGE, writer_key=WRITER_KEY, fronts=FRONTS, zone_ids=ZONE_IDS)
