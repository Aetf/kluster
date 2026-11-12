"""cert-manager: the controller every certificate in the cluster is issued through, its issuer and the Gateways' certificates.

Installed after the sealing controller (declarative/cluster-infra.md §1, item
4), because the credential its ACME solver answers DNS-01 challenges with is a
sealed value.

The chart ships its definitions as templates behind a value rather than in
its `crds/` directory, so they are switched on here; and its start-up check is
a Helm hook, which `helm.v4.Chart` drops, so it is switched off rather than
left to vanish (cluster-infra.md §1.2). The issuer and the certificates,
which are instances of the chart's definitions, are declared behind the
chart, which covers two things: the definitions exist, and the chart's
Deployments, the webhook's among them, are ready. It is not the check's
own wait, for the API server to reach the webhook, which also needs the
webhook's CA injected into its configuration first.

**One `ClusterIssuer`, Let's Encrypt over DNS-01** (rfc-007 §5.2), whose
Cloudflare token is the sealed value `conventions.sealed.DNS01_TOKEN`, in
cert-manager's own namespace, the one a cluster issuer's credentials are read
from under the chart's defaults. **One certificate per served zone, for the
apex and the wildcard together** (dns.md §4), each issued into the Secret the
Gateways' listeners for that zone read, in the Gateways' namespace. A renewal
reaches the listeners with nothing restarted: Cilium's operator copies the
Secret into its own namespace and the agent pushes it to Envoy over SDS
(cluster-infra.md §2).

While the token is absent from the stack's configuration, no SealedSecret is
declared for it, and the issuer and the certificates are declared all the same
and stay pending until it is sealed in (`kluster.lib.k8s.sealed_value`).

Its namespace is `restricted`: the chart's own security contexts meet it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.k8s import helm_chart, namespace, sealed_value
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = ('ACME_DIRECTORY', 'ISSUER', 'CertManager', 'chart_values')

#: The one issuer every certificate names.
ISSUER = 'letsencrypt'

#: Let's Encrypt's production directory, the only certificate authority the
#: cluster's zones authorize (dns.md §1.1).
ACME_DIRECTORY = 'https://acme-v02.api.letsencrypt.org/directory'


def chart_values() -> dict[str, object]:
    """The chart's values: its definitions installed, and its start-up check, a hook, off."""
    return {
        'crds': {'enabled': True},
        'startupapicheck': {'enabled': False},
    }


class CertManager(Component, pulumi_type='kluster:certificates:CertManager'):
    """The cert-manager controller, its webhook and its definitions, its issuer, and one certificate per served zone.

    `after` is what the controller is installed behind. It is stated on the
    children rather than left to the component's own `depends_on`, which
    Pulumi does not push down to a component's children.

    `dns01_token` is the ciphertext the stack's configuration holds for
    `conventions.sealed.DNS01_TOKEN`, `None` while it holds none; the `None`
    is the bring-up's tolerance, and goes with it (physical/bring-up.md §1).
    `certificates` names, per served zone, the Secret the Gateways' listeners
    read that zone's certificate from, in `certificates_namespace`.
    """

    def __init__(
        self,
        name: str,
        *,
        chart: ChartPin,
        after: Sequence[pulumi.Resource],
        dns01_token: Mapping[str, str] | None,
        certificates: Mapping[str, str],
        certificates_namespace: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))

        self.namespace = namespace(f'{name}-namespace', name=conventions.CERT_MANAGER_NAMESPACE, opts=behind)
        self.chart = helm_chart(
            name,
            pin=chart,
            namespace=self.namespace.metadata.name,
            values=chart_values(),
            opts=behind,
        )

        token = conventions.sealed.DNS01_TOKEN
        (token_key,) = token.keys
        # Behind the sealing controller, whose chart defines the kind, and
        # the namespace the value is sealed to.
        self.dns01_token = sealed_value(
            f'{name}-dns01-token',
            token,
            ciphertext=dns01_token,
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )

        # Instances of the chart's definitions, so behind the chart.
        after_the_chart = self.child_opts(depends_on=[self.chart])
        self.issuer = crds.cert_manager.v1.ClusterIssuer(
            f'{name}-issuer',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=ISSUER),
            spec=crds.cert_manager.v1.ClusterIssuerSpecArgs(
                acme=crds.cert_manager.v1.ClusterIssuerSpecAcmeArgs(
                    server=ACME_DIRECTORY,
                    private_key_secret_ref=crds.cert_manager.v1.ClusterIssuerSpecAcmePrivateKeySecretRefArgs(
                        name=f'{ISSUER}-account'
                    ),
                    solvers=[
                        crds.cert_manager.v1.ClusterIssuerSpecAcmeSolversArgs(
                            dns01=crds.cert_manager.v1.ClusterIssuerSpecAcmeSolversDns01Args(
                                cloudflare=crds.cert_manager.v1.ClusterIssuerSpecAcmeSolversDns01CloudflareArgs(
                                    api_token_secret_ref=crds.cert_manager.v1.ClusterIssuerSpecAcmeSolversDns01CloudflareApiTokenSecretRefArgs(
                                        name=token.name, key=token_key
                                    )
                                )
                            )
                        )
                    ],
                )
            ),
            opts=after_the_chart,
        )
        self.certificates = {
            zone: crds.cert_manager.v1.Certificate(
                f'{name}-{zone}',
                metadata=k8s.meta.v1.ObjectMetaArgs(name=zone, namespace=certificates_namespace),
                spec=crds.cert_manager.v1.CertificateSpecArgs(
                    dns_names=[zone, f'*.{zone}'],
                    secret_name=secret,
                    issuer_ref=crds.cert_manager.v1.CertificateSpecIssuerRefArgs(
                        name=ISSUER, kind='ClusterIssuer', group='cert-manager.io'
                    ),
                ),
                opts=after_the_chart,
            )
            for zone, secret in certificates.items()
        }

        self.register_outputs({})
