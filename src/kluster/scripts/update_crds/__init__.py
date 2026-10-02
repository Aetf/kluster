"""Regenerate the CRD bundle in `packages/crds` and the SDK in `sdks/crds` from the pinned chart set.

The chart set is the `versions:` block of `Pulumi.yaml`, read through
`kluster.lib.versions`; `pins` holds what only this script reads. `sources`
turns the pins into CRD YAML without touching a cluster and the selection into
the bundle, `cli` writes the bundle where the `packages:` block names it and
regenerates the SDK from that block with `pulumi install`, and `record` writes
beside the bundle the pins it was rendered from.
"""

from kluster.scripts.update_crds.cli import main

__all__ = ('main',)
