"""How the VictoriaMetrics operator names and serves a vmsingle: facts of the operator, which a store's readers address it by."""

from __future__ import annotations

__all__ = ('VMSINGLE_PORT', 'vmsingle_workload')

#: The port the operator serves a vmsingle's HTTP API at when its spec sets
#: no `port`.
VMSINGLE_PORT = 8428


def vmsingle_workload(vmsingle: str) -> str:
    """The name the operator gives a vmsingle object's Deployment, Service and claim: its `PrefixedName`."""
    return f'vmsingle-{vmsingle}'
