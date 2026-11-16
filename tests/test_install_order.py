"""The post-renderer every chart is installed with orders a render by class, and changes nothing else.

`kluster.lib.install_order` reads a chart's render and writes
`config.kubernetes.io/depends-on` onto what runs pods and routes API calls
(runtime), naming every object no other class claims (prerequisites), and onto
the objects of a kind the render defines (products), naming every runtime
object (declarative/cluster-infra.md §1.2). These cases hand it renders of
their own and read the annotation back as the provider's dependency graph
reads it, a reference at a time in cli-utils' format; whether the provider
takes the references, and turns them into the engine's dependencies, is
`tests/test_install_order_engine.py`'s.
"""

from __future__ import annotations

import random
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

import process_sessions
import pytest
from ruamel.yaml import YAML

from kluster.lib.install_order import DEPENDS_ON, Unordered, ordered

#: The namespace the cases install their renders into.
NAMESPACE = 'home'


@dataclass(frozen=True, order=True)
class Ref:
    """An object as a dependency reference names it, which is what the provider's graph matches against."""

    group: str
    kind: str
    name: str
    namespace: str | None = None


def parsed(reference: str) -> Ref:
    """A reference in cli-utils' format: `<group>/namespaces/<namespace>/<kind>/<name>`, or `<group>/<kind>/<name>`."""
    fields = reference.strip().split('/')
    if len(fields) == 3:
        group, kind, name = fields
        return Ref(group, kind, name)
    assert len(fields) == 5, reference
    group, namespaces, namespace, kind, name = fields
    assert namespaces == 'namespaces', reference
    return Ref(group, kind, name, namespace)


def documents(stream: str) -> list[dict[str, Any]]:
    """Every object of `stream`, as a YAML reader loads it."""
    yaml = YAML(typ='safe')
    loaded: Iterable[object] = yaml.load_all(stream)  # pyright: ignore[reportUnknownMemberType] -- untyped stream
    return [cast('dict[str, Any]', document) for document in loaded if isinstance(document, dict)]


def annotations_of(document: dict[str, Any]) -> dict[str, str]:
    return cast('dict[str, str]', document['metadata'].get('annotations') or {})


def dependencies(stream: str) -> dict[Ref, set[Ref]]:
    """What each object of `stream` names in its annotation, by the object's own identity as the stream writes it."""
    found: dict[Ref, set[Ref]] = {}
    for document in documents(stream):
        metadata = document['metadata']
        group = str(document['apiVersion']).rpartition('/')[0]
        identity = Ref(group, document['kind'], metadata['name'], metadata.get('namespace'))
        found[identity] = {
            parsed(reference) for reference in annotations_of(document).get(DEPENDS_ON, '').split(',') if reference
        }
    return found


def stream_of(*objects: str) -> str:
    """A render as Helm hands it over: each object a document under a `# Source:` comment."""
    return ''.join(f'---\n# Source: chart/templates/{index}.yaml\n{text}' for index, text in enumerate(objects))


def deployment(name: str, *, labels: str = 'app: runner', namespace: str | None = NAMESPACE) -> str:
    where = f'\n  namespace: {namespace}' if namespace is not None else ''
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}{where}
spec:
  template:
    metadata:
      labels: {{{labels}}}
    spec:
      containers:
        - name: main
          image: registry.example.invalid/main:1
"""


def service(name: str, *, selector: str | None, namespace: str = NAMESPACE) -> str:
    spec = (
        f'  selector: {{{selector}}}\n' if selector is not None else '  type: ExternalName\n  externalName: x.invalid\n'
    )
    return f"""\
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {namespace}
spec:
{spec}"""


ACCOUNT = f"""\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: runner
  namespace: {NAMESPACE}
"""
SETTINGS = f"""\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: {NAMESPACE}
data:
  level: info
"""
SECRET = f"""\
apiVersion: v1
kind: Secret
metadata:
  name: token
  namespace: {NAMESPACE}
stringData:
  token: not-a-secret
"""
CLUSTER_ROLE = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: runner
rules: []
"""
BINDING = f"""\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: runner
roleRef: {{apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: runner}}
subjects:
  - {{kind: ServiceAccount, name: runner, namespace: {NAMESPACE}}}
"""
DEFINITION = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
spec:
  group: example.com
  names: {kind: Widget, plural: widgets}
  scope: Namespaced
  versions: [{name: v1, served: true, storage: true}]
"""
WIDGET = f"""\
apiVersion: example.com/v1
kind: Widget
metadata:
  name: first
  namespace: {NAMESPACE}
"""
WEBHOOK = f"""\
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
  name: runner
webhooks:
  - name: widgets.example.com
    clientConfig:
      service: {{name: runner, namespace: {NAMESPACE}}}
"""
API_SERVICE = f"""\
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1beta1.metrics.example.com
spec:
  service: {{name: runner, namespace: {NAMESPACE}}}
"""
#: A kind another installation defines: cert-manager's, in a chart that is not cert-manager.
CERTIFICATE = f"""\
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: serving
  namespace: {NAMESPACE}
spec:
  secretName: serving
"""

RUNNER = Ref('apps', 'Deployment', 'runner', NAMESPACE)


def refs(stream: str, *identities: str) -> set[Ref]:
    """The identities of the objects in `stream` whose kinds are named, each as the stream writes it."""
    return {obj for obj in dependencies(stream) if obj.kind in identities}


def test_a_workload_depends_on_every_object_no_other_class_claims_and_on_nothing_else() -> None:
    prerequisites = (ACCOUNT, SETTINGS, SECRET, CLUSTER_ROLE, BINDING, DEFINITION, CERTIFICATE)
    out = dependencies(ordered(stream_of(*prerequisites, deployment('runner')), NAMESPACE))

    assert out[RUNNER] == set(out) - {RUNNER}
    # And none of them depends on anything.
    assert all(not on for obj, on in out.items() if obj != RUNNER)


@pytest.mark.parametrize(
    ('api_version', 'kind', 'spec'),
    [
        ('v1', 'Pod', 'spec: {containers: [{name: main, image: main}]}\n'),
        ('v1', 'ReplicationController', 'spec: {template: {spec: {}}}\n'),
        ('apps/v1', 'Deployment', 'spec: {template: {spec: {}}}\n'),
        ('apps/v1', 'ReplicaSet', 'spec: {template: {spec: {}}}\n'),
        ('apps/v1', 'StatefulSet', 'spec: {template: {spec: {}}}\n'),
        ('apps/v1', 'DaemonSet', 'spec: {template: {spec: {}}}\n'),
        ('batch/v1', 'Job', 'spec: {template: {spec: {}}}\n'),
        ('batch/v1', 'CronJob', 'spec: {jobTemplate: {spec: {template: {spec: {}}}}}\n'),
    ],
)
def test_every_kind_that_runs_pods_depends_on_the_prerequisites(api_version: str, kind: str, spec: str) -> None:
    workload = f'apiVersion: {api_version}\nkind: {kind}\nmetadata:\n  name: main\n  namespace: {NAMESPACE}\n{spec}'
    out = dependencies(ordered(stream_of(SETTINGS, workload), NAMESPACE))

    assert out[Ref(api_version.rpartition('/')[0], kind, 'main', NAMESPACE)] == {
        Ref('', 'ConfigMap', 'settings', NAMESPACE)
    }


def test_a_product_depends_on_every_runtime_object_and_the_runtime_on_every_prerequisite() -> None:
    """Webhook configurations and APIServices run beside the workloads that answer them, not before or after."""
    runtime = (deployment('runner'), service('runner', selector='app: runner'), WEBHOOK, API_SERVICE)
    stream = ordered(stream_of(ACCOUNT, DEFINITION, *runtime, WIDGET), NAMESPACE)
    out = dependencies(stream)
    runtime_objects = refs(stream, 'Deployment', 'Service', 'ValidatingWebhookConfiguration', 'APIService')
    prerequisites = refs(stream, 'ServiceAccount', 'CustomResourceDefinition')

    assert out[Ref('example.com', 'Widget', 'first', NAMESPACE)] == runtime_objects
    for obj in runtime_objects:
        assert out[obj] == prerequisites, obj


def test_a_product_in_a_render_with_nothing_running_gets_no_edge() -> None:
    stream = stream_of(DEFINITION, WIDGET, SETTINGS)

    assert ordered(stream, NAMESPACE) == stream


def test_a_service_is_runtime_where_a_template_of_the_render_makes_the_pods_it_selects() -> None:
    selecting = service('runner', selector='app: runner')
    elsewhere = service('elsewhere', selector='app: elsewhere')
    # The pods it selects would run in its namespace, which no template's are.
    other_namespace = service('runner', selector='app: runner', namespace='kube-system')
    # A selector naming more labels than the template carries selects none of its pods.
    narrower = service('narrower', selector='app: runner, tier: web')
    unselecting = service('external', selector=None)
    stream = ordered(
        stream_of(
            ACCOUNT,
            deployment('runner', labels='app: runner, tier: probe'),
            selecting,
            elsewhere,
            other_namespace,
            narrower,
            unselecting,
        ),
        NAMESPACE,
    )
    out = dependencies(stream)
    account = Ref('', 'ServiceAccount', 'runner', NAMESPACE)
    runtime = {RUNNER, Ref('', 'Service', 'runner', NAMESPACE)}

    assert out[Ref('', 'Service', 'runner', NAMESPACE)] == {account}
    for product in ('elsewhere', 'narrower', 'external'):
        assert out[Ref('', 'Service', product, NAMESPACE)] == runtime, product
    assert out[Ref('', 'Service', 'runner', 'kube-system')] == runtime


def test_an_object_of_a_kind_another_installation_defines_is_a_prerequisite() -> None:
    out = dependencies(ordered(stream_of(CERTIFICATE, deployment('runner')), NAMESPACE))

    certificate = Ref('cert-manager.io', 'Certificate', 'serving', NAMESPACE)
    assert out[RUNNER] == {certificate}
    assert out[certificate] == set()


def test_a_reference_names_the_namespace_the_provider_gives_an_object() -> None:
    """A namespaced object that names none is the chart's; a cluster-scoped one has none; one naming its own keeps it."""
    unplaced_role = 'apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n  name: reader\nrules: []\n'
    elsewhere = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: shared\n  namespace: kube-system\n'
    stream = ordered(stream_of(unplaced_role, CLUSTER_ROLE, elsewhere, deployment('runner', namespace=None)), NAMESPACE)

    (workload,) = refs(stream, 'Deployment')
    assert dependencies(stream)[workload] == {
        Ref('rbac.authorization.k8s.io', 'Role', 'reader', NAMESPACE),
        Ref('rbac.authorization.k8s.io', 'ClusterRole', 'runner'),
        Ref('', 'ConfigMap', 'shared', 'kube-system'),
    }
    # And a workload naming no namespace is itself the chart's to a product.
    widget = ordered(stream_of(DEFINITION, WIDGET, deployment('runner', namespace=None)), NAMESPACE)
    assert dependencies(widget)[Ref('example.com', 'Widget', 'first', NAMESPACE)] == {RUNNER}


def test_an_annotation_the_chart_writes_is_kept_and_extended() -> None:
    annotated = deployment('runner').replace(
        'metadata:\n  name: runner\n',
        'metadata:\n  name: runner\n  annotations:\n'
        f'    {DEPENDS_ON}: /namespaces/{NAMESPACE}/Secret/token\n'
        '    example.com/kept: "yes"\n',
    )
    stream = ordered(stream_of(SETTINGS, SECRET, annotated), NAMESPACE)
    (written,) = [document for document in documents(stream) if document['kind'] == 'Deployment']
    annotations = written['metadata']['annotations']

    assert annotations['example.com/kept'] == 'yes'
    references = annotations[DEPENDS_ON].split(',')
    # The chart's own reference first, and each one once: the graph refuses a duplicate.
    assert parsed(references[0]) == Ref('', 'Secret', 'token', NAMESPACE)
    assert sorted(map(parsed, references)) == sorted(
        {Ref('', 'Secret', 'token', NAMESPACE), Ref('', 'ConfigMap', 'settings', NAMESPACE)}
    )


def test_annotations_held_as_null_become_the_annotation() -> None:
    held = deployment('runner').replace(
        'metadata:\n  name: runner\n', 'metadata:\n  name: runner\n  annotations: null\n'
    )
    out = dependencies(ordered(stream_of(SETTINGS, held), NAMESPACE))

    assert out[RUNNER] == {Ref('', 'ConfigMap', 'settings', NAMESPACE)}


UNPLACED_FOREIGN = 'apiVersion: example.org/v1\nkind: Gadget\nmetadata:\n  name: unplaced\n'


def test_a_prerequisite_whose_scope_cannot_be_told_stops_the_filter_naming_it() -> None:
    with pytest.raises(Unordered, match="Gadget 'unplaced'"):
        _ = ordered(stream_of(UNPLACED_FOREIGN, deployment('runner')), NAMESPACE)


def test_an_object_whose_scope_cannot_be_told_passes_where_nothing_references_it() -> None:
    """A chart of another installation's objects alone runs nothing, so nothing waits on them."""
    stream = stream_of(UNPLACED_FOREIGN, CERTIFICATE)

    assert ordered(stream, NAMESPACE) == stream


LISTED_ITEMS = (
    f'items:\n  - {{apiVersion: v1, kind: ServiceAccount, metadata: {{name: runner, namespace: {NAMESPACE}}}}}\n'
)


def test_a_list_is_refused_naming_it() -> None:
    listed = f'apiVersion: v1\nkind: List\nmetadata:\n  name: bundle\n{LISTED_ITEMS}'
    with pytest.raises(Unordered, match="List 'bundle'"):
        _ = ordered(stream_of(listed, deployment('runner')), NAMESPACE)


@pytest.mark.parametrize(
    'metadata', [pytest.param('metadata: {}\n', id='empty-metadata'), pytest.param('', id='no-metadata')]
)
def test_a_list_that_names_itself_nothing_is_refused_by_its_kind_and_line(metadata: str) -> None:
    """How a list is usually written, and the provider registers its items all the same."""
    listed = f'apiVersion: v1\nkind: List\n{metadata}{LISTED_ITEMS}'
    stream = stream_of(listed, deployment('runner'))
    # The line its document's content starts on, counted from one as an
    # editor counts: the `apiVersion:` line, just before `kind:`.
    line = stream.splitlines().index('kind: List')
    with pytest.raises(Unordered, match=f'List at line {line} is a list'):
        _ = ordered(stream, NAMESPACE)


def test_the_annotation_is_the_same_whatever_order_the_render_comes_in() -> None:
    objects = [ACCOUNT, SETTINGS, SECRET, CLUSTER_ROLE, BINDING, CERTIFICATE, deployment('runner')]
    first = dependencies(ordered(stream_of(*objects), NAMESPACE))
    texts: list[str] = []
    for seed in range(3):
        shuffled = objects.copy()
        random.Random(seed).shuffle(shuffled)
        (written,) = [d for d in documents(ordered(stream_of(*shuffled), NAMESPACE)) if d['kind'] == 'Deployment']
        texts.append(written['metadata']['annotations'][DEPENDS_ON])
        assert dependencies(ordered(stream_of(*shuffled), NAMESPACE)) == first
    assert len(set(texts)) == 1


#: A render holding what a YAML reader other than the provider's would read
#: differently, or a writer would write differently: comments, YAML 1.1's
#: booleans and octals, a timestamp, block scalars, flow and block styles,
#: annotations the chart writes, and an `annotations:` with no value.
VERBATIM = f"""\
---
# Source: chart/templates/settings.yaml
# A comment ahead of the object.
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings   # a trailing comment
  namespace: {NAMESPACE}
  annotations:
    example.com/flag: yes
data:
  mode: 0644
  enabled: on
  when: 2001-12-14t21:59:43.10-05:00
  script: |
    #!/bin/sh
    echo "kept"

  folded: >-
    one
    two
---
# Source: chart/templates/runner.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: runner
  namespace: {NAMESPACE}
  annotations:
  labels:
    app: runner
spec:
  replicas: 0x1
  template:
    metadata:
      labels: {{app: runner}}
    spec:
      containers:
        - name: main
          image: 'registry.example.invalid/main:1'
          args: [--level, "info", ~]
---
# Source: chart/templates/service.yaml
apiVersion: v1
kind: Service
metadata: {{name: runner, namespace: {NAMESPACE}, annotations: {{example.com/flow: "yes"}}}}
spec:
  selector:
    app: runner
---
# Source: chart/templates/elsewhere.yaml
apiVersion: v1
kind: Service
metadata: {{name: elsewhere, namespace: {NAMESPACE}}}
spec: {{selector: {{app: elsewhere}}}}
---
# Source: chart/templates/webhook.yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingWebhookConfiguration
metadata:
    name: runner
    annotations:
        example.com/indented: "four"
webhooks: []
"""


def is_subsequence(of: str, within: str) -> bool:
    """Whether `of` is what is left of `within` with some characters taken out."""
    remaining = iter(within)
    return all(character in remaining for character in of)


def without_dependencies(stream: str) -> list[dict[str, Any]]:
    """Every object of `stream` with the dependency annotation taken out, and an annotation set left empty, or null, dropped."""
    objects = documents(stream)
    for obj in objects:
        metadata = obj['metadata']
        annotations = metadata.get('annotations')
        if isinstance(annotations, dict):
            _ = cast('dict[str, Any]', annotations).pop(DEPENDS_ON, None)
        if not annotations:
            _ = metadata.pop('annotations', None)
    return objects


def test_removing_the_annotation_gives_back_the_render_byte_for_byte() -> None:
    out = ordered(VERBATIM, NAMESPACE)

    # Something was written, to each object that runs or is a product.
    assert sum(bool(on) for on in dependencies(out).values()) == 4
    # Nothing was taken out or rewritten: every character of the render is
    # still there, in order.
    assert is_subsequence(VERBATIM, out)
    # And what was put in is the annotation and nothing else.
    assert without_dependencies(out) == without_dependencies(VERBATIM)


def test_the_filter_runs_as_helm_chart_names_it() -> None:
    """`python -I -m`, the render on stdin and the namespace its argument: ordered on stdout, refused on stderr."""
    command = [sys.executable, '-I', '-m', 'kluster.lib.install_order', NAMESPACE]
    stream = stream_of(SETTINGS, deployment('runner'))

    filtered = process_sessions.run(command, input=stream.encode(), timeout=60, check=True)
    assert filtered.stdout.decode() == ordered(stream, NAMESPACE)

    refused = process_sessions.run(
        command, input=stream_of(UNPLACED_FOREIGN, deployment('runner')).encode(), timeout=60
    )
    assert refused.returncode != 0
    assert refused.stdout == b''
    assert b"Gadget 'unplaced'" in refused.stderr
