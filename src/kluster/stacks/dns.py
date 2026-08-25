"""The `dns` stack: zones, the estate records that belong to no app, anchors.

Per-app records live beside their apps in `apps` (docs/declarative/dns.md);
what lands here is what has no app to co-locate with — mail, the ZeroTier host
block, verifications, the family and alias zones — plus the anchors every app
record points at, plus the split-horizon rewrites for every app: they are
read from the same plain-data route declaration `apps` builds its routes
from, and they are the reason this is the one stack that joins ZeroTier.
"""

from __future__ import annotations


async def main() -> None:
    raise NotImplementedError('dns stack: see docs/declarative/dns.md')
