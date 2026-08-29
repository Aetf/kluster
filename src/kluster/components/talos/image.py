"""The Talos images: pinned Image Factory schematics, and what is built from them.

The fleet runs two artefacts of the same family, and neither of them is a stock
image:

-   the **cloud nodes** boot an OCI custom image. There is no official Talos
    image in OCI's catalogue, so one is built by the Image Factory and imported.
-   the **homelab worker** boots a `nocloud` disk image written into a libvirt
    volume. Its schematic is not the cloud one — it carries the i915 firmware
    the Wave C GPU cutover needs present from day 0 (physical/homelab-host.md
    §3), and the cloud nodes have no use for it — so what the two share is the
    shape rather than the resource: one schematic each, one way of asking the
    factory where its artefact is.

The schematic id is part of the declared state either way, which is what makes
an image's contents reproducible rather than a thing someone once uploaded.

**The worker's artefact takes a detour through the machine running the
program.** The factory serves `nocloud` as `.raw.xz`; the libvirt provider does
not decompress an xz source (dmacvicar/terraform-provider-libvirt#390) and a
raw image cannot back a copy-on-write chain, so there is no way to hand libvirt
the factory URL directly. `FactoryImage` — the custom provider in
`kluster.providers.talos_factory` — fetches and decompresses it where the
program runs, and the libvirt volume is created *from that file*, which the
provider uploads into the pool over its own connection.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pulumi
import pulumi_oci as oci
from pulumiverse_talos import imagefactory

from kluster.providers.talos_factory import FactoryImage
from putils import Component, async_output, resolve

#: The cloud nodes are ARM; the homelab worker is x86.
CLOUD_ARCHITECTURE = 'arm64'
HOMELAB_ARCHITECTURE = 'amd64'

#: Which platform's artefact each half boots: OCI's own image format, and the
#: `nocloud` variant that reads its machine configuration off a seed image
#: because there is no metadata service under libvirt.
CLOUD_PLATFORM = 'oracle'
HOMELAB_PLATFORM = 'nocloud'

#: The GPU cutover needs the i915 firmware present *before* it happens, so the
#: worker's schematic carries it from day 0 — harmless without a GPU, and the
#: cutover then touches no OS image (physical/homelab-host.md §3).
HOMELAB_EXTENSIONS = ('siderolabs/i915',)

#: Where a decompressed artefact is kept on the machine running the program.
#: The path is a constant rather than a home-relative or temporary directory on
#: purpose: it is an *input* of the libvirt volume, so a workstation and a CI
#: runner that spelled it differently would each propose replacing the worker's
#: disk with an identical image. `/var/tmp` is disk-backed and survives a
#: reboot, which a 1.25 GB artefact wants and `/tmp` does not promise.
IMAGE_CACHE = Path('/var/tmp/kluster-talos-images')


class TalosArtefact(Component):
    """A pinned Image Factory schematic, and where the factory serves it.

    The half both artefacts share. A subclass adds what is done with the URL:
    imported into a cloud catalogue, or fetched onto the machine running the
    program.
    """

    def __init__(
        self,
        name: str,
        *,
        talos_version: str,
        extensions: Sequence[str] = (),
        architecture: str,
        platform: str,
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

    async def _disk_url(self) -> str:
        """The factory's disk image for this schematic, architecture and platform."""
        schematic_id = await resolve(self.schematic.id)
        urls = await imagefactory.get_urls_output(
            talos_version=self.talos_version,
            schematic_id=schematic_id,
            platform=self.platform,
            architecture=self.architecture,
            # Parented like every other invoke here. The factory is not an
            # account this program authenticates to, so what it inherits is the
            # image factory's own default provider — the parent carries none
            # for that package.
            opts=pulumi.InvokeOptions(parent=self),
        ).future()
        assert urls is not None
        return urls.urls.disk_image


# Both type tokens are stated rather than derived. `Component` only computes one
# for a class that does not already have it, and a subclass inherits its base's —
# so leaving them out would file every artefact in the state under
# `TalosArtefact` and rename resources the day the hierarchy changes.
class TalosImage(TalosArtefact, pulumi_type='kluster:physical:image:TalosImage'):
    """One schematic and the OCI custom image built from it."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        talos_version: str,
        extensions: Sequence[str] = (),
        architecture: str = CLOUD_ARCHITECTURE,
        platform: str = CLOUD_PLATFORM,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            name,
            talos_version=talos_version,
            extensions=extensions,
            architecture=architecture,
            platform=platform,
            opts=opts,
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


class TalosNocloudImage(TalosArtefact, pulumi_type='kluster:physical:image:TalosNocloudImage'):
    """The worker's schematic, and its disk image on the machine that runs the program.

    `path` is what a libvirt volume is created from. The volume's size is then
    the image's size and cannot be declared — the provider refuses `size`
    alongside `source` — so the worker's disk reaches its working size through
    the same host-side step that grows it later (physical/homelab-host.md §1).
    """

    def __init__(
        self,
        name: str,
        *,
        talos_version: str,
        extensions: Sequence[str] = HOMELAB_EXTENSIONS,
        architecture: str = HOMELAB_ARCHITECTURE,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            name,
            talos_version=talos_version,
            extensions=extensions,
            architecture=architecture,
            platform=HOMELAB_PLATFORM,
            opts=opts,
        )

        self.artefact = FactoryImage(
            f'{name}-nocloud',
            url=async_output(self._disk_url),
            path=async_output(self._local_path),
            opts=self.child_opts(),
        )
        #: The decompressed image, where a libvirt volume can be created from it.
        self.path: pulumi.Output[str] = self.artefact.path

        self.register_outputs({})

    async def _local_path(self) -> str:
        """Where this artefact lives locally, named by what it contains.

        The schematic id and the Talos version are the artefact's identity, so
        they are the file name: two schematics never share a file, the same
        schematic is never fetched twice, and nothing about *when* the program
        ran enters the name.
        """
        schematic_id = await resolve(self.schematic.id)
        name = f'talos-{self.talos_version}-{self.platform}-{self.architecture}-{schematic_id}'
        return str(IMAGE_CACHE / f'{name}.raw')


def _schematic_document(extensions: Sequence[str]) -> str:
    """The factory's customization document, as YAML-shaped JSON."""
    return json.dumps({'customization': {'systemExtensions': {'officialExtensions': list(extensions)}}})
