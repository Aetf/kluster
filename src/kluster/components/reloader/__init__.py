"""Reloader: rolls a workload when a ConfigMap or Secret it reads changes.

One of the small standing set the legacy cluster proved, with no ordering
constraint beyond the CNI (declarative/cluster-infra.md §1, item 8).

Its namespace is `restricted`, the level an application gets, because nothing
it runs needs the host. The chart's pod security context already meets that
level, and its container security context is empty, with what `restricted`
asks of a container written out only as comments in the chart's own values;
so the component sets it (cluster-infra.md §0, rfc-007 §8).
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster.lib.k8s import PodSecurity, helm_chart, namespace
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = ('NAMESPACE', 'Reloader', 'chart_values')

#: The namespace the controller runs in. It watches every namespace, so
#: nothing else names this one.
NAMESPACE = 'reloader'


def chart_values() -> dict[str, object]:
    """The chart's values: a container that meets `restricted`, on a read-only root.

    The read-only root is the chart's own switch rather than the container
    context's field, because the switch also mounts a writable `/tmp`.
    """
    return {
        'reloader': {
            'readOnlyRootFileSystem': True,
            'deployment': {
                'containerSecurityContext': {
                    'allowPrivilegeEscalation': False,
                    'capabilities': {'drop': ['ALL']},
                },
            },
        },
    }


class Reloader(Component, pulumi_type='kluster:reloader:Reloader'):
    """The Reloader controller, in a namespace of its own.

    `after` is what the controller is installed behind. It is stated on the
    children rather than left to the component's own `depends_on`, which
    Pulumi does not push down to a component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        chart: ChartPin,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))

        self.namespace = namespace(
            f'{name}-namespace', name=NAMESPACE, pod_security=PodSecurity.RESTRICTED, opts=behind
        )
        self.chart = helm_chart(
            name,
            pin=chart,
            namespace=self.namespace.metadata.name,
            values=chart_values(),
            opts=behind,
        )

        self.register_outputs({})
