"""The volume snapshot definitions: the `snapshot.storage.k8s.io` kinds, served for VolSync's controller.

VolSync's manager watches `VolumeSnapshot` objects whatever a source's
`copyMethod`, and its caches never sync on a cluster that does not serve the
kind, so the controller cannot start without these definitions
(docs/cluster/storage.md §3.1). It creates a snapshot only for
`copyMethod: Snapshot`, and every source and destination this installation
declares copies `Direct` (`kluster.components.apps.storage`), so nothing
creates one and no snapshot controller runs: the definitions are served and
nothing more.

They come from external-snapshotter, which publishes them only in its source
tree, so the stack program fetches them through a `versions:source-<name>` pin
(`kluster.lib.source_files`) and hands them here as text. Installed ahead of
VolSync (declarative/cluster-infra.md §1, item 5).
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_kubernetes as k8s

from putils import Component

__all__ = ('SnapshotDefinitions',)


class SnapshotDefinitions(Component, pulumi_type='kluster:backup:SnapshotDefinitions'):
    """The definitions, applied as upstream wrote them.

    `definitions` is the text of each file the pin names, one or more YAML
    documents each, applied as one stream. `after` is what they are applied
    behind, stated on the child as VolSync states its own.
    """

    def __init__(
        self,
        name: str,
        *,
        definitions: Sequence[str],
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        self.definitions = k8s.yaml.v2.ConfigGroup(
            name, yaml='\n---\n'.join(definitions), opts=self.child_opts(depends_on=list(after))
        )

        self.register_outputs({})
