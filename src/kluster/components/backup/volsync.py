"""VolSync: the operator that backs up a PVC with restic into the backup bucket.

The operator alone, with no secret: a `backed_pvc` declares its own
`ReplicationSource`, and the restic repository's password and the writer key
for its prefix arrive with it (rfc-007 §8). Installed after cert-manager, with
the database operator (declarative/cluster-infra.md §1, item 5).

Its namespace is `restricted`: the chart's security contexts meet it, and the
movers that touch a volume run in the namespace of the PVC they back up, not
here (cluster-infra.md §0).
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster.lib.k8s import PodSecurity, helm_chart, namespace
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = ('NAMESPACE', 'VolSync')

#: The namespace the operator runs in, its upstream's default. It watches
#: every namespace, so nothing else names this one.
NAMESPACE = 'volsync-system'


class VolSync(Component, pulumi_type='kluster:backup:VolSync'):
    """The VolSync operator and its definitions, from the chart's pin, in a namespace of their own.

    `after` is what the operator is installed behind. It is stated on the
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
        self.chart = helm_chart(name, pin=chart, namespace=self.namespace.metadata.name, opts=behind)

        self.register_outputs({})
