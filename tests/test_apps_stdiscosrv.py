"""syncthing's discovery server, declared against mocks.

Every row here is the test's own: a raw port on TCP, on a port no census row
carries, dialed at two names through the balancer, one of them held, and at a
third at home. So the cases show that the row the component is handed decides
what it declares, never what the census happens to say. The image pin is the
test's own too.

Literals written here are the upstream projects' contracts: stdiscosrv's flags
and its `/ping`, cert-manager's issuer reference, Reloader's annotation,
Kubernetes' Pod Security label, and the files a cert-manager Secret holds.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.apps import WORLD_ENTITY, Fronts
from kluster.components.apps import stdiscosrv as stdiscosrv_app
from kluster.components.apps.stdiscosrv import Stdiscosrv
from kluster.components.certificates import ISSUER
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions import CONTROL_PLANE_LABEL
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    REWRITE,
    Balancer,
    Dedicated,
    Direct,
    Exposure,
    Held,
    Host,
    Passthrough,
    Port,
    Route,
    Vip,
)
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.versions import ImagePin

SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CERTIFICATE = 'crds:cert-manager.io/v1:Certificate'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'

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

#: The discovery port: dialed at two names through the balancer, the first
#: held as a row whose name the legacy VPS keeps, and at a third at home.
FIND = Port(
    'st-find',
    8444,
    Transports.TCP,
    internet=Balancer((Held(Host('st-find')), Host('st-also'))),
    lan=Vip('st-home'),
)
EXPOSURE = Exposure(ports=(FIND,))

IMAGE = ImagePin('registry.example.invalid/discosrv', 'v9.9.9', f'sha256:{"3" * 64}')


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The discovery server, as the `apps` program declares it."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Stdiscosrv('stdiscosrv', exposure=EXPOSURE, image=IMAGE, fronts=FRONTS, zone_ids=ZONE_IDS)
    return monitor


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == stdiscosrv_app.NAME]


def deployment_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment


def pod_of(monitor: Recorder) -> dict[str, Any]:
    return deployment_of(monitor)['spec']['template']


def server(monitor: Recorder) -> dict[str, Any]:
    (container,) = pod_of(monitor)['spec']['containers']
    return container


def flags(container: dict[str, Any]) -> dict[str, str]:
    """The server's `--name=value` arguments, by name."""
    return dict(arg.removeprefix('--').split('=', 1) for arg in container['args'])


def world_allow(monitor: Recorder) -> dict[str, Any]:
    """The one policy that admits the world, among the namespace's own."""
    (found,) = [
        policy
        for policy in in_namespace(monitor, POLICY)
        if any(WORLD_ENTITY in rule.get('fromEntities', []) for rule in policy.get('spec', {}).get('ingress', []))
    ]
    return found


def mounted(monitor: Recorder) -> dict[str, dict[str, Any]]:
    """Each volume the server mounts, by the path it is mounted at."""
    volumes = {volume['name']: volume for volume in pod_of(monitor)['spec']['volumes']}
    return {mount['mountPath']: volumes[mount['name']] for mount in server(monitor)['volumeMounts']}


def mount_of(monitor: Recorder, path: str) -> tuple[str, dict[str, Any]]:
    """The mount a file path lies under, and its volume."""
    (found,) = [(mount, volume) for mount, volume in mounted(monitor).items() if path.startswith(f'{mount}/')]
    return found


# -- its certificate -----------------------------------------------------------


def test_the_certificate_is_the_cluster_issuers_for_each_name_the_port_is_dialed_at(declared: Recorder) -> None:
    """A device checks the server's certificate against the public roots, for whichever of the names it dialed.

    A held name is dialed all the same once its record moves, so the
    certificate carries it ahead of the cut.
    """
    (certificate,) = in_namespace(declared, CERTIFICATE)
    primary = conventions.ZONE_PRIMARY

    assert certificate['spec']['dnsNames'] == [f'st-find.{primary}', f'st-also.{primary}', f'st-home.{primary}']
    assert certificate['spec']['issuerRef'] == {'name': ISSUER, 'kind': 'ClusterIssuer', 'group': 'cert-manager.io'}


def test_the_server_serves_the_certificate_the_issuer_writes(declared: Recorder) -> None:
    """The key pair it loads is the Secret the certificate is issued into, mounted where its flags point."""
    (certificate,) = in_namespace(declared, CERTIFICATE)
    found = flags(server(declared))

    for file, flag in (('tls.crt', 'cert'), ('tls.key', 'key')):
        mount, volume = mount_of(declared, found[flag])
        assert found[flag] == f'{mount}/{file}'
        assert volume['secret']['secretName'] == certificate['spec']['secretName']


def test_a_renewal_rolls_the_server(declared: Recorder) -> None:
    """The server reads its key pair at its start alone, so Reloader rolls it when the Secret it mounts changes."""
    assert deployment_of(declared)['metadata']['annotations']['reloader.stakater.com/auto'] == 'true'


# -- its port ------------------------------------------------------------------


def test_the_server_listens_on_the_rows_port_which_its_services_forward_and_its_allow_admits(
    declared: Recorder,
) -> None:
    """A device dials the row's port at its name, and every hop to the pod keeps it."""
    container = server(declared)
    services = in_namespace(declared, SERVICE)
    allow = world_allow(declared)
    (allowed,) = [port for rule in allow['spec']['ingress'] for ports in rule['toPorts'] for port in ports['ports']]

    assert flags(container)['listen'] == f':{FIND.port}'
    assert [(port['name'], port['containerPort']) for port in container['ports']] == [('https', FIND.port)]
    assert {(port['port'], port['targetPort']) for service in services for port in service['spec']['ports']} == {
        (FIND.port, FIND.port)
    }
    assert allowed == {'port': str(FIND.port), 'protocol': 'TCP'}


def test_the_raw_port_is_served_at_each_gateway_the_row_sets_by_the_servers_pods(declared: Recorder) -> None:
    services = in_namespace(declared, SERVICE)
    allow = world_allow(declared)
    labels = pod_of(declared)['metadata']['labels']

    assert sorted(service['metadata']['name'] for service in services) == ['st-find-internet', 'st-find-lan']
    for service in services:
        assert service['spec']['selector'].items() <= labels.items()
    assert allow['spec']['endpointSelector']['matchLabels'].items() <= labels.items()


def test_the_server_is_probed_at_its_ping_over_https_on_the_port_it_listens_on(declared: Recorder) -> None:
    container = server(declared)
    named = {port['name']: port['containerPort'] for port in container['ports']}

    for probe in (container['readinessProbe'], container['livenessProbe']):
        assert probe['httpGet']['path'] == '/ping'
        assert probe['httpGet']['scheme'] == 'HTTPS'
        assert named[probe['httpGet']['port']] == FIND.port


# -- its pod -------------------------------------------------------------------


def test_the_database_is_written_to_an_empty_dir_beside_a_read_only_root(declared: Recorder) -> None:
    """Its announcements are rebuilt within minutes, so nothing of them is kept across a restart."""
    container = server(declared)
    database = flags(container)['db-dir']

    assert container['securityContext']['readOnlyRootFilesystem'] is True
    assert 'emptyDir' in mounted(declared)[database]


def test_the_server_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(declared: Recorder) -> None:
    """Its image names no user, and `restricted`, which its namespace is held to, refuses a pod that does not."""
    (namespace,) = [
        it.inputs for it in declared.of_type(NAMESPACE) if it.inputs['metadata']['name'] == stdiscosrv_app.NAME
    ]
    spec = pod_of(declared)['spec']
    context = spec['securityContext']
    container = server(declared)

    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED.value
    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    assert spec['automountServiceAccountToken'] is False
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities'] == {'drop': ['ALL']}


def test_the_server_runs_the_image_it_is_handed(declared: Recorder) -> None:
    assert server(declared)['image'] == str(IMAGE)


def test_the_server_runs_on_the_cloud_pool_with_a_limit_of_cpu_and_of_memory(declared: Recorder) -> None:
    """Devices find each other with the homelab down, and a pod on the cloud pool carries both limits (nodes.md §1.1)."""
    spec = pod_of(declared)['spec']
    (term,) = spec['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']

    assert term['matchExpressions'] == [{'key': CONTROL_PLANE_LABEL, 'operator': 'Exists'}]
    assert server(declared)['resources']['limits'].keys() == {'cpu', 'memory'}


# -- refusals ------------------------------------------------------------------


def declare(exposure: Exposure) -> None:
    _ = Stdiscosrv('stdiscosrv', exposure=exposure, image=IMAGE, fronts=FRONTS, zone_ids=ZONE_IDS)


@pytest.mark.parametrize(
    'row',
    [
        Passthrough('st-find', internet=Direct('devices present a certificate'), lan=REWRITE),
        Route('st-find', internet=Direct('devices present a certificate')),
    ],
    ids=['passthrough', 'route'],
)
def test_a_route_row_is_refused(row: Passthrough | Route) -> None:
    """A Gateway would end the TLS that carries a device's ID."""
    with pytest.raises(ValueError, match='raw port and at no route'):
        declare(Exposure(routes=(row,), ports=(FIND,)))


def test_an_exposure_without_exactly_one_port_is_refused() -> None:
    other = Port('st-other', 8445, Transports.TCP, internet=Balancer((Host('st-other'),)))
    with pytest.raises(ValueError, match='listens on one port'):
        declare(Exposure(ports=(FIND, other)))


@pytest.mark.parametrize('transports', [Transports.UDP, Transports.TCP_AND_UDP])
def test_a_port_on_any_transport_but_tcp_is_refused(transports: Transports) -> None:
    with pytest.raises(ValueError, match='over TCP alone'):
        declare(Exposure(ports=(Port('st-find', 8444, transports, internet=Balancer((Host('st-find'),))),)))


@pytest.mark.parametrize(
    'internet', [Balancer(()), Dedicated()], ids=['a balancer front with no host', 'a dedicated front']
)
def test_a_port_dialed_at_no_name_is_refused(internet: Balancer | Dedicated) -> None:
    """Its certificate is for the names a device dials, and a port with none leaves it nothing to be issued for."""
    with pytest.raises(ValueError, match='names no host'):
        declare(Exposure(ports=(Port('st-find', 8444, Transports.TCP, internet=internet),)))
