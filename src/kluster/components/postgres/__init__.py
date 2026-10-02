"""CloudNativePG: the operator every database in the cluster runs under, and its backup plugin.

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
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster.lib.k8s import PodSecurity, helm_chart, namespace
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = ('NAMESPACE', 'PostgresOperator')

#: The namespace the operator and its backup plugin run in: the operator's
#: upstream default, which the plugin's documentation installs into too.
NAMESPACE = 'cnpg-system'


class PostgresOperator(Component, pulumi_type='kluster:postgres:PostgresOperator'):
    """The CloudNativePG operator and the Barman Cloud plugin, each from its pin, in one namespace.

    `after` is what both are installed behind. It is stated on the children
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

        self.namespace = namespace(
            f'{name}-namespace', name=NAMESPACE, pod_security=PodSecurity.RESTRICTED, opts=behind
        )
        self.operator = helm_chart(name, pin=operator_chart, namespace=self.namespace.metadata.name, opts=behind)
        self.barman_cloud = helm_chart(
            f'{name}-barman-cloud',
            pin=barman_cloud_chart,
            namespace=self.namespace.metadata.name,
            opts=self.child_opts(depends_on=[*after, self.operator]),
        )

        self.register_outputs({})
