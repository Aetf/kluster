"""cert-manager: the controller every certificate in the cluster is issued through.

Installed after the sealing controller (declarative/cluster-infra.md §1, item
4), because the credential its ACME solver answers DNS-01 challenges with is a
sealed value. The issuer and the certificates are later pieces of this same
component, once that value is sealed (rfc-007 §5.2, §14).

The chart ships its definitions as templates behind a value rather than in
its `crds/` directory, so they are switched on here; and its start-up check is
a Helm hook, which `helm.v4.Chart` drops, so it is switched off rather than
left to vanish (cluster-infra.md §1.2). The wait it did is the install order's:
nothing declares an issuer or a certificate before the controller is up.

Its namespace is `restricted`: the chart's own security contexts meet it.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi

from kluster import conventions
from kluster.lib.k8s import PodSecurity, helm_chart, namespace
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = ('CertManager', 'chart_values')


def chart_values() -> dict[str, object]:
    """The chart's values: its definitions installed, and its start-up check, a hook, off."""
    return {
        'crds': {'enabled': True},
        'startupapicheck': {'enabled': False},
    }


class CertManager(Component, pulumi_type='kluster:certificates:CertManager'):
    """The cert-manager controller, its webhook and its definitions, in a namespace of their own.

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
            f'{name}-namespace',
            name=conventions.CERT_MANAGER_NAMESPACE,
            pod_security=PodSecurity.RESTRICTED,
            opts=behind,
        )
        self.chart = helm_chart(
            name,
            pin=chart,
            namespace=self.namespace.metadata.name,
            values=chart_values(),
            opts=behind,
        )

        self.register_outputs({})
