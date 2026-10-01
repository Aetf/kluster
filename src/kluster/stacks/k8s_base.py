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
with the kubeconfig the `physical` stack publishes. That is a machine fact, so
it crosses the stack boundary by StackReference (declarative/README.md §2,
rfc-007 §3.1), and it is read so that anything but a kubeconfig stops the run
(`kluster.lib.k8s.kubeconfig_from`). Once `physical` is under a passphrase of
its own (rfc-005 §5.1), a StackReference can no longer carry that secret, and
the kubeconfig reaches this stack through its own configuration instead
(kluster-ops#487).

What gates the implementation is recorded rather than assumed: the chart set is
pinned on first contact (declarative/README.md, "Deliberately not
pre-decided"), and the pins are stack configuration so that renovate can bump
them. The custom resources — the Cilium pools, BGP configuration and Gateways —
will be written against `packages/crds`, which `uv run update_crds` regenerates
from the chart set its own register pins.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.k8s import kubeconfig_from


async def main() -> None:
    physical = pulumi.StackReference(
        f'{pulumi.get_organization()}/{pulumi.get_project()}/{conventions.STACK_NAMES.physical}'
    )

    # Read so that anything but a kubeconfig stops the run, naming what it
    # found: an absent output, a secret this stack cannot decrypt, the unknown
    # sentinel a targeted apply exports. The provider would read each as an
    # unreachable cluster -- plain resources previewing green by echoing their
    # inputs -- or, handed nothing, fall back to the shell's `$KUBECONFIG`.
    _ = k8s.Provider(f'{conventions.CLUSTER_NAME}-kubernetes', kubeconfig=kubeconfig_from(physical))
