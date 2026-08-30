"""The shared Kubernetes helpers, declared against mocks.

What these check is the part a chart or a controller would otherwise only tell
us at apply time: that a pin comes from configuration rather than code, that a
search through a chart's rendered set refuses to guess, and that a SealedSecret
carries its scope where the controller looks for it.
"""

from __future__ import annotations

from typing import cast

import pulumi
import pulumi_kubernetes as k8s
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions

#: Chart pins, in the namespace every version pin shares: the kind is the key's
#: prefix rather than a namespace of its own (rfc-002 §11.1).
CHART_CONFIG = {
    'versions:chart-cilium': 'https://helm.cilium.io/:1.20.0',
    'versions:chart-registry-only': 'oci://example.invalid/charts/thing:0.4.0',
}


@pytest_asyncio.fixture(scope='module', autouse=True)
async def declarations() -> Recorder:
    """One of each helper, declared once; the cases below read what they became."""
    from kluster.lib.k8s import SealingScope, SecretTemplate, helm_chart, sealed_secret

    pulumi.runtime.set_all_config(CHART_CONFIG)
    monitor = await run_with(Recorder(), stack='k8s-base')
    async with declaring():
        helm_chart('cilium', chart='cilium', namespace='kube-system', values={'kubeProxyReplacement': True})
        helm_chart(
            'registry-only',
            chart='oci://example.invalid/charts/thing',
            namespace='things',
            pin='registry-only',
        )
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
SEALED_SECRET = 'kubernetes:bitnami.com/v1alpha1:SealedSecret'


def test_a_chart_takes_its_repository_and_version_from_config(declarations: Recorder) -> None:
    chart = declarations.inputs_of('cilium', CHART)
    assert chart['chart'] == 'cilium'
    assert chart['version'] == '1.20.0'
    assert chart['repositoryOpts'] == {'repo': 'https://helm.cilium.io/'}
    assert chart['values'] == {'kubeProxyReplacement': True}


def test_a_registry_chart_carries_no_repository(declarations: Recorder) -> None:
    """An `oci://` reference is self-locating, and needs a pin key of its own
    because the reference itself is not a usable config key."""
    chart = declarations.inputs_of('registry-only', CHART)
    assert chart['version'] == '0.4.0'
    assert 'repositoryOpts' not in chart


def test_an_unpinned_chart_is_refused_by_name() -> None:
    from kluster.lib.k8s import helm_chart

    with pytest.raises(KeyError, match='nowhere'):
        helm_chart('nowhere', chart='nowhere', namespace='default')


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


def test_load_balancer_pools_are_a_service_label() -> None:
    """Cilium allocates from a pool a Service asks for; the legacy cluster
    decided it on the node, with k3s `svccontroller` labels that have no
    successor here."""
    from kluster.lib.k8s import lb_pool_labels

    assert lb_pool_labels(conventions.LAN_POOL.name) == {conventions.LB_POOL_LABEL: conventions.LAN_POOL.name}
    with pytest.raises(ValueError, match='no such load-balancer pool'):
        _ = lb_pool_labels('homelab')
