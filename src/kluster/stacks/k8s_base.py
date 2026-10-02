"""The `k8s-base` stack: everything cluster-scoped that speaks the k8s API.

Gateway API CRDs, Cilium, sealed-secrets, cert-manager, CNPG and VolSync,
VictoriaMetrics, NFD and the GPU plugin, and the small standing set the legacy
cluster proved — in that dependency order, per
docs/declarative/cluster-infra.md. The component list is closed: additions
argue for themselves in writing first.

Its components will live in areas of `kluster/components/`, the way `physical`
composes the areas it declares: this module stays the wiring, one component per
entry of the closed list. What every component shares — installing a pinned
chart, sealing a secret, labeling a Service into a load-balancer pool — is in
`kluster.lib.k8s`.

What the program builds today is the provider every one of those components
will be declared through, and nothing else: one Kubernetes provider, opened
with the cluster-admin kubeconfig in this stack's own configuration
(rfc-007 §3.1). The `physical` stack generates it, and it cannot cross the
stack boundary by StackReference, the way the machine facts do
(declarative/README.md §2): `physical` is encrypted under a passphrase of its
own (rfc-005 §5.1), and a StackReference elides the secrets the reading stack
cannot decrypt. So `credentials derived sync --only kubeconfig` copies it out
of `physical`'s state into this stack's configuration, and it is read so that
anything but a kubeconfig stops the run (`kluster.lib.k8s.kubeconfig_from`).

What gates the implementation is recorded rather than assumed: the chart set is
pinned on first contact (declarative/README.md, "Deliberately not
pre-decided"), and each chart is a project-level `versions:chart-<name>` pin in
`Pulumi.yaml`'s `config:` block, which renovate moves (framework/pulumi.md
§3.2); each component that installs a chart will read its pin through
`kluster.lib.versions`. The custom
resources — the Cilium pools, BGP configuration and Gateways — will be written
against `packages/crds`, which `uv run update_crds` regenerates from the same
pins.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.k8s import KUBECONFIG_KEY, kubeconfig_from


async def main() -> None:
    config = pulumi.Config()

    # Read so that anything but a kubeconfig stops the run, naming what it
    # found: no copy in this stack's configuration, or one that is blank or
    # the unknown sentinel a targeted apply exports. The provider would read
    # each as an unreachable cluster -- plain resources previewing green by
    # echoing their inputs -- or, handed nothing, fall back to the shell's
    # `$KUBECONFIG`.
    _ = k8s.Provider(
        f'{conventions.CLUSTER_NAME}-kubernetes',
        kubeconfig=kubeconfig_from(config.get_secret(KUBECONFIG_KEY)),
    )
