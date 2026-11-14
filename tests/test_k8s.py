"""The shared Kubernetes helpers, declared against mocks.

What these check is the part a chart or a controller would otherwise only tell
us at apply time: that a chart is installed from the pin its caller resolved
and from no other, that each webhook configuration a chart renders awaits the
CA bundle of every entry the API server could not yet verify, that a manifest is refused unless its bytes are the pinned
ones, that a search through a chart's rendered set refuses to guess, that a
SealedSecret carries its scope where the controller looks for it, that a value
of the sealed census is read from where it is written and declared as its row
says -- or, while it is absent, not declared and named in a warning -- that an
application's namespace is one a route attaches from, whose pods only the
Gateways reach from outside it, and whose pods reach out anywhere but private
space outside the cluster, a node's listeners and the API, except where the
namespace declares a reach or its API clients, and that a custom resource of
the generated CRD SDK reaches the provider as the object the API server takes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_network
from types import MappingProxyType
from typing import Any, cast

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s
import pytest
import pytest_asyncio
from k8s_base_installation import Run, applied
from mock_monitor import Recorder, declaring, run_with, the_run_failed_with

from kluster import conventions
from kluster.conventions import Transports
from kluster.lib.config import Shape, ShapeRefused
from kluster.lib.versions import ChartPin, ManifestPin, versions

DIGEST = f'sha256:{"c" * 64}'

#: Chart pins, in the namespace every pin a stack program reads shares: the kind
#: is the key's prefix rather than a namespace of its own (framework/pulumi.md
#: §3.2), and each is an object the engine hands the program as JSON text. The
#: fixture below resolves them the way a stack program does and passes them
#: down; the helper itself reads none of them.
CHART_CONFIG = {
    'versions:chart-cilium': json.dumps(
        {'repository': 'https://helm.cilium.io/', 'version': '1.20.0', 'definitions': False}
    ),
    'versions:chart-thing': json.dumps(
        {'repository': 'oci://example.invalid/charts', 'version': '0.4.0', 'digest': DIGEST, 'definitions': False}
    ),
}

#: A pin handed to the helper that no configuration holds, for the same chart
#: the `cilium` pin names — so a helper that consulted configuration would
#: install `CHART_CONFIG`'s version instead of this one.
EXPLICIT = ChartPin(
    name='cilium', repository='https://mirror.example.invalid/', version='1.19.4', digest=None, definitions=False
)


@pytest_asyncio.fixture(scope='module', loop_scope='module', autouse=True)
async def declarations() -> Recorder:
    """One of each helper, declared once; the cases below read what they became."""
    from kluster.lib.k8s import SealingScope, SecretTemplate, helm_chart, sealed_secret

    pulumi.runtime.set_all_config(CHART_CONFIG)
    monitor = await run_with(Recorder(), stack='k8s-base')
    async with declaring():
        helm_chart(
            'cilium',
            pin=versions.chart['cilium'],
            namespace='kube-system',
            values={'kubeProxyReplacement': True},
        )
        helm_chart('registry-only', pin=versions.chart['thing'], namespace='things')
        helm_chart('explicit', pin=EXPLICIT, namespace='kube-system')
        sealed_secret(
            'cert-manager-dns01',
            name='cloudflare-dns01',
            namespace='cert-manager',
            encrypted_data={'token': 'AgBv...'},
            template=SecretTemplate(
                data={'config.ini': 'dns_cloudflare_api_token = {{ index . "token" }}'},
                type='Opaque',
                labels={'app': 'cert-manager'},
            ),
            scope=SealingScope.STRICT,
        )
        sealed_secret('ported', name='ported', namespace='apps', encrypted_data={'password': 'AgAx...'})
        crds.cilium.v2.CiliumLoadBalancerIPPool(
            'lan',
            metadata=k8s.meta.v1.ObjectMetaArgs(name='lan'),
            spec=crds.cilium.v2.CiliumLoadBalancerIPPoolSpecArgs(
                blocks=[crds.cilium.v2.CiliumLoadBalancerIPPoolSpecBlocksArgs(cidr='192.0.2.0/24')]
            ),
        )
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def rendered_search(declarations: Recorder) -> tuple[str, str]:
    """Search a rendered set for a kind that is in it, and then for one that is not.

    Both searches run here rather than in the case because the names arrive as
    Outputs: resolving them needs a running loop, and what the case is about is
    the two answers, not the awaiting.
    """
    from kluster.lib.k8s import find_rendered

    service = k8s.core.v1.Service('kube-system/metrics-server')
    config = k8s.core.v1.ConfigMap('kube-system/metrics-server-config')

    found = await find_rendered([service, config], k8s.core.v1.Service).urn.future()
    try:
        _ = await find_rendered([service, config], k8s.core.v1.Secret).future()
    except LookupError as refused:
        # And the run fails on it, as a deployment would at its end.
        assert await the_run_failed_with(LookupError) is refused
        return (found or '').rsplit('::', 1)[-1], str(refused)
    raise AssertionError('searching for a kind nothing rendered found something')


CHART = 'kubernetes:helm.sh/v4:Chart'
#: A class of the CRD SDK carries the extension's package in its token, not the
#: provider's (framework/pulumi.md §4).
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
LB_IP_POOL = 'crds:cilium.io/v2:CiliumLoadBalancerIPPool'


def test_a_chart_installs_the_pin_its_caller_resolved(declarations: Recorder) -> None:
    chart = declarations.inputs_of('cilium', CHART)
    assert chart['chart'] == 'cilium'
    assert chart['version'] == '1.20.0'
    assert chart['repositoryOpts'] == {'repo': 'https://helm.cilium.io/'}
    assert chart['values'] == {'kubeProxyReplacement': True}


def test_a_registry_chart_is_installed_by_its_digest(declarations: Recorder) -> None:
    """An OCI chart is located by its digest-pinned reference, which Helm pulls by.

    The reference carries its registry itself, so the repository the pin
    carries is not passed to Helm as one, and the version goes beside it:
    Helm refuses the pull when that version's tag resolves to another digest.
    """
    chart = declarations.inputs_of('registry-only', CHART)
    assert chart['chart'] == f'oci://example.invalid/charts/thing@{DIGEST}'
    assert chart['version'] == '0.4.0'
    assert 'repositoryOpts' not in chart


def test_a_chart_installs_the_pin_it_is_given_whatever_is_configured(declarations: Recorder) -> None:
    """The helper reads no pin: the chart's version and repository are the
    ones passed in, although configuration pins the same chart differently.
    Which pin applies is the stack program's decision (style/pulumi.md,
    "Layering"), so a helper that looked it up would override its caller."""
    chart = declarations.inputs_of('explicit', CHART)
    assert chart['chart'] == 'cilium'
    assert chart['version'] == EXPLICIT.version
    assert chart['repositoryOpts'] == {'repo': EXPLICIT.repository}


# -- a chart's admission path ----------------------------------------------------------


WEBHOOK_CONFIGURATIONS = ('ValidatingWebhookConfiguration', 'MutatingWebhookConfiguration')


def webhook_entry(name: str, *, bundle: str | None = None, url: str | None = None) -> dict[str, Any]:
    """A webhook entry as a chart renders it: calling its Service, or `url` if given, and carrying `bundle` if given."""
    client_config: dict[str, Any] = (
        {'url': url}
        if url is not None
        else {'service': {'name': 'webhook', 'namespace': 'cert-manager', 'path': f'/{name}'}}
    )
    if bundle is not None:
        client_config['caBundle'] = bundle
    return {
        'name': f'{name}.example.io',
        'clientConfig': client_config,
        'failurePolicy': 'Fail',
        'sideEffects': 'None',
        'admissionReviewVersions': ['v1'],
    }


def rendered(kind: str, *entries: dict[str, Any], annotations: dict[str, str] | None = None) -> dict[str, Any]:
    """An admission webhook configuration as a chart renders it, holding `entries`."""
    metadata: dict[str, Any] = {'name': 'cert-manager-webhook'}
    if annotations is not None:
        metadata['annotations'] = annotations
    return {
        'apiVersion': 'admissionregistration.k8s.io/v1',
        'kind': kind,
        'metadata': metadata,
        'webhooks': list(entries),
    }


def through(transforms: Sequence[pulumi.ResourceTransform], props: dict[str, Any]) -> dict[str, Any]:
    """`props` as a chart's rendered object comes out of the transforms the chart's registration bound.

    The engine applies them in order, each to what the one before it made of
    the object, and an answer of `None` leaves the object as it was.
    """
    for transform in transforms:
        result = transform(
            pulumi.ResourceTransformArgs(
                custom=True,
                type_=f'kubernetes:admissionregistration.k8s.io/v1:{props.get("kind")}',
                name=f'cert-manager/{props["metadata"]["name"]}',
                props=props,
                opts=pulumi.ResourceOptions(),
            )
        )
        assert not isinstance(result, Awaitable)
        if result is not None:
            props = dict(result.props)
    return props


def transformed(declarations: Recorder, props: dict[str, Any]) -> dict[str, Any]:
    """What a chart installed through `helm_chart` makes of `props`, one of the objects it renders."""
    return through(declarations.transforms_of('cilium', CHART), props)


def awaited(props: dict[str, Any]) -> list[str]:
    """The expressions an object's `pulumi.com/waitFor` holds, which the provider reads as a JSON array."""
    expressions = json.loads(props['metadata']['annotations']['pulumi.com/waitFor'])
    assert isinstance(expressions, list)
    return cast('list[str]', expressions)


@pytest.mark.parametrize('kind', WEBHOOK_CONFIGURATIONS)
def test_each_service_entry_rendered_without_a_bundle_is_awaited_by_its_own_index(
    declarations: Recorder, kind: str
) -> None:
    """One expression per entry, each naming the entry by index: the provider
    evaluates only the first value a path selects, so a path over every entry
    would hold once any one of them had a bundle."""
    props = transformed(declarations, rendered(kind, webhook_entry('first'), webhook_entry('second')))

    assert awaited(props) == [
        'jsonpath={.webhooks[0].clientConfig.caBundle}',
        'jsonpath={.webhooks[1].clientConfig.caBundle}',
    ]


@pytest.mark.parametrize('kind', WEBHOOK_CONFIGURATIONS)
def test_an_entry_with_a_bundle_or_naming_a_url_is_not_awaited(declarations: Recorder, kind: str) -> None:
    """Nothing will write a bundle into either, so awaiting one would hold the
    chart until the provider's timeout; the entries that are awaited keep
    their own indices around them."""
    props = transformed(
        declarations,
        rendered(
            kind,
            webhook_entry('injected', bundle='LS0tLS1CRUdJTg=='),
            webhook_entry('pending'),
            webhook_entry('external', url='https://admission.example.invalid/validate'),
            webhook_entry('also-pending'),
        ),
    )

    assert awaited(props) == [
        'jsonpath={.webhooks[1].clientConfig.caBundle}',
        'jsonpath={.webhooks[3].clientConfig.caBundle}',
    ]


def test_the_wait_is_added_to_what_the_chart_rendered_and_changes_nothing_else(declarations: Recorder) -> None:
    original = rendered(
        'ValidatingWebhookConfiguration',
        webhook_entry('pending'),
        annotations={'cert-manager.io/inject-ca-from-secret': 'cert-manager/cert-manager-webhook-ca'},
    )
    props = transformed(declarations, json.loads(json.dumps(original)))

    annotations = props['metadata'].pop('annotations')
    assert annotations.pop('pulumi.com/waitFor')
    assert annotations == original['metadata']['annotations']
    assert props == {**original, 'metadata': {'name': original['metadata']['name']}}


@pytest.mark.parametrize(
    'props',
    [
        pytest.param(
            rendered(
                'ValidatingWebhookConfiguration',
                webhook_entry('validate', bundle='LS0tLS1CRUdJTg=='),
                webhook_entry('external', url='https://admission.example.invalid/validate'),
            ),
            id='every-entry-has-a-bundle-or-a-url',
        ),
        pytest.param(
            rendered(
                'MutatingWebhookConfiguration',
                webhook_entry('mutate'),
                annotations={'pulumi.com/waitFor': 'jsonpath={.webhooks[0].name}'},
            ),
            id='awaited-by-its-own-annotation',
        ),
        pytest.param(
            rendered(
                'ValidatingWebhookConfiguration',
                webhook_entry('validate'),
                annotations={'pulumi.com/skipAwait': 'true'},
            ),
            id='its-await-waived',
        ),
        pytest.param(
            {**rendered('ValidatingWebhookConfiguration', webhook_entry('validate')), 'apiVersion': 'example.io/v1'},
            id='a-kind-of-the-same-name-in-another-group',
        ),
        pytest.param(
            rendered('ValidatingAdmissionPolicy', webhook_entry('validate')),
            id='another-kind-of-the-admission-group',
        ),
        pytest.param(
            {
                'apiVersion': 'apps/v1',
                'kind': 'Deployment',
                'metadata': {'name': 'cert-manager-webhook', 'namespace': 'cert-manager'},
                'spec': {'replicas': 1},
            },
            id='a-deployment',
        ),
        pytest.param(
            {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'cert-manager-webhook'}},
            id='a-service',
        ),
    ],
)
def test_an_object_with_nothing_to_await_passes_through_as_rendered(
    declarations: Recorder, props: dict[str, Any]
) -> None:
    assert transformed(declarations, json.loads(json.dumps(props))) == props


# -- the release manifest --------------------------------------------------------

#: A release asset, small enough to read; the pin below records its sha256.
ASSET = (
    b'apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n  name: gateways.example.com\n'
)

MANIFEST = ManifestPin(
    name='gateway-api',
    repository='kubernetes-sigs/gateway-api',
    release='v1.6.1',
    asset='experimental-install.yaml',
    sha256=hashlib.sha256(ASSET).hexdigest(),
)


class FakeAsset:
    """A downloaded release asset holding the bytes it was given."""

    def __init__(self, content: bytes) -> None:
        self.content: bytes = content

    def raise_for_status(self) -> None:
        return None


def serve(monkeypatch: pytest.MonkeyPatch, content: bytes) -> list[str]:
    """Replace the download seam, and hand back the list of URLs it was asked for."""
    from kluster.lib import release_assets as helpers

    requested: list[str] = []

    def get(url: str, **_: object) -> FakeAsset:
        requested.append(url)
        return FakeAsset(content)

    monkeypatch.setattr(helpers.requests, 'get', get)
    return requested


def test_a_manifest_is_the_asset_its_pin_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from kluster.lib.release_assets import fetch_manifest

    requested = serve(monkeypatch, ASSET)

    assert fetch_manifest(MANIFEST) == ASSET.decode()
    assert requested == [
        'https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.1/experimental-install.yaml'
    ]


def test_a_manifest_whose_digest_differs_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """One byte off is a different asset: uploaded again under the tag, or not the release at all.

    The refusal names the pin's key and both digests, since what the caller
    has to decide is which of the two is wrong.
    """
    from kluster.lib.release_assets import ManifestDigestMismatch, fetch_manifest

    altered = ASSET.replace(b'gateways', b'gatewayz')
    _ = serve(monkeypatch, altered)

    with pytest.raises(ManifestDigestMismatch, match='versions:manifest-gateway-api') as refused:
        _ = fetch_manifest(MANIFEST)
    assert hashlib.sha256(altered).hexdigest() in str(refused.value)
    assert MANIFEST.sha256 in str(refused.value)


def test_picking_a_rendered_resource_refuses_to_guess() -> None:
    """Reaching into a chart is a search, so an ambiguous or empty result is an
    error: a silently chosen resource would be wired somewhere by address."""
    from kluster.lib.k8s import pick_resource

    service = k8s.core.v1.Service.get('one', 'kube-system/sealed-secrets')
    metrics = k8s.core.v1.Service.get('two', 'kube-system/sealed-secrets-metrics')
    named = [
        (cast('pulumi.Resource', service), 'urn:pulumi:s::p::kubernetes:core/v1:Service::kube-system/sealed-secrets'),
        (
            cast('pulumi.Resource', metrics),
            'urn:pulumi:s::p::kubernetes:core/v1:Service::kube-system/sealed-secrets-metrics',
        ),
    ]

    assert pick_resource(named, k8s.core.v1.Service, '*/sealed-secrets') is service

    with pytest.raises(LookupError, match='2 resources match'):
        _ = pick_resource(named, k8s.core.v1.Service)

    with pytest.raises(LookupError, match='0 resources match'):
        _ = pick_resource(named, k8s.core.v1.ConfigMap)


def test_a_rendered_resource_is_found_through_its_outputs(rendered_search: tuple[str, str]) -> None:
    """The names being searched arrive as Outputs, so the search has to resolve
    them all before it can decide — including deciding that it cannot."""
    found, refusal = rendered_search
    assert found == 'kube-system/metrics-server'
    assert refusal.startswith('0 resources match Secret')


def test_a_sealed_secret_carries_its_scope_where_the_controller_looks(declarations: Recorder) -> None:
    """The controller reads the scope off the template's metadata first, so an
    annotation only on the resource itself would be silently ignored — and the
    ciphertext, sealed for a scope, would then fail to decrypt."""
    secret = declarations.inputs_of('cert-manager-dns01', SEALED_SECRET)

    assert secret['metadata']['name'] == 'cloudflare-dns01'
    assert secret['metadata']['namespace'] == 'cert-manager'
    assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/strict': 'true'}

    spec = secret['spec']
    assert spec['encryptedData'] == {'token': 'AgBv...'}
    assert spec['template']['type'] == 'Opaque'
    # The plaintext half stays readable, with a hole where the credential goes.
    assert spec['template']['data'] == {'config.ini': 'dns_cloudflare_api_token = {{ index . "token" }}'}
    assert spec['template']['metadata']['annotations'] == {'sealedsecrets.bitnami.com/strict': 'true'}
    assert spec['template']['metadata']['labels'] == {'app': 'cert-manager'}


def test_a_sealed_secret_defaults_to_the_scope_the_legacy_manifests_carry(declarations: Recorder) -> None:
    """Ported manifests decrypt only under the scope they were sealed with
    (cluster/migration.md §0.5), so the default cannot quietly move."""
    secret = declarations.inputs_of('ported', SEALED_SECRET)
    assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}


def test_a_custom_resource_reaches_the_provider_as_the_object_the_api_server_takes(declarations: Recorder) -> None:
    """A pool the stacks will declare, from the CRD SDK: its kind's token, and `apiVersion` and `kind` set.

    The provider sends the inputs as the object, so a class that registered
    without the two would be an object the API server refuses. The kind is one
    of the Cilium definitions, which the bundle reads from the source tree at
    the Cilium chart's version rather than from a chart.
    """
    pool = declarations.inputs_of('lan', LB_IP_POOL)

    assert pool['apiVersion'] == 'cilium.io/v2'
    assert pool['kind'] == 'CiliumLoadBalancerIPPool'
    assert pool['metadata'] == {'name': 'lan'}
    assert pool['spec'] == {'blocks': [{'cidr': '192.0.2.0/24'}]}


def test_a_pool_the_cluster_does_not_have_is_refused() -> None:
    """Cilium allocates from a pool a Service asks for; the legacy cluster
    decided it on the node, with k3s `svccontroller` labels that have no
    successor here."""
    from kluster.lib.k8s import lb_pool_labels

    with pytest.raises(ValueError, match='no such load-balancer pool'):
        _ = lb_pool_labels('homelab')


# -- an application's namespace -----------------------------------------------

NAMESPACE = 'kubernetes:core/v1:Namespace'
NETWORK_POLICY = 'crds:cilium.io/v2:CiliumNetworkPolicy'


def admitted_peers(rules: list[dict[str, Any]]) -> set[str]:
    """Every peer a policy's ingress rules admit, one entry per selector or entity, whichever rule names it.

    A peer named by any other field -- a CIDR, a node, a service account -- is
    kept under that field's name, so a rule admitting one cannot hide.
    """
    peers: set[str] = set()
    for rule in rules:
        for field, value in rule.items():
            if field == 'fromEntities':
                peers |= {f'entity {entity}' for entity in value}
            elif field == 'fromEndpoints':
                peers |= {f'endpoints {json.dumps(selector, sort_keys=True)}' for selector in value}
            else:
                peers.add(f'{field} {json.dumps(value, sort_keys=True)}')
    return peers


#: The addresses the namespace `relay` below declares as its reaches, with
#: the port each opens: two ports of one address, so that what one reach
#: opens is not shut by the other's rule; the same machine in the other
#: family; and an address on the cluster VLAN, which the nodes share.
REACHED = (
    (conventions.HOME_ASSISTANT.v4, 8123, Transports.TCP),
    (conventions.HOME_ASSISTANT.v4, 5683, Transports.UDP),
    (conventions.HOME_ASSISTANT.v6, 8123, Transports.TCP),
    (conventions.HOMELAB_HOST_LEG_IPV4, 8480, Transports.TCP_AND_UDP),
)


@pytest_asyncio.fixture(loop_scope='module')
async def application_namespaces() -> Recorder:
    """An application's namespace at the default level, one at a looser level, one with reaches, one with API clients."""
    from kluster.lib.k8s import ApiClients, LooserLevel, PodSecurity, Reach, application_namespace

    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = application_namespace('echo', name='echo')
        _ = application_namespace(
            'loose',
            name='loose',
            looser=LooserLevel(level=PodSecurity.BASELINE, reason='a stand-in for what the pods need'),
        )
        _ = application_namespace(
            'relay',
            name='relay',
            reaches=[
                Reach(address=address, port=port, transports=transports, reason='a stand-in for the device')
                for address, port, transports in REACHED
            ],
        )
        _ = application_namespace(
            'database', name='database', api_clients=ApiClients(reason='a stand-in for the instance manager')
        )
    return monitor


def test_an_application_namespace_enforces_restricted_and_carries_the_route_label(
    application_namespaces: Recorder,
) -> None:
    """Restricted is what an application gets by naming nothing, and the label is what the Gateways admit routes from.

    The label is checked against the mapping the Gateways' `allowedRoutes`
    select on, so a namespace that lost it would hold routes that attach to
    nothing.
    """
    labels = application_namespaces.inputs_of('echo', NAMESPACE)['metadata']['labels']

    assert labels['pod-security.kubernetes.io/enforce'] == 'restricted'
    assert conventions.routes.ROUTE_LABEL.items() <= labels.items()


def test_an_application_namespace_admits_its_own_pods_and_the_gateways_and_nothing_else(
    application_namespaces: Recorder,
) -> None:
    """Every pod in the namespace is held to default-deny on ingress, and the two peers it admits.

    `ingress` is the identity Cilium gives what a Gateway's Envoy forwards; an
    endpoint selector with no namespace in it is the policy's own namespace.
    Default-deny is set by name, since the cluster-wide baseline leaves it off.
    """
    policy = application_namespaces.inputs_of('echo-default-deny', NETWORK_POLICY)

    assert policy['metadata']['namespace'] == 'echo'
    spec = policy['spec']
    assert spec['endpointSelector'] == {}
    assert spec['enableDefaultDeny']['ingress'] is True
    assert admitted_peers(spec['ingress']) == {'endpoints {}', 'entity ingress'}
    assert 'ingressDeny' not in spec


def test_an_application_namespace_at_a_looser_level_keeps_the_label_and_the_policy(
    application_namespaces: Recorder,
) -> None:
    """A loosened level loosens admission of pods, not who reaches them or which routes attach."""
    labels = application_namespaces.inputs_of('loose', NAMESPACE)['metadata']['labels']
    policy = application_namespaces.inputs_of('loose-default-deny', NETWORK_POLICY)

    assert labels['pod-security.kubernetes.io/enforce'] == 'baseline'
    assert conventions.routes.ROUTE_LABEL.items() <= labels.items()
    assert policy['metadata']['namespace'] == 'loose'
    assert admitted_peers(policy['spec']['ingress']) == {'endpoints {}', 'entity ingress'}


def test_a_namespace_refuses_its_level_as_a_label() -> None:
    """A level handed in as a label would bypass `LooserLevel`, which is where an exception's reason is written."""
    from kluster.lib.k8s import namespace

    with pytest.raises(ValueError, match=re.escape('pod-security.kubernetes.io/enforce')):
        _ = namespace('sneaky', name='sneaky', labels={'pod-security.kubernetes.io/enforce': 'privileged'})


# -- an application's egress -----------------------------------------------------
#
# The cases below read the policies `application_namespace` emits through a
# small evaluator, which answers what Cilium would deny a pod of the
# namespace, by address, protocol and port, from the rules themselves. A
# field it does not read fails the case rather than being skipped, so a rule
# the evaluator would misjudge cannot pass in silence.


class Peer(Enum):
    """A destination Cilium knows by identity, which a CIDR rule never matches (`layer3.rst` at v1.20.4)."""

    OWN_NODE = 'the node the pod runs on'
    OTHER_NODE = 'another node'
    POD = 'a pod'


type Destination = IPv4Address | IPv6Address | Peer

#: A set of addresses, or of ports, as merged closed intervals under a key:
#: the address family's version, or the protocol's index in `PROTOCOLS`.
type Span = tuple[int, int, int]

PROTOCOLS = ('TCP', 'UDP', 'SCTP')
EVERY_PORT = (1, 65535)
EVERY_ADDRESS: list[Span] = [(4, 0, 2**32 - 1), (6, 0, 2**128 - 1)]

#: What each entity matches: the identities, and for `world` and `all` every address too.
ENTITY_PEERS: dict[str, frozenset[Peer]] = {
    'host': frozenset({Peer.OWN_NODE}),
    'remote-node': frozenset({Peer.OTHER_NODE}),
    'cluster': frozenset(Peer),
    'world': frozenset(),
    'all': frozenset(Peer),
}
ENTITY_ADDRESSES: dict[str, list[Span]] = {'world': EVERY_ADDRESS, 'all': EVERY_ADDRESS}


def span(network: IPv4Network | IPv6Network) -> Span:
    return (network.version, int(network.network_address), int(network.broadcast_address))


def merged(spans: Iterable[Span]) -> list[Span]:
    out: list[Span] = []
    for key, first, last in sorted(spans):
        if out and out[-1][0] == key and first <= out[-1][2] + 1:
            out[-1] = (key, out[-1][1], max(out[-1][2], last))
        else:
            out.append((key, first, last))
    return out


def minus(spans: Iterable[Span], removed: Iterable[Span]) -> list[Span]:
    rest = merged(spans)
    for key, first, last in merged(removed):
        cut: list[Span] = []
        for k, a, b in rest:
            if k != key or b < first or a > last:
                cut.append((k, a, b))
                continue
            if a < first:
                cut.append((k, a, first - 1))
            if b > last:
                cut.append((k, last + 1, b))
        rest = cut
    return rest


@dataclass(frozen=True)
class DenyRule:
    """One egress deny rule: the addresses and identities it names, and its ports, `None` for every protocol."""

    addresses: tuple[Span, ...]
    peers: frozenset[Peer]
    ports: tuple[Span, ...] | None

    def names(self, destination: Destination) -> bool:
        if isinstance(destination, Peer):
            return destination in self.peers
        return any(
            key == destination.version and first <= int(destination) <= last for key, first, last in self.addresses
        )


#: The most `ports` one `toPorts` entry may carry (`rule_validation.go` L582-585 at v1.20.4).
MAX_PORTS = 40


def read_rule(rule: dict[str, Any]) -> DenyRule:
    """One egress deny rule, refused where Cilium's `Sanitize` would refuse it.

    The agent refuses a policy whole when one rule of it fails `Sanitize`
    (`cnp_types.go` L205-209 at v1.20.4), and the API server, whose schema
    bounds none of these, applies it all the same: a namespace whose block
    is refused has no block at all. So what the program's output can break
    is asserted here rather than read as some lesser rule: an `except` outside
    its `cidr`, a `toPorts` entry of more than `MAX_PORTS` ports, and a rule
    naming two kinds of destination at once (`rule_validation.go` L411-417,
    L582-585 and L721-731).
    """
    unread = set(rule) - {'toCIDRSet', 'toCIDR', 'toEntities', 'toPorts'}
    assert not unread, f'the evaluator reads no {sorted(unread)}'
    members = [member for member in ('toCIDRSet', 'toCIDR', 'toEntities') if rule.get(member)]
    assert len(members) <= 1, f'Cilium refuses a rule combining {members}'
    addresses: list[Span] = []
    for entry in rule.get('toCIDRSet', []):
        assert set(entry) <= {'cidr', 'except'}, entry
        cidr = ip_network(entry['cidr'])
        held = [ip_network(each) for each in entry.get('except', [])]
        outside = [
            str(each)
            for each in held
            if not (each.version == cidr.version and each.network_address in cidr and each.broadcast_address in cidr)
        ]
        assert not outside, f'Cilium refuses {cidr} excepting {outside}, which it does not contain'
        addresses += minus([span(cidr)], [span(each) for each in held])
    addresses += [span(ip_network(cidr)) for cidr in rule.get('toCIDR', [])]
    peers: set[Peer] = set()
    for entity in rule.get('toEntities', []):
        assert entity in ENTITY_PEERS, f'the evaluator reads no entity {entity!r}'
        peers |= ENTITY_PEERS[entity]
        addresses += ENTITY_ADDRESSES.get(entity, [])
    ports: list[Span] | None = None
    if 'toPorts' in rule:
        ports = []
        for port_rule in rule['toPorts']:
            assert set(port_rule) <= {'ports'}, port_rule
            assert len(port_rule['ports']) <= MAX_PORTS, f'Cilium refuses {len(port_rule["ports"])} ports in one entry'
            for entry in port_rule['ports']:
                assert set(entry) <= {'port', 'endPort', 'protocol'}, entry
                first = int(entry['port'])
                last = int(entry.get('endPort', first))
                if first == 0:
                    first, last = EVERY_PORT
                protocol = entry.get('protocol', 'ANY')
                named = PROTOCOLS if protocol in ('ANY', '') else (protocol,)
                ports += [(PROTOCOLS.index(each), first, last) for each in named]
    return DenyRule(tuple(merged(addresses)), frozenset(peers), None if ports is None else tuple(merged(ports)))


def selects(selector: dict[str, Any], labels: Mapping[str, str]) -> bool:
    unread = set(selector) - {'matchLabels', 'matchExpressions'}
    assert not unread, f'the evaluator reads no {sorted(unread)}'
    if any(labels.get(key) != value for key, value in selector.get('matchLabels', {}).items()):
        return False
    for expression in selector.get('matchExpressions', []):
        key, values = expression['key'], expression.get('values', [])
        match expression['operator']:
            case 'In':
                held = labels.get(key) in values
            case 'NotIn':
                held = labels.get(key) not in values
            case 'Exists':
                held = key in labels
            case 'DoesNotExist':
                held = key not in labels
            case operator:
                raise AssertionError(f'the evaluator reads no operator {operator!r}')
        if not held:
            return False
    return True


@dataclass(frozen=True)
class Egress:
    """What the policies of one namespace do to one of its pods' outbound traffic."""

    default_deny: bool
    rules: tuple[DenyRule, ...]

    def open_ports(self, destination: Destination) -> set[tuple[str, int, int]]:
        """The port ranges the pod reaches at `destination`, by protocol."""
        if self.default_deny:
            return set()
        denied: list[Span] = []
        for rule in self.rules:
            if rule.names(destination):
                denied += (
                    [(index, *EVERY_PORT) for index in range(len(PROTOCOLS))] if rule.ports is None else rule.ports
                )
        every = [(index, *EVERY_PORT) for index in range(len(PROTOCOLS))]
        return {(PROTOCOLS[index], first, last) for index, first, last in minus(every, denied)}

    def passes_icmp(self, destination: Destination) -> bool:
        """Whether a ping reaches `destination`, which only a rule naming no port denies."""
        return not self.default_deny and not any(rule.names(destination) and rule.ports is None for rule in self.rules)

    def reaches(self, destination: Destination, protocol: str, port: int) -> bool:
        return any(named == protocol and first <= port <= last for named, first, last in self.open_ports(destination))

    def denied_whole(self) -> list[Span]:
        """Every address denied on every protocol."""
        return merged(held for rule in self.rules if rule.ports is None for held in rule.addresses)

    def denied_anywhere(self) -> list[Span]:
        """Every address some rule denies something of."""
        return merged(held for rule in self.rules for held in rule.addresses)


def egress_of(monitor: Recorder, namespace: str, labels: Mapping[str, str]) -> Egress:
    """What every policy declared in `namespace` does to the egress of a pod there carrying `labels`.

    A direction is default-deny where any policy selecting the pod asks for
    it, and a policy asks for it where it states so or, stating nothing,
    has a rule in that direction (`rule.go` L119-136 at v1.20.4).
    """
    specs: list[dict[str, Any]] = []
    for declaration in monitor.of_type(NETWORK_POLICY):
        if declaration.inputs['metadata']['namespace'] == namespace:
            specs += [declaration.inputs['spec']] if 'spec' in declaration.inputs else []
            specs += declaration.inputs.get('specs', [])
    assert specs, f'no policy is declared in {namespace}'
    default_deny = False
    rules: list[DenyRule] = []
    for spec in specs:
        if not selects(spec['endpointSelector'], labels):
            continue
        assert 'egress' not in spec, 'the evaluator models no egress allow'
        denies = spec.get('egressDeny', [])
        stated = spec.get('enableDefaultDeny', {}).get('egress')
        default_deny |= bool(denies) if stated is None else bool(stated)
        rules += [read_rule(rule) for rule in denies]
    return Egress(default_deny, tuple(rules))


#: The namespaces of `application_namespaces`, and the two pods each may
#: hold: one carrying nothing, and one carrying the API client's label.
EVERY_NAMESPACE = ('echo', 'loose', 'relay', 'database')
UNLABELLED: Mapping[str, str] = MappingProxyType({})
CLIENT: Mapping[str, str] = conventions.API_CLIENT_LABEL
EVERY_POD = pytest.mark.parametrize('labels', [UNLABELLED, CLIENT], ids=['unlabelled', 'client'])

#: Every network a declaration numbers outside the cluster, in each family it has.
NUMBERED_OUTSIDE = (
    *(network for site in conventions.SITE_NETWORKS for network in (site.v4, site.v6)),
    ip_network(conventions.HOMELAB_HOST_LEG_IPV4),
    conventions.overlay.SUBNET,
    conventions.VCN_CIDR,
)

#: Every range the cluster numbers itself from.
CLUSTER_NUMBERED = (
    conventions.POD_CIDR_V4,
    conventions.POD_CIDR_V6,
    conventions.SERVICE_CIDR_V4,
    conventions.SERVICE_CIDR_V6,
    conventions.LAN_POOL.v4,
    conventions.LAN_POOL.v6,
)

#: Addresses on the internet, from the ranges set aside for documentation.
INTERNET = (IPv4Address('192.0.2.10'), IPv6Address('2001:db8::10'))


@pytest.mark.parametrize('namespace', ['echo', 'loose', 'database'])
@EVERY_POD
def test_every_network_numbered_outside_the_cluster_is_denied_whole(
    application_namespaces: Recorder, namespace: str, labels: Mapping[str, str]
) -> None:
    """The home's networks, the cluster VLAN's other residents, the overlay and the cloud network, in both families.

    Read off the declarations rather than listed, so a network declared
    tomorrow is held to the same deny without a word here.
    """
    open_there = minus(
        [span(network) for network in NUMBERED_OUTSIDE],
        egress_of(application_namespaces, namespace, labels).denied_whole(),
    )
    assert open_there == []


@pytest.mark.parametrize('namespace', EVERY_NAMESPACE)
@EVERY_POD
def test_every_range_the_cluster_numbers_itself_from_is_outside_the_deny(
    application_namespaces: Recorder, namespace: str, labels: Mapping[str, str]
) -> None:
    """A pod that started a moment ago on another node has no identity yet, and is reached by its address."""
    denied = egress_of(application_namespaces, namespace, labels).denied_anywhere()
    for cluster in CLUSTER_NUMBERED:
        assert minus([span(cluster)], denied) == [span(cluster)], cluster


@EVERY_POD
def test_a_namespace_declaring_nothing_opens_no_private_space_but_the_clusters_and_no_port_on_a_node(
    application_namespaces: Recorder, labels: Mapping[str, str]
) -> None:
    """What is open in private space is the pod, Service and `lan` pool ranges, and a node answers on no port.

    A ping to a node passes, since a port rule does not cover it, and no
    listener answers one.
    """
    from kluster.lib.k8s import PRIVATE_SPACE

    egress = egress_of(application_namespaces, 'echo', labels)
    open_private = minus([span(space) for space in PRIVATE_SPACE], egress.denied_whole())

    assert minus(open_private, [span(cluster) for cluster in CLUSTER_NUMBERED]) == []
    assert egress.open_ports(Peer.OWN_NODE) == set()
    assert egress.open_ports(Peer.OTHER_NODE) == set()


#: Each address the namespace `relay` reaches, once however many ports it names there.
REACHED_ADDRESSES = tuple(dict.fromkeys(address for address, _, _ in REACHED))


@pytest.mark.parametrize('address', REACHED_ADDRESSES, ids=str)
def test_a_reach_opens_its_address_on_its_ports_and_nothing_beside_them(
    application_namespaces: Recorder, address: IPv4Address | IPv6Address
) -> None:
    """Every port the namespace declares at the address, on its transports, and no other; the next address stays shut.

    Two reaches at one address each open their own port, so neither's
    deny of the address's other ports shuts the other. A ping to the
    address passes, as a port rule does not cover it.
    """
    egress = egress_of(application_namespaces, 'relay', UNLABELLED)
    declared = {(protocol.upper(), p, p) for a, p, t in REACHED if a == address for protocol in t.protocols}

    assert egress.open_ports(address) == declared
    assert egress.passes_icmp(address)
    neighbor = address + 1
    assert egress.open_ports(neighbor) == set()
    assert not egress.passes_icmp(neighbor)


#: How many TCP ports at one address fill one `toPorts` entry to the brim:
#: each opened port splits the protocol's range once, and UDP and SCTP each
#: take a whole range, so the complement is that many ports plus three.
PORTS_FILLING_AN_ENTRY = MAX_PORTS - 3


@pytest.mark.parametrize('count', [PORTS_FILLING_AN_ENTRY, PORTS_FILLING_AN_ENTRY + 1])
@pytest.mark.asyncio
async def test_a_reach_of_many_ports_opens_each_of_them_in_a_policy_cilium_accepts(count: int) -> None:
    """At the most ports one `toPorts` entry holds, and one past it: every declared port open, none beside them.

    The evaluator refuses an entry of more than Cilium takes, so the case
    past the boundary is what holds the split into entries.
    """
    from kluster.lib.k8s import Reach, application_namespace

    address = conventions.HOME_ASSISTANT.v4
    ports = range(10000, 10000 + 2 * count, 2)
    monitor = await run_with(Recorder(), stack='apps')
    async with declaring():
        _ = application_namespace(
            'many',
            name='many',
            reaches=[
                Reach(address=address, port=port, transports=Transports.TCP, reason='a stand-in') for port in ports
            ],
        )

    assert egress_of(monitor, 'many', UNLABELLED).open_ports(address) == {('TCP', port, port) for port in ports}


@pytest.mark.parametrize(
    'address',
    [
        *INTERNET,
        conventions.LAN_POOL.default_vip.v4,
        conventions.LAN_POOL.default_vip.v6,
        conventions.POD_CIDR_V4.network_address + 10,
        conventions.SERVICE_CIDR_V6.network_address + 1,
    ],
    ids=str,
)
def test_a_reach_to_an_address_the_base_does_not_deny_is_refused(address: IPv4Address | IPv6Address) -> None:
    """An address on the internet is open already, and the cluster's own is never denied, so naming either is no exception."""
    from kluster.lib.k8s import Reach

    with pytest.raises(ValueError, match=re.escape(str(address))):
        _ = Reach(address=address, port=443, transports=Transports.TCP, reason='a stand-in')


def test_a_reach_to_a_node_is_refused_by_name() -> None:
    """A node is held by identity whatever a reach carves out of private space, so naming one would open nothing."""
    from kluster.lib.k8s import Reach

    with pytest.raises(ValueError, match=re.escape(f'{conventions.HOMELAB_NODE_IPV4} is a node')):
        _ = Reach(address=conventions.HOMELAB_NODE_IPV4, port=50000, transports=Transports.TCP, reason='a stand-in')


@pytest.mark.parametrize('port', [-1, 0, 65536])
def test_a_reach_on_a_port_no_socket_listens_on_is_refused(port: int) -> None:
    """Port 0 or one past the range would open nothing at the address, and say so nowhere."""
    from kluster.lib.k8s import Reach

    with pytest.raises(ValueError, match=f'on port {port},'):
        _ = Reach(address=conventions.HOME_ASSISTANT.v4, port=port, transports=Transports.TCP, reason='a stand-in')


@pytest.mark.parametrize('node', [Peer.OWN_NODE, Peer.OTHER_NODE])
def test_a_node_answers_the_api_alone_and_only_to_a_declared_client(
    application_namespaces: Recorder, node: Peer
) -> None:
    """The API's port opens to a labelled pod of a namespace that declares its clients, and to no other pod anywhere."""
    api = {('TCP', conventions.MANAGEMENT_PORTS.kubernetes, conventions.MANAGEMENT_PORTS.kubernetes)}

    assert egress_of(application_namespaces, 'database', CLIENT).open_ports(node) == api
    assert egress_of(application_namespaces, 'database', UNLABELLED).open_ports(node) == set()
    for namespace in ('echo', 'loose', 'relay'):
        for labels in (UNLABELLED, CLIENT):
            assert egress_of(application_namespaces, namespace, labels).open_ports(node) == set(), (namespace, labels)


@pytest.mark.parametrize('namespace', EVERY_NAMESPACE)
@EVERY_POD
def test_the_block_leaves_egress_open_to_the_internet_and_the_clusters_dns(
    application_namespaces: Recorder, namespace: str, labels: Mapping[str, str]
) -> None:
    """Default-deny stays off for egress, so a pod with no policy of its own still reaches out.

    A deny rule alone would switch it on for every pod it selects, and with
    no allow anywhere that denies the pod everything outbound.
    """
    egress = egress_of(application_namespaces, namespace, labels)

    assert not egress.default_deny
    for address in INTERNET:
        assert egress.reaches(address, 'TCP', 443), address
        assert egress.passes_icmp(address), address
    assert egress.reaches(Peer.POD, 'UDP', 53)
    assert egress.reaches(Peer.POD, 'TCP', 53)


# -- a value of the sealed census --------------------------------------------------


def _as_the_cli_stores(writes: dict[str, str]) -> dict[str, object]:
    """The structured key `pulumi config set --path` builds out of these writes, path by path.

    Each path is `sealedSecrets["<name>"]["<key>"]`, a quoted segment per level,
    as `SealedValue.path` writes it; the object returned is what is then held
    under `sealedSecrets`.
    """
    held: dict[str, object] = {}
    for path, ciphertext in writes.items():
        assert path.split('[', 1)[0] == conventions.sealed.CONFIG_KEY, path
        segments = re.findall(r'\["([^"]*)"\]', path)
        level = held
        for segment in segments[:-1]:
            level = cast('dict[str, object]', level.setdefault(segment, {}))
        level[segments[-1]] = ciphertext
    return held


@pytest.mark.parametrize('value', conventions.sealed.VALUES.values(), ids=conventions.sealed.VALUES)
def test_a_sealed_value_is_read_back_from_where_its_path_writes_it(value: conventions.sealed.SealedValue) -> None:
    """The program reads each of a row's keys from the place the sealing command writes it, data keys with dots included."""
    from kluster.lib.k8s import sealed_ciphertext

    writes = {value.path(key): f'AgB-sealed-{key}' for key in value.keys}

    assert sealed_ciphertext(value, _as_the_cli_stores(writes)) == {key: f'AgB-sealed-{key}' for key in value.keys}


def test_a_value_the_stack_does_not_hold_reads_as_absent() -> None:
    """Absent from the stack, or the stack holding no sealed value at all: nothing has been sealed in yet."""
    from kluster.lib.k8s import sealed_ciphertext

    value = conventions.sealed.DNS01_TOKEN
    other = conventions.sealed.BGP_PASSWORD

    assert sealed_ciphertext(value, None) is None
    assert sealed_ciphertext(value, {other.name: dict.fromkeys(other.keys, 'AgB-other')}) is None


#: The row the refusals below are read for, and one data key of it: the cases
#: are built from the row's own keys, so a renamed key leaves each case the
#: kind of mistake it names.
REFUSED = conventions.sealed.DNS01_TOKEN
REFUSED_KEY = REFUSED.keys[0]


@pytest.mark.parametrize(
    ('configured', 'what', 'shape'),
    [
        ({REFUSED.name: {}}, REFUSED.path(REFUSED_KEY), Shape.WRONG_TYPE),
        ({REFUSED.name: dict.fromkeys(REFUSED.keys, '')}, REFUSED.path(REFUSED_KEY), Shape.EMPTY),
        ({REFUSED.name: dict.fromkeys(REFUSED.keys, 3)}, REFUSED.path(REFUSED_KEY), Shape.WRONG_TYPE),
        (
            {REFUSED.name: 'AgB-not-a-mapping'},
            f'{conventions.sealed.CONFIG_KEY}["{REFUSED.name}"]',
            Shape.WRONG_TYPE,
        ),
        ('AgB-not-a-mapping', conventions.sealed.CONFIG_KEY, Shape.WRONG_TYPE),
    ],
    ids=['no-key', 'blank', 'not-text', 'value-not-a-mapping', 'sealed-key-not-a-mapping'],
)
def test_a_value_held_but_not_whole_is_refused_naming_its_path(configured: object, what: str, shape: Shape) -> None:
    """A value whose ciphertext is missing a key, or is not text, is a write gone wrong, never a value still to come.

    Refused by where it is and what it is instead, never by what it holds:
    the refusal reaches the run's log.
    """
    from kluster.lib.k8s import sealed_ciphertext

    with pytest.raises(ShapeRefused) as refused:
        _ = sealed_ciphertext(REFUSED, configured)

    assert (refused.value.what, refused.value.shape) == (what, shape)
    assert 'AgB-not-a-mapping' not in str(refused.value)


def warnings_recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every warning the run logs from here on, kept rather than sent to the engine."""
    warned: list[str] = []

    def warn(message: str, *_: object, **__: object) -> None:
        warned.append(message)

    monkeypatch.setattr(pulumi.log, 'warn', warn)
    return warned


@pytest.mark.asyncio
async def test_a_sealed_value_is_declared_as_its_row_says_under_its_callers_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The object named, placed and scoped as the row says, which is what its ciphertext is sealed to, and no warning.

    The resource is the caller's to name: its logical name carries the
    declaring component's, and the row's name is the object's alone.
    """
    from kluster.lib.k8s import sealed_value

    value = conventions.sealed.DNS01_TOKEN
    warned = warnings_recorded(monkeypatch)
    monitor = await run_with(Recorder(), stack='k8s-base')
    async with declaring():
        _ = sealed_value('declaring-component-token', value, ciphertext=dict.fromkeys(value.keys, 'AgB-sealed'))

    secret = monitor.inputs_of('declaring-component-token', SEALED_SECRET)
    assert (secret['metadata']['name'], secret['metadata']['namespace']) == (value.name, value.namespace)
    assert secret['spec']['encryptedData'] == dict.fromkeys(value.keys, 'AgB-sealed')
    assert secret['spec']['template']['metadata']['annotations'] == {
        f'sealedsecrets.bitnami.com/{value.scope.value}': 'true'
    }
    assert warned == []


@pytest.mark.asyncio
async def test_an_absent_sealed_value_declares_nothing_and_warns_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Until the bring-up's gate, a value not yet sealed in is no SealedSecret, and the run names it (bring-up.md §1)."""
    from kluster.lib.k8s import sealed_value

    value = conventions.sealed.DNS01_TOKEN
    warned = warnings_recorded(monkeypatch)
    monitor = await run_with(Recorder(), stack='k8s-base')
    async with declaring():
        declared = sealed_value('declaring-component-token', value, ciphertext=None)

    assert declared is None
    assert monitor.of_type(SEALED_SECRET) == []
    assert [message for message in warned if repr(value.name) in message and conventions.sealed.CONFIG_KEY in message]


# -- every chart of the installation ---------------------------------------------------


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The whole `k8s-base` program against a `physical` that has published every output."""
    return await applied()


def test_every_chart_the_installation_declares_awaits_its_admission_path(applied: Run) -> None:
    """Each chart registration's own transforms put the wait on a webhook configuration it renders.

    The program runs whole, so a component that declares its chart some other
    way than through `helm_chart` is caught here without being named.
    """
    charts = [request.name for request in applied.monitor.requested if request.type == CHART]
    assert charts, 'the installation declared no chart'
    pending = rendered('ValidatingWebhookConfiguration', webhook_entry('first'), webhook_entry('second'))

    for chart in charts:
        props = through(applied.monitor.transforms_of(chart, CHART), json.loads(json.dumps(pending)))
        assert 'pulumi.com/waitFor' in props['metadata'].get('annotations', {}), f'{chart} awaits nothing'
        assert awaited(props) == [
            'jsonpath={.webhooks[0].clientConfig.caBundle}',
            'jsonpath={.webhooks[1].clientConfig.caBundle}',
        ], chart
