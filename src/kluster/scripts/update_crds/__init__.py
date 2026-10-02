"""Regenerate the CRD bindings in `packages/crds` from the pinned chart set.

The chart set is the `versions:` block of `Pulumi.yaml`, read through
`kluster.lib.versions`; `pins` holds what only this script reads. `sources`
turns the pins into CRD YAML without touching a cluster, `cli` hands the result
to `crd2pulumi`, and `record` writes beside the bindings the pins they were
generated from.
"""

from kluster.scripts.update_crds.cli import main

__all__ = ('main',)
