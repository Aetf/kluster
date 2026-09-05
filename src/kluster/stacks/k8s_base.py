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

What gates the implementation is recorded rather than assumed: the chart set is
pinned on first contact (declarative/README.md, "Deliberately not
pre-decided"), and the pins are stack configuration so that renovate can bump
them. The custom resources — the Cilium pools, BGP configuration and
Gateways — are written against the bindings in `packages/crds`, which
`uv run update_crds` regenerates from the chart set its own register pins.
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('k8s-base stack: see docs/declarative/cluster-infra.md')
