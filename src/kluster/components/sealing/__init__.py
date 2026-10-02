"""The sealed-secrets controller, which opens every SealedSecret the cluster is given.

It generates its own key pair and needs nothing else, so it is installed right
after the CNI and every later component's credentials can be sealed values
(declarative/cluster-infra.md §1, item 3). The chart installs it under the
name and in the namespace `kubeseal` looks for unless told otherwise (rfc-007
§6.1), so `kubeseal` run by hand finds it with no flag, and the `credentials`
command, which names both from `conventions`, reaches the same controller.
Otherwise the controller keeps the chart's
defaults, the thirty-day key renewal among them: renewal adds a key and keeps
the old ones, so a committed ciphertext stays readable.

`kube-system` is under Talos' Pod Security exemption, so this component creates
no namespace (cluster-infra.md §0).
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster import conventions
from kluster.lib.k8s import helm_chart
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = ('SealedSecretsController', 'chart_values')


def chart_values() -> dict[str, object]:
    """The chart's values: its default name replaced by the one `kubeseal` assumes."""
    return {'fullnameOverride': conventions.SEALING_CONTROLLER}


class SealedSecretsController(Component, pulumi_type='kluster:sealing:SealedSecretsController'):
    """The controller and the `SealedSecret` definition, from the chart's pin.

    `after` is what the controller is installed behind. It is stated on the
    chart rather than left to the component's own `depends_on`, which Pulumi
    does not push down to a component's children.
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

        self.chart = helm_chart(
            name,
            pin=chart,
            namespace=conventions.SEALING_NAMESPACE,
            values=chart_values(),
            opts=self.child_opts(depends_on=list(after)),
        )

        self.register_outputs({})
