"""The shared Kubernetes helpers, declared against mocks.

What these check is the part a chart or a controller would otherwise only tell
us at apply time: that a chart is installed from the pin its caller resolved
and from no other, that a manifest is refused unless its bytes are the pinned
ones, that a search through a chart's rendered set refuses to guess, that a
SealedSecret carries its scope where the controller looks for it, and that a
custom resource of the generated CRD SDK reaches the provider as the object the
API server takes.
"""

from __future__ import annotations

import hashlib
import json
from typing import cast

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
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


@pytest_asyncio.fixture(scope='module', autouse=True)
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
            'cloudflare-dns01',
            namespace='cert-manager',
            encrypted_data={'token': 'AgBv...'},
            template=SecretTemplate(
                data={'config.ini': 'dns_cloudflare_api_token = {{ index . "token" }}'},
                type='Opaque',
                labels={'app': 'cert-manager'},
            ),
            scope=SealingScope.STRICT,
        )
        sealed_secret('ported', namespace='apps', encrypted_data={'password': 'AgAx...'})
        crds.cilium.v2.CiliumLoadBalancerIPPool(
            'lan',
            metadata=k8s.meta.v1.ObjectMetaArgs(name='lan'),
            spec=crds.cilium.v2.CiliumLoadBalancerIPPoolSpecArgs(
                blocks=[crds.cilium.v2.CiliumLoadBalancerIPPoolSpecBlocksArgs(cidr='192.0.2.0/24')]
            ),
        )
    return monitor


@pytest_asyncio.fixture(scope='module')
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
    secret = declarations.inputs_of('cloudflare-dns01', SEALED_SECRET)

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


def test_load_balancer_pools_are_a_service_label() -> None:
    """Cilium allocates from a pool a Service asks for; the legacy cluster
    decided it on the node, with k3s `svccontroller` labels that have no
    successor here."""
    from kluster.lib.k8s import lb_pool_labels

    assert lb_pool_labels(conventions.LAN_POOL.name) == {conventions.LB_POOL_LABEL: conventions.LAN_POOL.name}
    with pytest.raises(ValueError, match='no such load-balancer pool'):
        _ = lb_pool_labels('homelab')
