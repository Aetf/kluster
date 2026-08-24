"""The `dns` stack: zones, the estate records that belong to no app, anchors.

Per-app records live beside their apps in `apps` (docs/declarative/dns.md);
what lands here is what has no app to co-locate with — mail, the ZeroTier host
block, verifications, the family and alias zones — plus the anchors every app
record points at.
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('dns stack: see docs/declarative/dns.md')
