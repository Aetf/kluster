"""The fleet's two Talos artefacts, and how the worker's one reaches a volume.

The cloud half is an import into OCI's catalogue and has been for as long as
the stack has existed. The worker's half is the interesting one: the factory
serves `nocloud` compressed, the libvirt provider will not decompress what it
is handed, and a raw image cannot back a copy-on-write chain — so the program
itself fetches the artefact, decompresses it where it runs, and hands libvirt a
file. What is asserted below is mostly the consequences of that: the file is
named by what it contains and by nothing else, a stream that ends early leaves
nothing that looks finished, and a machine that has never seen the file is not
mistaken for a resource somebody deleted.
"""

from __future__ import annotations

import json
import lzma
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pulumi
import pulumi.dynamic as dynamic
import pytest
import pytest_asyncio
import requests
from pulumi.runtime import rpc

from kluster.physical import image

CLUSTER = 'kluster'
WORKER = f'{CLUSTER}-worker'
TALOS_VERSION = 'v1.11.0'
COMPARTMENT = 'ocid1.compartment.oc1..test'
FACTORY = 'https://factory.talos.dev/image'

#: What the mock monitor hands back as a schematic's id. It stands in for the
#: factory's content hash, and the file name below is built out of it.
CLOUD_SCHEMATIC = f'{CLUSTER}-schematic_id'
WORKER_SCHEMATIC = f'{WORKER}-schematic_id'

#: Every `getUrls` invoke the run made, so a test can ask which artefact was
#: requested rather than only which URL came back.
CALLS: list[dict[str, Any]] = []


class Fake(pulumi.runtime.Mocks):
    """The Image Factory, as far as this module needs it.

    The URL is built the way the factory builds it — `nocloud` is served as
    `.raw.xz`, `oracle` as `.qcow2` — because the shape of that name is part of
    what the program depends on.
    """

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        return args.name + '_id', dict(cast('dict[str, Any]', args.inputs))

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token != 'talos:imageFactory/getUrls:getUrls':
            return {}, []
        arguments = dict(cast('dict[str, Any]', args.args))
        CALLS.append(arguments)
        platform = str(arguments['platform'])
        architecture = str(arguments['architecture'])
        suffix = 'raw.xz' if platform == 'nocloud' else 'qcow2'
        schematic = arguments['schematicId']
        version = arguments['talosVersion']
        return {'urls': {'diskImage': f'{FACTORY}/{schematic}/{version}/{platform}-{architecture}.{suffix}'}}, []


@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    CALLS.clear()
    pulumi.runtime.set_mocks(Fake(), project='kluster', stack='physical', preview=False)


def build_cloud() -> image.TalosImage:
    return image.TalosImage(CLUSTER, compartment_id=COMPARTMENT, talos_version=TALOS_VERSION)


def build_worker() -> image.TalosNocloudImage:
    return image.TalosNocloudImage(WORKER, talos_version=TALOS_VERSION)


# -- the two schematics -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_artefacts_do_not_share_a_schematic() -> None:
    cloud = await build_cloud().schematic.schematic.future()
    worker = await build_worker().schematic.schematic.future()

    # The module docstring used to say the worker consumed *the same*
    # schematic. It cannot: the worker's carries a firmware extension the
    # cloud nodes have no hardware for, and a schematic's contents are its id.
    assert json.loads(str(cloud))['customization']['systemExtensions']['officialExtensions'] == []
    assert json.loads(str(worker))['customization']['systemExtensions']['officialExtensions'] == ['siderolabs/i915']


@pytest.mark.asyncio
async def test_the_worker_carries_the_gpu_firmware_before_it_has_a_gpu() -> None:
    worker = build_worker()

    # Day 0, deliberately (physical/homelab-host.md §3): the Wave C cutover
    # adds a PCI device to a running domain and must touch no OS image, so the
    # extension has to have been there since the disk was written.
    document = json.loads(str(await worker.schematic.schematic.future()))
    assert 'siderolabs/i915' in document['customization']['systemExtensions']['officialExtensions']


@pytest.mark.asyncio
async def test_the_worker_asks_the_factory_for_the_nocloud_x86_artefact() -> None:
    worker = build_worker()

    url = await worker.artefact.url.future()
    assert url == f'{FACTORY}/{WORKER_SCHEMATIC}/{TALOS_VERSION}/nocloud-amd64.raw.xz'
    # And it asked for that artefact rather than merely receiving it: platform
    # and architecture are what pick one file out of the factory's matrix.
    assert {'platform': 'nocloud', 'architecture': 'amd64'}.items() <= CALLS[-1].items()


# -- the cloud half, unchanged ----------------------------------------------


@pytest.mark.asyncio
async def test_the_cloud_image_is_still_imported_from_the_factory_url() -> None:
    cloud = build_cloud()

    # The worker's artefact is new; the OCI import is not, and nothing about
    # sharing a base class may have moved it.
    details = await cloud.image.image_source_details.future()
    assert details is not None
    assert details.source_type == 'objectStorageUri'
    assert details.source_image_type == 'QCOW2'
    assert details.source_uri == f'{FACTORY}/{CLOUD_SCHEMATIC}/{TALOS_VERSION}/oracle-arm64.qcow2'
    assert await cloud.image.display_name.future() == f'talos-{TALOS_VERSION}-arm64-{CLOUD_SCHEMATIC[:12]}'


def test_each_artefact_keeps_its_own_type_token() -> None:
    # A subclass inherits its base's token unless it states one, and a token is
    # part of every URN: sharing one would file both artefacts in the state
    # under the same type and rename resources whenever the hierarchy moved.
    assert image.TalosImage.__pulumi_type__ == 'kluster:physical:image:TalosImage'
    assert image.TalosNocloudImage.__pulumi_type__ == 'kluster:physical:image:TalosNocloudImage'
    assert image.TalosImage.__pulumi_type__ != image.TalosArtefact.__pulumi_type__


# -- where the artefact lands ------------------------------------------------


@pytest.mark.asyncio
async def test_the_local_artefact_is_named_by_what_it_contains() -> None:
    worker = build_worker()

    # Schematic and version, and nothing else. The path is an input of the
    # libvirt volume, so anything else in it — a timestamp, a temporary
    # directory, the user's home — would propose replacing a protected disk
    # the next time the program ran somewhere else.
    expected = image.IMAGE_CACHE / f'talos-{TALOS_VERSION}-nocloud-amd64-{WORKER_SCHEMATIC}.raw'
    assert await worker.path.future() == str(expected)
    assert expected.is_absolute()


def test_the_cache_is_somewhere_both_a_workstation_and_a_runner_have() -> None:
    # `/var/tmp` rather than `$HOME` or `$TMPDIR`: the path travels in state as
    # an input, and it is disk-backed, which a 1.25 GB artefact wants.
    assert image.IMAGE_CACHE == Path('/var/tmp/kluster-talos-images')


# -- fetching and decompressing ----------------------------------------------


PAYLOAD = b'not really a disk image, but it does compress\n' * 64


class FakeStream:
    """A response that yields the bytes it was given, in the pieces it was given."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks: list[bytes] = chunks

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def iter_content(self, _size: int) -> Iterator[bytes]:
        return iter(self.chunks)


def serve(monkeypatch: pytest.MonkeyPatch, *chunks: bytes) -> list[str]:
    """Replace the fetch seam, and hand back the list of URLs it was asked for."""
    requested: list[str] = []

    def fetch(url: str) -> requests.Response:
        requested.append(url)
        return cast('requests.Response', FakeStream(list(chunks)))

    monkeypatch.setattr(image, 'fetch', fetch)
    return requested


def test_a_fetched_artefact_lands_decompressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compressed = lzma.compress(PAYLOAD)
    _ = serve(monkeypatch, compressed[:20], compressed[20:])
    path = tmp_path / 'nested' / 'talos.raw'

    image.materialise('https://factory.invalid/nocloud-amd64.raw.xz', path)

    # Decompressed, whole, and in a directory the program created: the libvirt
    # provider is handed a plain raw image because it will not unpack an xz.
    assert path.read_bytes() == PAYLOAD


def test_an_artefact_already_on_disk_is_reused_rather_than_fetched_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = serve(monkeypatch, lzma.compress(PAYLOAD))
    path = tmp_path / 'talos.raw'
    _ = path.write_bytes(PAYLOAD)

    image.materialise('https://factory.invalid/nocloud-amd64.raw.xz', path)

    # A file under the final name is complete by construction — the download
    # is renamed into place, never written into place — so re-creating the
    # resource on a machine that still has the artefact costs nothing.
    assert requested == []


def test_a_stream_that_ends_early_leaves_nothing_that_looks_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truncated = lzma.compress(PAYLOAD)[:-8]
    _ = serve(monkeypatch, truncated)
    path = tmp_path / 'talos.raw'

    with pytest.raises(image.TruncatedArtefact):
        image.materialise('https://factory.invalid/nocloud-amd64.raw.xz', path)

    # The failure mode this guards against is silent: half an image written
    # into a volume boots into nothing, and the next run would have reused it.
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


# -- the resource around it --------------------------------------------------


def props(url: str, path: Path) -> dict[str, Any]:
    return {'url': url, 'path': str(path)}


def test_creating_the_resource_fetches_it_and_is_identified_by_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = serve(monkeypatch, lzma.compress(PAYLOAD))
    path = tmp_path / 'talos.raw'

    result = image.FactoryImageProvider().create(props('https://factory.invalid/a.raw.xz', path))

    assert result.id == str(path)
    assert path.read_bytes() == PAYLOAD


def test_a_new_schematic_is_a_new_artefact_rather_than_an_update(tmp_path: Path) -> None:
    olds = props('https://factory.invalid/old.raw.xz', tmp_path / 'old.raw')
    news = props('https://factory.invalid/new.raw.xz', tmp_path / 'new.raw')

    result = image.FactoryImageProvider().diff('old', olds, news)

    # There is no update: the resource *is* one artefact at one path, so a
    # different image is a different resource. The volume it feeds is
    # protected, which turns the consequence into a refusal an operator has to
    # answer rather than a disk that vanishes.
    assert result.changes is True
    assert set(result.replaces or []) == {'url', 'path'}


def test_a_preview_that_cannot_know_the_url_does_not_claim_a_change(tmp_path: Path) -> None:
    olds = props('https://factory.invalid/old.raw.xz', tmp_path / 'old.raw')
    news = {'url': rpc.UNKNOWN, 'path': rpc.UNKNOWN}

    result = image.FactoryImageProvider().diff('old', olds, news)

    # Before the schematic exists there is no answer, and "unknown" is the
    # honest one; reporting a replacement would put a protected volume in the
    # plan on the strength of a placeholder.
    assert result.changes is None


def test_a_local_artefact_that_is_gone_is_not_a_deleted_resource(tmp_path: Path) -> None:
    stored = props('https://factory.invalid/a.raw.xz', tmp_path / 'absent.raw')

    result = image.FactoryImageProvider().read('an-id', stored)

    # The file is a build artefact, not managed state: every CI runner starts
    # without it. A refresh that called this a deleted resource would take the
    # worker's boot disk down with it on the next apply.
    assert result.id == 'an-id'
    assert result.outs == stored


def test_deleting_the_resource_takes_the_local_copy_with_it(tmp_path: Path) -> None:
    path = tmp_path / 'talos.raw'
    _ = path.write_bytes(PAYLOAD)

    image.FactoryImageProvider().delete('an-id', props('https://factory.invalid/a.raw.xz', path))

    # Deterministic cleanup, and tolerant of the file already being gone —
    # which is the ordinary case on a machine that never created it.
    assert not path.exists()
    image.FactoryImageProvider().delete('an-id', props('https://factory.invalid/a.raw.xz', path))


def test_the_provider_carries_no_state_of_its_own() -> None:
    # A dynamic provider is pickled into the stack's state and revived in
    # another process, so anything it closed over would travel with it.
    provider: dynamic.ResourceProvider = image.FactoryImageProvider()
    assert vars(provider) == {}
