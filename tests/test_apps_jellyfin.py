"""Jellyfin on the worker, declared against mocks.

Every row here is the test's own: an interface route held while the legacy
VPS keeps its name, on `internet` and on `media`, and a discovery port heard
on the home segments, each under a name no census row carries, with a single
sign-on client under the route's name. So what is held is how the rows a
component is handed decide what it declares, never what the census happens to
say. The server's pin is the test's own too.

Literals written here are the upstream projects' contracts: the elements of
Jellyfin's network configuration (`NetworkConfiguration.cs` at v10.11.4) and
the environment its server reads its directories and published address from,
the font directory its image scans, Kubernetes' Pod Security label, and the Gateway API's spellings.
"""

from __future__ import annotations

import ipaddress
import re
import xml.etree.ElementTree as ET
from ipaddress import IPv4Network, IPv6Network
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps import jellyfin as jellyfin_app
from kluster.components.apps.jellyfin import Jellyfin
from kluster.components.cilium import InternetPoolMembers
from kluster.components.gpu import GPU_RESOURCE
from kluster.conventions.backup import BACKED_NAMESPACES, BackupFamily
from kluster.conventions.cluster import Transports
from kluster.conventions.routes import (
    PROXIED,
    REWRITE,
    Apex,
    Balancer,
    Exposure,
    Heard,
    Held,
    Host,
    Port,
    Route,
    RouteRow,
)
from kluster.conventions.sso import Client, Factor, Grant, Scope, TokenEndpointAuth
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
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

ROUTE = Route('jf-tube', internet=PROXIED, media=REWRITE)
DISCOVERY = Port('jf-find', 7359, Transports.UDP, media=Heard())
#: The route held, as it is while the legacy VPS keeps the name.
TUBE = Exposure(routes=(Held(ROUTE),), ports=(DISCOVERY,))
CLIENT = Client(
    'jf',
    'jf-tube',
    ('/sso/OID/redirect/Authelia',),
    policy=(Grant('jf-users', Factor.ONE),),
    token_endpoint_auth=TokenEndpointAuth.POST,
    scopes=(Scope.OPENID, Scope.PROFILE),
)

IMAGE = ImagePin('registry.example.invalid/jellyfin', 'v9.9.9', f'sha256:{"1" * 64}')

#: A directory the image's fontconfig scans for fonts (its `/etc/fonts/fonts.conf`).
FONTCONFIG_DIR = '/usr/local/share/fonts'

#: Every element Jellyfin's network configuration holds, as its class names
#: them (`MediaBrowser.Common/Net/NetworkConfiguration.cs` at v10.11.4).
NETWORK_ELEMENTS = frozenset(
    {
        'BaseUrl',
        'EnableHttps',
        'RequireHttps',
        'CertificatePath',
        'CertificatePassword',
        'InternalHttpPort',
        'InternalHttpsPort',
        'PublicHttpPort',
        'PublicHttpsPort',
        'AutoDiscovery',
        'EnableUPnP',
        'EnableIPv4',
        'EnableIPv6',
        'EnableRemoteAccess',
        'LocalNetworkSubnets',
        'LocalNetworkAddresses',
        'KnownProxies',
        'IgnoreVirtualInterfaces',
        'VirtualInterfaceNames',
        'EnablePublishedServerUriByRequest',
        'PublishedServerUriBySubnet',
        'RemoteIPFilter',
        'IsRemoteIPFilterBlacklist',
    }
)


def writer_key(namespace: str) -> dict[str, str]:
    """A namespace's sealed writer key, its ciphertext naming the namespace."""
    return {'AWS_ACCESS_KEY_ID': f'{namespace}-id', 'AWS_SECRET_ACCESS_KEY': f'{namespace}-secret'}


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The server, as the `apps` program declares it."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Jellyfin(
            'jellyfin',
            exposure=TUBE,
            client=CLIENT,
            image=IMAGE,
            writer_key=writer_key(jellyfin_app.NAME),
            fronts=FRONTS,
            zone_ids=ZONE_IDS,
        )
    return monitor


def in_namespace(monitor: Recorder, typ: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata'].get('namespace') == jellyfin_app.NAME]


def pod_of(monitor: Recorder) -> dict[str, Any]:
    (deployment,) = in_namespace(monitor, DEPLOYMENT)
    return deployment['spec']['template']


def container(pod: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [it for it in [*pod['spec'].get('initContainers', []), *pod['spec']['containers']] if it['name'] == name]
    return found


def server(monitor: Recorder) -> dict[str, Any]:
    return container(pod_of(monitor), jellyfin_app.NAME)


def environment(monitor: Recorder) -> dict[str, str]:
    return {it['name']: it['value'] for it in server(monitor)['env']}


def mounts(pod: dict[str, Any], container_name: str) -> dict[str, dict[str, Any]]:
    """Each volume a container mounts, by the path it is mounted at, with the mount itself."""
    volumes = {volume['name']: volume for volume in pod['spec']['volumes']}
    return {
        mount['mountPath']: {'volume': volumes[mount['name']], 'mount': mount}
        for mount in container(pod, container_name)['volumeMounts']
    }


def claim_at(pod: dict[str, Any], container_name: str, path: str) -> str:
    return mounts(pod, container_name)[path]['volume']['persistentVolumeClaim']['claimName']


def nas_volume(monitor: Recorder, claim_name: str) -> dict[str, Any]:
    (volume,) = [
        it.inputs
        for it in monitor.of_type(VOLUME)
        if it.inputs['spec']['claimRef'] == {'namespace': jellyfin_app.NAME, 'name': claim_name}
    ]
    return volume


def interface_service(monitor: Recorder) -> dict[str, Any]:
    (service,) = [it for it in in_namespace(monitor, SERVICE) if it['spec'].get('type') != 'LoadBalancer']
    return service


def network_config(monitor: Recorder) -> ET.Element:
    (config_map,) = in_namespace(monitor, CONFIG_MAP)
    (text,) = config_map['data'].values()
    return ET.fromstring(text)


def strings(element: ET.Element, name: str) -> list[str]:
    found = element.find(name)
    assert found is not None, name
    return [it.text or '' for it in found.findall('string')]


def text(element: ET.Element, name: str) -> str:
    found = element.find(name)
    assert found is not None, name
    return found.text or ''


# -- the pod ---------------------------------------------------------------------


def test_the_pod_runs_as_a_numeric_user_that_is_not_root_and_escalates_nothing(declared: Recorder) -> None:
    """The image names no user, and `restricted`, which the namespace is held to, refuses a pod that does not state one."""
    (namespace,) = [it.inputs for it in declared.of_type(NAMESPACE)]
    spec = pod_of(declared)['spec']
    context = spec['securityContext']

    assert namespace['metadata']['name'] == jellyfin_app.NAME
    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    assert context['runAsNonRoot'] is True
    # The mock hands numbers back as floats, so the user is held to a whole one.
    assert context['runAsUser'] == int(context['runAsUser']) > 0
    assert context['seccompProfile'] == {'type': 'RuntimeDefault'}
    for each in [*spec['initContainers'], *spec['containers']]:
        assert each['securityContext']['allowPrivilegeEscalation'] is False, each['name']
        assert each['securityContext']['capabilities'] == {'drop': ['ALL']}, each['name']


def test_each_container_runs_the_pin_it_is_handed(declared: Recorder) -> None:
    spec = pod_of(declared)['spec']

    assert {it['image'] for it in [*spec['initContainers'], *spec['containers']]} == {str(IMAGE)}


def test_each_directory_a_mount_inside_the_data_claim_needs_is_made_by_the_pods_user(declared: Recorder) -> None:
    """The runtime creates a missing parent of a mount as root, so each one inside the claim is made by an `install -d` first.

    The server writes into every such directory, its database's among them,
    and as root's it cannot.
    """
    pod = pod_of(declared)
    data = PurePosixPath(environment(declared)['JELLYFIN_DATA_DIR'])
    nested = [PurePosixPath(it) for it in mounts(pod, jellyfin_app.NAME) if PurePosixPath(it).is_relative_to(data)]
    needed = {parent for path in nested for parent in path.parents if parent.is_relative_to(data) and parent != data}
    made = {
        PurePosixPath(argument)
        for init in pod['spec']['initContainers']
        if init['command'][:2] == ['install', '-d']
        for argument in init['command'][2:]
        if argument.startswith('/')
    }

    assert needed
    assert needed <= made


def test_each_probe_asks_a_port_the_container_listens_on(declared: Recorder) -> None:
    serving = server(declared)
    named = {it['name'] for it in serving['ports']}

    for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
        assert serving[probe]['httpGet']['port'] in named, probe


def test_the_pod_is_handed_no_variable_under_the_servers_prefix_but_its_own(declared: Recorder) -> None:
    """Service links would hand it `JELLYFIN_*` variables for its own Service, a prefix the server reads settings from."""
    assert pod_of(declared)['spec']['enableServiceLinks'] is False


def test_the_server_never_runs_beside_itself(declared: Recorder) -> None:
    """Its database is one process's and the GPU one device, so a rollout stops the old pod before it starts the new one."""
    (deployment,) = in_namespace(declared, DEPLOYMENT)

    assert deployment['spec']['strategy']['type'] == 'Recreate'


def test_the_server_is_given_one_gpu(declared: Recorder) -> None:
    resources = server(declared)['resources']

    assert resources['requests'][GPU_RESOURCE] == resources['limits'][GPU_RESOURCE]
    assert int(resources['limits'][GPU_RESOURCE]) == 1


def test_the_pod_runs_on_the_node_its_nas_volumes_are_reachable_from(declared: Recorder) -> None:
    """The pod states the placement the NAS volumes' node affinity forces, so it is placed by its own declaration."""
    terms = pod_of(declared)['spec']['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
    nas = [it.inputs for it in declared.of_type(VOLUME)]

    assert nas
    for volume in nas:
        assert volume['spec']['nodeAffinity']['required'] == terms


# -- the exposure ------------------------------------------------------------------


def test_the_route_attaches_at_each_gateway_its_row_sets_and_reaches_the_server_ungated(declared: Recorder) -> None:
    """The server signs its users in itself, so neither route asks the portal, and each forwards to the HTTP port."""
    routes = in_namespace(declared, HTTP_ROUTE)
    service = interface_service(declared)
    pod = pod_of(declared)
    (port,) = service['spec']['ports']
    named = {it['name']: it['containerPort'] for it in server(declared)['ports']}

    assert sorted(ref['name'] for route in routes for ref in route['spec']['parentRefs']) == sorted(
        [conventions.GATEWAY_INTERNET, conventions.GATEWAY_MEDIA]
    )
    for route in routes:
        (rule,) = route['spec']['rules']
        assert route['spec']['hostnames'] == [f'{ROUTE.host}.{conventions.ZONE_PRIMARY}']
        assert [it for it in rule['filters'] if it['type'] == 'ExternalAuth'] == []
        assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]
    assert service['spec']['selector'].items() <= pod['metadata']['labels'].items()
    assert named[port['targetPort']] == port['port']


def test_the_discovery_ports_service_and_allow_reach_the_server_on_udp(declared: Recorder) -> None:
    """The heard side is served at the media VIP, where the relay sends each broadcast, and the world is admitted to it."""
    pod = pod_of(declared)
    labels = pod['metadata']['labels']
    listening = {(it['containerPort'], it['protocol']) for it in server(declared)['ports']}
    (service,) = [it for it in in_namespace(declared, SERVICE) if it['spec'].get('type') == 'LoadBalancer']
    (allow,) = [it for it in in_namespace(declared, POLICY) if it['metadata']['name'] == f'{DISCOVERY.name}-world']
    (admitted,) = allow['spec']['ingress']

    assert service['spec']['selector'].items() <= labels.items()
    assert {(it['targetPort'], it['protocol']) for it in service['spec']['ports']} == {(DISCOVERY.port, 'UDP')}
    assert (DISCOVERY.port, 'UDP') in listening
    assert allow['spec']['endpointSelector']['matchLabels'].items() <= labels.items()
    assert admitted['toPorts'] == [{'ports': [{'port': str(DISCOVERY.port), 'protocol': 'UDP'}]}]


def test_discovery_hands_a_client_the_routes_name_over_https(declared: Recorder) -> None:
    """A client dials the address the reply names, which the Gateway on its segment serves at 443."""
    published = urlsplit(environment(declared)['JELLYFIN_PublishedServerUrl'])
    hostnames = {name for route in in_namespace(declared, HTTP_ROUTE) for name in route['spec']['hostnames']}

    assert published.scheme == 'https'
    assert published.port is None
    assert published.path == ''
    assert published.hostname in hostnames


# -- the network configuration ---------------------------------------------------


def test_the_server_reads_the_network_configuration_the_init_container_installs(declared: Recorder) -> None:
    """The file lands in the directory the server reads its settings from, on the claim both containers mount there.

    The rendered file is the ConfigMap's, mounted where the init container
    reads it.
    """
    pod = pod_of(declared)
    # The init container that mounts the ConfigMap. It is autonamed, a name
    # the mock never computes, so the volume is held to being the namespace's
    # one ConfigMap.
    (init,) = [
        it
        for it in pod['spec']['initContainers']
        if any('configMap' in mounted['volume'] for mounted in mounts(pod, it['name']).values())
    ]
    *_, source, target = init['command']
    settings = environment(declared)['JELLYFIN_CONFIG_DIR']
    data = environment(declared)['JELLYFIN_DATA_DIR']
    (config_map,) = in_namespace(declared, CONFIG_MAP)
    rendered = mounts(pod, init['name'])[str(PurePosixPath(source).parent)]['volume']

    assert target == f'{settings}/network.xml'
    assert PurePosixPath(settings).is_relative_to(data)
    assert claim_at(pod, init['name'], data) == claim_at(pod, jellyfin_app.NAME, data)
    assert 'configMap' in rendered
    assert PurePosixPath(source).name in config_map['data']


def test_the_network_configuration_states_only_what_the_server_reads(declared: Recorder) -> None:
    """An element the server's class does not name is dropped in silence when the server reads the file."""
    config = network_config(declared)

    assert config.tag == 'NetworkConfiguration'
    assert {it.tag for it in config} <= NETWORK_ELEMENTS


def test_the_server_serves_plain_http_on_the_port_its_service_forwards_to(declared: Recorder) -> None:
    """The Gateway terminates TLS for the name, so the server neither serves HTTPS nor redirects to it."""
    config = network_config(declared)
    (port,) = interface_service(declared)['spec']['ports']
    named = {it['name']: it['containerPort'] for it in server(declared)['ports']}

    assert int(text(config, 'InternalHttpPort')) == named[port['targetPort']]
    assert text(config, 'EnableHttps') == 'false'
    assert text(config, 'RequireHttps') == 'false'


def test_the_server_trusts_what_the_gateways_forward_from_the_pod_ranges(declared: Recorder) -> None:
    """Envoy reaches the pod from an address in the pod ranges, and the server reads a client from `X-Forwarded-For` only behind a known proxy.

    Without it every client is the Gateway's private address, which the
    server counts as its local network.
    """
    proxies = [ipaddress.ip_network(it) for it in strings(network_config(declared), 'KnownProxies')]
    v4 = [it for it in proxies if isinstance(it, IPv4Network)]
    v6 = [it for it in proxies if isinstance(it, IPv6Network)]

    assert any(conventions.POD_CIDR_V4.subnet_of(it) for it in v4)
    assert any(conventions.POD_CIDR_V6.subnet_of(it) for it in v6)


def test_the_server_answers_discovery_and_clients_from_outside(declared: Recorder) -> None:
    config = network_config(declared)

    assert text(config, 'AutoDiscovery') == 'true'
    assert text(config, 'EnableRemoteAccess') == 'true'


# -- the storage -----------------------------------------------------------------


def test_the_namespace_is_one_whose_writer_key_is_sealed() -> None:
    """It declares a claim backed up by VolSync, whose repository Secret carries the namespace's sealed writer key."""
    assert BackupFamily.VOLSYNC in BACKED_NAMESPACES[jellyfin_app.NAME]


def test_the_data_directory_is_a_backed_claim_read_by_a_mover_as_the_server(declared: Recorder) -> None:
    pod = pod_of(declared)
    data = claim_at(pod, jellyfin_app.NAME, environment(declared)['JELLYFIN_DATA_DIR'])
    (source,) = in_namespace(declared, SOURCE)
    node_claims = {it['metadata']['name'] for it in in_namespace(declared, CLAIM)} - {
        it.inputs['spec']['claimRef']['name'] for it in declared.of_type(VOLUME)
    }

    assert node_claims == {data}
    assert source['spec']['sourcePVC'] == data
    assert source['spec']['restic']['moverSecurityContext']['runAsUser'] == pod['spec']['securityContext']['runAsUser']


def test_the_metadata_and_scrub_previews_are_on_the_nas_where_the_server_keeps_them(declared: Recorder) -> None:
    """Each is a directory of its own under one writable NAS path, mounted under the server's data directory."""
    pod = pod_of(declared)
    mounted = mounts(pod, jellyfin_app.NAME)
    data = environment(declared)['JELLYFIN_DATA_DIR']
    kept = [mounted[it] for it in (jellyfin_app.METADATA, jellyfin_app.TRICKPLAY)]
    (claim_name,) = {it['volume']['persistentVolumeClaim']['claimName'] for it in kept}
    volume = nas_volume(declared, claim_name)

    for path in (jellyfin_app.METADATA, jellyfin_app.TRICKPLAY):
        assert PurePosixPath(path).is_relative_to(data)
    assert len({it['mount']['subPath'] for it in kept}) == 2
    assert all(not it['mount'].get('readOnly', False) for it in kept)
    assert volume['spec']['nfs']['path'] == f'{conventions.NAS_EXPORT}/{jellyfin_app.NAS_DERIVED}'
    assert volume['spec']['accessModes'] == ['ReadWriteMany']


def test_the_library_is_the_exports_media_mounted_read_only(declared: Recorder) -> None:
    pod = pod_of(declared)
    library = mounts(pod, jellyfin_app.NAME)[jellyfin_app.MEDIA]
    volume = nas_volume(declared, library['volume']['persistentVolumeClaim']['claimName'])

    assert library['mount']['readOnly'] is True
    assert volume['spec']['nfs']['path'] == f'{conventions.NAS_EXPORT}/{jellyfin_app.LIBRARY}'
    assert volume['spec']['nfs']['readOnly'] is True


def test_the_cache_is_a_bounded_empty_dir_where_the_server_keeps_it(declared: Recorder) -> None:
    """Transcodes are written there, which a restart loses nothing by; the bound evicts the pod before the node's disk fills."""
    cache = mounts(pod_of(declared), jellyfin_app.NAME)[environment(declared)['JELLYFIN_CACHE_DIR']]['volume']
    bound = re.fullmatch(r'(\d+)Gi', cache['emptyDir']['sizeLimit'])

    assert bound is not None
    assert 0 < int(bound[1]) < conventions.HOMELAB_DISK_GIB


def test_custom_fonts_are_read_from_the_data_claim_where_fontconfig_scans(declared: Recorder) -> None:
    """Fonts kept beside the server's data, as the legacy server kept them, reach its subtitle rendering."""
    pod = pod_of(declared)
    data = claim_at(pod, jellyfin_app.NAME, environment(declared)['JELLYFIN_DATA_DIR'])
    fonts = [
        mounted
        for path, mounted in mounts(pod, jellyfin_app.NAME).items()
        if PurePosixPath(path).is_relative_to(FONTCONFIG_DIR)
    ]

    assert [(it['volume']['persistentVolumeClaim']['claimName'], it['mount']['readOnly']) for it in fonts] == [
        (data, True)
    ]


# -- refusals ----------------------------------------------------------------------


def declare_jellyfin(**changed: Any) -> None:
    arguments: dict[str, Any] = {
        'exposure': TUBE,
        'client': CLIENT,
        'image': IMAGE,
        'writer_key': writer_key(jellyfin_app.NAME),
        'fronts': FRONTS,
        'zone_ids': ZONE_IDS,
    } | changed
    _ = Jellyfin('jellyfin', **arguments)


@pytest.mark.parametrize(
    'routes',
    [
        pytest.param((), id='none'),
        pytest.param((ROUTE, Route('jf-other', internet=PROXIED)), id='two'),
    ],
)
def test_an_exposure_without_exactly_one_route_is_refused(routes: tuple[RouteRow, ...]) -> None:
    exposure = Exposure(routes=routes, ports=(DISCOVERY,))
    with pytest.raises(ValueError, match='one name'):
        declare_jellyfin(exposure=exposure)


def test_an_apex_in_place_of_the_route_is_refused() -> None:
    with pytest.raises(ValueError, match='route row'):
        declare_jellyfin(exposure=Exposure(routes=(Apex((conventions.ZONE_PRIMARY,)),), ports=(DISCOVERY,)))


def test_a_route_published_in_two_zones_is_refused() -> None:
    """Discovery replies with one address, so a second zone is a name no client is told."""
    route = Route('jf-tube', internet=PROXIED, zones=(conventions.ZONE_PRIMARY, conventions.ZONE_SHORT))
    with pytest.raises(ValueError, match='one address'):
        declare_jellyfin(exposure=Exposure(routes=(route,), ports=(DISCOVERY,)))


@pytest.mark.parametrize(
    'ports',
    [
        pytest.param((), id='none'),
        pytest.param((DISCOVERY, Port('jf-ssdp', 1900, Transports.UDP, media=Heard())), id='two'),
    ],
)
def test_an_exposure_without_exactly_one_raw_port_is_refused(ports: tuple[Port, ...]) -> None:
    with pytest.raises(ValueError, match='one raw port'):
        declare_jellyfin(exposure=Exposure(routes=(ROUTE,), ports=ports))


@pytest.mark.parametrize(
    'port',
    [
        pytest.param(Port('jf-find', 7360, Transports.UDP, media=Heard()), id='another-port'),
        pytest.param(Port('jf-find', 7359, Transports.TCP, internet=Balancer((Host('jf-find'),))), id='tcp'),
    ],
)
def test_a_discovery_port_the_server_does_not_listen_on_is_refused(port: Port) -> None:
    with pytest.raises(ValueError, match='listens for discovery'):
        declare_jellyfin(exposure=Exposure(routes=(ROUTE,), ports=(port,)))


@pytest.mark.parametrize(
    'client',
    [
        pytest.param(
            Client(
                'jf',
                'jf-elsewhere',
                CLIENT.paths,
                policy=CLIENT.policy,
                token_endpoint_auth=CLIENT.token_endpoint_auth,
                scopes=CLIENT.scopes,
            ),
            id='another-host',
        ),
        pytest.param(
            Client(
                'jf',
                CLIENT.host,
                CLIENT.paths,
                policy=CLIENT.policy,
                token_endpoint_auth=CLIENT.token_endpoint_auth,
                scopes=CLIENT.scopes,
                zone=conventions.ZONE_SHORT,
            ),
            id='another-zone',
        ),
    ],
)
def test_a_client_whose_redirects_are_under_another_name_is_refused(client: Client) -> None:
    """The portal sends a browser back to its client's host alone, so a sign-in through any other name fails."""
    with pytest.raises(ValueError, match='redirects to'):
        declare_jellyfin(client=client)
