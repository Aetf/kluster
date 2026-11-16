"""A chart's webhook configurations reach the provider awaiting their CA bundles, held by the pinned `pulumi` itself.

`kluster.lib.k8s.helm_chart` binds one resource transform on the chart, and
what makes that reach the objects the chart renders is the engine: it keeps a
remote component's transforms and applies them to every registration whose
parent chain passes through it. The mock monitor applies no transform, so only
a run against the engine shows a rendered object carrying what the transform
set (framework/testing.md §8).

So the case installs a chart of its own through `helm_chart` on
`tests/chart_engine.py`'s harness, with no cluster. The chart renders a
validating and a mutating webhook configuration and a ConfigMap; what the case
reads is the inputs each object was created with, from the stack's state, and
the manifest the provider wrote for each webhook configuration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from chart_engine import CASE_TIMEOUT, installed, pulumi_and_uv_on_path

pytestmark = pulumi_and_uv_on_path

#: The chart, as files under its directory. Two webhook configurations, each
#: with an entry calling a Service and carrying no CA bundle; the validating
#: one has a second entry that carries one. And one object of another kind.
CHART = {
    'Chart.yaml': """\
apiVersion: v2
name: probe
version: 0.1.0
""",
    'templates/webhooks.yaml': """\
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: probe-validating
webhooks:
  - name: pending.probe.example.com
    clientConfig:
      service: {name: probe-webhook, namespace: probe, path: /validate}
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
  - name: injected.probe.example.com
    clientConfig:
      service: {name: probe-webhook, namespace: probe, path: /validate}
      caBundle: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCg==
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
---
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: probe-mutating
webhooks:
  - name: pending.probe.example.com
    clientConfig:
      service: {name: probe-webhook, namespace: probe, path: /mutate}
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
""",
    'templates/configmap.yaml': """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-settings
data:
  level: info
""",
}

WAIT_FOR = 'pulumi.com/waitFor'


@pytest.mark.timeout(CASE_TIMEOUT)
def test_a_charts_webhook_configurations_reach_the_provider_awaiting_their_bundles(tmp_path: Path) -> None:
    with installed(tmp_path, CHART) as run:
        created = {
            str(resource['inputs']['kind']): cast('dict[str, Any]', resource['inputs'])
            for resource in run.resources
            if resource['type'].startswith('kubernetes:') and 'kind' in resource.get('inputs', {})
        }
        assert sorted(created) == ['ConfigMap', 'MutatingWebhookConfiguration', 'ValidatingWebhookConfiguration']

        def awaited(kind: str) -> list[str]:
            return json.loads(created[kind]['metadata']['annotations'][WAIT_FOR])

        # The validating configuration's second entry carries its bundle, so
        # only the first is awaited.
        assert awaited('ValidatingWebhookConfiguration') == ['jsonpath={.webhooks[0].clientConfig.caBundle}']
        assert awaited('MutatingWebhookConfiguration') == ['jsonpath={.webhooks[0].clientConfig.caBundle}']
        assert WAIT_FOR not in created['ConfigMap']['metadata'].get('annotations', {})

        # And the manifest the provider wrote for each carries it.
        manifests = {path.name: path.read_text() for path in run.rendered.rglob('*.yaml')}
        for kind in ('validatingwebhookconfiguration', 'mutatingwebhookconfiguration'):
            (manifest,) = [text for name, text in manifests.items() if kind in name.lower()]
            assert WAIT_FOR in manifest, manifest
