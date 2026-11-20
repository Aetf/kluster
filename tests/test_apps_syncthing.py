"""syncthing on the NAS and the WebDAV server beside it, declared against mocks, and the merge their configuration runs.

Every row here is the test's own: an interface route, a peer port dialed at one
name at the balancer and another at the LAN VIP, a discovery port, and the
WebDAV server's route, each under a name no census row carries. So what is held
is how the rows a component is handed decide what it declares, never what the
census happens to say. The pins of syncthing and the WebDAV server are the
test's own too; the merge's is the one `Pulumi.yaml` pins, because the merge
script runs here under that image's busybox and its `yq`, the bytes the init
container runs (`image_busybox`).

Literals written here are the upstream projects' contracts: syncthing's
listener schemes and the elements of its configuration, Kubernetes' Pod
Security label, the Gateway API's spellings, and the label Talos gives a
control plane node.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import image_busybox
import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import stdiscosrv as stdiscosrv_app
from kluster.components.apps import syncthing as syncthing_app
from kluster.components.apps.stdiscosrv import Stdiscosrv
from kluster.components.apps.syncthing import Syncthing
from kluster.components.apps.syncthing import webdav as webdav_app
from kluster.components.apps.syncthing.webdav import WebDav
from kluster.components.cilium import InternetPoolMembers
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import Balancer, Exposure, Gate, Held, Host, Port, Proxied, Route, Vip
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PORTAL, PodSecurity
from kluster.lib.versions import ImagePin

HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
SERVICE = 'kubernetes:core/v1:Service'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
NAMESPACE = 'kubernetes:core/v1:Namespace'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
VOLUME = 'kubernetes:core/v1:PersistentVolume'
SOURCE = 'crds:volsync.backube/v1alpha1:ReplicationSource'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'
CERTIFICATE = 'crds:cert-manager.io/v1:Certificate'

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

#: The peer port, dialed at one name through the balancer, held as a row
#: whose name the legacy VPS keeps, and at another at home.
PEER = Port(
    'st-peer',
    22001,
    Transports.TCP_AND_UDP,
    internet=Balancer((Held(Host('st-dial')),)),
    lan=Vip('st-home'),
)
SYNC = Exposure(routes=(Route('st-ui', internet=Proxied(Gate.SESSION)),), ports=(PEER,))
#: The same instance with a peer port on TCP alone, and no device retired.
TCP_PEER = Port('st-peer', 22001, Transports.TCP, internet=Balancer((Host('st-dial'),)))
DISCOVERY = Port('st-find', 8444, Transports.TCP, internet=Balancer((Host('st-find'),)))
DAV = Exposure(routes=(Route('st-files', internet=Proxied(Gate.BASIC)),))

#: Device IDs that syncthing 2.1.2 generated (`syncthing generate`), check
#: characters and all: the instance's own, one it keeps, and the two it
#: retires.
SELF = '3BQUJA2-F4Y56AW-RABFL5R-GZUGGJE-YC3ESPT-VP7WVUQ-7BML2LO-D74F5AB'
KEPT = 'TQV3FPD-DQLPTUE-OIPSLXM-GKTBLNH-J23UKBL-GQMONSZ-QMIS5IJ-HRUYPQH'
RETIRED = '3RC4PLI-37YMKAT-PKEXMJM-QKPPZ4Z-T7IG4QT-OM7E3O2-JZ6GWX5-RRKBJQN'
RETIRED_TOO = 'IDKJET4-TUZKHCH-PL2EQ5S-AA72AQT-MBXXBD2-DXDFWES-6DL6SM3-6B4WCA6'
#: `RETIRED` with one letter changed, which syncthing refuses by its check
#: character.
MISTYPED = '3RC4PLI-37YMKAT-PKEXMJM-QKPPZ4Z-T7IG4QT-OM7E3O2-JZ6GWX5-RSKBJQN'

SYNCTHING_IMAGE = ImagePin('registry.example.invalid/syncthing', 'v9.9.9', f'sha256:{"1" * 64}')
WEBDAV_IMAGE = ImagePin('registry.example.invalid/dufs', 'v9.9.9', f'sha256:{"2" * 64}')
#: The merge's pin, which the script runs under here.
MERGE_IMAGE = image_busybox.pinned('syncthing-config')


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The instance, retiring two devices, and the WebDAV server, as the `apps` program declares them."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Syncthing(
            'syncthing',
            exposure=SYNC,
            discovery=DISCOVERY,
            retired_devices=(RETIRED, RETIRED_TOO),
            image=SYNCTHING_IMAGE,
            config_image=MERGE_IMAGE,
            writer_key=writer_key(syncthing_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
        _ = WebDav(
            'webdav',
            exposure=DAV,
            image=WEBDAV_IMAGE,
            writer_key=writer_key(webdav_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def tcp_only() -> Recorder:
    """The instance on a peer port on TCP alone, retiring no device."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Syncthing(
            'syncthing',
            exposure=Exposure(ports=(TCP_PEER,)),
            discovery=DISCOVERY,
            retired_devices=(),
            image=SYNCTHING_IMAGE,
            config_image=MERGE_IMAGE,
            writer_key=writer_key(syncthing_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
    return monitor


def in_namespace(monitor: Recorder, typ: str, namespace: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == namespace]


def pod_of(monitor: Recorder, namespace: str) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT, namespace)
    return deployment['spec']['template']


def container(pod: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [it for it in [*pod['spec'].get('initContainers', []), *pod['spec']['containers']] if it['name'] == name]
    return found


def mounted_claims(pod: dict[str, Any], container_name: str) -> dict[str, str]:
    """Each claim a container mounts, by the path it is mounted at."""
    claims = {
        volume['name']: volume['persistentVolumeClaim']['claimName']
        for volume in pod['spec']['volumes']
        if 'persistentVolumeClaim' in volume
    }
    return {
        mount['mountPath']: claims[mount['name']]
        for mount in container(pod, container_name)['volumeMounts']
        if mount['name'] in claims
    }


def nfs_path(monitor: Recorder, namespace: str, claim_name: str) -> str:
    (volume,) = [
        it.inputs
        for it in monitor.of_type(VOLUME)
        if it.inputs['spec']['claimRef'] == {'namespace': namespace, 'name': claim_name}
    ]
    return volume['spec']['nfs']['path']


def rendered(monitor: Recorder) -> Mapping[str, str]:
    (config_map,) = in_namespace(monitor, CONFIG_MAP, syncthing_app.NAME)
    return config_map['data']


def options(monitor: Recorder) -> ET.Element:
    found = ET.fromstring(rendered(monitor)['config.xml']).find('options')
    assert found is not None
    return found


# -- the pods ------------------------------------------------------------------


@pytest.mark.parametrize('namespace', [syncthing_app.NAME, webdav_app.NAME])
def test_each_pod_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(
    declared: Recorder, namespace: str
) -> None:
    """No image here names a numeric user, and `restricted`, which each namespace is held to, refuses a pod that does not."""
    (declared_namespace,) = [
        it.inputs for it in declared.of_type(NAMESPACE) if it.inputs['metadata']['name'] == namespace
    ]
    spec = pod_of(declared, namespace)['spec']
    context = spec['securityContext']

    assert declared_namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    for each in [*spec.get('initContainers', []), *spec['containers']]:
        assert each['securityContext']['allowPrivilegeEscalation'] is False, each['name']
        assert each['securityContext']['capabilities'] == {'drop': ['ALL']}, each['name']


def test_each_container_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    syncthing = pod_of(declared, syncthing_app.NAME)
    webdav = pod_of(declared, webdav_app.NAME)

    assert container(syncthing, syncthing_app.NAME)['image'] == str(SYNCTHING_IMAGE)
    assert container(syncthing, 'config')['image'] == str(MERGE_IMAGE)
    assert container(webdav, webdav_app.NAME)['image'] == str(WEBDAV_IMAGE)


@pytest.mark.parametrize('namespace', [syncthing_app.NAME, webdav_app.NAME])
def test_each_probe_asks_a_port_the_container_listens_on(declared: Recorder, namespace: str) -> None:
    serving = container(pod_of(declared, namespace), namespace)
    named = {it['name'] for it in serving['ports']}

    for probe in ('readinessProbe', 'livenessProbe'):
        assert serving[probe]['httpGet']['port'] in named, probe


# -- the exposure --------------------------------------------------------------


@pytest.mark.parametrize(
    ('namespace', 'row', 'gate'),
    [(syncthing_app.NAME, SYNC.routes[0], Gate.SESSION), (webdav_app.NAME, DAV.routes[0], Gate.BASIC)],
)
def test_each_route_reaches_its_pods_behind_its_rows_gate(
    declared: Recorder, namespace: str, row: Route, gate: Gate
) -> None:
    """The route attaches where the row says, asks the portal for the row's form, and forwards to a port the pods listen on."""
    (route,) = in_namespace(declared, HTTP_ROUTE, namespace)
    (service,) = [it for it in in_namespace(declared, SERVICE, namespace) if it['spec'].get('type') != 'LoadBalancer']
    pod = pod_of(declared, namespace)
    serving = container(pod, namespace)
    (port,) = service['spec']['ports']
    named = {it['name']: it['containerPort'] for it in serving['ports']}
    (rule,) = route['spec']['rules']
    (gate_filter,) = [it for it in rule['filters'] if it['type'] == 'ExternalAuth']

    assert [ref['name'] for ref in route['spec']['parentRefs']] == [conventions.GATEWAY_INTERNET]
    assert route['spec']['hostnames'] == [f'{row.host}.{conventions.ZONE_PRIMARY}']
    assert gate_filter['externalAuth']['http']['path'] == PORTAL.paths[gate]
    assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
    assert service['spec']['selector'].items() <= pod['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port']


def test_the_peer_ports_services_and_allow_reach_the_instance_on_each_transport(declared: Recorder) -> None:
    """Each Gateway's Service and the world's allow select the instance's pods, which listen on the row's port and transports."""
    pod = pod_of(declared, syncthing_app.NAME)
    labels = pod['metadata']['labels']
    listening = {(it['containerPort'], it['protocol']) for it in container(pod, syncthing_app.NAME)['ports']}
    services = [
        it for it in in_namespace(declared, SERVICE, syncthing_app.NAME) if it['spec'].get('type') == 'LoadBalancer'
    ]
    (allow,) = [
        it
        for it in in_namespace(declared, POLICY, syncthing_app.NAME)
        if it['metadata']['name'] == f'{PEER.name}-world'
    ]

    assert len(services) == 2
    for service in services:
        assert service['spec']['selector'].items() <= labels.items()
        assert {(it['targetPort'], it['protocol']) for it in service['spec']['ports']} == {
            (PEER.port, protocol.upper()) for protocol in PEER.transports.protocols
        }
        assert {(it['targetPort'], it['protocol']) for it in service['spec']['ports']} <= listening
    assert allow['spec']['endpointSelector']['matchLabels'].items() <= labels.items()


# -- the rendered configuration --------------------------------------------------


def test_the_instance_listens_on_the_peer_port_and_announces_each_name_it_is_dialed_at(declared: Recorder) -> None:
    """TCP and QUIC at the row's port, the relays, and the same at each name the row is dialed at, held or not.

    A device with `dynamic` learns the instance's address from what it
    announces, and the discovery server sees no client address of its own to
    announce in its place.
    """
    primary = conventions.ZONE_PRIMARY
    addresses = [it.text or '' for it in options(declared).findall('listenAddress')]
    # Which relay pool is the program's choice; that there is one is the relation.
    relays = [it for it in addresses if it.startswith('dynamic+')]

    assert len(relays) == 1
    assert sorted(set(addresses) - set(relays)) == sorted(
        [
            'tcp://:22001',
            'quic://:22001',
            f'tcp://st-dial.{primary}:22001',
            f'quic://st-dial.{primary}:22001',
            f'tcp://st-home.{primary}:22001',
            f'quic://st-home.{primary}:22001',
        ]
    )


def test_a_peer_port_on_tcp_alone_is_listened_for_on_tcp_alone(tcp_only: Recorder) -> None:
    """QUIC is syncthing's transport over UDP, so a row that carries no UDP gets no QUIC address, which would never connect."""
    addresses = [it.text or '' for it in options(tcp_only).findall('listenAddress')]

    assert [it for it in addresses if it.startswith('quic://')] == []
    assert f'tcp://st-dial.{conventions.ZONE_PRIMARY}:22001' in addresses


def test_the_instance_announces_to_the_discovery_server_at_its_name_and_raw_port(declared: Recorder) -> None:
    assert [it.text for it in options(declared).findall('globalAnnounceServer')] == [
        f'https://st-find.{conventions.ZONE_PRIMARY}:8444'
    ]


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def with_the_discovery_server() -> Recorder:
    """The instance and the discovery server, each from its row of the census, as the `apps` program wires them."""
    discovery = conventions.routes.EXPOSURES['stdiscosrv']
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Syncthing(
            'syncthing',
            exposure=conventions.routes.EXPOSURES['syncthing'],
            discovery=discovery.ports[0],
            retired_devices=(),
            image=SYNCTHING_IMAGE,
            config_image=MERGE_IMAGE,
            writer_key=writer_key(syncthing_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
        _ = Stdiscosrv(
            'stdiscosrv',
            exposure=discovery,
            image=ImagePin('registry.example.invalid/discosrv', 'v9.9.9', f'sha256:{"3" * 64}'),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
    return monitor


def test_the_instance_announces_to_a_name_the_discovery_servers_certificate_carries_on_the_port_it_listens_on(
    with_the_discovery_server: Recorder,
) -> None:
    """The instance checks the server's certificate against the name it dials, so that name is one the certificate is for."""
    (announced,) = [it.text or '' for it in options(with_the_discovery_server).findall('globalAnnounceServer')]
    (certificate,) = in_namespace(with_the_discovery_server, CERTIFICATE, stdiscosrv_app.NAME)
    (deployment,) = in_namespace(with_the_discovery_server, DEPLOYMENT, stdiscosrv_app.NAME)
    (server,) = deployment['spec']['template']['spec']['containers']
    address = urlsplit(announced)

    assert address.scheme == 'https'
    assert address.hostname in certificate['spec']['dnsNames']
    assert f':{address.port}' == flag(server['args'], 'listen')


def flag(arguments: list[str], name: str) -> str:
    """The value of the one `--<name>=` among a container's arguments."""
    (value,) = [it.removeprefix(f'--{name}=') for it in arguments if it.startswith(f'--{name}=')]
    return value


def test_the_interface_binds_plain_http_on_the_port_its_service_forwards_to(declared: Recorder) -> None:
    """The Gateway terminates TLS for the name and speaks HTTP to the backend, so the interface serves plain HTTP there.

    The flag decides the bind: syncthing reads it ahead of the image's
    environment, which sets an address of its own, and ahead of the
    configuration's `<gui>`.
    """
    serving = container(pod_of(declared, syncthing_app.NAME), syncthing_app.NAME)
    named = {it['name']: it['containerPort'] for it in serving['ports']}
    (service,) = [
        it for it in in_namespace(declared, SERVICE, syncthing_app.NAME) if it['spec'].get('type') != 'LoadBalancer'
    ]
    (port,) = service['spec']['ports']
    bind = urlsplit(flag(serving['args'], 'gui-address'))

    assert bind.scheme == 'http'
    assert bind.hostname is None
    assert bind.port == named[port['targetPort']]


def test_the_instance_reads_its_configuration_where_the_merge_writes_it(declared: Recorder) -> None:
    """syncthing's home is the directory of the file the merge writes, and both containers mount the state claim there.

    Without its own home, the image's environment names another, and the
    instance starts as a new device beside a configuration it never reads.
    """
    pod = pod_of(declared, syncthing_app.NAME)
    serving = container(pod, syncthing_app.NAME)
    _, _, target, _ = container(pod, 'config')['command']
    (state,) = [
        it['metadata']['name']
        for it in in_namespace(declared, CLAIM, syncthing_app.NAME)
        if it['metadata']['name'] != 'sync'
    ]
    home = flag(serving['args'], 'home')

    assert str(PurePosixPath(target).parent) == home
    assert mounted_claims(pod, syncthing_app.NAME)[home] == state
    assert mounted_claims(pod, 'config')[home] == state


def test_the_instance_never_runs_beside_itself(declared: Recorder) -> None:
    """Its index is one process's, so a rollout stops the old pod before it starts the new one."""
    (deployment,) = in_namespace(declared, DEPLOYMENT, syncthing_app.NAME)
    strategy = deployment['spec']['strategy']

    assert strategy['type'] == 'Recreate' or strategy.get('rollingUpdate', {}).get('maxSurge') in (0, '0', '0%')


def test_a_new_folder_lands_on_the_nas(declared: Recorder) -> None:
    """The default folder path is where the instance mounts its NAS claim."""
    defaults = ET.fromstring(rendered(declared)['config.xml']).find('defaults/folder')
    assert defaults is not None
    pod = pod_of(declared, syncthing_app.NAME)
    mounted = mounted_claims(pod, syncthing_app.NAME)
    nas_claims = {
        it.inputs['spec']['claimRef']['name']
        for it in declared.of_type(VOLUME)
        if it.inputs['spec']['claimRef']['namespace'] == syncthing_app.NAME
    }

    assert mounted[defaults.get('path', '')] in nas_claims


# -- the claims ------------------------------------------------------------------

#: The label Talos gives a control plane node, which every cloud node is and the worker is not.
TALOS_CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'

#: The cluster's nodes, by the labels Talos gives them.
NODES: dict[str, dict[str, str]] = {
    **{node: {TALOS_CONTROL_PLANE_LABEL: ''} for node in conventions.CLOUD_NODES},
    conventions.HOMELAB_NODE: {},
}


def schedulable(affinity: Mapping[str, Any]) -> set[str]:
    """The nodes an affinity admits, by its required node affinity, as the scheduler reads it."""
    required = affinity.get('nodeAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution')
    if required is None:
        return set(NODES)

    def holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
        match expression['operator']:
            case 'Exists':
                return expression['key'] in labels
            case 'DoesNotExist':
                return expression['key'] not in labels
            case operator:
                raise AssertionError(f'no reading of {operator!r} here')

    def admits(term: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
        # A term with no requirement matches no node; one on fields is not read here.
        if 'matchFields' in term:
            raise AssertionError(f'no reading of field requirements here: {term!r}')
        expressions = term.get('matchExpressions', [])
        return bool(expressions) and all(holds(it, labels) for it in expressions)

    return {
        node for node, labels in NODES.items() if any(admits(term, labels) for term in required['nodeSelectorTerms'])
    }


@pytest.mark.parametrize('namespace', [syncthing_app.NAME, webdav_app.NAME])
def test_each_pod_and_its_claims_mover_run_on_the_worker_alone(declared: Recorder, namespace: str) -> None:
    """The NAS is reached from the worker alone, so a mover that bound the node claim elsewhere would strand the pod."""
    pod = pod_of(declared, namespace)
    sources = in_namespace(declared, SOURCE, namespace)

    assert schedulable(pod['spec'].get('affinity', {})) == {conventions.HOMELAB_NODE}
    assert sources
    for source in sources:
        assert schedulable(source['spec']['restic'].get('moverAffinity', {})) == {conventions.HOMELAB_NODE}


@pytest.mark.parametrize('namespace', [syncthing_app.NAME, webdav_app.NAME])
def test_each_namespace_is_one_whose_writer_key_is_sealed(namespace: str) -> None:
    """Each declares a claim backed up by VolSync, whose repository Secret carries the namespace's sealed writer key."""
    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[namespace]


@pytest.mark.parametrize('namespace', [syncthing_app.NAME, webdav_app.NAME])
def test_each_claim_on_a_node_is_backed_up_and_its_mover_runs_as_the_pod(declared: Recorder, namespace: str) -> None:
    """The instance's home and the server's own files are backed up, read by a mover as the user that wrote them."""
    pod = pod_of(declared, namespace)
    sources = in_namespace(declared, SOURCE, namespace)
    node_claims = {it['metadata']['name'] for it in in_namespace(declared, CLAIM, namespace)} - {
        it.inputs['spec']['claimRef']['name'] for it in declared.of_type(VOLUME)
    }

    assert node_claims
    assert node_claims <= set(mounted_claims(pod, namespace).values())
    assert {it['spec']['sourcePVC'] for it in sources} == node_claims
    for source in sources:
        assert (
            source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']
        )


def test_the_webdav_servers_folder_is_syncthings_stuff_under_its_own_files(declared: Recorder) -> None:
    """The server serves its own files at its root and, under them, the `Stuff` folder the instance syncs on the NAS."""
    syncthing = pod_of(declared, syncthing_app.NAME)
    webdav = pod_of(declared, webdav_app.NAME)
    synced = mounted_claims(syncthing, syncthing_app.NAME)
    served = mounted_claims(webdav, webdav_app.NAME)
    (root,) = [it for it in container(webdav, webdav_app.NAME)['args'] if not it.startswith('--')]
    (stuff,) = [path for path in served if path != root]

    assert root in served
    assert stuff.startswith(f'{root}/')
    assert nfs_path(declared, webdav_app.NAME, served[stuff]) == (
        f'{nfs_path(declared, syncthing_app.NAME, synced[syncthing_app.FOLDER_ROOT])}/Stuff'
    )


# -- refusals ----------------------------------------------------------------------


def declare_syncthing(**changed: Any) -> None:
    arguments: dict[str, Any] = {
        'exposure': SYNC,
        'discovery': DISCOVERY,
        'retired_devices': (RETIRED,),
        'image': SYNCTHING_IMAGE,
        'config_image': MERGE_IMAGE,
        'writer_key': writer_key(syncthing_app.NAME),
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
    } | changed
    _ = Syncthing('syncthing', **arguments)


@pytest.mark.parametrize('device', [RETIRED.lower(), RETIRED.replace('-', ''), f'{RETIRED} ', 'VPS', MISTYPED])
def test_a_retired_device_that_is_no_device_id_is_refused(device: str) -> None:
    """An ID that is not one matches no device, so the instance would keep the one it was meant to drop, with nothing said.

    A letter changed in a real ID keeps its shape and is caught by the check
    characters alone.
    """
    with pytest.raises(ValueError, match='is not a device ID'):
        declare_syncthing(retired_devices=(RETIRED, device))


@pytest.mark.parametrize('ports', [(), (PEER, DISCOVERY)])
def test_an_exposure_without_exactly_one_peer_port_is_refused(ports: tuple[Port, ...]) -> None:
    with pytest.raises(ValueError, match='one port'):
        declare_syncthing(exposure=Exposure(routes=SYNC.routes, ports=ports))


def test_a_discovery_port_dialed_at_no_name_is_refused() -> None:
    with pytest.raises(ValueError, match='names no host'):
        declare_syncthing(discovery=Port('st-find', 8444, Transports.TCP, internet=Balancer(())))


# -- the merge -----------------------------------------------------------------------

#: The bound on one run of the merge, which takes a fraction of a second.
MERGE_TIMEOUT = 30

#: A configuration the instance keeps: its devices, among them the two it
#: retires, folders shared with them, one with a retired device alone, and
#: parts the rendered file replaces, the interface's key and an option it does
#: not hold.
KEPT_CONFIG = f"""<configuration version="52">
  <folder id="shared" label="Shared" path="/hostsync/Shared" type="sendreceive">
    <device id="{SELF}" introducedBy=""></device>
    <device id="{KEPT}" introducedBy=""></device>
    <device id="{RETIRED}" introducedBy=""></device>
    <device id="{RETIRED_TOO}" introducedBy=""></device>
  </folder>
  <folder id="alone" label="Alone" path="/hostsync/Alone" type="sendreceive">
    <device id="{RETIRED}" introducedBy=""></device>
  </folder>
  <device id="{SELF}" name="nas" compression="metadata" introducer="false">
    <address>dynamic</address>
  </device>
  <device id="{KEPT}" name="laptop" compression="metadata" introducer="true">
    <address>dynamic</address>
  </device>
  <device id="{RETIRED}" name="vps" compression="metadata" introducer="false">
    <address>tcp://old.example:22000</address>
  </device>
  <device id="{RETIRED_TOO}" name="old-phone" compression="metadata" introducer="false">
    <address>dynamic</address>
  </device>
  <gui enabled="true" tls="true">
    <address>:8384</address>
    <apikey>kept-by-the-instance</apikey>
  </gui>
  <options>
    <listenAddress>tcp://:22000</listenAddress>
    <maxSendKbps>100</maxSendKbps>
  </options>
</configuration>
"""


@pytest.fixture(scope='module', name='merge_path')
def merge_path_fixture(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> str:
    """The `PATH` the merge's image gives it: its busybox's applets and its `yq`."""
    cache = image_busybox.cache_directory(request.config, tmp_path_factory)
    shell = image_busybox.applets(image_busybox.extracted(MERGE_IMAGE, cache), tmp_path_factory.mktemp('merge-shell'))
    yq = image_busybox.copied(MERGE_IMAGE, '/usr/bin/yq', cache)
    return f'{shell}:{yq.parent}'


def run_merge(
    monitor: Recorder, tmp_path: Path, path: str, kept: str | None
) -> tuple[subprocess.CompletedProcess[bytes], Path]:
    """The declared init container run over `kept`, or over none, and the home it ran in."""
    pod = pod_of(monitor, syncthing_app.NAME)
    init = container(pod, 'config')
    home, mounted = tmp_path / 'home', tmp_path / 'rendered'
    home.mkdir()
    mounted.mkdir()
    for name, text in rendered(monitor).items():
        _ = (mounted / name).write_text(text)
    if kept is not None:
        _ = (home / 'config.xml').write_text(kept)

    def local(argument: str) -> str:
        for prefix, directory in ((syncthing_app.HOME, home), (syncthing_app.RENDERED_MOUNT, mounted)):
            if argument.startswith(f'{prefix}/'):
                return str(directory / argument.removeprefix(f'{prefix}/'))
        return argument

    shell, *arguments = init['command']
    assert shell == '/bin/sh'
    environment = {it['name']: it['value'] for it in init['env']} | {'PATH': path}
    ran = process_sessions.run(
        [str(Path(path.split(':')[0]) / 'sh'), *map(local, arguments)],
        env=environment,
        timeout=MERGE_TIMEOUT,
    )
    return ran, home


def merged(monitor: Recorder, tmp_path: Path, path: str, kept: str | None) -> ET.Element:
    """The instance's configuration after the declared init container has run over `kept`, or over none."""
    ran, home = run_merge(monitor, tmp_path, path, kept)
    assert ran.returncode == 0, ran.stderr
    return ET.parse(home / 'config.xml').getroot()


def device_ids(element: ET.Element) -> list[str]:
    return [it.get('id', '') for it in element.findall('device')]


def test_an_instance_with_no_configuration_takes_the_rendered_one_as_its_initial_state(
    declared: Recorder, tmp_path: Path, merge_path: str
) -> None:
    result = merged(declared, tmp_path, merge_path, kept=None)

    assert ET.tostring(result) == ET.tostring(ET.fromstring(rendered(declared)['config.xml']))


def test_the_merge_replaces_the_rendered_parts_and_keeps_the_instances_devices_and_folders(
    tcp_only: Recorder, tmp_path: Path, merge_path: str
) -> None:
    """The options and the interface are the rendered ones whole; every device and folder the instance holds stays.

    With no device retired, the default device the rendered file declares,
    whose ID is empty, stays too.
    """
    result = merged(tcp_only, tmp_path, merge_path, kept=KEPT_CONFIG)
    source = ET.fromstring(rendered(tcp_only)['config.xml'])
    kept = ET.fromstring(KEPT_CONFIG)

    for part in ('gui', 'options', 'defaults'):
        found, expected = result.find(part), source.find(part)
        assert found is not None
        assert expected is not None
        assert ET.tostring(found) == ET.tostring(expected), part
    assert device_ids(result) == device_ids(kept)
    assert {it.get('id'): device_ids(it) for it in result.findall('folder')} == {
        it.get('id'): device_ids(it) for it in kept.findall('folder')
    }


def test_the_merge_drops_a_retired_device_from_the_devices_and_from_every_folder(
    declared: Recorder, tmp_path: Path, merge_path: str
) -> None:
    """Each of them, including from a folder one was the only device on, which the configuration holds as one element.

    Two are retired, so the list the component hands the script is split
    where the component joined it.
    """
    result = merged(declared, tmp_path, merge_path, kept=KEPT_CONFIG)

    assert device_ids(result) == [SELF, KEPT]
    assert {it.get('id'): device_ids(it) for it in result.findall('folder')} == {
        'shared': [SELF, KEPT],
        'alone': [],
    }
    defaults = result.find('defaults')
    assert defaults is not None
    assert device_ids(defaults) == ['']


@pytest.mark.parametrize(
    'kept',
    [
        pytest.param('', id='empty'),
        pytest.param(' \n\n  \n', id='whitespace'),
        pytest.param('<configuration/>\n', id='no-device'),
        pytest.param(KEPT_CONFIG[: KEPT_CONFIG.index('<device id=')], id='truncated'),
    ],
)
def test_the_merge_refuses_a_configuration_holding_no_device_and_leaves_it_as_it_was(
    declared: Recorder, tmp_path: Path, merge_path: str, kept: str
) -> None:
    """yq reads a file cut short or emptied as nothing, and a merge over it would keep no device and no folder.

    The init container fails instead, so the pod does not start on a
    configuration that shares nothing, and the file is left for whoever
    restores it.
    """
    ran, home = run_merge(declared, tmp_path, merge_path, kept)

    assert ran.returncode != 0
    assert (home / 'config.xml').read_text() == kept
    assert sorted(it.name for it in home.iterdir()) == ['config.xml']
