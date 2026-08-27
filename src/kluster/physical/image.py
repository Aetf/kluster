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
the factory URL directly. `FactoryImage` fetches and decompresses it where the
program runs, and the libvirt volume is created *from that file*, which the
provider uploads into the pool over its own connection.
"""

from __future__ import annotations

import json
import lzma
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic
import pulumi_oci as oci
import requests
from pulumi.runtime import rpc
from pulumiverse_talos import imagefactory

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

#: How long a single read from the factory may stall before the fetch is
#: abandoned. Not a budget for the whole transfer — the artefact is large and a
#: slow link is not a failure; a link that has stopped moving is.
FETCH_TIMEOUT = 60

#: Bytes pulled from the network per decompression step. The artefact never
#: exists in memory whole, in either form.
CHUNK_BYTES = 1 << 20

#: The mode the finished artefact is left at. `tempfile` creates the download
#: private to its owner, and this is a public image whose only secret is the
#: bandwidth it took to get.
ARTEFACT_MODE = 0o644


@final
class TruncatedArtefact(Exception):
    """The stream ended before the compressed artefact did."""

    def __init__(self, url: str) -> None:
        super().__init__(f'{url} ended mid-stream: the artefact is incomplete and was not kept')
        self.url: str = url


def fetch(url: str) -> requests.Response:
    """Open the artefact's byte stream. The one seam a test replaces."""
    response = requests.get(url, stream=True, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response


def materialise(url: str, path: Path) -> None:
    """Leave the artefact at `url` sitting decompressed at `path`.

    A file already at `path` is the artefact and is reused: the download lands
    under a temporary name in the same directory and is renamed only once the
    xz stream has ended cleanly, so a file under the final name is whole by
    construction. That is what keeps a re-created resource — or a second stack
    on the same machine — from spending a gigabyte of bandwidth again.

    Fetching and decompressing are the same pass. Neither form of the artefact
    is ever held in memory, and a failure removes the partial file rather than
    leaving something that looks finished.
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f'{path.name}.', suffix='.part')
    partial = Path(name)
    try:
        decompressor = lzma.LZMADecompressor()
        # The sink is entered first so that a fetch which raises still closes
        # the descriptor `mkstemp` handed over.
        with os.fdopen(descriptor, 'wb') as sink, fetch(url) as response:
            for chunk in response.iter_content(CHUNK_BYTES):
                _ = sink.write(decompressor.decompress(chunk))
        if not decompressor.eof:
            raise TruncatedArtefact(url)
        partial.chmod(ARTEFACT_MODE)
        # Atomic, and within one directory so it stays atomic: either the
        # whole artefact is under its final name or nothing is.
        _ = partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def _is_unknown(value: Any) -> bool:
    """Whether a property is still a preview placeholder."""
    return isinstance(value, str) and value == rpc.UNKNOWN


@final
class FactoryImageProvider(dynamic.ResourceProvider):
    """One factory artefact, decompressed on whatever machine runs the program.

    There is no update: the resource *is* a particular artefact at a particular
    path, so a different schematic or a different Talos version is a different
    file and a replacement.

    **`read` is deliberately the inherited one, which reports no drift.** The
    file is a build artefact rather than managed state — a CI runner is fresh
    every time and has none of them — and a refresh that called a missing file
    a deleted resource would take the worker's disk down with it.
    """

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        path = Path(str(props['path']))
        materialise(str(props['url']), path)
        return dynamic.CreateResult(id_=str(path), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if any(_is_unknown(news.get(key)) for key in ('url', 'path')):
            # The schematic has not been created yet, so what the factory will
            # serve is not knowable here; the engine plans on "unknown" rather
            # than on a guess.
            return dynamic.DiffResult(changes=None)
        replaces = [key for key in ('url', 'path') if olds.get(key) != news.get(key)]
        return dynamic.DiffResult(
            changes=bool(replaces),
            replaces=replaces,
            # The paths differ whenever the artefact does, so the new file can
            # exist before the old one goes.
            delete_before_replace=False,
        )

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        Path(str(props['path'])).unlink(missing_ok=True)


@final
class FactoryImage(dynamic.Resource, module='physical', name='FactoryImage'):
    """A factory artefact, fetched and decompressed at `path`."""

    url: pulumi.Output[str]
    path: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        url: pulumi.Input[str],
        path: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `path` holds the decompressed contents of `url`.

        `path` is derived from the schematic and the Talos version rather than
        generated, so the same declaration names the same file on every run and
        on every machine — which is what lets a consumer take the path as an
        input without proposing a change each time the program moves.
        """
        super().__init__(FactoryImageProvider(), name, {'url': url, 'path': path}, opts)


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
