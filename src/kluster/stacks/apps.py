"""The `apps` stack: the applications and everything that travels with them.

Each app component owns its namespace, workload, storage and backups, its
exposure (routes, gateways, the AdGuard rewrites) and its DNS records —
the contract in docs/declarative/workloads.md. This is the daily driver: most
deployments touch only this stack.
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('apps stack: see docs/declarative/workloads.md')
