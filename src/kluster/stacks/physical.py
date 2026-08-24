"""The `physical` stack: everything that exists before the Kubernetes API.

OCI network and nodes, the Talos day-1 chain, the homelab worker VM and the
adopted HAOS domain, the UDM's gw-config and firewall, and the B2 buckets —
declared per docs/declarative/physical.md. The state-backend appliance is
deliberately *not* here: it is this program's own prerequisite
(docs/physical/state-backend.md).
"""

from __future__ import annotations


async def main() -> None:
    # The AWS-era program in kluster.physical.aws is reference-only and is
    # replaced wholesale by the OCI + libvirt + gw-config declaration.
    raise NotImplementedError('physical stack: see docs/declarative/physical.md')
