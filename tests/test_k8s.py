"""The shared Kubernetes helpers, declared against mocks.

What these check is the part a chart or a controller would otherwise only tell
us at apply time: that a pin comes from configuration rather than code, that a
search through a chart's rendered set refuses to guess, and that a SealedSecret
carries its scope where the controller looks for it.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pulumi
import pulumi_kubernetes as k8s
import pytest
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions

#: Chart pins, in the namespace every version pin shares: the kind is the key's
#: prefix rather than a namespace of its own (rfc-002 §11.1).
CHART_CONFIG = {
    'versions:chart-cilium': 'https://helm.cilium.io/:1.20.0',
    'versions:chart-registry-only': 'oci://example.invalid/charts/thing:0.4.0',
}

#: Every resource declared below: (type, logical name, inputs).
declared: list[tuple[str, str, dict[str, Any]]] = []

#: What searching a rendered set found, and what it said when it found nothing.
search_result: tuple[str, str] = ('', '')


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        declared.append((args.typ, args.name, outputs))
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


def inputs_of(typ: str, name: str) -> dict[str, Any]:
    for declared_type, declared_name, inputs in declared:
        if declared_type == typ and declared_name == name:
            return inputs
    raise AssertionError(f'{typ} {name} was never declared; got {[(t, n) for t, n, _ in declared]}')


@pytest_asyncio.fixture(scope='module', autouse=True)
async def resources() -> None:
    """Declare one of each helper, then drain the registrations they queued.

    Declaring a resource only schedules its registration, so without draining
    them the mocks have seen nothing and every assertion would pass vacuously.
    Only the tasks this module added are awaited: the queue is process-global
    and other modules park deliberately failing outputs in it.
    """
    pulumi.runtime.set_all_config(CHART_CONFIG)
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='k8s-base', preview=False)

    from kluster.lib.k8s import SealingScope, SecretTemplate, helm_chart, sealed_secret

    before = asyncio.all_tasks()

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

    global search_result
    search_result = await _search_a_rendered_set()

    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


async def _search_a_rendered_set() -> tuple[str, str]:
    """Search a set of resources for one of a kind, and then for none.

    Run from the fixture rather than from an async test: the names arrive as
    Outputs, and resolving them needs the event loop the fixture already holds.
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


def test_a_chart_takes_its_repository_and_version_from_config() -> None:
    chart = inputs_of(CHART, 'cilium')
    assert chart['chart'] == 'cilium'
    assert chart['version'] == '1.20.0'
    assert chart['repositoryOpts'] == {'repo': 'https://helm.cilium.io/'}
    assert chart['values'] == {'kubeProxyReplacement': True}


def test_a_registry_chart_carries_no_repository() -> None:
    """An `oci://` reference is self-locating, and needs a pin key of its own
    because the reference itself is not a usable config key."""
    chart = inputs_of(CHART, 'registry-only')
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


def test_a_rendered_resource_is_found_through_its_outputs() -> None:
    """The names being searched arrive as Outputs, so the search has to resolve
    them all before it can decide — including deciding that it cannot."""
    found, refusal = search_result
    assert found == 'kube-system/metrics-server'
    assert refusal.startswith('0 resources match Secret')


def test_a_sealed_secret_carries_its_scope_where_the_controller_looks() -> None:
    """The controller reads the scope off the template's metadata first, so an
    annotation only on the resource itself would be silently ignored — and the
    ciphertext, sealed for a scope, would then fail to decrypt."""
    secret = inputs_of(SEALED_SECRET, 'cloudflare-dns01')

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


def test_a_sealed_secret_defaults_to_the_scope_the_legacy_manifests_carry() -> None:
    """Ported manifests decrypt only under the scope they were sealed with
    (cluster/migration.md §0.5), so the default cannot quietly move."""
    secret = inputs_of(SEALED_SECRET, 'ported')
    assert secret['metadata']['annotations'] == {'sealedsecrets.bitnami.com/namespace-wide': 'true'}


def test_load_balancer_pools_are_a_service_label() -> None:
    """Cilium allocates from a pool a Service asks for; the legacy cluster
    decided it on the node, with k3s `svccontroller` labels that have no
    successor here."""
    from kluster.lib.k8s import lb_pool_labels

    assert lb_pool_labels(conventions.LAN_POOL.name) == {conventions.LB_POOL_LABEL: conventions.LAN_POOL.name}
    with pytest.raises(ValueError, match='no such load-balancer pool'):
        _ = lb_pool_labels('homelab')
