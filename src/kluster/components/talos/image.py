"""The Talos images: pinned Image Factory schematics, and what is built from them.

The fleet runs two artifacts of the same family, and neither of them is a stock
image:

-   the **cloud nodes** boot an OCI custom image. There is no official Talos
    image in OCI's catalog, so one is built by the Image Factory and imported.
-   the **homelab worker** boots a `nocloud` disk image written into a libvirt
    volume. Its schematic is not the cloud one — it carries the driver,
    firmware and GSC modules for the host's Arc A380, which the Wave C GPU
    cutover passes through to the worker and needs present from day 0
    (physical/homelab-host.md §3), and the cloud nodes have no use for it — so
    what the two share is the shape rather than the resource: one schematic
    each, one way of asking the factory where its artifact is.

The schematic id is part of the declared state either way, which is what makes
an image's contents reproducible rather than a thing someone once uploaded.

**Each artifact also names its installer.** A node boots its disk image once;
every later release reaches it through `talosctl upgrade`, which installs
whatever installer image it is handed, and the default it is handed is the
stock one, built from no schematic. So the factory lookup that finds an
artifact's disk image also yields the installer the factory builds from the
same schematic for the same platform, and the node's machine configuration
names it (`kluster.components.talos.installer_patch`), where the upgrade
runbook reads it (operations.md §2.1).

**The worker's artifact takes a detour through the machine running the
program.** The factory serves `nocloud` as `.raw.xz`; the libvirt provider does
not decompress an xz source (dmacvicar/terraform-provider-libvirt#390) and a
raw image cannot back a copy-on-write chain, so there is no way to hand libvirt
the factory URL directly. `FactoryImage` — the custom provider in
`kluster.providers.talos_factory` — fetches and decompresses it where the
program runs, and the libvirt volume is created *from that file*, which the
provider uploads into the pool over its own connection.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pulumi
import pulumi_oci as oci
from pulumiverse_talos import imagefactory

from kluster.lib import templates
from kluster.providers.talos_factory import FactoryImage
from putils import Component, async_output, resolve

#: The package `importlib.resources` resolves this module's `templates/`
#: directory against, so the schematic document travels with the code that
#: renders it (rfc-002 §9.1).
_TEMPLATE_PACKAGE = 'kluster.components.talos'

#: The cloud nodes are ARM; the homelab worker is x86.
CLOUD_ARCHITECTURE = 'arm64'
HOMELAB_ARCHITECTURE = 'amd64'

#: Which platform's artifact each half boots: OCI's own image format, and the
#: `nocloud` variant that reads its machine configuration off a seed image
#: because there is no metadata service under libvirt.
CLOUD_PLATFORM = 'oracle'
HOMELAB_PLATFORM = 'nocloud'

#: What each schematic carries beyond stock Talos. These rolls are stated here
#: and not handed down by the stack program, because an extension set is what
#: an artifact *is* — like its platform and architecture, which each subclass
#: states the same way — rather than a table a caller chooses for it: the
#: cloud image and the worker image are two schematics, and the subclass that
#: is each artifact names its own. The cloud schematic is the
#: stock one, because what the fleet adds to stock Talos is for a GPU the
#: cloud nodes do not have: the Arc A380 the GPU cutover passes through to the
#: worker. Each worker extension is one part of what that card needs:
#:
#: -   `siderolabs/i915` is its driver and its DG2 firmware: the `i915` module,
#:     and linux-firmware's whole `i915/` directory, DMC, GuC and HuC included.
#: -   `siderolabs/mei` is the path the DG2 HuC loads and authenticates through.
#:     On DG2 the card's GSC does both, and `i915` reaches the GSC through the
#:     `mei-gsc` and `mei_pxp` modules, which only this extension ships.
#:
#: The cutover needs them present *before* it happens, so the worker's
#: schematic carries them from day 0 — harmless without a GPU, and the cutover
#: then touches no OS image (physical/homelab-host.md §3).
#:
#: Each entry is an official extension the Image Factory lists for the pinned
#: Talos version (`/version/<version>/extensions/official`). Nothing here
#: checks the spelling, and the factory refuses an unknown name only when the
#: disk image is fetched.
CLOUD_EXTENSIONS: tuple[str, ...] = ()
HOMELAB_EXTENSIONS = ('siderolabs/i915', 'siderolabs/mei')

GIB = 1 << 30

#: The room the worker's disk has past the end of the Talos image when it is
#: created, which is the room Talos has to boot into before anything grows it.
#:
#: The factory builds `nocloud` at the size Talos's imager gives a metal image,
#: which holds the boot partitions and META and nothing more: from v1.8 on, a
#: disk image carries no data partitions, and Talos creates STATE and
#: EPHEMERAL past the image's last partition at first boot (talos v1.13.0
#: `cmd/installer/pkg/install/install.go`, `SkipDataPartitions`). EPHEMERAL is
#: where the kubelet runs from, and Talos will not make it smaller than its
#: minimum (`pkg/machinery/imager/quirks/partitions.go`, `EphemeralMinSize`), so
#: a disk at the image's own size boots a node whose kubelet never starts.
#:
#: 3 GiB holds STATE and EPHEMERAL's minimum with most of a gigabyte to spare
#: for partition alignment and the kubelet's own image. It is not the worker's
#: working size: the libvirt provider uploads every byte of the file it creates
#: a volume from, zeros included, over the session to the host, so the room is
#: kept to what the first boot needs, and the disk reaches its working size on
#: the host afterwards (physical/homelab-host.md §1).
HOMELAB_DISK_ROOM = 3 * GIB

#: Where the machine running the program keeps decompressed artifacts, a
#: directory for each stack: `IMAGE_CACHE/<project>/<stack>/<file>`, the file
#: named by what it holds (`TalosNocloudImage._local_path`).
#:
#: The root is a constant rather than a home-relative or temporary directory on
#: purpose: the path is an *input* of the image, so a workstation and a CI
#: runner that spelled it differently would each replace the image whenever
#: the program ran on the other one, fetching the whole artifact to change
#: nothing. The project and the stack are under it because every stack's runs
#: on a machine share the root, and a file two stacks declared would be the
#: last writer's, whose delete removes it from under the other
#: (`talos_factory.materialize`). Both are names the stack has on every
#: machine, so they keep one declaration at one path wherever it runs.
#:
#: `/var/tmp` is disk-backed and survives a reboot, which a 1.25 GB artifact
#: wants and `/tmp` does not promise.
IMAGE_CACHE = Path('/var/tmp/kluster-talos-images')


class FactoryNamedNoInstaller(ValueError):
    """The factory's answer for one schematic named no installer image.

    The type is the rule: it is the one thing this module refuses in the
    factory's answer, so a program tells the refusal apart by the type alone.
    The fields say which lookup the answer was for, and the message, built
    from them, is for a person.
    """

    def __init__(self, *, schematic_id: str, platform: str, architecture: str) -> None:
        super().__init__(
            f'the factory lookup for schematic {schematic_id} ({platform}, {architecture}) named no installer image'
        )
        self.schematic_id: str = schematic_id
        self.platform: str = platform
        self.architecture: str = architecture


class TalosArtifact(Component, abc.ABC):
    """A pinned Image Factory schematic, and where the factory serves it.

    The half both artifacts share. A subclass supplies `_declare_artifact`,
    which is what is done with the URL: imported into a cloud catalog, or
    fetched onto the machine running the program — and the three facts that
    pick its artifact out of the factory's matrix: `extensions`, the roll that
    makes its schematic the one it is, and the `platform` and `architecture`
    the factory builds that schematic for. All three are required here and
    stated by the subclass, none taken from a caller: they are what each
    artifact *is*, so no caller has a second opinion about them, and a
    defaulted parameter nobody passes would be a property wearing a
    parameter's clothes (style/pulumi.md). The roll is required rather than
    defaulted to empty for the same reason, so a subclass says what it carries
    even when that is nothing.

    Abstract, and that is load-bearing rather than documentation. This class
    opens the parent backstop's scope and the closing `register_outputs` is
    here, after the subclass has declared its artifact, so the scope is closed
    exactly once however the hierarchy grows — and a base built directly would
    open a scope nothing closes.
    """

    def __init__(
        self,
        name: str,
        *,
        talos_version: str,
        extensions: Sequence[str],
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
        #: What the factory serves for this schematic, platform and
        #: architecture: looked up once, and read by both the disk image below
        #: and the installer, so the two cannot name different schematics.
        self._urls = async_output(self._factory_urls)
        #: The installer image the factory builds from this schematic for this
        #: platform: `factory.talos.dev/<platform>-installer/<schematic id>:<version>`.
        #: It carries what the disk image carries, extensions included, which
        #: the stock `ghcr.io/siderolabs/installer` does not, so it is what an
        #: upgrade of a node booted from this artifact has to install.
        self.installer: pulumi.Output[str] = async_output(self._installer)
        self._declare_artifact(name)
        self.register_outputs({})

    @abc.abstractmethod
    def _declare_artifact(self, name: str) -> None:
        """Declare what this schematic's disk image becomes.

        Called with the schematic already declared and the component still
        open, so the resources it declares are this component's children.
        """

    async def _factory_urls(self) -> _FactoryArtifacts:
        """The factory's artifacts for this schematic, architecture and platform."""
        schematic_id = await resolve(self.schematic.id)
        urls = await resolve(
            imagefactory.get_urls_output(
                talos_version=self.talos_version,
                schematic_id=schematic_id,
                platform=self.platform,
                architecture=self.architecture,
                # Parented like every other invoke here: an invoke inherits a
                # provider only through its parent, and the Talos provider is
                # one this component carries.
                opts=pulumi.InvokeOptions(parent=self),
            )
        )
        if not urls.urls.installer:
            # The provider names an installer for every platform it names a
            # disk image for. An answer without one would render a node
            # configuration naming no installer of its own, so it is refused
            # rather than carried on.
            raise FactoryNamedNoInstaller(
                schematic_id=schematic_id, platform=self.platform, architecture=self.architecture
            )
        return _FactoryArtifacts(disk_image=urls.urls.disk_image, installer=urls.urls.installer)

    async def _disk_url(self) -> str:
        """The factory's disk image for this schematic, architecture and platform."""
        return (await resolve(self._urls)).disk_image

    async def _installer(self) -> str:
        """The factory's installer image for this schematic and platform."""
        return (await resolve(self._urls)).installer


class TalosImage(TalosArtifact, pulumi_type='kluster:talos:TalosImage'):
    """One schematic and the OCI custom image built from it.

    The extension set, platform and architecture are stated here rather than
    taken as parameters, for the same reason the worker's are
    (`TalosNocloudImage`): they are what this artifact *is* — the stock
    schematic, built for OCI's own image format on the ARM shape the cloud
    nodes run — and no caller has a second opinion about any of them.
    """

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        talos_version: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        self._compartment_id = compartment_id
        super().__init__(
            name,
            talos_version=talos_version,
            extensions=CLOUD_EXTENSIONS,
            architecture=CLOUD_ARCHITECTURE,
            platform=CLOUD_PLATFORM,
            opts=opts,
        )

    def _declare_artifact(self, name: str) -> None:
        self.image = oci.core.Image(
            f'{name}-image',
            compartment_id=self._compartment_id,
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

    async def _image_name(self) -> str:
        schematic_id = await resolve(self.schematic.id)
        return f'talos-{self.talos_version}-{self.architecture}-{str(schematic_id)[:12]}'


class TalosNocloudImage(TalosArtifact, pulumi_type='kluster:talos:TalosNocloudImage'):
    """The worker's schematic, and its disk image on the machine that runs the program.

    The extension set, platform and architecture are stated here rather than
    taken as parameters: they are what this artifact *is* — the schematic the
    worker boots, the A380's driver and firmware included, as the `nocloud`
    x86 image — and no caller has a second opinion about any of them. What varies
    between artifacts is carried by the base's `extensions`, `platform` and
    `architecture`, which is how this artifact differs from the cloud one.

    `path` is what a libvirt volume is created from, and the volume's capacity
    is that file's length: the provider refuses `size` alongside `source`. So
    the file carries `HOMELAB_DISK_ROOM` after the image, and the disk is
    created with the room Talos needs to boot. Its working size is reached on
    the host afterwards (physical/homelab-host.md §1).
    """

    def __init__(
        self,
        name: str,
        *,
        talos_version: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            name,
            talos_version=talos_version,
            extensions=HOMELAB_EXTENSIONS,
            architecture=HOMELAB_ARCHITECTURE,
            platform=HOMELAB_PLATFORM,
            opts=opts,
        )

    def _declare_artifact(self, name: str) -> None:
        self.artifact = FactoryImage(
            f'{name}-nocloud',
            url=async_output(self._disk_url),
            path=async_output(self._local_path),
            room=HOMELAB_DISK_ROOM,
            opts=self.child_opts(),
        )
        #: The decompressed image, where a libvirt volume can be created from it.
        self.path: pulumi.Output[str] = self.artifact.path

    async def _local_path(self) -> str:
        """Where this artifact lives locally: in the stack's directory, named by what it contains.

        The directory is `IMAGE_CACHE/<project>/<stack>`, so no other stack's
        run on the machine declares the file, and no other stack's delete
        removes it (`talos_factory.materialize` states the rule). The schematic
        id and the Talos version are the artifact's identity, and the room
        after it is the rest of what the file holds, so they are the file name:
        two schematics never share a file, a stack reuses an artifact it
        already holds on a machine rather than fetching it again, a file made
        with another room is never reused as this one, and nothing about
        *when* or *where* the program ran enters the path.
        """
        schematic_id = await resolve(self.schematic.id)
        name = f'talos-{self.talos_version}-{self.platform}-{self.architecture}-{schematic_id}'
        directory = IMAGE_CACHE / pulumi.get_project() / pulumi.get_stack()
        return str(directory / f'{name}-room{HOMELAB_DISK_ROOM}.raw')


@final
@dataclass(frozen=True)
class _FactoryArtifacts:
    """The two of the factory's artifacts for one schematic that this program uses.

    A class of its own rather than the provider's result, whose fields do not
    survive being carried through an output: what comes back is the mapping
    alone, without its attributes.
    """

    disk_image: str
    installer: str


@final
@dataclass(frozen=True)
class _SchematicParams:
    """What `schematic.yaml.j2` reads."""

    extensions: Sequence[str]


def _schematic_document(extensions: Sequence[str]) -> str:
    """The factory's customization document, as YAML-shaped JSON.

    One line and no trailing newline, because a schematic's contents decide its
    id and the id names every image built from it: re-spelling the document
    would rebuild artifacts that have not changed.
    """
    return templates.render(_TEMPLATE_PACKAGE, 'templates/schematic.yaml.j2', _SchematicParams(extensions=extensions))
