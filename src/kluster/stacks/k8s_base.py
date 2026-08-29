"""The `k8s-base` stack: everything cluster-scoped that speaks the k8s API.

Gateway API CRDs, Cilium, sealed-secrets, cert-manager, CNPG and VolSync,
VictoriaMetrics, NFD and the GPU plugin, and the small standing set the legacy
cluster proved — in that dependency order, per
docs/declarative/cluster-infra.md. The component list is closed: additions
argue for themselves in writing first.

Its components will live in areas of `kluster/components/`, the way `physical`
composes the areas it declares: this module stays the wiring, one component per
entry of the closed list. What every component shares — installing a pinned
chart, sealing a secret, labelling a Service into a load-balancer pool — is in
`kluster.lib.k8s`.

Two things gate the implementation, both recorded rather than assumed. The
chart set is pinned on first contact (declarative/README.md, "Deliberately not
pre-decided"), and the pins are stack configuration so that renovate can bump
them. The custom resources — the Cilium pools, BGP configuration and
Gateways — need bindings this repository does not have yet: `packages/crds`
still holds the legacy cluster's, and is regenerated against the new chart set
once that set exists.
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('k8s-base stack: see docs/declarative/cluster-infra.md')
