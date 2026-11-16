"""The order a chart's objects are created in: the Helm post-renderer every chart `helm_chart` installs runs.

`pulumi-kubernetes` registers what a chart renders with no order between the
objects beyond two heuristics -- a custom resource after its definition, a
namespaced object after its Namespace -- and whatever the
`config.kubernetes.io/depends-on` annotation names. This writes that
annotation, by three classes of object
([declarative/cluster-infra.md](../../../docs/declarative/cluster-infra.md)
§1.2):

-   **Runtime**: what runs pods (a kind with a pod template, or a Pod); a
    Service that selects pods a pod template of the same render produces, in
    the Service's namespace; and what routes API requests to a Service, a
    webhook configuration or an APIService.
-   **Products**: objects of a kind a definition in the same render defines,
    and a Service that selects pods no pod template of the render produces --
    an operator's pods, or another installation's.
-   **Prerequisites**: everything else -- definitions, accounts,
    permissions, configuration, Secrets, and objects of kinds another
    installation defines.

Every runtime object depends on every prerequisite, and every product on
every runtime object. A render with no prerequisites gives the runtime no
edge, and one with no runtime gives the products none.

**It runs in Helm's place in the provider, not in the program.** Helm hands it
the whole rendered stream on stdin, the definitions from the chart's `crds/`
included, and reads the stream back from its stdout; the chart's namespace is
its one argument. `helm_chart` names it as `sys.executable -I -m
kluster.lib.install_order <namespace>` (style/pulumi.md, "Layering"), so it
imports nothing of Pulumi's: it runs once per render of every chart.

**A reference names an object the way the provider will.** The provider gives
a namespaced object that names no namespace the chart's, and leaves a
cluster-scoped one without; the dependency graph matches a reference against
the object so normalized, and refuses a reference to anything outside the
render. So an object's own namespace is used where it names one, and
otherwise its kind's scope decides. Only prerequisites and runtime objects
are referenced, and a kind the render defines makes a product, so the kinds
whose scope matters are built-in kinds (`_BUILT_IN_SCOPES`) and kinds another
installation defines. An object of the second sort that names no namespace
cannot be referenced, and is refused by name (`Unordered`) where it has to
be, so the render fails before anything is applied.

**It changes no byte of the stream but the entry it adds.** The stream is
composed, never constructed or re-emitted: the composed nodes give each
object's fields and their positions in the text, and the annotation is
written into the text at those positions. A scalar the provider's YAML reader
takes otherwise than a Python one would (`yes`, `0644`, a timestamp) reaches
it as Helm rendered it. What is written besides the entry is the
`annotations:` key on an object that has none, and a mapping in place of an
`annotations:` whose value is null; an annotation the chart already writes is
kept and extended.
"""

# `ruamel.yaml`'s nodes and marks carry untyped attributes, and this module
# reads a render through them, so it is glue over that library.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

__all__ = ('DEPENDS_ON', 'Unordered', 'ordered')

#: The annotation `pulumi-kubernetes`' dependency graph (cli-utils') reads a
#: chart object's dependencies from: references separated by commas.
DEPENDS_ON = 'config.kubernetes.io/depends-on'

_NULL_TAG = 'tag:yaml.org,2002:null'

_CRD = ('apiextensions.k8s.io', 'CustomResourceDefinition')
_SERVICE = ('', 'Service')

#: The objects that route API requests to a Service, by group and kind.
_ROUTERS = frozenset(
    {
        ('admissionregistration.k8s.io', 'ValidatingWebhookConfiguration'),
        ('admissionregistration.k8s.io', 'MutatingWebhookConfiguration'),
        ('apiregistration.k8s.io', 'APIService'),
    }
)

#: The built-in kinds that run pods, by group and kind, each with the path to
#: its pod template; a Pod is its own.
_POD_TEMPLATES: Mapping[tuple[str, str], tuple[str, ...]] = {
    ('', 'Pod'): (),
    ('', 'ReplicationController'): ('spec', 'template'),
    ('apps', 'Deployment'): ('spec', 'template'),
    ('apps', 'ReplicaSet'): ('spec', 'template'),
    ('apps', 'StatefulSet'): ('spec', 'template'),
    ('apps', 'DaemonSet'): ('spec', 'template'),
    ('batch', 'Job'): ('spec', 'template'),
    ('batch', 'CronJob'): ('spec', 'jobTemplate', 'spec', 'template'),
}

_NAMESPACED = True
_CLUSTER = False

#: Whether each built-in kind is namespaced, by group and kind, as
#: Kubernetes' own API defines it.
_BUILT_IN_SCOPES: Mapping[tuple[str, str], bool] = {
    **dict.fromkeys(
        [
            ('', 'Binding'),
            ('', 'ConfigMap'),
            ('', 'Endpoints'),
            ('', 'Event'),
            ('', 'LimitRange'),
            ('', 'PersistentVolumeClaim'),
            ('', 'Pod'),
            ('', 'PodTemplate'),
            ('', 'ReplicationController'),
            ('', 'ResourceQuota'),
            ('', 'Secret'),
            ('', 'Service'),
            ('', 'ServiceAccount'),
            ('apps', 'ControllerRevision'),
            ('apps', 'DaemonSet'),
            ('apps', 'Deployment'),
            ('apps', 'ReplicaSet'),
            ('apps', 'StatefulSet'),
            ('autoscaling', 'HorizontalPodAutoscaler'),
            ('batch', 'CronJob'),
            ('batch', 'Job'),
            ('coordination.k8s.io', 'Lease'),
            ('discovery.k8s.io', 'EndpointSlice'),
            ('events.k8s.io', 'Event'),
            ('networking.k8s.io', 'Ingress'),
            ('networking.k8s.io', 'NetworkPolicy'),
            ('policy', 'PodDisruptionBudget'),
            ('rbac.authorization.k8s.io', 'Role'),
            ('rbac.authorization.k8s.io', 'RoleBinding'),
            ('resource.k8s.io', 'ResourceClaim'),
            ('resource.k8s.io', 'ResourceClaimTemplate'),
            ('storage.k8s.io', 'CSIStorageCapacity'),
        ],
        _NAMESPACED,
    ),
    **dict.fromkeys(
        [
            ('', 'ComponentStatus'),
            ('', 'Namespace'),
            ('', 'Node'),
            ('', 'PersistentVolume'),
            ('admissionregistration.k8s.io', 'MutatingAdmissionPolicy'),
            ('admissionregistration.k8s.io', 'MutatingAdmissionPolicyBinding'),
            ('admissionregistration.k8s.io', 'MutatingWebhookConfiguration'),
            ('admissionregistration.k8s.io', 'ValidatingAdmissionPolicy'),
            ('admissionregistration.k8s.io', 'ValidatingAdmissionPolicyBinding'),
            ('admissionregistration.k8s.io', 'ValidatingWebhookConfiguration'),
            ('apiextensions.k8s.io', 'CustomResourceDefinition'),
            ('apiregistration.k8s.io', 'APIService'),
            ('certificates.k8s.io', 'CertificateSigningRequest'),
            ('certificates.k8s.io', 'ClusterTrustBundle'),
            ('flowcontrol.apiserver.k8s.io', 'FlowSchema'),
            ('flowcontrol.apiserver.k8s.io', 'PriorityLevelConfiguration'),
            ('networking.k8s.io', 'IngressClass'),
            ('networking.k8s.io', 'IPAddress'),
            ('networking.k8s.io', 'ServiceCIDR'),
            ('node.k8s.io', 'RuntimeClass'),
            ('rbac.authorization.k8s.io', 'ClusterRole'),
            ('rbac.authorization.k8s.io', 'ClusterRoleBinding'),
            ('resource.k8s.io', 'DeviceClass'),
            ('resource.k8s.io', 'ResourceSlice'),
            ('scheduling.k8s.io', 'PriorityClass'),
            ('storage.k8s.io', 'CSIDriver'),
            ('storage.k8s.io', 'CSINode'),
            ('storage.k8s.io', 'StorageClass'),
            ('storage.k8s.io', 'VolumeAttachment'),
            ('storage.k8s.io', 'VolumeAttributesClass'),
        ],
        _CLUSTER,
    ),
}


class Unordered(ValueError):
    """A render the filter cannot order: an object it would have to reference, or to annotate, it cannot read."""


class _Class(Enum):
    PREREQUISITE = 'prerequisite'
    RUNTIME = 'runtime'
    PRODUCT = 'product'


@dataclass(frozen=True)
class _Object:
    """One rendered object: the node it was composed into, and what identifies it."""

    node: MappingNode
    group: str
    kind: str
    name: str
    #: The namespace the object names, `None` where it names none.
    namespace: str | None

    @property
    def group_kind(self) -> tuple[str, str]:
        return (self.group, self.kind)

    def __str__(self) -> str:
        where = f' in {self.namespace}' if self.namespace is not None else ''
        return f'{self.kind} {self.name!r}{where}'


def ordered(stream: str, namespace: str) -> str:
    """`stream`, a chart's render, with each runtime object and each product annotated with what it depends on.

    `namespace` is the chart's, which the provider gives every namespaced
    object that names none.
    """
    documents = list(YAML(typ='safe').compose_all(stream))
    objects = [read for document in documents if (read := _object(document)) is not None]
    defined = _defined(objects)
    templates = _pod_templates(objects, namespace)
    classes = {obj: _class_of(obj, namespace, defined, templates) for obj in objects}

    def references(of: _Class) -> list[str]:
        return sorted(_reference(obj, namespace) for obj, is_of in classes.items() if is_of is of)

    depends = {
        _Class.RUNTIME: references(_Class.PREREQUISITE) if _Class.RUNTIME in classes.values() else [],
        _Class.PRODUCT: references(_Class.RUNTIME) if _Class.PRODUCT in classes.values() else [],
    }
    edits = [
        _annotated(stream, obj, on)
        for obj, of in classes.items()
        if of is not _Class.PREREQUISITE and (on := depends[of])
    ]
    for start, end, text in sorted(edits, reverse=True):
        stream = stream[:start] + text + stream[end:]
    return stream


# -- reading the render ------------------------------------------------------


def _object(document: Node | None) -> _Object | None:
    """The object `document` holds, `None` for a document that holds none the provider would register.

    A list -- any mapping whose `items` is a sequence, which is how the
    provider tells one -- is refused first, whatever else it names: the
    provider registers its items one by one, and this does not order them.
    A list that names itself is refused by its kind and name, and one that
    does not by its kind and the line its document starts on. Any other
    document that is no mapping, or names no kind, version or name, is left
    to the provider, which refuses it in its own words.
    """
    if not isinstance(document, MappingNode):
        return None
    kind = _text(_field(document, 'kind'))
    api_version = _text(_field(document, 'apiVersion'))
    metadata = _field(document, 'metadata')
    name = _text(_field(metadata, 'name'))
    if isinstance(_field(document, 'items'), SequenceNode):
        what = (
            f'{kind or "a document"} {name!r}'
            if name
            else f'{kind or "a document"} at line {document.start_mark.line + 1}'
        )
        raise Unordered(f'{what} is a list, whose items the provider registers one by one and this does not order')
    if not kind or not api_version or not name or not isinstance(metadata, MappingNode):
        return None
    group = api_version.rpartition('/')[0]
    return _Object(document, group, kind, name, _text(_field(metadata, 'namespace')) or None)


def _field(node: Node | None, key: str) -> Node | None:
    """The value `node`, a mapping, holds under `key`, `None` where it is no mapping or holds none."""
    return _entry(node, key)[1] if isinstance(node, MappingNode) else None


def _path(node: Node | None, path: Sequence[str]) -> Node | None:
    for key in path:
        node = _field(node, key)
    return node


def _text(node: Node | None) -> str | None:
    """A scalar's text, `None` for a null or for anything that is no scalar."""
    if not isinstance(node, ScalarNode) or node.tag == _NULL_TAG:
        return None
    text: str = node.value
    return text


def _labels(node: Node | None) -> dict[str, str]:
    """A mapping of scalars as text, each value as written: what a label set or a selector holds."""
    if not isinstance(node, MappingNode):
        return {}
    pairs: list[tuple[Node, Node]] = node.value
    return {
        key: value
        for key_node, value_node in pairs
        if (key := _text(key_node)) is not None and (value := _text(value_node)) is not None
    }


def _defined(objects: Sequence[_Object]) -> frozenset[tuple[str, str]]:
    """The kinds the render's definitions define, by group and kind."""
    defined: set[tuple[str, str]] = set()
    for obj in objects:
        if obj.group_kind == _CRD:
            spec = _field(obj.node, 'spec')
            group = _text(_field(spec, 'group'))
            kind = _text(_path(spec, ('names', 'kind')))
            if group is not None and kind is not None:
                defined.add((group, kind))
    return frozenset(defined)


def _namespace_of(obj: _Object, namespace: str) -> str | None:
    """The namespace the provider registers `obj` in: its own, the chart's for a namespaced kind, or none.

    Refused where the object names none and its kind is no built-in one. Only
    a prerequisite or a runtime object is ever referenced, and neither is of a
    kind the render defines, since that is a product: so whatever scope a
    definition in the render gives its kind never decides a reference.
    """
    if obj.namespace is not None:
        return obj.namespace
    namespaced = _BUILT_IN_SCOPES.get(obj.group_kind)
    if namespaced is None:
        raise Unordered(
            f'{obj} names no namespace, and {obj.group or "core"}/{obj.kind} is no built-in kind, so whether it '
            'is namespaced, and so how the provider will register it, cannot be told from the render'
        )
    return namespace if namespaced else None


@dataclass(frozen=True)
class _PodTemplate:
    namespace: str | None
    labels: Mapping[str, str]


def _pod_templates(objects: Sequence[_Object], namespace: str) -> list[_PodTemplate]:
    """The pod template of every object in the render that runs pods, with the namespace its pods run in."""
    return [
        _PodTemplate(
            _namespace_of(obj, namespace),
            _labels(_path(_path(obj.node, path), ('metadata', 'labels'))),
        )
        for obj in objects
        if (path := _POD_TEMPLATES.get(obj.group_kind)) is not None
    ]


def _class_of(
    obj: _Object, namespace: str, defined: frozenset[tuple[str, str]], templates: Sequence[_PodTemplate]
) -> _Class:
    """Which of the three classes `obj` is in, given what else the render holds."""
    if obj.group_kind in defined:
        return _Class.PRODUCT
    if obj.group_kind in _POD_TEMPLATES or obj.group_kind in _ROUTERS:
        return _Class.RUNTIME
    if obj.group_kind == _SERVICE:
        selector = _labels(_path(obj.node, ('spec', 'selector')))
        where = _namespace_of(obj, namespace)
        selects = bool(selector) and any(
            template.namespace == where and selector.items() <= template.labels.items() for template in templates
        )
        return _Class.RUNTIME if selects else _Class.PRODUCT
    return _Class.PREREQUISITE


def _reference(obj: _Object, namespace: str) -> str:
    """`obj` as a dependency reference names it: `<group>/namespaces/<ns>/<Kind>/<name>`, or `<group>/<Kind>/<name>`."""
    where = _namespace_of(obj, namespace)
    if where is None:
        return f'{obj.group}/{obj.kind}/{obj.name}'
    return f'{obj.group}/namespaces/{where}/{obj.kind}/{obj.name}'


# -- writing the annotation --------------------------------------------------


def _annotated(stream: str, obj: _Object, references: Sequence[str]) -> tuple[int, int, str]:
    """The edit that gives `obj` its dependencies: the span of `stream` it replaces, and the text that replaces it.

    Every edit but one inserts, replacing an empty span. The exception is a
    value that has to change: an annotation the chart already writes,
    extended, and an `annotations:` holding a null, which becomes a mapping.
    """
    metadata = _field(obj.node, 'metadata')
    assert isinstance(metadata, MappingNode)  # `_object` holds no other
    annotations_key, annotations = _entry(metadata, 'annotations')
    if annotations is None:
        return _inserted(stream, metadata, 'annotations', _flow({DEPENDS_ON: _joined(references)}))
    if isinstance(annotations, MappingNode):
        _, existing = _entry(annotations, DEPENDS_ON)
        if existing is None:
            return _inserted(stream, annotations, DEPENDS_ON, json.dumps(_joined(references)))
        held = _text(existing)
        if held is None:
            raise Unordered(f"{obj}'s {DEPENDS_ON} annotation is no string")
        written = [reference.strip() for reference in held.split(',') if reference.strip()]
        extended = written + [reference for reference in references if reference not in written]
        return _replaced(stream, existing, json.dumps(_joined(extended)))
    if isinstance(annotations, ScalarNode) and annotations.tag == _NULL_TAG:
        assert annotations_key is not None  # the value was found under it
        mapping = _flow({DEPENDS_ON: _joined(references)})
        if annotations.start_mark.index == annotations.end_mark.index:
            # An empty value. Its position is the parser's, and parsers differ
            # on it, so the mapping goes after the key's colon instead.
            colon = stream.index(':', annotations_key.end_mark.index) + 1
            return (colon, colon, f' {mapping}')
        return _replaced(stream, annotations, mapping)
    raise Unordered(f"{obj}'s annotations are no mapping")


def _entry(mapping: MappingNode, key: str) -> tuple[Node | None, Node | None]:
    """The key and the value `mapping` holds under `key`, both `None` where it holds none."""
    pairs: list[tuple[Node, Node]] = mapping.value
    for key_node, value_node in pairs:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return key_node, value_node
    return None, None


def _joined(references: Sequence[str]) -> str:
    return ','.join(references)


def _flow(entries: Mapping[str, str]) -> str:
    return '{' + ', '.join(f'{key}: {json.dumps(value)}' for key, value in entries.items()) + '}'


def _inserted(stream: str, mapping: MappingNode, key: str, value: str) -> tuple[int, int, str]:
    """The edit that adds `key: value` to `mapping` as its first entry, in the mapping's own style."""
    entry = f'{key}: {value}'
    start = mapping.start_mark.index
    if mapping.flow_style:
        # A flow mapping starts at its brace.
        assert stream[start] == '{', stream[start : start + 20]
        pairs: list[tuple[Node, Node]] = mapping.value
        return (start + 1, start + 1, entry + (', ' if pairs else ''))
    # A block mapping starts at its first key, which keeps its column on the
    # line the entry pushes it to.
    return (start, start, f'{entry}\n{" " * mapping.start_mark.column}')


def _replaced(stream: str, node: Node, text: str) -> tuple[int, int, str]:
    """The edit that writes `text` in place of `node`, keeping the line breaks a block scalar's span ends with."""
    start, end = node.start_mark.index, node.end_mark.index
    replaced = stream[start:end]
    return (start, end, text + replaced[len(replaced.rstrip()) :])


# -- the filter --------------------------------------------------------------


#: The module's own name, which `python -m` runs it under `__main__` instead of.
_MODULE = __spec__.name if __spec__ is not None else __name__


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f'python -m {_MODULE}',
        description="A Helm post-renderer: the chart's render on stdin, the same render ordered on stdout.",
    )
    parser.add_argument(
        'namespace', help="the chart's namespace, which the provider gives a namespaced object naming none"
    )
    arguments = parser.parse_args(argv)
    namespace: str = arguments.namespace
    if not namespace:
        parser.error("the chart's namespace is empty")
    try:
        result = ordered(sys.stdin.buffer.read().decode(), namespace)
    except (Unordered, YAMLError) as refused:
        print(f'{_MODULE}: {refused}', file=sys.stderr)
        return 1
    _ = sys.stdout.buffer.write(result.encode())
    return 0


if __name__ == '__main__':
    sys.exit(_main())
