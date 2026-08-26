"""Regenerate the CRD bindings in `packages/crds` from the pinned chart set.

`pins` is the register: the chart set of cluster-infra.md §1, each entry with
the version floor it has to clear. `sources` turns those pins into CRD YAML
without touching a cluster, and `cli` hands the result to `crd2pulumi`.
"""

from kluster.scripts.update_crds.cli import main

__all__ = ('main',)
