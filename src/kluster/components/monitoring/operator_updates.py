"""The VictoriaMetrics operator's updates, made whole for a field a resource stops setting.

**The defect.** The operator writes a workload only when it sees a
difference between its rendering of the resource and the live object, and
its comparison skips a difference whose desired value is a nil pointer, an
empty string or an empty slice (`fieldDiffRecorder.Report`, `diff.go` L36-52
of `internal/controller/operator/factory/reconcile/` at operator v0.74.0). So
a field the resource stops setting whole -- an `affinity`, the last
toleration, a `priorityClassName` -- is never seen as a change, and the
workload keeps it for as long as nothing else of it changes. A change to
the object's labels or annotations is seen, and on one the operator replaces
the workload's whole spec with its rendering, the removed field gone with
the rest (`deploy.go` L65-78, `statefulset.go` L140-153 there). Nothing
reports the miss: the operator logs a successful reconcile.

**The layer** marks each VM resource the component declares with a digest
of what it is rendered from, as the annotation `RENDERED_FROM` in its
`spec.managedMetadata`, which the operator copies onto every object it
renders from the resource. A change to anything the resource is rendered
from changes the digest, which the operator sees as a change of metadata,
and the workload takes the resource's rendering whole; a change that reaches
no pod template rolls no pod. A chart resource's digest is of the chart and
the whole of the values (`marked_values`), since each such resource's spec
is rendered from values outside its own key too, and the chart's version
pins the operator; the component's own resource's is of its spec and the
chart (`marked_spec`). Callers ask for nothing new: the component's values
and specs are what they would be without the defect, and pass through here
last.

**What the marks leave out.** The history store's digest leaves out the
operator's own configuration, the stack's values under
`victoria-metrics-operator`. A default the operator is configured with there,
such as one of its `VM_VMSINGLEDEFAULT_` variables, changes what it renders
the history store as without moving the store's mark; a field such a change
drops stays on that one workload. Today those values set nothing the
operator renders a resource from.

**Its boundary** is everything that exists for the layer: every mention of
`operator_updates`, `RENDERED_FROM` or `tests/test_operator_updates.py`, the
controller manager's pin with every procedure that moves it, and the harness
the tripwire alone runs beside the server. Today that is:

-   this module, and its two call sites in `components.monitoring`: the last
    step of `stack_values`, and the history store's `spec=`;
-   the paragraph of that package's docstring that points here, and the
    sentence of `stack_values`' docstring that names this step;
-   `tests/test_operator_updates.py`, which holds the layer's own cases, the
    tripwire, and the operator image the tripwire runs;
-   in `tests/control_plane.py`, the `kube-controller-manager` entry of
    `CONTROL_PLANE_PINS`, `running`'s `controllers`, `ApiServer.alongside`,
    and the docstring paragraph that names them, all of which no other suite
    uses (a suite that comes to use them keeps them, and the pin with them);
-   the sentence of `tests/pinned_image.py`'s docstring that names the
    operator's pin;
-   the controller manager in the Kubernetes-version row of
    docs/operations.md and in the Talos rule's note in `renovate.json5`;
-   the layer's entries in docs/style/README.md and
    docs/framework/testing.md.

**Removal**: once the operator applies a field removed from its resource by
itself, which the tripwire's case of the defect observes on the pinned
operator, red, for both a Deployment and a StatefulSet. Removing the layer
deletes its boundary. The deploy that ships the removal takes the annotation
off each resource and the operator off each object, one more whole write of
each spec, which rolls nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from kluster import conventions
from kluster.lib.versions import ChartPin

__all__ = ('CHART_RESOURCES', 'RENDERED_FROM', 'marked_spec', 'marked_values')

#: The annotation each VM resource carries in `spec.managedMetadata`: the
#: digest of what it is rendered from.
RENDERED_FROM = f'{conventions.LABEL_DOMAIN}/rendered-from'

#: The stack chart's values keys whose `spec` is a resource of a kind that
#: carries `spec.managedMetadata` and that the component configures, which
#: the chart renders, at 0.91.2, from the key's `spec` with the chart's own
#: fields merged in. `tests/test_operator_updates.py` holds it to every such
#: key, by the record `update_crds` keeps of the chart's values.
CHART_RESOURCES = ('alertmanager', 'vmagent', 'vmalert', 'vmsingle')


def marked_values(chart: ChartPin, values: Mapping[str, object]) -> dict[str, object]:
    """The stack chart's `values`, each resource of `CHART_RESOURCES` marked with the digest of `chart` and `values`."""
    digest = _digest(chart, {'values': values})
    marked = dict(values)
    for key in CHART_RESOURCES:
        settings = values.get(key)
        if not isinstance(settings, Mapping):
            raise ValueError(f'the values configure no {key}, whose resource the layer marks')
        spec = settings.get('spec', {})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- values maps of str keys
        marked[key] = {**settings, 'spec': _marked(spec, digest)}  # pyright: ignore[reportUnknownArgumentType] -- values maps of str keys
    return marked


def marked_spec(chart: ChartPin, spec: Mapping[str, object]) -> dict[str, object]:
    """A resource's `spec`, rendered by the operator `chart` installs, marked with the digest of both."""
    return _marked(spec, _digest(chart, {'spec': spec}))


def _digest(chart: ChartPin, rendered_from: Mapping[str, object]) -> str:
    """The sha256 of `chart` and `rendered_from`, in one spelling of each."""
    document = json.dumps({'chart': [chart.reference, chart.version], **rendered_from}, sort_keys=True)
    return hashlib.sha256(document.encode()).hexdigest()


def _marked(spec: Mapping[str, Any], digest: str) -> dict[str, object]:
    """`spec` with `RENDERED_FROM` at `digest`, merged into whatever `managedMetadata` it carries."""
    metadata: Mapping[str, Any] = spec.get('managedMetadata', {})
    annotations: Mapping[str, str] = metadata.get('annotations', {})
    if RENDERED_FROM in annotations:
        raise ValueError(f'the spec sets {RENDERED_FROM} itself, which the layer alone sets')
    return {**spec, 'managedMetadata': {**metadata, 'annotations': {**annotations, RENDERED_FROM: digest}}}
