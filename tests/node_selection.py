"""The cluster's nodes as their labels say, and what a pod's node constraints or a label selector select.

Every suite that holds a placement as a relation reads it here, against nodes
labelled the way they are, rather than against a pinned affinity
(framework/testing.md §2): a node's site from the machine configuration
`components.talos` renders for it, the control plane label Talos puts on a
control plane node, and the architecture the kubelet labels every node with.

`schedulable` reads a pod spec as the scheduler reads its node constraints,
against the cluster's nodes or, through `schedulable_among`, others: its
`nodeSelector`, and its required node affinity, at least one term of which
must have every requirement hold, a term with no requirement matching no
node. `matches` reads a label selector, a `matchLabels` and its
`matchExpressions`, as Kubernetes does wherever one selects nodes or
objects, and `selected` is the nodes one selects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from kluster import conventions
from kluster.components import talos

#: The label Talos puts on every control plane node, with an empty value
#: (`node_label_spec.go` at v1.13.9, L83–85).
CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'

#: The label the kubelet puts on every node with its architecture.
ARCHITECTURE_LABEL = 'kubernetes.io/arch'

#: The label the kubelet puts on every node with its name.
HOSTNAME_LABEL = 'kubernetes.io/hostname'


def labelled(name: str, site: conventions.Site, *, control_plane: bool, architecture: str) -> dict[str, str]:
    """The labels of a node named `name` at `site`, in the role and of the architecture given."""
    return {
        HOSTNAME_LABEL: name,
        **cast('dict[str, str]', talos.node_patch(site)['machine']['nodeLabels']),
        **({CONTROL_PLANE_LABEL: ''} if control_plane else {}),
        ARCHITECTURE_LABEL: architecture,
    }


#: The cluster's nodes, labelled as they are: every cloud node a control
#: plane of A1.Flex, arm64, and the worker at home, amd64 (nodes.md §3.1, §4).
NODES: dict[str, dict[str, str]] = {
    **{
        node: labelled(node, conventions.Site.CLOUD, control_plane=True, architecture='arm64')
        for node in conventions.CLOUD_NODES
    },
    conventions.HOMELAB_NODE: labelled(
        conventions.HOMELAB_NODE, conventions.Site.HOME, control_plane=False, architecture='amd64'
    ),
}

#: The cluster's cloud nodes, by their site label.
CLOUD = {node for node, labels in NODES.items() if labels[conventions.SITE_LABEL] == conventions.Site.CLOUD}


def holds(expression: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether one selector requirement holds of `labels`."""
    key = cast('str', expression['key'])
    values = cast('list[str]', expression.get('values', []))
    match expression['operator']:
        case 'In':
            return labels.get(key) in values
        case 'NotIn':
            return labels.get(key) not in values
        case 'Exists':
            return key in labels
        case 'DoesNotExist':
            return key not in labels
        case operator:
            raise AssertionError(f'no reading of {operator!r} here')


def matches(selector: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """Whether a present label selector matches `labels`: every `matchLabels` pair and every `matchExpressions` requirement holds."""
    match_labels = cast('dict[str, str]', selector.get('matchLabels', {}))
    expressions = cast('list[dict[str, Any]]', selector.get('matchExpressions', []))
    return all(labels.get(key) == value for key, value in match_labels.items()) and all(
        holds(it, labels) for it in expressions
    )


def selected[N](selector: Mapping[str, Any] | None, nodes: Mapping[N, Mapping[str, str]]) -> set[N]:
    """The nodes of `nodes` a label selector selects (`matches`); an absent selector selects every node."""
    if selector is None:
        return set(nodes)
    return {node for node, labels in nodes.items() if matches(selector, labels)}


def schedulable(spec: Mapping[str, Any]) -> set[str]:
    """The cluster's nodes a pod of `spec` may be scheduled to: `schedulable_among` the cluster's `NODES`."""
    return schedulable_among(spec, NODES)


def schedulable_among[N](spec: Mapping[str, Any], nodes: Mapping[N, Mapping[str, str]]) -> set[N]:
    """The nodes of `nodes` a pod of `spec` may be scheduled to by its node selector and its required node affinity."""
    node_selector = cast('dict[str, str]', spec.get('nodeSelector') or {})
    affinity = cast('dict[str, Any]', spec.get('affinity') or {})
    required = affinity.get('nodeAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution')

    def admits(labels: Mapping[str, str]) -> bool:
        if any(labels.get(key) != value for key, value in node_selector.items()):
            return False
        if required is None:
            return True
        return any(
            term.get('matchExpressions') and all(holds(it, labels) for it in term['matchExpressions'])
            for term in cast('list[dict[str, Any]]', required['nodeSelectorTerms'])
        )

    return {node for node, labels in nodes.items() if admits(labels)}
