"""The Talos image: a pinned Image Factory schematic, imported into OCI.

There is no official Talos image in OCI's catalogue, so one is built by the
Image Factory and imported as a custom image. The schematic id is part of the
declared state, which is what makes the image's contents reproducible rather
than a thing someone once uploaded.

The homelab worker consumes the same schematic through a different artefact
(a nocloud image for libvirt), so the schematic lives here rather than inside
the OCI half.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pulumi
import pulumi_oci as oci
from pulumiverse_talos import imagefactory

from putils import Component, async_output, resolve

#: The cloud nodes are ARM; the homelab worker is x86.
CLOUD_ARCHITECTURE = 'arm64'
HOMELAB_ARCHITECTURE = 'amd64'

#: The GPU cutover needs the i915 firmware present *before* it happens, so the
#: worker's schematic carries it from day 0 — harmless without a GPU, and the
#: cutover then touches no OS image (physical/homelab-host.md §3).
HOMELAB_EXTENSIONS = ('siderolabs/i915',)


class TalosImage(Component):
    """One schematic and the OCI custom image built from it."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        talos_version: str,
        extensions: Sequence[str] = (),
        architecture: str = CLOUD_ARCHITECTURE,
        platform: str = 'oracle',
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.talos_version = talos_version
        self.architecture = architecture
        self.platform = platform

        self.schematic = imagefactory.Schematic(
            f'{name}-schematic',
            schematic=_schematic_document(extensions),
            opts=self.child_opts(),
        )

        self.image = oci.core.Image(
            f'{name}-image',
            compartment_id=compartment_id,
            display_name=async_output(self._image_name),
            launch_mode='PARAVIRTUALIZED',
            image_source_details=oci.core.ImageImageSourceDetailsArgs(
                source_type='objectStorageUri',
                source_uri=async_output(self._disk_url),
                source_image_type='QCOW2',
            ),
            # Rebuilding an image is cheap; replacing one out from under
            # running nodes is not, so a version bump creates the new image
            # before anything stops using the old one.
            opts=self.child_opts(delete_before_replace=False),
        )

        self.register_outputs({})

    async def _image_name(self) -> str:
        schematic_id = await resolve(self.schematic.id)
        return f'talos-{self.talos_version}-{self.architecture}-{str(schematic_id)[:12]}'

    async def _disk_url(self) -> str:
        """The factory's disk image for this schematic, architecture and platform."""
        schematic_id = await resolve(self.schematic.id)
        urls = await imagefactory.get_urls_output(
            talos_version=self.talos_version,
            schematic_id=schematic_id,
            platform=self.platform,
            architecture=self.architecture,
        ).future()
        assert urls is not None
        return urls.urls.disk_image


def _schematic_document(extensions: Sequence[str]) -> str:
    """The factory's customization document, as YAML-shaped JSON."""
    return json.dumps({'customization': {'systemExtensions': {'officialExtensions': list(extensions)}}})
