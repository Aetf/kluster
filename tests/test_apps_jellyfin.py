"""Jellyfin, declared against mocks, and its network configuration's merge and server run as the pod runs them.

Every row here is the test's own: an interface route held while the legacy
VPS keeps its name, on `internet` and on `media`, and a discovery port heard
on the home segments, each under a name no census row carries, with a single
sign-on client under the route's name. So what is held is how the rows a
component is handed decide what it declares, never what the census happens to
say. The server's pin is the test's own too.

The cases that run the merge, and the server after it, run the image
`Pulumi.yaml` pins, with podman, as the pod runs it: the declared command,
user, group and environment, with every capability dropped and no network,
the claim and the rendered files' mount each a scratch filesystem the case
lays the files out in. The server's callers ask from a container of the curl
image `Pulumi.yaml` pins, which holds the network the server's container
joins and gives it the callers' addresses. They run as `pinned_image` has it:
skipped where podman is not on `PATH`, as in a cloud session, and failed on
CI's runner, which has podman; each image pulled by its digest; and every
container carrying `--rm`, a `--timeout` and the module's label
(framework/testing.md §1).

Literals written here are the upstream projects' contracts: the elements of
Jellyfin's network configuration (`NetworkConfiguration.cs` at v10.11.4, and
one of v10.8.13's that v10.11.4's no longer holds), the shape its serializer
writes the file in and the byte order mark its migration writes ahead of it,
the answers of its health check and the lines it logs its
network settings with, the `X-Forwarded-For` header it judges a forwarded
request by, an address of Cloudflare's edge, the environment its server reads its directories and
published address from, the font directory its image scans, Kubernetes' Pod
Security label, and the Gateway API's spellings.
"""

from __future__ import annotations

import io
import ipaddress
import json
import re
import shlex
import subprocess
import tarfile
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import process_sessions
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pinned_image import (
    COMMAND_TIMEOUT,
    CONTAINER_TIMEOUT,
    LARGE_PULL_TIMEOUT,
    PULL_TIMEOUT,
    pinned,
    pulled,
    require_podman,
)

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
from kluster.conventions.sso import Client, Factor, Grant, Issuer, Scope, TokenEndpointAuth
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
#: The portal's issuer, as the program would hand it.
ISSUER = Issuer('https://auth.example.invalid')

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
            issuer=ISSUER,
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


def network_init(pod: dict[str, Any]) -> dict[str, Any]:
    """The init container that mounts the ConfigMap.

    The ConfigMap is autonamed, a name the mock never computes, so the volume
    is held to being the namespace's one ConfigMap.
    """
    (init,) = [
        it
        for it in pod['spec']['initContainers']
        if any('configMap' in mounted['volume'] for mounted in mounts(pod, it['name']).values())
    ]
    return init


def rendered_files(monitor: Recorder) -> dict[str, str]:
    """The files the ConfigMap holds, by name."""
    (config_map,) = in_namespace(monitor, CONFIG_MAP)
    return dict(config_map['data'])


def rendered_network(monitor: Recorder) -> str:
    """The rendered network configuration, the file the init container merges from."""
    *_, source = network_init(pod_of(monitor))['command']
    return rendered_files(monitor)[PurePosixPath(source).name]


def network_config(monitor: Recorder) -> ET.Element:
    return ET.fromstring(rendered_network(monitor))


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


def test_the_pod_runs_at_the_held_priority(declared: Recorder) -> None:
    """Its claim and the GPU it requests each keep it to one node, so it outranks free pods there."""
    assert pod_of(declared)['spec']['priorityClassName'] == conventions.HELD_PRIORITY_CLASS


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


def test_the_server_reads_the_network_configuration_the_init_container_merges_into(declared: Recorder) -> None:
    """The file lands in the directory the server reads its settings from, on the claim both containers mount there.

    The merge and the rendered file are the ConfigMap's, mounted where the
    init container reads them.
    """
    pod = pod_of(declared)
    init = network_init(pod)
    shell, script, target, source = init['command']
    settings = environment(declared)['JELLYFIN_CONFIG_DIR']
    data = environment(declared)['JELLYFIN_DATA_DIR']
    files = rendered_files(declared)

    assert shell == '/bin/sh'
    assert target == f'{settings}/network.xml'
    assert PurePosixPath(settings).is_relative_to(data)
    assert claim_at(pod, init['name'], data) == claim_at(pod, jellyfin_app.NAME, data)
    for path in (script, source):
        mounted = mounts(pod, init['name'])[str(PurePosixPath(path).parent)]
        assert 'configMap' in mounted['volume'], path
        assert mounted['mount']['readOnly'] is True, path
        assert PurePosixPath(path).name in files, path


def test_the_network_configuration_states_only_what_the_server_reads(declared: Recorder) -> None:
    """An element the server's class does not name is dropped in silence when the server reads the file."""
    config = network_config(declared)

    assert config.tag == 'NetworkConfiguration'
    assert {it.tag for it in config} <= NETWORK_ELEMENTS


def test_the_server_serves_plain_http_on_the_port_its_service_forwards_to(declared: Recorder) -> None:
    """The Gateway terminates TLS for the name, so the server serves no HTTPS, and redirects to it only while it does."""
    config = network_config(declared)
    (port,) = interface_service(declared)['spec']['ports']
    named = {it['name']: it['containerPort'] for it in server(declared)['ports']}

    assert int(text(config, 'InternalHttpPort')) == named[port['targetPort']]
    assert text(config, 'EnableHttps') == 'false'


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


def test_the_server_answers_a_client_from_outside_whatever_its_address(declared: Recorder) -> None:
    """Behind the Gateway a request is judged by the address Envoy was handed, Cloudflare's edge's for the internet route, which no filter written for the old network names."""
    assert strings(network_config(declared), 'RemoteIPFilter') == []


def test_the_server_answers_at_the_root_of_the_name(declared: Recorder) -> None:
    """The route, the probes and the single sign-on client's redirect addresses are under no base path."""
    assert text(network_config(declared), 'BaseUrl') == ''


def test_the_server_binds_every_ipv4_address_of_the_pod(declared: Recorder) -> None:
    """A bind address the file names is one of the host it came from, and the Service and the probes reach the pod on IPv4."""
    config = network_config(declared)

    assert strings(config, 'LocalNetworkAddresses') == []
    assert text(config, 'EnableIPv4') == 'true'


# -- the merge, and the server after it, as the pod runs them -----------------------

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=test_apps_jellyfin`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why the cases that run the pod's containers need podman.
WHY = "the pod's containers are run from the pinned image with it"

#: The prefix of the variables a container is handed the files it lays out in.
FILE_VARIABLE = 'KLUSTER_TEST_FILE_'

#: The pin of the container the server's callers ask from: curl, beside a
#: busybox whose `ip` gives its network the callers' addresses. The pin is a
#: multi-platform index, so `pulled` takes the host's own platform from it.
PROBER = 'auth-canary'

#: The bound on a case that runs the merge, which can make the first pull of
#: the server's image, a download over half a gigabyte: the pull, the check
#: before it, and the merge's container. A stop-loss, which nothing asserts on.
MERGE_CASE_TIMEOUT = LARGE_PULL_TIMEOUT + 2 * COMMAND_TIMEOUT

#: The bound on the case that starts the server: the server's image's pull,
#: the prober's, which is small, the merge, the server's start, which takes
#: seconds, and the probes. A stop-loss, which nothing asserts on.
SERVER_CASE_TIMEOUT = LARGE_PULL_TIMEOUT + PULL_TIMEOUT + 5 * COMMAND_TIMEOUT + 60

#: A network configuration the server keeps, in the shape its serializer writes
#: it: every element the component owns but `AutoDiscovery` at a value of the
#: host it came from, a remote allowlist written for that host's network among
#: them; settings the component leaves to the dashboard, the local subnets
#: among them; and an element of v10.8.13's class that v10.11.4's no longer
#: holds.
KEPT = """<?xml version="1.0" encoding="utf-8"?>
<NetworkConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <BaseUrl>/jf</BaseUrl>
  <EnableHttps>true</EnableHttps>
  <RequireHttps>true</RequireHttps>
  <CertificatePath>/config/ssl/jf.pfx</CertificatePath>
  <CertificatePassword />
  <InternalHttpPort>8097</InternalHttpPort>
  <EnableIPv4>false</EnableIPv4>
  <EnableRemoteAccess>false</EnableRemoteAccess>
  <LocalNetworkSubnets>
    <string>192.0.2.0/24</string>
  </LocalNetworkSubnets>
  <LocalNetworkAddresses>
    <string>198.51.100.7</string>
  </LocalNetworkAddresses>
  <KnownProxies>
    <string>198.51.100.1</string>
  </KnownProxies>
  <RemoteIPFilter>
    <string>203.0.113.0/24</string>
  </RemoteIPFilter>
  <IsRemoteIPFilterBlacklist>false</IsRemoteIPFilterBlacklist>
  <HDHomerunPortRange>65001-65100</HDHomerunPortRange>
</NetworkConfiguration>
"""

#: The UTF-8 byte order mark the server's network-configuration migration
#: writes ahead of the declaration (`MigrateNetworkConfiguration`, through an
#: `XmlWriter` whose encoding is UTF-8 with its preamble).
BOM = '\N{ZERO WIDTH NO-BREAK SPACE}'

#: The subnets `KEPT` names as its local network.
KEPT_SUBNETS = strings(ET.fromstring(KEPT), 'LocalNetworkSubnets')

#: An address of Cloudflare's edge, which the internet route reaches the
#: Gateway from and which `KEPT`'s allowlist leaves out.
CLOUDFLARE_EDGE = '172.68.10.20'


def shape(element: ET.Element) -> tuple[str, str, list[Any]]:
    """An element as the server reads it: its name, its text, and each child's shape, whatever its indentation."""
    return element.tag, (element.text or '').strip(), [shape(it) for it in element]


def lay_out(files: Mapping[str, str]) -> tuple[list[str], str]:
    """The `podman run` options handing a container `files`, by path, and the shell lines that write each at its path."""
    options = [f'--env={FILE_VARIABLE}{n}={contents}' for n, contents in enumerate(files.values())]
    lines = [
        f'mkdir -p {shlex.quote(str(PurePosixPath(path).parent))} && printf %s "${FILE_VARIABLE}{n}" >{shlex.quote(path)}'
        for n, path in enumerate(files)
    ]
    return options, '\n'.join(lines)


def run_options(monitor: Recorder, name: str, mounted: Sequence[str], *, network: str = 'none') -> list[str]:
    """The options of a `podman run` of the pod's image as the pod runs its containers, with a scratch filesystem at each of `mounted`."""
    context = pod_of(monitor)['spec']['securityContext']
    return [
        '--rm',
        f'--timeout={CONTAINER_TIMEOUT}',
        f'--label={LABEL}',
        process_sessions.LOG_TO_FILE,
        f'--name={name}',
        f'--network={network}',
        '--cap-drop=all',
        '--security-opt=no-new-privileges',
        f'--user={int(context["runAsUser"])}:{int(context["runAsGroup"])}',
        *(f'--mount=type=tmpfs,destination={path},tmpfs-mode=1777' for path in mounted),
    ]


def network_files(monitor: Recorder, kept: str | None) -> dict[str, str]:
    """What the network init container finds on its mounts: the ConfigMap's files, and `kept` as the server's file where it is not `None`."""
    pod = pod_of(monitor)
    init = network_init(pod)
    *_, target, _ = init['command']
    (rendered_mount,) = [path for path, it in mounts(pod, init['name']).items() if 'configMap' in it['volume']]
    files = {f'{rendered_mount}/{name}': contents for name, contents in rendered_files(monitor).items()}
    return files | ({} if kept is None else {target: kept})


def run_merge(monitor: Recorder, kept: str | None) -> tuple[subprocess.CompletedProcess[bytes], dict[str, bytes]]:
    """The declared network init container run over `kept`, or over none, and each file of the directory it writes, by name."""
    require_podman(WHY)
    reference = pulled(pinned('jellyfin'), timeout=LARGE_PULL_TIMEOUT)
    pod = pod_of(monitor)
    init = network_init(pod)
    *_, target, _ = init['command']
    options, layout = lay_out(network_files(monitor, kept))
    name = f'kluster-test-jellyfin-{uuid.uuid4().hex[:12]}'
    # The merge's output goes to stderr, so stdout carries the directory alone.
    script = '\n'.join(
        [
            'set -e',
            layout,
            'set +e',
            '"$@" >&2',
            'status=$?',
            f'tar -c -C {shlex.quote(str(PurePosixPath(target).parent))} .',
            'exit "$status"',
        ]
    )

    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        ran = process_sessions.run(
            [
                'podman',
                'run',
                *run_options(monitor, name, list(mounts(pod, init['name']))),
                *options,
                '--entrypoint=/bin/sh',
                reference,
                '-c',
                script,
                'sh',
                *init['command'],
            ],
            timeout=COMMAND_TIMEOUT,
        )

    written: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(ran.stdout)) as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            if extracted is not None:
                written[PurePosixPath(member.name).name] = extracted.read()
    return ran, written


def merged(monitor: Recorder, kept: str) -> ET.Element:
    """The server's network configuration after the declared init container has run over `kept`."""
    ran, written = run_merge(monitor, kept)
    assert ran.returncode == 0, ran.stderr
    return ET.fromstring(written['network.xml'])


@pytest.fixture(scope='module', name='kept_merged')
def kept_merged_fixture(declared: Recorder) -> ET.Element:
    """The server's network configuration after the declared init container has run over `KEPT`."""
    return merged(declared, KEPT)


@pytest.mark.timeout(MERGE_CASE_TIMEOUT)
def test_a_server_with_no_network_configuration_takes_the_rendered_one_as_its_initial_state(
    declared: Recorder,
) -> None:
    ran, written = run_merge(declared, None)

    assert ran.returncode == 0, ran.stderr
    assert written == {'network.xml': rendered_network(declared).encode()}


@pytest.mark.timeout(MERGE_CASE_TIMEOUT)
def test_the_merge_sets_each_setting_the_component_owns(declared: Recorder, kept_merged: ET.Element) -> None:
    """Each element the rendered file holds is the merged file's one of that name, whether the server's file held it at another value or not at all."""
    kept = ET.fromstring(KEPT)

    for element in network_config(declared):
        assert [shape(it) for it in kept_merged.findall(element.tag)] == [shape(element)], element.tag
        assert [shape(it) for it in kept.findall(element.tag)] != [shape(element)], element.tag


@pytest.mark.timeout(MERGE_CASE_TIMEOUT)
def test_the_merge_reads_a_file_the_servers_migration_wrote_with_a_byte_order_mark(declared: Recorder) -> None:
    """The mark stays where it was, and each setting the component owns is set as in a file without one."""
    ran, written = run_merge(declared, BOM + KEPT)
    result = ET.fromstring(written['network.xml'])

    assert ran.returncode == 0, ran.stderr
    assert written['network.xml'].startswith(BOM.encode())
    for element in network_config(declared):
        assert [shape(it) for it in result.findall(element.tag)] == [shape(element)], element.tag


@pytest.mark.timeout(MERGE_CASE_TIMEOUT)
def test_the_merge_keeps_every_other_setting_where_the_server_had_it(
    declared: Recorder, kept_merged: ET.Element
) -> None:
    """A setting the dashboard set, and one this server no longer reads, stay as the server's file holds them, in its order."""
    owned = {it.tag for it in network_config(declared)}
    kept = [shape(it) for it in ET.fromstring(KEPT) if it.tag not in owned]

    assert [shape(it) for it in kept_merged if it.tag not in owned] == kept
    assert ('HDHomerunPortRange', '65001-65100', []) in kept
    assert ('LocalNetworkSubnets', '', [('string', KEPT_SUBNETS[0], [])]) in kept


@pytest.mark.timeout(MERGE_CASE_TIMEOUT)
@pytest.mark.parametrize(
    'kept',
    [
        pytest.param('', id='empty'),
        pytest.param(KEPT[: KEPT.index('<string>192.0.2.0/24') + len('<string>192.0')], id='cut-within-a-line'),
        pytest.param(KEPT[: KEPT.index('  <KnownProxies>')], id='cut-between-lines'),
    ],
)
def test_the_merge_refuses_a_file_cut_short_and_leaves_it_as_it_was(declared: Recorder, kept: str) -> None:
    """The server reads a file it cannot parse as no configuration, and a merge over one would write back only what the cut left.

    The init container fails instead, so the pod does not start, and the file
    is left for whoever restores it.
    """
    ran, written = run_merge(declared, kept)

    assert ran.returncode != 0
    assert written == {'network.xml': kept.encode()}


def asked(prober: str, url: str, source: ipaddress.IPv4Address, forwarded: str | None) -> list[str]:
    """What `url` answers `prober`'s curl from `source`, with `forwarded` as its `X-Forwarded-For`: the body's words, then the status."""
    header = [] if forwarded is None else ['-H', f'X-Forwarded-For: {forwarded}']
    return (
        process_sessions.run(
            ['podman', 'exec', prober, 'curl', '-s', '-w', ' %{http_code}', '--interface', str(source), *header, url],
            timeout=COMMAND_TIMEOUT,
        )
        .stdout.decode()
        .split()
    )


@pytest.mark.timeout(SERVER_CASE_TIMEOUT)
def test_the_server_answers_the_probe_and_every_route_through_the_filter_the_file_kept(declared: Recorder) -> None:
    """Run as the pod runs it after the merge over `KEPT`, the server answers the kubelet's probe and each request the Gateways forward.

    Each caller asks from an address of its own in the network the server's
    container shares: from loopback, or from the pod's own address, the
    server exempts a request from its filter. The kubelet probes from the
    address `cilium_host` holds, in the pod range, with no forwarded
    address. Envoy connects from the pod range and appends its peer to
    `X-Forwarded-For`, and the server, which trusts the pod ranges, judges
    the request by the address before Envoy's: Cloudflare's edge for the
    internet route, the client for one that dials the balancer, and a home
    client, on a subnet `KEPT` counts as local, for `media`. `KEPT`'s
    allowlist leaves out every address but the home client's, so a server
    that kept it would refuse both internet callers. `KEPT` also names
    another port, a base path, a bind address the pod does not have and IPv4
    off, so a server that read it unmerged would answer none. While it
    starts the server answers from a placeholder, which listens whatever the
    base path, so the callers ask once the server has logged its start
    complete, in the log directory its image names.

    The network settings the server logs are read from that same file. The
    server logs them while it builds its services, ahead of its start
    complete, and writes the file in the order it logs, so they are there
    by the time the callers ask.
    """
    require_podman(WHY)
    reference = pulled(pinned('jellyfin'), timeout=LARGE_PULL_TIMEOUT)
    prober_reference = pulled(pinned(PROBER))
    pod = pod_of(declared)
    init = network_init(pod)
    serving = server(declared)
    probe = serving['readinessProbe']['httpGet']
    (port,) = [it['containerPort'] for it in serving['ports'] if it['name'] == probe['port']]
    entrypoint = json.loads(
        process_sessions.run(
            ['podman', 'image', 'inspect', '--format', '{{json .Config.Entrypoint}}', reference],
            timeout=COMMAND_TIMEOUT,
            check=True,
        ).stdout
    )
    options, layout = lay_out(network_files(declared, KEPT))
    mounted = {
        *mounts(pod, init['name']),
        environment(declared)['JELLYFIN_DATA_DIR'],
        environment(declared)['JELLYFIN_CACHE_DIR'],
    }
    cilium_host, pod_address, envoy = (conventions.POD_CIDR_V4[n] for n in (1, 10, 20))
    home_client = next(ipaddress.ip_network(KEPT_SUBNETS[0]).hosts())
    # Each caller: the address it connects from, and the `X-Forwarded-For` it sends.
    callers: dict[str, tuple[ipaddress.IPv4Address, str | None]] = {
        'kubelet': (cilium_host, None),
        'internet': (envoy, f'198.51.100.50, {CLOUDFLARE_EDGE}'),
        'balancer': (envoy, '198.51.100.77'),
        'media': (envoy, str(home_client)),
    }
    run = uuid.uuid4().hex[:12]
    name, prober = f'kluster-test-jellyfin-{run}', f'kluster-test-jellyfin-prober-{run}'
    url = f'http://{pod_address}:{int(port)}{probe["path"]}'

    with process_sessions.removing(name, prober, timeout=COMMAND_TIMEOUT):
        # The network the server joins, its addresses given by the prober,
        # which alone holds the capability to.
        _ = process_sessions.run(
            [
                'podman', 'run', '--detach', '--rm', f'--timeout={CONTAINER_TIMEOUT}', f'--label={LABEL}',
                process_sessions.LOG_TO_FILE, f'--name={prober}', '--network=none', '--user=0:0', '--cap-drop=all', '--cap-add=NET_ADMIN',
                '--entrypoint=sleep', prober_reference, str(CONTAINER_TIMEOUT),
            ],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )  # fmt: skip
        for address in {pod_address, *(source for source, _ in callers.values())}:
            _ = process_sessions.run(
                ['podman', 'exec', prober, 'ip', 'address', 'add', f'{address}/32', 'dev', 'lo'],
                timeout=COMMAND_TIMEOUT,
                check=True,
            )
        _ = process_sessions.run(
            [
                'podman',
                'run',
                '--detach',
                *run_options(declared, name, sorted(mounted), network=f'container:{prober}'),
                *(f'--env={key}={value}' for key, value in environment(declared).items()),
                *options,
                '--entrypoint=/bin/sh',
                reference,
                '-c',
                f'set -e\n{layout}\n"$@"\nexec {shlex.join(entrypoint)}',
                'sh',
                *init['command'],
            ],
            timeout=COMMAND_TIMEOUT,
            check=True,
        )
        _ = process_sessions.run(
            [
                'podman',
                'exec',
                name,
                'sh',
                '-c',
                'until grep -qs "Startup complete" "$JELLYFIN_LOG_DIR"/*.log; do sleep 0.2; done',
            ],
            timeout=COMMAND_TIMEOUT + 30,
            check=True,
        )
        answers = {caller: asked(prober, url, *how) for caller, how in callers.items()}
        log = process_sessions.run(
            ['podman', 'exec', name, 'sh', '-c', 'cat "$JELLYFIN_LOG_DIR"/*.log'],
            timeout=COMMAND_TIMEOUT,
            check=True,
        ).stdout.decode()

    subnets = re.findall(r'Defined LAN subnets: (\[.*\])', log)
    assert answers == {caller: ['Healthy', '200'] for caller in callers}
    assert len(subnets) == 1, log
    assert 'Bind Addresses ["0.0.0.0"]' in log, log
    assert set(KEPT_SUBNETS) <= set(json.loads(subnets[0]))


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
        'issuer': ISSUER,
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
