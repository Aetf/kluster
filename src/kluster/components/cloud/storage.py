"""The cloud site's block volumes: one per entry of the fleet's volume table.

A volume here is data that outlives the container using it and is not part of
any backup regime — its redundancy is elsewhere, in the network the dataset
came from or in the other replicas of it (docs/cluster/storage.md §3.3). That
combination is exactly what `protect=True` is for. The attachment carries the
same protection, because detaching a disk and deleting it cost the same thing:
a node replacement that silently proposes a detach is the failure this catches,
and node replacement is already an explicit, reviewed procedure (physical.md
§2).

A volume is not given a device path. OCI's consistent-device-path feature is
gated on the image advertising support for it, which a Talos custom image does
not; the disk is selected by its properties in machine configuration instead.
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci

from kluster import conventions
from putils import Component


class NodeVolume(Component):
    """One block volume, attached to the node that mounts it."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        availability_domain: pulumi.Input[str],
        instance_id: pulumi.Input[str],
        size_gb: int,
        vpus_per_gb: int = conventions.NODE_VOLUME_VPUS,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.size_gb = size_gb

        self.volume = oci.core.Volume(
            f'{name}-volume',
            compartment_id=compartment_id,
            availability_domain=availability_domain,
            display_name=name,
            size_in_gbs=str(size_gb),
            vpus_per_gb=str(vpus_per_gb),
            # Data-bearing and outside every backup regime (storage.md §3.3).
            opts=self.child_opts(protect=True),
        )

        self.attachment = oci.core.VolumeAttachment(
            f'{name}-attachment',
            # Paravirtualized rather than iSCSI: an iSCSI attachment needs a
            # login the node would have to perform, and Talos ships no agent
            # to perform it.
            attachment_type='paravirtualized',
            instance_id=instance_id,
            volume_id=self.volume.id,
            display_name=name,
            opts=self.child_opts(protect=True),
        )

        self.register_outputs({})
