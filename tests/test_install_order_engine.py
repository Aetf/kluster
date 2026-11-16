"""A chart's objects reach the engine depending on one another by class, held by the pinned `pulumi` itself.

`kluster.lib.k8s.helm_chart` hands every chart a Helm post-renderer,
`kluster.lib.install_order`, which writes `config.kubernetes.io/depends-on`
onto the objects a chart renders; the provider turns the annotation into
dependencies between the chart's children as it registers them. The mock
monitor runs no provider, so neither the post-renderer nor what the provider
makes of its references shows there: only a run against the engine does
(framework/testing.md §8). So this case reads, from the stack's state, the
dependencies each object of a chart of the case's own was registered with.

That one reading holds four things at once: that the provider runs the
post-renderer as `helm_chart` names it, from a project that is no checkout of
this repository; that every reference it writes parses; that each one names
an object the way the provider registers it, the chart's namespace on a
namespaced object that names none included; and that the edges reach the
engine. It also holds that the Chart itself records no inputs, so the
interpreter's path in the post-renderer is never a diff between two
checkouts.

The harness is `tests/chart_engine.py`'s, which `tests/test_admission_wait_engine.py`
runs on too: the pinned CLI, the Kubernetes provider plugin at the version the
locked `pulumi-kubernetes` registers, in its `renderYamlToDirectory` mode, and
the chart served from a Helm repository the case runs on `127.0.0.1`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from chart_engine import CASE_TIMEOUT, installed, pulumi_and_uv_on_path

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pulumi_and_uv_on_path

#: The chart. Each object's class is by what it is and by what else the
#: render holds, and `CLASSES` below says which; the names say nothing.
CHART = {
    'Chart.yaml': """\
apiVersion: v2
name: probe
version: 0.1.0
""",
    'crds/widgets.yaml': """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.probe.example.com
spec:
  group: probe.example.com
  names: {kind: Widget, plural: widgets, singular: widget, listKind: WidgetList}
  scope: Namespaced
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema: {type: object, x-kubernetes-preserve-unknown-fields: true}
""",
    'templates/access.yaml': """\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: runner
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: reader
rules:
  - apiGroups: [""]
    resources: [configmaps]
    verbs: [get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: runner
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: view}
subjects:
  - {kind: ServiceAccount, name: runner, namespace: probe}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
data:
  level: info
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: serving
  namespace: probe
spec:
  secretName: serving
  dnsNames: [runner.probe.svc]
  issuerRef: {name: probe, kind: Issuer}
""",
    'templates/runtime.yaml': """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: runner
  namespace: probe
spec:
  selector:
    matchLabels: {app: runner}
  template:
    metadata:
      labels: {app: runner, tier: probe}
    spec:
      serviceAccountName: runner
      containers:
        - name: runner
          image: registry.example.invalid/runner:1
---
apiVersion: v1
kind: Service
metadata:
  name: runner
  namespace: probe
spec:
  selector: {app: runner}
  ports: [{port: 443}]
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: runner
webhooks:
  - name: widgets.probe.example.com
    clientConfig:
      service: {name: runner, namespace: probe, path: /validate}
    admissionReviewVersions: [v1]
    sideEffects: None
    failurePolicy: Fail
""",
    'templates/products.yaml': """\
apiVersion: probe.example.com/v1
kind: Widget
metadata:
  name: first
  namespace: probe
---
apiVersion: v1
kind: Service
metadata:
  name: elsewhere
  namespace: probe
spec:
  selector: {app: elsewhere}
  ports: [{port: 80}]
""",
}

PREREQUISITES = {
    ('CustomResourceDefinition', 'widgets.probe.example.com'),
    ('ServiceAccount', 'probe/runner'),
    # Rendered with no namespace: the provider gives it the chart's, and a
    # reference that named it otherwise would fail the chart.
    ('Role', 'probe/reader'),
    ('ClusterRoleBinding', 'runner'),
    ('ConfigMap', 'probe/settings'),
    # A kind no definition in the render defines.
    ('Certificate', 'probe/serving'),
}
RUNTIME = {
    ('Deployment', 'probe/runner'),
    # It selects the Deployment's pods.
    ('Service', 'probe/runner'),
    ('ValidatingWebhookConfiguration', 'runner'),
}
PRODUCTS = {
    ('Widget', 'probe/first'),
    # It selects pods no template of the render makes.
    ('Service', 'probe/elsewhere'),
}

CHART_TYPE = 'kubernetes:helm.sh/v4:Chart'
#: The name `PROGRAM` installs the chart under.
CHART_NAME = 'probe'


@pytest.mark.timeout(CASE_TIMEOUT)
def test_a_charts_objects_reach_the_engine_depending_on_the_class_before_theirs(tmp_path: Path) -> None:
    with installed(tmp_path, CHART) as run:
        resources = run.resources

    def identity(resource: dict[str, Any]) -> tuple[str, str]:
        """An object as its kind and its name in the chart: `<namespace>/<name>` for a namespaced one.

        The provider names a chart's child `<chart>:<that name>`.
        """
        kind = str(resource['type']).rsplit(':', 1)[-1]
        name = str(resource['urn']).rsplit('::', 1)[-1].removeprefix(f'{CHART_NAME}:')
        return kind, name

    objects = {
        identity(resource): resource
        for resource in resources
        if resource['type'].startswith('kubernetes:') and resource['type'] != CHART_TYPE
    }
    assert set(objects) == PREREQUISITES | RUNTIME | PRODUCTS

    def urns(of: set[tuple[str, str]]) -> set[str]:
        return {str(objects[obj]['urn']) for obj in of}

    def dependencies(obj: tuple[str, str]) -> set[str]:
        return set(objects[obj].get('dependencies', []))

    for obj in RUNTIME:
        assert urns(PREREQUISITES) <= dependencies(obj), obj
    for obj in PRODUCTS:
        assert urns(RUNTIME) <= dependencies(obj), obj
    # And nothing the chart runs waits on what it runs before.
    for obj in PREREQUISITES:
        assert not dependencies(obj) & urns(RUNTIME | PRODUCTS), obj

    # The Chart records no inputs, so the interpreter's path in its
    # post-renderer is no diff between two checkouts.
    (chart,) = [resource for resource in resources if resource['type'] == CHART_TYPE]
    assert not chart.get('inputs'), chart['inputs']
