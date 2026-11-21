"""The blog, declared against mocks: its static site, its rows, and Matrix's delegation at its apex.

The route rows, the single sign-on client and the pins are the test's own, so
what is held is how the blog composes what it is handed. The rows' zones are
the primary, which is the Matrix server name, and a zone of the test's own, as
the blog's two website zones are one server name's apex and another zone.
What the static site does with any source is held in `test_apps_static_site`;
what is held here is the blog's own: one site under every name, pulled with no
credential, holding no data, and the delegation of the client it is handed at
the apex Matrix reads it from.

Literals written here are the upstream projects' contracts: the Matrix
delegation documents' paths and keys, the Kubernetes, Gateway API and
Cloudflare spellings, and the label the scheduler spreads pods over.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from node_selection import NODES, schedulable

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.apps.blog import blog
from kluster.components.apps.matrix import SERVER_NAME
from kluster.components.apps.static_site import StaticSite
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.gateways import apex_listener
from kluster.conventions.routes import PROXIED, Apex, Exposure, Held, Route
from kluster.conventions.sso import Client, Factor, Grant, Scope, TokenEndpointAuth
from kluster.lib.k8s import POD_SECURITY_ENFORCE_LABEL, PodSecurity
from kluster.lib.versions import ImagePin

NAMESPACE = 'kubernetes:core/v1:Namespace'
DEPLOYMENT = 'kubernetes:apps/v1:Deployment'
SERVICE = 'kubernetes:core/v1:Service'
CONFIG_MAP = 'kubernetes:core/v1:ConfigMap'
CLAIM = 'kubernetes:core/v1:PersistentVolumeClaim'
SECRET = 'kubernetes:core/v1:Secret'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'

#: The blog's zones: the primary, whose apex is the Matrix server name, and
#: one of the test's own.
ZONES = (conventions.ZONE_PRIMARY, 'blog-cohost.example')

#: The blog's rows: the apex and `www` of each zone.
EXPOSURE = Exposure(routes=(Apex(ZONES), Route('www', internet=PROXIED, zones=ZONES)))

#: The same, `www` held by the legacy VPS, as the land declares it.
HELD = Exposure(routes=(Apex(ZONES), Held(Route('www', internet=PROXIED, zones=ZONES))))

#: A homeserver's client of the test's own, at a host no census row carries.
CLIENT = Client(
    'mx-test',
    'mx-home',
    ('/callback',),
    policy=(Grant('mx-test-users', Factor.ONE),),
    token_endpoint_auth=TokenEndpointAuth.BASIC,
    scopes=(Scope.OPENID,),
)

#: The pins the blog is declared with here: references that are no published
#: image, so a container carrying one carries the pin.
SERVER = ImagePin('registry.example.invalid/static', 'v9.9.9', f'sha256:{"a" * 64}')
SYNC = ImagePin('registry.example.invalid/git-sync', 'v9.9.9', f'sha256:{"b" * 64}')

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
ZONE_IDS = {zone: f'{zone}-id' for zone in ZONES}

#: The paths a Matrix server and client read the delegation at, under the
#: server name (the Matrix specification, "Server Discovery").
SERVER_DOCUMENT = '/.well-known/matrix/server'
CLIENT_DOCUMENT = '/.well-known/matrix/client'


def declare(exposure: Exposure = EXPOSURE) -> StaticSite:
    return blog(
        exposure=exposure,
        matrix=CLIENT,
        image=SERVER,
        sync=SYNC,
        fronts=FRONTS,
        zone_ids=ZONE_IDS,
    )


async def _declared(exposure: Exposure) -> Recorder:
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = declare(exposure)
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def declared() -> Recorder:
    """The blog with its rows its own, as the cut leaves them."""
    return await _declared(EXPOSURE)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def held() -> Recorder:
    """The blog with `www` held, as the land declares it."""
    return await _declared(HELD)


def the_one(monitor: Recorder, typ: str) -> dict[str, Any]:
    (found,) = [it.inputs for it in monitor.of_type(typ)]
    return found


def pod_of(monitor: Recorder) -> dict[str, Any]:
    return the_one(monitor, DEPLOYMENT)['spec']['template']['spec']


def container(monitor: Recorder, name: str, *, init: bool = False) -> dict[str, Any]:
    (found,) = [it for it in pod_of(monitor)['initContainers' if init else 'containers'] if it['name'] == name]
    return found


def flags(entry: dict[str, Any]) -> dict[str, str]:
    """A container's `--name=value` arguments, by name."""
    return dict(argument.removeprefix('--').partition('=')[::2] for argument in entry['args'])


def server_configuration(monitor: Recorder) -> str:
    """The server's configuration: the ConfigMap the server mounts at `conf.d`."""
    (mount,) = [it for it in container(monitor, 'server')['volumeMounts'] if it['mountPath'] == '/etc/nginx/conf.d']
    (volume,) = [it for it in pod_of(monitor)['volumes'] if it['name'] == mount['name']]
    (config_map,) = [
        it.inputs for it in monitor.of_type(CONFIG_MAP) if it.inputs['metadata']['name'] == volume['configMap']['name']
    ]
    ((text,),) = [config_map['data'].values()]
    return text


def blocks(monitor: Recorder) -> dict[str, str]:
    """Each server block that names its server, by every name it answers."""
    found: dict[str, str] = {}
    for block in re.findall(r'server \{(.*?)\n\}', server_configuration(monitor), re.S):
        text = str(block)
        if (names := re.search(r'^\s*server_name ([^;]+);', text, re.M)) is not None:
            found |= dict.fromkeys(names[1].split(), text)
    return found


def served_documents(monitor: Recorder) -> dict[str, Any]:
    """Each document the server reads from its documents' ConfigMap, by its path under the documents' root, parsed."""
    (volume,) = [it for it in pod_of(monitor)['volumes'] if it['name'] == 'documents']
    (config_map,) = [
        it.inputs for it in monitor.of_type(CONFIG_MAP) if it.inputs['metadata']['name'] == volume['configMap']['name']
    ]
    return {f'/{item["path"]}': json.loads(config_map['data'][item['key']]) for item in volume['configMap']['items']}


# -- the delegation ----------------------------------------------------------------


def test_the_server_names_apex_delegates_to_the_homeserver_of_the_client_it_is_handed(declared: Recorder) -> None:
    """A Matrix server finds the homeserver on 443, and a Matrix client at its base URL, both at the client's host."""
    homeserver = f'{CLIENT.host}.{CLIENT.zone}'

    assert served_documents(declared) == {
        SERVER_DOCUMENT: {'m.server': f'{homeserver}:443'},
        CLIENT_DOCUMENT: {'m.homeserver': {'base_url': f'https://{homeserver}'}},
    }


def test_the_delegation_is_answered_at_the_server_name(declared: Recorder) -> None:
    """The server name's block serves both documents, and the route that adds their CORS header attaches at its apex listener."""
    server_name = blocks(declared)[SERVER_NAME]
    (documents,) = [
        it.inputs
        for it in declared.of_type(HTTP_ROUTE)
        if any(
            filtered['type'] == 'ResponseHeaderModifier'
            for rule in it.inputs['spec']['rules']
            for filtered in rule.get('filters', [])
        )
    ]
    paths = {
        match['path']['value'] for rule in documents['spec']['rules'] for match in rule['matches'] if 'path' in match
    }

    assert sorted(re.findall(r'location = (\S+) \{', server_name)) == sorted((SERVER_DOCUMENT, CLIENT_DOCUMENT))
    assert SERVER_NAME in documents['spec']['hostnames']
    assert apex_listener(SERVER_NAME) in [ref['sectionName'] for ref in documents['spec']['parentRefs']]
    assert paths == {SERVER_DOCUMENT, CLIENT_DOCUMENT}


@pytest.mark.parametrize(
    'exposure',
    [
        pytest.param(
            Exposure(routes=(Apex(('blog-cohost.example',)), Route('www', internet=PROXIED, zones=ZONES))),
            id='an apex elsewhere',
        ),
        pytest.param(Exposure(routes=(Route('www', internet=PROXIED, zones=ZONES),)), id='no apex'),
    ],
)
def test_an_exposure_with_no_apex_at_the_server_name_is_refused(exposure: Exposure) -> None:
    with pytest.raises(ValueError, match='no apex row there'):
        _ = declare(exposure)


# -- the content -------------------------------------------------------------------


def test_every_name_is_one_site(declared: Recorder) -> None:
    """The apex and `www` of each zone read the content's root, the checkout git-sync keeps behind its link, as one site."""
    sync = container(declared, 'sync')
    (synced,) = [it for it in sync['volumeMounts'] if it['name'] == 'content']
    (served,) = [it for it in container(declared, 'server')['volumeMounts'] if it['name'] == 'content']
    under = flags(sync)['root'].removeprefix(synced['mountPath'])
    checkout = f'{served["mountPath"]}{under}/{flags(sync)["link"]}'
    # The server's own `root`, outside its locations, where a document's
    # location names its own.
    roots = {
        name: re.findall(r'^\s*root ([^;]+);', re.sub(r'location [^{]*\{[^}]*\}', '', block), re.M)
        for name, block in blocks(declared).items()
    }

    assert set(roots) == {*ZONES, *(f'www.{zone}' for zone in ZONES)}
    assert {tuple(found) for found in roots.values()} == {(checkout,)}


def test_the_content_is_pulled_from_a_public_repository_with_no_credential(declared: Recorder) -> None:
    """The cluster clones over HTTPS, naming no user, and holds no Secret for it: the blog's CI holds nothing of the cluster's, nor the cluster anything of the blog's."""
    for sync in (container(declared, 'first-sync', init=True), container(declared, 'sync')):
        repository = urlsplit(flags(sync)['repo'])
        assert repository.scheme == 'https'
        assert repository.username is None
        assert sync.get('env', []) == []
        assert sync.get('envFrom', []) == []
    assert not declared.of_type(SECRET)
    assert not declared.of_type(SEALED_SECRET)


def test_the_blog_holds_no_data(declared: Recorder) -> None:
    """The built branch is the content, synced into an empty directory, so nothing is claimed."""
    (content,) = [it for it in pod_of(declared)['volumes'] if it['name'] == 'content']

    assert 'emptyDir' in content
    assert not declared.of_type(CLAIM)


# -- the rows ----------------------------------------------------------------------


def test_each_row_reaches_the_sites_service_at_internet_gw_ungated(declared: Recorder) -> None:
    """The apex at each zone's apex listener and `www` at each zone, both on `internet-gw`, with no gate, forwarding to the site's Service."""
    service = the_one(declared, SERVICE)
    (port,) = service['spec']['ports']
    routes = [it.inputs for it in declared.of_type(HTTP_ROUTE)]
    hostnames = {name for route in routes for name in route['spec']['hostnames']}
    apex_sections = {
        ref['sectionName']
        for route in routes
        for ref in route['spec']['parentRefs']
        if ref.get('sectionName', '').startswith('apex-')
    }

    assert hostnames == {*ZONES, *(f'www.{zone}' for zone in ZONES)}
    assert apex_sections == {apex_listener(zone) for zone in ZONES}
    for route in routes:
        assert {ref['name'] for ref in route['spec']['parentRefs']} == {conventions.GATEWAY_INTERNET}
        for rule in route['spec']['rules']:
            assert [it for it in rule.get('filters', []) if it['type'] == 'ExternalAuth'] == []
            assert rule['backendRefs'] == [{'name': service['metadata']['name'], 'port': port['port']}]


def test_www_is_published_in_each_zone_once_its_row_is_the_blogs(declared: Recorder, held: Recorder) -> None:
    """The cut's rows publish `www` in each zone; the land's publish none while the legacy VPS holds the name; the apex is `dns`'s in both."""
    published = {(it.inputs['zoneId'], it.inputs['name']) for it in declared.of_type(RECORD)}

    assert published == {(ZONE_IDS[zone], f'www.{zone}') for zone in ZONES}
    assert not held.of_type(RECORD)


# -- the pods ----------------------------------------------------------------------


def test_the_pods_meet_restricted(declared: Recorder) -> None:
    (namespace,) = [it.inputs for it in declared.of_type(NAMESPACE)]
    pod = pod_of(declared)

    assert namespace['metadata']['labels'][POD_SECURITY_ENFORCE_LABEL] == PodSecurity.RESTRICTED
    assert pod['securityContext']['runAsNonRoot'] is True
    assert pod['securityContext']['seccompProfile'] == {'type': 'RuntimeDefault'}
    for entry in (*pod['containers'], *pod['initContainers']):
        context = entry['securityContext']
        # The mock hands numbers back as floats, so the user is held to a whole one.
        assert context['runAsUser'] == int(context['runAsUser']) > 0
        assert context['allowPrivilegeEscalation'] is False
        assert context['capabilities'] == {'drop': ['ALL']}


def test_the_replicas_are_spread_over_the_nodes_wherever_the_scheduler_places_them(declared: Recorder) -> None:
    """The site keeps no state, so more than one replica serves it, one per node, on any node."""
    (spread,) = pod_of(declared)['topologySpreadConstraints']

    assert the_one(declared, DEPLOYMENT)['spec']['replicas'] > 1
    assert spread['topologyKey'] == 'kubernetes.io/hostname'
    assert schedulable(pod_of(declared)) == set(NODES)
