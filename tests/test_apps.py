"""The application base's exposure, declared against mocks: `expose`, `port` and the `route` it calls.

Every row here is the test's own, one of each shape the census can hold, so
what is held is how a row's fields decide what is emitted rather than what the
census happens to say. The addresses the program would read from `physical`
are documentation and private ranges, each class distinct from the others, so
a Service at the wrong front reads differently. The Gateways are declared in
the same run, so a raw home port is held to the sharing key its Gateway
actually carries rather than to a copy of it.

Literals written here are the upstream projects' contracts: the Gateway API's
filter and listener spellings, the annotation and the families LB IPAM and
Kubernetes read, the entity Cilium names a client outside the cluster.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.apps import Backend, Fronts, expose
from kluster.components.cilium import InternetPoolMembers
from kluster.components.cilium.gateways import (
    SHARING_KEY,
    Gateways,
    InternetGatewayAddresses,
    apex_listener,
)
from kluster.conventions.cluster import POOL_WORKER, Transports
from kluster.conventions.routes import (
    BARE,
    PROXIED,
    REWRITE,
    Apex,
    Balancer,
    Dedicated,
    Direct,
    Exposure,
    Gate,
    Heard,
    Held,
    Host,
    Passthrough,
    Port,
    Proxied,
    Route,
    SiteForward,
    Vip,
)
from kluster.lib.k8s import PORTAL, PassthroughRefused, bare_listener

HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
GATEWAY = 'crds:gateway.networking.k8s.io/v1:Gateway'
SERVICE = 'kubernetes:core/v1:Service'
POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'
IPS = 'lbipam.cilium.io/ips'

NODE_PRIVATE_IPS = {'cp-1': '10.0.1.11', 'cp-2': '10.0.1.12'}
NODE_GUAS = {'cp-1': '2001:db8:1::11', 'cp-2': '2001:db8:1::12'}
DEDICATED_VIP = '10.0.1.99'
BALANCER_V4 = '203.0.113.10'
BALANCER_V6 = '2001:db8::10'
WORKER_GUA = '2001:db8:70::10'


FRONTS = Fronts(
    internet=InternetPoolMembers(
        node_private_ips=NODE_PRIVATE_IPS,
        node_guas=NODE_GUAS,
        dedicated_vip=DEDICATED_VIP,
        balancer_v4=BALANCER_V4,
        balancer_v6=BALANCER_V6,
    ),
    worker_gua=WORKER_GUA,
)

#: The applications this suite exposes, one per shape, by the namespace each
#: is declared in.
EXPOSURES: dict[str, Exposure] = {
    'shop': Exposure(routes=(Route('shop', internet=Proxied(Gate.SESSION), lan=REWRITE),)),
    'dav': Exposure(routes=(Route('dav', internet=Proxied(Gate.BASIC)),)),
    'tv': Exposure(
        routes=(Route('tv', internet=PROXIED, media=REWRITE),),
        ports=(Port('tv-discovery', 7359, Transports.UDP, media=Heard()),),
    ),
    'links': Exposure(routes=(Route('links', lan=BARE),)),
    'peer': Exposure(
        ports=(Port('peer', 22000, Transports.TCP_AND_UDP, internet=Balancer((Host('peer'),)), lan=Vip('peer')),),
    ),
    'cache': Exposure(ports=(Port('cache', 60011, Transports.TCP, internet=Dedicated()),)),
    'torrent': Exposure(ports=(Port('torrent', 53363, Transports.TCP_AND_UDP, internet=SiteForward()),)),
    'site': Exposure(routes=(Apex(('a.example', 'b.example')), Route('www', internet=PROXIED, zones=('a.example',)))),
    'held': Exposure(routes=(Held(Route('held', internet=PROXIED)),)),
}


def selector(application: str) -> dict[str, str]:
    return {'app': application}


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def exposed() -> Recorder:
    """Every exposure above, each in its own namespace, beside the Gateways."""
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = Gateways(
            'gateways',
            gateway_class='cilium',
            exposures=(),
            internet=InternetGatewayAddresses(
                node_private_ips=NODE_PRIVATE_IPS,
                node_guas=NODE_GUAS,
                balancer_v4=BALANCER_V4,
                balancer_v6=BALANCER_V6,
            ),
            after=(),
        )
        for application, exposure in EXPOSURES.items():
            _ = expose(
                application,
                exposure,
                namespace=application,
                backend=Backend(service=application, port=8080),
                selector=selector(application),
                fronts=FRONTS,
            )
    return monitor


def in_namespace(monitor: Recorder, typ: str, namespace: str) -> list[dict[str, Any]]:
    return [it.inputs for it in monitor.of_type(typ) if it.inputs['metadata']['namespace'] == namespace]


def parents(route: dict[str, Any]) -> list[str]:
    return [ref['name'] for ref in route['spec']['parentRefs']]


def filters(route: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for rule in route['spec']['rules'] for item in rule.get('filters', [])]


def service(monitor: Recorder, namespace: str, side: str) -> dict[str, Any]:
    (found,) = [it for it in in_namespace(monitor, SERVICE, namespace) if it['metadata']['name'].endswith(f'-{side}')]
    return found


# -- route ---------------------------------------------------------------------


def test_route_attaches_each_gateway_the_row_sets_and_the_gate_to_internets_alone(exposed: Recorder) -> None:
    """One route per Gateway, every zone's name on each, and the filter only where the row is on `internet` gated.

    The home has no gate, so the gated row's `lan` route forwards to the
    backend with no question asked of the portal.
    """
    routes = {parents(it)[0]: it for it in in_namespace(exposed, HTTP_ROUTE, 'shop')}

    assert sorted(routes) == sorted([conventions.GATEWAY_INTERNET, conventions.GATEWAY_LAN])
    for route in routes.values():
        assert route['spec']['hostnames'] == [f'shop.{conventions.ZONE_PRIMARY}']
        assert [ref['namespace'] for ref in route['spec']['parentRefs']] == [conventions.GATEWAY_NAMESPACE]
        (rule,) = route['spec']['rules']
        assert rule['backendRefs'] == [{'name': 'shop', 'port': 8080}]
    assert [item['type'] for item in filters(routes[conventions.GATEWAY_INTERNET])] == ['ExternalAuth']
    assert 'ExternalAuth' not in [item['type'] for item in filters(routes[conventions.GATEWAY_LAN])]


def removed_headers(route: dict[str, Any]) -> set[str]:
    """Every request header a route's filters remove."""
    return {
        name.lower()
        for item in filters(route)
        if item['type'] == 'RequestHeaderModifier'
        for name in item['requestHeaderModifier'].get('remove', [])
    }


@pytest.mark.parametrize('application', ['shop', 'tv', 'site', 'held'])
def test_every_route_without_a_gate_removes_the_portals_identity_headers(exposed: Recorder, application: str) -> None:
    """Nothing but the client writes them on a route the portal does not answer for, so they are removed.

    An application that trusts `Remote-User` would otherwise take a home
    client's, or an ungated internet client's, word for who it is. A gated
    route does not remove them: there the removal would run after the gate
    and take the portal's own answer away.
    """
    for route in in_namespace(exposed, HTTP_ROUTE, application):
        gated = any(item['type'] == 'ExternalAuth' for item in filters(route))
        if gated:
            assert removed_headers(route) == set(), route['metadata']['name']
        else:
            assert removed_headers(route) >= set(PORTAL.identity_headers), route['metadata']['name']


@pytest.mark.parametrize(('application', 'gate'), [('shop', Gate.SESSION), ('dav', Gate.BASIC)])
def test_each_form_of_the_gate_asks_the_portal_for_its_own_authorization(
    exposed: Recorder, application: str, gate: Gate
) -> None:
    """The filter asks the portal's Service, at the endpoint of the row's own form, and each form's endpoint is its own.

    A Basic client cannot follow the redirect the session form answers with,
    so a Basic row sent to the session endpoint is a row no client of it can
    pass.
    """
    (route,) = [
        it for it in in_namespace(exposed, HTTP_ROUTE, application) if parents(it) == [conventions.GATEWAY_INTERNET]
    ]
    (gate_filter,) = filters(route)
    auth = gate_filter['externalAuth']

    assert auth['protocol'] == 'HTTP'
    assert auth['backendRef'] == {'name': PORTAL.service, 'namespace': PORTAL.namespace, 'port': PORTAL.port}
    assert auth['http']['path'] == PORTAL.paths[gate]
    assert len(set(PORTAL.paths.values())) == len(Gate)


def gate_of(monitor: Recorder, application: str) -> dict[str, Any]:
    (route,) = [
        it for it in in_namespace(monitor, HTTP_ROUTE, application) if parents(it) == [conventions.GATEWAY_INTERNET]
    ]
    (gate_filter,) = filters(route)
    return gate_filter['externalAuth']['http']


def test_the_session_form_hands_the_portal_the_session_and_the_basic_form_does_not(exposed: Recorder) -> None:
    """The session lives in a cookie, and the portal builds the address it sends a browser back to from the scheme.

    Without the cookie every signed-in request is redirected to sign in
    again, and without the scheme the redirect is to the wrong address. The
    Basic form authenticates by the `Authorization` header alone, which the
    filter always sends, and has no session to forward.
    """
    session = {name.lower() for name in gate_of(exposed, 'shop')['allowedHeaders']}
    basic = {name.lower() for name in gate_of(exposed, 'dav')['allowedHeaders']}

    assert {'cookie', 'x-forwarded-proto'} <= session
    assert 'x-forwarded-proto' in basic
    assert 'cookie' not in basic


@pytest.mark.parametrize('application', ['shop', 'dav'])
def test_the_gate_copies_the_portals_identity_headers_and_nothing_else(exposed: Recorder, application: str) -> None:
    """The portal's answer reaches the application only under the names it states who signed in by.

    An empty list would copy every header of the answer onto the request.
    """
    copied = gate_of(exposed, application)['allowedResponseHeaders']

    assert copied
    assert all(name.lower().startswith('remote-') for name in copied)


def test_a_bare_name_redirects_at_lan_gws_listener_for_its_label_to_the_full_name(exposed: Recorder) -> None:
    """The label is answered on plain HTTP, where it arrives, with a redirect to the name over HTTPS."""
    routes = {it['metadata']['name']: it for it in in_namespace(exposed, HTTP_ROUTE, 'links')}
    (bare,) = [it for it in routes.values() if it['spec']['hostnames'] == ['links']]

    assert bare['spec']['parentRefs'] == [
        {
            'name': conventions.GATEWAY_LAN,
            'namespace': conventions.GATEWAY_NAMESPACE,
            'sectionName': bare_listener('links'),
        }
    ]
    (rule,) = bare['spec']['rules']
    assert 'backendRefs' not in rule
    assert rule['filters'] == [
        {
            'type': 'RequestRedirect',
            'requestRedirect': {
                'hostname': f'links.{conventions.ZONE_PRIMARY}',
                'scheme': 'https',
                'statusCode': 301,
            },
        }
    ]
    assert len(routes) == 2


def test_a_held_row_attaches_all_the_same(exposed: Recorder) -> None:
    """The legacy VPS holds the name, and the route is how the new application is verified before the cut."""
    assert [parents(it) for it in in_namespace(exposed, HTTP_ROUTE, 'held')] == [[conventions.GATEWAY_INTERNET]]


# -- the passthrough and the apex ----------------------------------------------


def test_a_passthrough_is_refused_naming_its_host_and_where_its_reach_is_decided() -> None:
    """No Gateway can carry it: Cilium drops a passthrough listener with the zone's wildcard on the same port."""
    exposure = Exposure(routes=(Passthrough('peer', internet=Direct('clients present a certificate'), lan=REWRITE),))

    with pytest.raises(PassthroughRefused, match=r"'peer'.*kluster-ops#723"):
        _ = expose(
            'peer',
            exposure,
            namespace='peer',
            backend=Backend(service='peer', port=8080),
            selector=None,
            fronts=FRONTS,
        )


def test_an_apex_attaches_at_internet_gws_apex_listener_of_each_of_its_zones(exposed: Recorder) -> None:
    (apex,) = [it for it in in_namespace(exposed, HTTP_ROUTE, 'site') if it['metadata']['name'] == 'apex']

    assert apex['spec']['hostnames'] == ['a.example', 'b.example']
    assert [(ref['name'], ref['sectionName']) for ref in apex['spec']['parentRefs']] == [
        (conventions.GATEWAY_INTERNET, apex_listener('a.example')),
        (conventions.GATEWAY_INTERNET, apex_listener('b.example')),
    ]


def test_two_apex_rows_in_one_exposure_are_refused() -> None:
    """One apex route per namespace: a second would take the first one's name."""
    exposure = Exposure(routes=(Apex(('a.example',)), Apex(('b.example',))))

    with pytest.raises(ValueError, match='one apex row'):
        _ = expose(
            'site',
            exposure,
            namespace='site',
            backend=Backend(service='site', port=8080),
            selector=None,
            fronts=FRONTS,
        )


def test_an_exposure_with_route_rows_and_no_backend_is_refused() -> None:
    with pytest.raises(ValueError, match='needs the Service'):
        _ = expose('dav', EXPOSURES['dav'], namespace='dav', backend=None, selector=None, fronts=FRONTS)


@pytest.mark.parametrize('pods', [None, {}], ids=['none', 'empty'])
def test_an_exposure_with_ports_and_no_pods_named_is_refused(pods: dict[str, str] | None) -> None:
    """An empty selector picks every pod in the namespace, and the port's allow would open them all to the world."""
    with pytest.raises(ValueError, match='needs the pods'):
        _ = expose(
            'cache',
            EXPOSURES['cache'],
            namespace='cache',
            backend=None,
            selector=pods,
            fronts=FRONTS,
        )


# -- port ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ('application', 'pool', 'addresses', 'families'),
    [
        (
            'peer',
            conventions.POOL_INTERNET,
            [*NODE_PRIVATE_IPS.values(), *NODE_GUAS.values(), BALANCER_V4, BALANCER_V6],
            ['IPv4', 'IPv6'],
        ),
        ('cache', conventions.POOL_INTERNET, [DEDICATED_VIP], ['IPv4']),
        ('torrent', POOL_WORKER, [str(conventions.HOMELAB_NODE_IPV4), WORKER_GUA], ['IPv4', 'IPv6']),
    ],
    ids=['balancer', 'dedicated', 'site-forward'],
)
def test_an_internet_front_is_a_cluster_service_at_its_fronts_addresses_with_no_node_ports(
    exposed: Recorder, application: str, pool: str, addresses: list[str], families: list[str]
) -> None:
    """Each front's Service asks for its own addresses out of its own pool, under `Cluster`, with no node port.

    A `Balancer` front's addresses are the node members and the balancer's
    two, a `Dedicated` front's the dedicated VIP's alone, and a `SiteForward`
    front's the worker's two, each in the pool that holds them. LB IPAM shares
    an address only between Services of one policy, and a node port is
    answered at a cloud node's public address ahead of its firewall.
    """
    spec_of = service(exposed, application, 'internet')
    spec = spec_of['spec']

    assert spec_of['metadata']['labels'] == {conventions.LB_POOL_LABEL: pool}
    assert sorted(spec_of['metadata']['annotations'][IPS].split(',')) == sorted(addresses)
    assert spec['type'] == 'LoadBalancer'
    assert spec['externalTrafficPolicy'] == 'Cluster'
    assert spec['allocateLoadBalancerNodePorts'] is False
    assert all('nodePort' not in it for it in spec['ports'])
    assert spec['ipFamilies'] == families
    assert spec['ipFamilyPolicy'] == ('RequireDualStack' if len(families) == 2 else 'SingleStack')
    assert spec['selector'] == selector(application)


def test_a_port_serves_each_of_its_transports_on_its_port(exposed: Recorder) -> None:
    ports = service(exposed, 'peer', 'internet')['spec']['ports']

    assert sorted((it['protocol'], it['port'], it['targetPort']) for it in ports) == [
        ('TCP', 22000, 22000),
        ('UDP', 22000, 22000),
    ]


@pytest.mark.parametrize(
    ('application', 'side', 'gateway', 'vip'),
    [
        ('peer', 'lan', conventions.GATEWAY_LAN, conventions.LAN_POOL.default_vip),
        ('tv', 'media', conventions.GATEWAY_MEDIA, conventions.LAN_POOL.media_vip),
    ],
    ids=['vip', 'heard'],
)
def test_a_raw_home_port_shares_its_vip_with_its_gateway_under_one_sharing_key(
    exposed: Recorder, application: str, side: str, gateway: str, vip: conventions.Vip
) -> None:
    """The Service asks for the VIP its Gateway holds, under the key the Gateway's own Service carries.

    A heard side is served as a `Vip` is, at its Gateway's VIP: the relay on
    the host sends what it hears there. Ports tell the Services apart, so no
    address is added.
    """
    (declared,) = [it.inputs for it in exposed.of_type(GATEWAY) if it.inputs['metadata']['name'] == gateway]
    held = declared['spec']['infrastructure']['annotations']
    found = service(exposed, application, side)
    annotations = found['metadata']['annotations']

    assert annotations[SHARING_KEY] == held[SHARING_KEY]
    assert annotations['lbipam.cilium.io/sharing-cross-namespace'] == '*'
    assert held['lbipam.cilium.io/sharing-cross-namespace'] == '*'
    assert sorted(annotations[IPS].split(',')) == sorted(it['value'] for it in declared['spec']['addresses'])
    assert sorted(annotations[IPS].split(',')) == sorted([str(vip.v4), str(vip.v6)])
    assert found['metadata']['labels'] == {conventions.LB_POOL_LABEL: conventions.LAN_POOL.name}
    assert found['spec']['externalTrafficPolicy'] == 'Cluster'
    assert found['spec']['allocateLoadBalancerNodePorts'] is False


def test_each_port_emits_one_allow_for_the_world_on_its_port_and_transports_alone(exposed: Recorder) -> None:
    """The namespace's default-deny admits only what a Gateway forwards; a raw port's client is the world.

    One allow per port, however many Gateways it sets, to the pods serving
    it and to nothing else in the namespace.
    """
    for application, exposure in EXPOSURES.items():
        allows = [it for it in in_namespace(exposed, POLICY, application) if it['metadata']['name'].endswith('-world')]
        assert len(allows) == len(exposure.ports), application
        for row, allow in zip(exposure.ports, allows, strict=True):
            spec = allow['spec']
            assert spec['endpointSelector'] == {'matchLabels': selector(application)}
            (rule,) = spec['ingress']
            assert rule['fromEntities'] == ['world']
            assert sorted((it['port'], it['protocol']) for to in rule['toPorts'] for it in to['ports']) == sorted(
                (str(row.port), protocol.upper()) for protocol in row.transports.protocols
            )
            assert set(rule) == {'fromEntities', 'toPorts'}


def test_a_port_on_no_gateway_of_a_kind_emits_no_service_for_it(exposed: Recorder) -> None:
    """A Service per Gateway the row sets, and none for the others."""
    assert sorted(it['metadata']['name'] for it in in_namespace(exposed, SERVICE, 'peer')) == [
        'peer-internet',
        'peer-lan',
    ]
    assert [it['metadata']['name'] for it in in_namespace(exposed, SERVICE, 'tv')] == ['tv-discovery-media']
