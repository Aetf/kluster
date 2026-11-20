"""CloudNativePG: the operator every database runs under, its backup plugin, and the restore drills' admission policy.

Installed after cert-manager (declarative/cluster-infra.md §1, item 5). The
operator issues its own webhook certificate, but the Barman Cloud plugin, the
operator's backup and WAL-archiving path, declares cert-manager `Issuer` and
`Certificate` objects for the TLS it speaks with the operator, so its chart
cannot be applied before cert-manager's definitions exist. The plugin installs
into the operator's own namespace, where the operator finds it, and behind the
operator's chart. Neither needs a secret: the archives and the writer keys they
are written with arrive with the first database (rfc-007 §8).

The operator's pin carries the floor the declarative major upgrades need
(workloads.md §4), which `update_crds` checks against the chart's own
`appVersion`.

The namespace is `restricted`: both charts' security contexts meet it, and
nothing either runs needs the host (cluster-infra.md §0).

**The policy that holds the name, labels, annotations and
`inheritedMetadata` of every database drill's cluster to the drill's
manifest** is declared here, behind the operator, whose chart serves the `Cluster`
definition it judges: `drill_admission` of the application base's database
shape, a `ValidatingAdmissionPolicy` and its one binding. Both are
cluster-scoped, and an application declares no cluster-scoped object
(workloads.md §1), so they are this component's; each database's shape
declares the parameters they read in its namespace.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster import conventions
from kluster.components.apps.database import drill_admission
from kluster.lib.k8s import helm_chart, namespace
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = ('PostgresOperator',)


class PostgresOperator(Component, pulumi_type='kluster:postgres:PostgresOperator'):
    """The CloudNativePG operator and its Barman Cloud plugin, from their pins, and the restore drills' admission policy.

    The operator and the plugin share one namespace. `after` is what every
    child is installed behind. It is stated on the children
    rather than left to the component's own `depends_on`, which Pulumi does
    not push down to a component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        operator_chart: ChartPin,
        barman_cloud_chart: ChartPin,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))

        self.namespace = namespace(f'{name}-namespace', name=conventions.CNPG_NAMESPACE, opts=behind)
        self.operator = helm_chart(name, pin=operator_chart, namespace=self.namespace.metadata.name, opts=behind)
        self.barman_cloud = helm_chart(
            f'{name}-barman-cloud',
            pin=barman_cloud_chart,
            namespace=self.namespace.metadata.name,
            opts=self.child_opts(depends_on=[*after, self.operator]),
        )
        self.drill_admission = drill_admission(
            f'{name}-restore-drill', opts=self.child_opts(depends_on=[*after, self.operator])
        )

        self.register_outputs({})
