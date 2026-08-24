"""The `k8s-base` stack: everything cluster-scoped that speaks the k8s API.

Gateway API CRDs, Cilium, sealed-secrets, cert-manager, CNPG and VolSync,
VictoriaMetrics, NFD and the GPU plugin, and the small standing set the legacy
cluster proved — in that dependency order, per
docs/declarative/cluster-infra.md. The component list is closed: additions
argue for themselves in writing first.
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('k8s-base stack: see docs/declarative/cluster-infra.md')
