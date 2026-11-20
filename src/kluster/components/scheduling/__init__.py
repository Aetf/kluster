"""Scheduling: the `PriorityClass` a pod held by its constraints runs at (declarative/workloads.md §1).

A pod is held by its constraints where a site or an architecture leaves it
only some nodes to run on (`kluster.lib.k8s.Constraints`). The scheduler
fills a node by requests, so without a priority a free pod that reaches the
node first can leave a held pod pending, since the held pod can run nowhere
else. At this class the scheduler preempts the free pod instead, and the
free pod starts again on a node it fits. The class is cluster-scoped, so
`k8s-base` declares it (declarative/cluster-infra.md §1), under the name a
held pod's constraints give it (`conventions.HELD_PRIORITY_CLASS`).

Its value is above 0, the priority of every pod that names no class, since
no class is the global default. The system's own classes, which start at two
billion and which some charts' pods name (metrics-server's, the
`local-path` helper's), are above it, so a held pod never preempts one of
them. It preempts, the default policy, stated because preemption is what
the class is for.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from putils import Component

__all__ = ('HELD_PRIORITY', 'HeldPriority')

#: The held class's value: above 0, a pod's priority when it names no class,
#: and below every class the system reserves.
HELD_PRIORITY = 1000


class HeldPriority(Component, pulumi_type='kluster:scheduling:HeldPriority'):
    """The held `PriorityClass`: a held pod preempts a free one to take a node it fits.

    `after` is what the class is installed behind: Cilium, as every
    component is. It is stated on the child rather than left to the
    component's own `depends_on`, which Pulumi does not push down to a
    component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        self.priority_class = k8s.scheduling.v1.PriorityClass(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=conventions.HELD_PRIORITY_CLASS),
            value=HELD_PRIORITY,
            global_default=False,
            preemption_policy='PreemptLowerPriority',
            description=(
                'A pod its constraints hold to some nodes, by a site or an architecture: '
                'it preempts a free pod to take a node it fits (declarative/workloads.md §1).'
            ),
            opts=self.child_opts(depends_on=list(after)),
        )

        self.register_outputs({})
