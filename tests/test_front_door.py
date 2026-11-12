"""The front door in the `k8s-base` program, declared against mocks: the issuer, the certificates and the Gateways.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), twice (rfc-007 §5.1, §5.2):

-   **as the stack stands until the operator seals the DNS-01 token in**,
    holding no sealed value, which declares no SealedSecret for it and says
    so, while the issuer and the certificates are declared all the same and
    wait for it (physical/bring-up.md §1);
-   **with the token sealed in, a census whose Gateway rows sit on other
    ports, and a route in a second zone**, so that a listener built from a
    fixed port, a certificate built from a fixed zone list, or two zones'
    certificates crossed, reads differently from what the census asks.

Literals written here are the upstream projects' contracts: the Gateway API's
address type and listener protocols, cert-manager's issuer kind.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
from k8s_base_installation import (
    CHART,
    OUTPUTS,
    PUBLISHED,
    VERSIONS_CONFIG,
    Autonaming,
    Run,
    release_assets,
)
from mock_monitor import declaring, run_under_backstop

from kluster import conventions
from kluster.lib.k8s import KUBECONFIG_KEY

GATEWAY = 'crds:gateway.networking.k8s.io/v1:Gateway'
HTTP_ROUTE = 'crds:gateway.networking.k8s.io/v1:HTTPRoute'
CONFIG_GROUP = 'kubernetes:yaml/v2:ConfigGroup'
CLUSTER_ISSUER = 'crds:cert-manager.io/v1:ClusterIssuer'
CERTIFICATE = 'crds:cert-manager.io/v1:Certificate'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
POOL = 'crds:cilium.io/v2:CiliumLoadBalancerIPPool'

TOKEN = conventions.sealed.DNS01_TOKEN

#: The ciphertext the sealed run's configuration holds for the DNS-01 token,
#: under its row's name and data key, as `credentials` writes it.
SEALED_TOKEN = 'AgB-the-sealed-dns01-token'

#: The second zone the sealed run's census routes a name in.
SECOND_ZONE = 'second.example'

#: The ports the sealed run moves the Gateways' two ports to.
MOVED_PORTS = conventions.GatewayPorts(http=10080, https=10443)

#: The sealed run's census: the installation's, and one route in a second zone.
PROBE = {
    **conventions.routes.EXPOSURES,
    'probe': conventions.routes.Exposure(
        routes=(conventions.routes.Route('probe', internet=conventions.routes.PROXIED, zones=(SECOND_ZONE,)),)
    ),
}


@contextmanager
def moved_census() -> Generator[None]:
    """The Gateways' ports moved, and one route in a second zone, for the length of a run."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(conventions, 'GATEWAY_PORTS', MOVED_PORTS)
        patch.setattr(conventions.routes, 'EXPOSURES', PROBE)
        yield


async def declare(*, sealed: dict[str, object] | None) -> tuple[Run, list[str]]:
    """The whole program with `sealed` as its sealed values, and every warning it logged."""
    from kluster.stacks import k8s_base

    config = {f'kluster:{KUBECONFIG_KEY}': 'a-fake-kubeconfig-that-reaches-no-cluster'}
    if sealed is not None:
        config[f'kluster:{conventions.sealed.CONFIG_KEY}'] = json.dumps(sealed)
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=[f'kluster:{KUBECONFIG_KEY}'])
    monitor = await run_under_backstop(Autonaming(), stack=conventions.STACK_NAMES.k8s_base)
    warned: list[str] = []

    def warn(message: str, *_: object, **__: object) -> None:
        warned.append(message)

    with release_assets() as fetched, pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.log, 'warn', warn)
        async with declaring():
            await k8s_base.main()
    return Run(monitor, fetched), warned


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def absent() -> tuple[Run, list[str]]:
    """The stack as it stands before the token is sealed in."""
    return await declare(sealed=None)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def sealed() -> Run:
    """The token sealed in, the Gateways' rows moved and a second zone served."""
    with moved_census():
        run, _ = await declare(sealed={TOKEN.name: dict.fromkeys(TOKEN.keys, SEALED_TOKEN)})
    return run


def _gateways(run: Run) -> dict[str, dict[str, Any]]:
    return {it.inputs['metadata']['name']: it.inputs for it in run.monitor.of_type(GATEWAY)}


def _certificates(run: Run) -> list[dict[str, Any]]:
    return [it.inputs for it in run.monitor.of_type(CERTIFICATE)]


def _https(gateway: dict[str, Any]) -> list[dict[str, Any]]:
    return [listener for listener in gateway['spec']['listeners'] if listener['protocol'] == 'HTTPS']


def _http(gateway: dict[str, Any]) -> dict[str, Any]:
    (listener,) = [listener for listener in gateway['spec']['listeners'] if listener['protocol'] == 'HTTP']
    return listener


# -- The certificates ------------------------------------------------------------


def test_a_certificate_per_served_zone_for_the_apex_and_the_wildcard(sealed: Run) -> None:
    """The primary, served by definition, and the zone a route names; each certificate covers both names of its zone."""
    names = sorted(sorted(certificate['spec']['dnsNames']) for certificate in _certificates(sealed))

    assert names == sorted([sorted([zone, f'*.{zone}']) for zone in (conventions.ZONE_PRIMARY, SECOND_ZONE)])


def test_every_certificate_is_issued_by_the_one_issuer(sealed: Run) -> None:
    (issuer,) = [it.inputs for it in sealed.monitor.of_type(CLUSTER_ISSUER)]

    for certificate in _certificates(sealed):
        assert certificate['spec']['issuerRef'] == {
            'name': issuer['metadata']['name'],
            'kind': 'ClusterIssuer',
            'group': 'cert-manager.io',
        }


def test_the_issuer_answers_dns01_with_the_sealed_token(sealed: Run) -> None:
    """The issuer's Cloudflare token is the Secret the SealedSecret yields, which holds the stack's ciphertext.

    The ciphertext is the stack's configuration's, under the row's name and
    data key, and the SealedSecret is named and placed as the row says, which
    is what the ciphertext is sealed to; the issuer reads the same name and key
    from the namespace cert-manager's chart is installed in.
    """
    (issuer,) = [it.inputs for it in sealed.monitor.of_type(CLUSTER_ISSUER)]
    (secret,) = [
        it.inputs for it in sealed.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == TOKEN.name
    ]
    (solver,) = issuer['spec']['acme']['solvers']
    reference = solver['dns01']['cloudflare']['apiTokenSecretRef']

    assert secret['spec']['encryptedData'] == {reference['key']: SEALED_TOKEN}
    assert secret['spec']['template']['metadata']['name'] == reference['name']
    assert secret['metadata']['namespace'] == sealed.inputs(CHART, 'cert-manager')['namespace']
    assert secret['metadata']['annotations'] == {f'sealedsecrets.bitnami.com/{TOKEN.scope.value}': 'true'}


def test_an_absent_token_declares_no_sealed_secret_and_warns_naming_it(absent: tuple[Run, list[str]]) -> None:
    """Until the token is sealed in, nothing is declared for it, and the run says which value it is waiting for.

    The issuer and the certificates are declared all the same: they read the
    Secret by name, and stay pending until it exists.
    """
    run, warned = absent

    assert [it for it in run.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == TOKEN.name] == []
    assert [message for message in warned if repr(TOKEN.name) in message]
    assert run.monitor.of_type(CLUSTER_ISSUER)
    assert _certificates(run)


# -- The Gateways ------------------------------------------------------------------


def test_each_gateways_addresses_are_its_pools(absent: tuple[Run, list[str]]) -> None:
    """`internet-gw` asks for the node members and the balancer's two; `lan-gw` and `media-gw` for their VIPs.

    Each asks from the pool its Service is labeled into, and `internet-gw`
    never for the dedicated VIP, which is hath's alone.
    """
    run, _ = absent
    gateways = _gateways(run)
    lan = conventions.LAN_POOL
    expected = {
        conventions.GATEWAY_INTERNET: (
            conventions.POOL_INTERNET,
            {
                *cast('dict[str, str]', PUBLISHED[OUTPUTS.node_private_ips]).values(),
                *cast('dict[str, str]', PUBLISHED[OUTPUTS.node_guas]).values(),
                cast('str', PUBLISHED[OUTPUTS.cluster_endpoint]),
                cast('str', PUBLISHED[OUTPUTS.cluster_endpoint_v6]),
            },
        ),
        conventions.GATEWAY_LAN: (lan.name, {str(lan.default_vip.v4), str(lan.default_vip.v6)}),
        conventions.GATEWAY_MEDIA: (lan.name, {str(lan.media_vip.v4), str(lan.media_vip.v6)}),
    }

    assert gateways.keys() == expected.keys()
    for name, (pool, addresses) in expected.items():
        spec = gateways[name]['spec']
        assert {address['type'] for address in spec['addresses']} == {'IPAddress'}
        assert sorted(address['value'] for address in spec['addresses']) == sorted(addresses), name
        assert spec['infrastructure']['labels'] == {conventions.LB_POOL_LABEL: pool}, name


def test_internet_gws_addresses_are_all_in_its_pool(absent: tuple[Run, list[str]]) -> None:
    """An address the pool does not hold is one LB IPAM never allocates, and the Service then holds none."""
    run, _ = absent
    (pool,) = [
        it.inputs for it in run.monitor.of_type(POOL) if it.inputs['metadata']['name'] == conventions.POOL_INTERNET
    ]
    blocks = {block['cidr'].split('/')[0] for block in pool['spec']['blocks']}
    asked = {address['value'] for address in _gateways(run)[conventions.GATEWAY_INTERNET]['spec']['addresses']}

    assert asked < blocks


def test_internet_gw_shares_its_addresses_with_other_namespaces(absent: tuple[Run, list[str]]) -> None:
    """The census's raw Services hold the same node addresses from their own namespaces (rfc-007 §4.4).

    LB IPAM shares an address only between Services carrying one sharing key,
    and across namespaces only where both carry the cross-namespace
    annotation, so `internet-gw` carries both.
    """
    run, _ = absent
    annotations = _gateways(run)[conventions.GATEWAY_INTERNET]['spec']['infrastructure']['annotations']

    assert annotations['lbipam.cilium.io/sharing-key']
    assert annotations['lbipam.cilium.io/sharing-cross-namespace'] == '*'


def test_the_listeners_ports_are_the_gateway_ports(sealed: Run) -> None:
    """Every Gateway listens on `GATEWAY_PORTS`, which are the ports the node firewall opens to Envoy and the balancer forwards.

    The run moves both ports, so a listener that kept a number of its own
    would differ from them.
    """
    for name, gateway in _gateways(sealed).items():
        assert {listener['port'] for listener in _https(gateway)} == {MOVED_PORTS.https}, name
        assert _http(gateway)['port'] == MOVED_PORTS.http, name


def test_each_https_listener_serves_its_own_zones_certificate(sealed: Run) -> None:
    """One HTTPS listener per served zone, on the zone's wildcard, terminating with that zone's certificate.

    The certificate is found by the names it covers, and it is in the
    Gateways' own namespace, so the listener refers across none.
    """
    by_wildcard = {
        name: certificate
        for certificate in _certificates(sealed)
        for name in certificate['spec']['dnsNames']
        if name.startswith('*.')
    }

    for name, gateway in _gateways(sealed).items():
        listeners = _https(gateway)
        assert sorted(listener['hostname'] for listener in listeners) == sorted(by_wildcard), name
        for listener in listeners:
            certificate = by_wildcard[listener['hostname']]
            assert listener['tls']['mode'] == 'Terminate'
            assert listener['tls']['certificateRefs'] == [{'name': certificate['spec']['secretName']}], name
            assert certificate['metadata']['namespace'] == gateway['metadata']['namespace']


def test_a_route_attaches_to_the_https_listeners_only_from_a_namespace_carrying_the_route_label(
    absent: tuple[Run, list[str]],
) -> None:
    run, _ = absent

    for name, gateway in _gateways(run).items():
        for listener in _https(gateway):
            assert listener['allowedRoutes']['namespaces'] == {
                'from': 'Selector',
                'selector': {'matchLabels': dict(conventions.routes.ROUTE_LABEL)},
            }, name


def test_plain_http_is_answered_with_a_redirect_to_https_alone(absent: tuple[Run, list[str]]) -> None:
    """The HTTP listener admits routes from the Gateways' own namespace, and the one route there redirects.

    The namespace carries no route label, so the redirect attaches to no HTTPS
    listener, and an application's route, in a namespace of its own, attaches
    to no HTTP listener.
    """
    run, _ = absent
    gateways = _gateways(run)
    (route,) = [it.inputs for it in run.monitor.of_type(HTTP_ROUTE)]
    namespaces = {
        it.inputs['metadata']['name']: it.inputs for it in run.monitor.of_type('kubernetes:core/v1:Namespace')
    }

    assert route['metadata']['namespace'] == conventions.GATEWAY_NAMESPACE
    assert sorted((ref['name'], ref['sectionName']) for ref in route['spec']['parentRefs']) == sorted(
        (name, _http(gateway)['name']) for name, gateway in gateways.items()
    )
    (rule,) = route['spec']['rules']
    assert 'backendRefs' not in rule
    assert rule['filters'] == [{'type': 'RequestRedirect', 'requestRedirect': {'scheme': 'https', 'statusCode': 301}}]
    for name, gateway in gateways.items():
        assert _http(gateway)['allowedRoutes']['namespaces'] == {'from': 'Same'}, name
    labels = namespaces[conventions.GATEWAY_NAMESPACE]['metadata']['labels']
    assert not conventions.routes.ROUTE_LABEL.items() <= labels.items()


# -- What waits for what ---------------------------------------------------------


@pytest.mark.parametrize(
    ('kind', 'definitions'),
    [
        (GATEWAY, (CONFIG_GROUP, 'cilium-gateway-api')),
        (HTTP_ROUTE, (CONFIG_GROUP, 'cilium-gateway-api')),
        (CLUSTER_ISSUER, (CHART, 'cert-manager')),
        (CERTIFICATE, (CHART, 'cert-manager')),
        (SEALED_SECRET, (CHART, 'sealed-secrets')),
    ],
    ids=['gateway', 'http-route', 'issuer', 'certificate', 'sealed-secret'],
)
def test_each_object_waits_for_what_installs_its_definition(
    sealed: Run, kind: str, definitions: tuple[str, str]
) -> None:
    """The provider waits for workloads, not for a definition to register, so each kind is declared behind its source.

    Held through the chain of declared dependencies, since an object can wait
    for its definition by way of another object that does.
    """
    source = sealed.urn(*definitions)
    instances = sealed.monitor.of_type(kind)

    assert instances
    for instance in instances:
        assert source in _behind(sealed, sealed.urn(kind, instance.name)), instance.name


def _behind(run: Run, urn: str) -> set[str]:
    """Every URN `urn` waits for, directly or through what it waits for."""
    registrations = run.monitor.registrations
    seen: set[str] = set()
    pending = list(registrations[urn].dependencies)
    while pending:
        dependency = pending.pop()
        if dependency not in seen:
            seen.add(dependency)
            if dependency in registrations:
                pending.extend(registrations[dependency].dependencies)
    return seen
