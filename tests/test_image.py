"""The fleet's two Talos artifacts, and how the worker's one reaches a volume.

The cloud half is an import into OCI's catalog and has been for as long as
the stack has existed. The worker's half is the interesting one: the factory
serves `nocloud` compressed, the libvirt provider will not decompress what it
is handed, and a raw image cannot back a copy-on-write chain — so the program
itself fetches the artifact, decompresses it where it runs, and hands libvirt a
file. What is asserted below is mostly the consequences of that: the file is
named by what it contains and by nothing else, a stream that ends early leaves
nothing that looks finished, and a machine that has never seen the file is not
mistaken for a resource somebody deleted.
"""

from __future__ import annotations

import inspect
import io
import json
import lzma
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pulumi
import pulumi.dynamic as dynamic
import pytest
import pytest_asyncio
import requests
from mock_monitor import Recorder, declaring, decline_every_invoke, run_with, the_run_failed_with
from pulumi.runtime import rpc

from kluster.components.talos import image
from kluster.providers import talos_factory

CLUSTER = 'kluster'
WORKER = f'{CLUSTER}-worker'
TALOS_VERSION = 'v1.11.0'
COMPARTMENT = 'ocid1.compartment.oc1..test'
FACTORY_HOST = 'factory.talos.dev'
FACTORY = f'https://{FACTORY_HOST}/image'

#: What the mock monitor hands back as a schematic's id. It stands in for the
#: factory's content hash, and the file name below is built out of it.
CLOUD_SCHEMATIC = f'{CLUSTER}-schematic_id'
WORKER_SCHEMATIC = f'{WORKER}-schematic_id'


class Factory(Recorder):
    """The Image Factory, as far as this module needs it.

    The URL is built the way the factory builds it -- `nocloud` is served as
    `.raw.xz`, `oracle` as `.qcow2` -- because the shape of that name is part of
    what the program depends on. Every request is kept, so a case can ask which
    artifact was asked for rather than only which URL came back.
    """

    def __init__(self) -> None:
        super().__init__()
        #: The arguments of every `getUrls` invoke, in the order they were made.
        self.invokes: list[dict[str, Any]] = []
        #: Whether an answer names the installer, as the provider's always does.
        self.names_installer = True

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token != 'talos:imageFactory/getUrls:getUrls':
            return {}
        arguments = dict(cast('dict[str, Any]', args.args))
        self.invokes.append(arguments)
        platform = str(arguments['platform'])
        architecture = str(arguments['architecture'])
        suffix = 'raw.xz' if platform == 'nocloud' else 'qcow2'
        schematic = arguments['schematicId']
        version = arguments['talosVersion']
        urls = {'diskImage': f'{FACTORY}/{schematic}/{version}/{platform}-{architecture}.{suffix}'}
        if self.names_installer:
            # The provider's own shape: one installer per platform, for every
            # architecture (terraform-provider-talos v0.11.0,
            # `talos_image_factory_urls_data_source.go`).
            urls['installer'] = f'{FACTORY_HOST}/{platform}-installer/{schematic}:{version}'
        return {'urls': urls}


@pytest_asyncio.fixture(autouse=True)
async def factory() -> Factory:
    return await run_with(Factory(), stack='physical')


def build_cloud() -> image.TalosImage:
    return image.TalosImage(CLUSTER, compartment_id=COMPARTMENT, talos_version=TALOS_VERSION)


def build_worker() -> image.TalosNocloudImage:
    return image.TalosNocloudImage(WORKER, talos_version=TALOS_VERSION)


# -- the two schematics -----------------------------------------------------


def official_extensions(schematic: object) -> list[str]:
    """The extensions a rendered schematic document asks the factory for."""
    document = json.loads(str(schematic))
    return cast('list[str]', document['customization']['systemExtensions']['officialExtensions'])


@pytest.mark.asyncio
async def test_the_two_artifacts_do_not_share_a_schematic() -> None:
    cloud = official_extensions(await build_cloud().schematic.schematic.future())
    worker = official_extensions(await build_worker().schematic.schematic.future())

    # The module docstring used to say the worker consumed *the same*
    # schematic. It cannot: the worker's carries extensions for a GPU the
    # cloud nodes have no hardware for, and a schematic's contents are its id.
    # So the cloud's asks for none of the worker's, whatever that set holds.
    assert cloud != worker
    assert not set(cloud) & set(worker)


@pytest.mark.asyncio
async def test_the_worker_schematic_asks_for_exactly_the_worker_roll() -> None:
    # Day 0, deliberately (physical/homelab-host.md §3): the Wave C cutover
    # adds a PCI device to a running domain and must touch no OS image, so the
    # schematic the disk is written from already asks for every extension the
    # worker's artifact declares, with no GPU anywhere in sight.
    worker = official_extensions(await build_worker().schematic.schematic.future())
    assert worker == list(image.HOMELAB_EXTENSIONS)


@pytest.mark.asyncio
async def test_the_worker_asks_the_factory_for_the_nocloud_x86_artifact(factory: Factory) -> None:
    worker = build_worker()

    url = await worker.artifact.url.future()

    assert url == f'{FACTORY}/{WORKER_SCHEMATIC}/{TALOS_VERSION}/nocloud-amd64.raw.xz'
    # And it asked for that artifact rather than merely receiving it: platform
    # and architecture are what pick one file out of the factory's matrix.
    assert {'platform': 'nocloud', 'architecture': 'amd64'}.items() <= factory.invokes[-1].items()


# -- the installers --------------------------------------------------------


@pytest.mark.asyncio
async def test_each_artifact_names_the_installer_of_its_own_schematic_and_platform(factory: Factory) -> None:
    """The installer is the one the factory builds from the schematic the disk image is built from.

    Read off the same lookup as the disk image, so the two name one schematic
    by construction: one lookup per artifact, and the installer carries the
    schematic and the platform that lookup asked about.
    """
    cloud = build_cloud()
    worker = build_worker()

    assert await cloud.installer.future() == f'{FACTORY_HOST}/oracle-installer/{CLOUD_SCHEMATIC}:{TALOS_VERSION}'
    assert await worker.installer.future() == f'{FACTORY_HOST}/nocloud-installer/{WORKER_SCHEMATIC}:{TALOS_VERSION}'
    assert sorted(str(invoke['schematicId']) for invoke in factory.invokes) == sorted(
        [CLOUD_SCHEMATIC, WORKER_SCHEMATIC]
    )


@pytest.mark.asyncio
async def test_a_factory_answer_naming_no_installer_is_refused(factory: Factory) -> None:
    """A node configuration must never be rendered with no installer of its own.

    The provider names one for every platform, so an answer without one is
    malformed; carried on, it would reach the configuration as a missing value
    rendered into a string. The same refusal reaches the artifact's own
    registration, whose inputs are built from that answer, and that is what
    fails the deployment.
    """
    factory.names_installer = False

    worker = build_worker()

    with pytest.raises(image.FactoryNamedNoInstaller) as refused:
        _ = await worker.installer.future()
    asked = factory.invokes[-1]
    assert (refused.value.schematic_id, refused.value.platform, refused.value.architecture) == (
        asked['schematicId'],
        asked['platform'],
        asked['architecture'],
    )
    assert await the_run_failed_with(image.FactoryNamedNoInstaller) is refused.value


# -- the cloud half, unchanged ----------------------------------------------


@pytest.mark.asyncio
async def test_the_cloud_image_is_still_imported_from_the_factory_url() -> None:
    cloud = build_cloud()

    # The worker's artifact is new; the OCI import is not, and nothing about
    # sharing a base class may have moved it.
    details = await cloud.image.image_source_details.future()
    assert details is not None
    assert details.source_type == 'objectStorageUri'
    assert details.source_image_type == 'QCOW2'
    assert details.source_uri == f'{FACTORY}/{CLOUD_SCHEMATIC}/{TALOS_VERSION}/oracle-arm64.qcow2'


@pytest.mark.asyncio
async def test_a_factory_lookup_the_engine_declines_leaves_the_source_unknown_rather_than_crashing(
    factory: Factory,
) -> None:
    """The unknown degrades the one input, and nothing surfaces as a traceback.

    Awaiting the lookup through `resolve` is what puts it under the rule every
    other awaited value in the artifact follows (framework/pulumi.md §1.2): an
    unknown aborts the coroutine, that input alone becomes unknown, and the
    rest of the image is declared as it would have been. Awaited directly, the
    same answer is a `None` for an `assert` to trip over -- a traceback on a
    run that may have converged everything it was asked to.

    Read off what the program handed the provider rather than off the image's
    outputs, because the source is nested inside `imageSourceDetails` and the
    mock's readback of a nested unknown is not the engine's
    (framework/testing.md §3.3): under this update run the key is dropped,
    under a preview it would hold an `Unknown`. The assertion is the form that
    holds in either kind of run, because what the case is about is that no
    value reached the provider under that key.
    """
    decline_every_invoke()

    async with declaring():
        cloud = build_cloud()

    assert await cloud.image.display_name.is_known() is True
    details = factory.inputs_of(f'{CLUSTER}-image', 'oci:Core/image:Image')['imageSourceDetails']
    assert not isinstance(details.get('sourceUri'), str)


def test_each_artifact_states_its_platform_and_architecture_rather_than_taking_them() -> None:
    """The subclass is the artifact, so the artifact's shape is not a parameter.

    Platform and architecture pick one file out of the factory's matrix the
    way the extension roll does, and no caller varies either: the cloud image
    is OCI's own format on the ARM shape, the worker's is `nocloud` on x86. A
    defaulted parameter nobody passes would read as a caller's choice while
    being the subclass's (style/pulumi.md), so the base requires both and
    each subclass states its own.
    """
    for artifact in (image.TalosImage, image.TalosNocloudImage):
        parameters = inspect.signature(artifact.__init__).parameters
        assert 'platform' not in parameters, artifact.__name__
        assert 'architecture' not in parameters, artifact.__name__
    base = inspect.signature(image.TalosArtifact.__init__).parameters
    assert base['platform'].default is inspect.Parameter.empty
    assert base['architecture'].default is inspect.Parameter.empty


def test_the_shared_base_cannot_be_built_on_its_own() -> None:
    """The base opens the parent backstop's scope and declares no artifact.

    Closing that scope is `register_outputs`, which the base runs after the
    subclass has declared its artifact. A base built directly would therefore
    leave the scope open, and the next resource declared without a parent —
    including a sibling component the stack program builds afterwards — would
    be refused in this component's name rather than its own.
    """
    with pytest.raises(TypeError, match='abstract'):
        _ = image.TalosArtifact(  # pyright: ignore[reportAbstractUsage]
            'kluster-artifact',
            talos_version=TALOS_VERSION,
            extensions=(),
            architecture='arm64',
            platform='oracle',
        )


# -- where the artifact lands ------------------------------------------------


async def worker_path(name: str, version: str = TALOS_VERSION) -> str:
    """Where a worker artifact declared under `name` at `version` lands, in a run of its own.

    The mock monitor hands each schematic the id its resource name gives it, so
    another name is another schematic.
    """
    _ = await run_with(Factory(), stack='physical')
    path = await image.TalosNocloudImage(name, talos_version=version).path.future()
    assert path is not None
    return path


@pytest.mark.asyncio
async def test_the_local_artifact_is_named_by_what_it_contains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Schematic and version, and nothing else. The path is an input of the
    # libvirt volume, so anything else in it — a timestamp, a temporary
    # directory, the user's home — would propose replacing a protected disk
    # the next time the program ran somewhere else.
    paths: list[str] = []
    for where in ('one', 'two'):
        monkeypatch.setenv('HOME', str(tmp_path / where / 'home'))
        monkeypatch.setenv('TMPDIR', str(tmp_path / where / 'tmp'))
        paths.append(await worker_path(WORKER))
    another = await worker_path(f'{WORKER}-another')

    assert paths[0] == paths[1]
    assert paths[0] != another
    # Another release is another file: the provider keeps a file already at
    # the path, so a release that kept the path would be served the old file.
    assert paths[0] != await worker_path(WORKER, f'{TALOS_VERSION}-next')
    # Another room is another file, for the same reason: a file already at the
    # path is reused, so a path that kept still would serve the old room.
    monkeypatch.setattr(image, 'HOMELAB_DISK_ROOM', image.HOMELAB_DISK_ROOM + 1)
    assert paths[0] != await worker_path(WORKER)
    assert Path(paths[0]).is_absolute()


def test_the_cache_is_somewhere_both_a_workstation_and_a_runner_have() -> None:
    # `/var/tmp` rather than `$HOME` or `$TMPDIR`: the path travels in state as
    # an input, and it is disk-backed, which a 1.25 GB artifact wants.
    assert image.IMAGE_CACHE.is_absolute()
    assert image.IMAGE_CACHE.parent == Path('/var/tmp')


# -- the room Talos boots into ------------------------------------------------

MIB = 1 << 20
GIB = 1 << 30

#: What Talos v1.13 creates past a disk image's last partition at first boot,
#: and the least it makes of each: STATE at its size, EPHEMERAL at no less than
#: its minimum (siderolabs/talos v1.13.0,
#: `pkg/machinery/imager/quirks/partitions.go`, `StateSize` and
#: `EphemeralMinSize`). A disk image carries neither from v1.8 on
#: (`cmd/installer/pkg/install/install.go`, `SkipDataPartitions`).
TALOS_STATE_SIZE = 100 * MIB
TALOS_EPHEMERAL_MIN_SIZE = 2 * GIB


@pytest.mark.asyncio
async def test_the_worker_disk_has_room_for_what_talos_creates_at_first_boot(
    factory: Factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The libvirt volume takes the length of the file it is created from as its
    # capacity, so the file the worker's declaration makes is the disk the
    # worker first boots on. The kubelet runs from EPHEMERAL, and the health
    # gate of the first apply waits on the kubelet, so a disk without room for
    # EPHEMERAL fails that apply rather than a later one.
    _ = await build_worker().path.future()
    declared = factory.inputs_of(f'{WORKER}-nocloud')
    _ = serve(monkeypatch, lzma.compress(PAYLOAD))
    disk = tmp_path / 'worker.raw'

    _ = talos_factory.FactoryImageProvider().create(declared | {'path': str(disk)})

    assert disk.stat().st_size - len(PAYLOAD) >= TALOS_STATE_SIZE + TALOS_EPHEMERAL_MIN_SIZE
    with disk.open('rb') as written:
        assert written.read(len(PAYLOAD)) == PAYLOAD


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

    monkeypatch.setattr(talos_factory, 'fetch', fetch)
    return requested


def test_a_fetched_artifact_lands_decompressed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compressed = lzma.compress(PAYLOAD)
    _ = serve(monkeypatch, compressed[:20], compressed[20:])
    path = tmp_path / 'nested' / 'talos.raw'

    talos_factory.materialize('https://factory.invalid/nocloud-amd64.raw.xz', path, room=0)

    # Decompressed, whole, and in a directory the program created: the libvirt
    # provider is handed a plain raw image because it will not unpack an xz.
    assert path.read_bytes() == PAYLOAD


def test_the_room_follows_the_artifact_as_zeros(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = serve(monkeypatch, lzma.compress(PAYLOAD))
    path = tmp_path / 'talos.raw'

    talos_factory.materialize('https://factory.invalid/nocloud-amd64.raw.xz', path, room=4096)

    # A volume created from the file takes its length as its capacity, so the
    # room is disk the guest sees past the image's end, and it reads as zeros.
    assert path.read_bytes() == PAYLOAD + bytes(4096)


def test_an_artifact_already_on_disk_is_reused_rather_than_fetched_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = serve(monkeypatch, lzma.compress(PAYLOAD))
    path = tmp_path / 'talos.raw'
    _ = path.write_bytes(PAYLOAD)

    talos_factory.materialize('https://factory.invalid/nocloud-amd64.raw.xz', path, room=0)

    # A file under the final name is complete by construction — the download
    # is renamed into place, never written into place — so re-creating the
    # resource on a machine that still has the artifact costs nothing.
    assert requested == []


@pytest.mark.parametrize('on_disk', [False, True], ids=['fetched', 'reused'])
def test_the_artifact_says_what_it_does_before_it_does_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_disk: bool
) -> None:
    # Through the engine's diagnostics, the channel `pulumi` draws while the
    # run goes: the fetch is about a gigabyte, and the standard `logging`
    # module's `info` is dropped in the process a dynamic provider runs in.
    url = 'https://factory.invalid/nocloud-amd64.raw.xz'
    events: list[tuple[str, str]] = []

    def said(message: str, *_args: object, **_kwargs: object) -> None:
        events.append(('said', message))

    def fetch(fetched: str) -> requests.Response:
        events.append(('fetched', fetched))
        return cast('requests.Response', FakeStream([lzma.compress(PAYLOAD)]))

    monkeypatch.setattr(talos_factory.pulumi.log, 'info', said)
    monkeypatch.setattr(talos_factory, 'fetch', fetch)
    path = tmp_path / 'talos.raw'
    if on_disk:
        _ = path.write_bytes(PAYLOAD)

    talos_factory.materialize(url, path, room=0)

    if on_disk:
        assert [kind for kind, _ in events] == ['said']
        assert str(path) in events[0][1] and 'reused' in events[0][1], events
    else:
        assert [kind for kind, _ in events] == ['said', 'fetched']
        assert url in events[0][1] and str(path) in events[0][1], events
        assert 'decompress' in events[0][1], events


def test_a_stream_that_ends_early_leaves_nothing_that_looks_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truncated = lzma.compress(PAYLOAD)[:-8]
    _ = serve(monkeypatch, truncated)
    path = tmp_path / 'talos.raw'

    with pytest.raises(talos_factory.TruncatedArtifact):
        talos_factory.materialize('https://factory.invalid/nocloud-amd64.raw.xz', path, room=0)

    # The failure mode this guards against is silent: half an image written
    # into a volume boots into nothing, and the next run would have reused it.
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def factory_response(status: int, body: bytes) -> requests.Response:
    """A response as `requests` builds it from a streamed reply."""
    response = requests.Response()
    response.status_code = status
    response.raw = io.BytesIO(body)
    return response


def answering(status: int, body: bytes) -> Callable[..., requests.Response]:
    """A stand-in for `requests.get` that answers every request with one response."""

    def get(*_: object, **__: object) -> requests.Response:
        return factory_response(status, body)

    return get


def test_a_refusal_carries_the_factorys_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory says why it refused in the body, and the error says it too.

    An extension name the factory does not list for the release is refused
    when the image is first built, which is when it is fetched, and the status
    alone names neither the extension nor the release.
    """
    reason = b'extension "siderolabs/i91" is not available for Talos version v1.13.9\n'
    asked: list[str] = []

    def get(url: str, **_: object) -> requests.Response:
        asked.append(url)
        return factory_response(400, reason)

    monkeypatch.setattr(talos_factory.requests, 'get', get)
    url = 'https://factory.invalid/image/0123/v1.13.9/nocloud-amd64.raw.xz'

    with pytest.raises(talos_factory.FactoryRefused) as refused:
        _ = talos_factory.fetch(url)

    assert asked == [url]
    assert refused.value.status == 400
    assert refused.value.reason == reason.decode().strip()
    assert reason.decode().strip() in str(refused.value)


def test_a_refusal_keeps_only_the_start_of_a_long_body(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'x' * (talos_factory.ERROR_BODY_BYTES * 3)
    monkeypatch.setattr(talos_factory.requests, 'get', answering(500, body))

    with pytest.raises(talos_factory.FactoryRefused) as refused:
        _ = talos_factory.fetch('https://factory.invalid/image')

    assert len(refused.value.reason) == talos_factory.ERROR_BODY_BYTES


def test_a_success_is_handed_back_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    compressed = lzma.compress(PAYLOAD)
    monkeypatch.setattr(talos_factory.requests, 'get', answering(200, compressed))

    with talos_factory.fetch('https://factory.invalid/image') as response:
        assert b''.join(response.iter_content(talos_factory.CHUNK_BYTES)) == compressed


# -- the resource around it --------------------------------------------------


def props(url: str, path: Path, room: int = 0) -> dict[str, Any]:
    return {'url': url, 'path': str(path), 'room': room}


def test_creating_the_resource_fetches_it_and_is_identified_by_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = serve(monkeypatch, lzma.compress(PAYLOAD))
    path = tmp_path / 'talos.raw'

    result = talos_factory.FactoryImageProvider().create(props('https://factory.invalid/a.raw.xz', path))

    assert result.id == str(path)
    assert path.read_bytes() == PAYLOAD


def test_a_new_schematic_is_a_new_artifact_rather_than_an_update(tmp_path: Path) -> None:
    olds = props('https://factory.invalid/old.raw.xz', tmp_path / 'old.raw')
    news = props('https://factory.invalid/new.raw.xz', tmp_path / 'new.raw')

    result = talos_factory.FactoryImageProvider().diff('old', olds, news)

    # There is no update: the resource *is* one artifact at one path, so a
    # different image is a different resource. The volume it feeds is
    # protected, which turns the consequence into a refusal an operator has to
    # answer rather than a disk that vanishes.
    assert result.changes is True
    assert set(result.replaces or []) == {'url', 'path'}


def test_another_room_is_another_artifact_too(tmp_path: Path) -> None:
    olds = props('https://factory.invalid/a.raw.xz', tmp_path / 'a.raw', room=1)
    news = props('https://factory.invalid/a.raw.xz', tmp_path / 'a.raw', room=2)

    result = talos_factory.FactoryImageProvider().diff('old', olds, news)

    # The room is part of what the file holds, so a file with the old room is
    # not the one declared, even at the same path.
    assert result.changes is True
    assert result.replaces == ['room']


def test_a_preview_that_cannot_know_the_url_does_not_claim_a_change(tmp_path: Path) -> None:
    olds = props('https://factory.invalid/old.raw.xz', tmp_path / 'old.raw')
    news = {'url': rpc.UNKNOWN, 'path': rpc.UNKNOWN}

    result = talos_factory.FactoryImageProvider().diff('old', olds, news)

    # Before the schematic exists there is no answer, and "unknown" is the
    # honest one; reporting a replacement would put a protected volume in the
    # plan on the strength of a placeholder.
    assert result.changes is None


def test_a_local_artifact_that_is_gone_is_not_a_deleted_resource(tmp_path: Path) -> None:
    stored = props('https://factory.invalid/a.raw.xz', tmp_path / 'absent.raw')

    result = talos_factory.FactoryImageProvider().read('an-id', stored)

    # The file is a build artifact, not managed state: every CI runner starts
    # without it. A refresh that called this a deleted resource would take the
    # worker's boot disk down with it on the next apply.
    assert result.id == 'an-id'
    assert result.outs == stored


def test_deleting_the_resource_takes_the_local_copy_with_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = serve(monkeypatch, lzma.compress(PAYLOAD))
    path = tmp_path / 'talos.raw'
    provider = talos_factory.FactoryImageProvider()
    created = provider.create(props('https://factory.invalid/a.raw.xz', path))

    provider.delete(str(created.id), created.outs or {})

    # Deterministic cleanup, and tolerant of the file already being gone —
    # which is the ordinary case on a machine that never created it.
    assert not path.exists()
    provider.delete(str(created.id), created.outs or {})


def warnings_of(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what the provider warns through `pulumi.log`, the channel a dynamic provider has."""
    warned: list[str] = []

    def warn(message: str, *_args: object, **_kwargs: object) -> None:
        warned.append(message)

    monkeypatch.setattr(talos_factory.pulumi.log, 'warn', warn)
    return warned


def until_the_clock_passes(path: Path) -> None:
    """Return once the file system stamps a status change later than `path`'s last one.

    A replacement is created in a later run than its original, whose write is
    then behind the file system's clock. Two creates back to back are not: a
    file system that stamps from a coarse clock gives both one status-change
    time. So a case that stands for the later run waits for the clock to pass
    the original's write, on a file of its own beside it, rather than
    inheriting a fine-grained clock from the kernel.
    """
    stamped = path.stat().st_ctime_ns
    probe = path.with_name(f'{path.name}.clock')
    probe.touch()
    while probe.stat().st_ctime_ns <= stamped:
        probe.chmod(0o644)


def test_a_replacement_that_reuses_the_file_keeps_it_when_the_original_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create-first replacement of the same artifact, as a targeted `--replace` makes, leaves the file.

    The replacement declares the same path, finds the file the original
    fetched and reuses it, and the engine then deletes the original with the
    original's outputs. That delete must not take the file the replacement
    stands for: no run that leaves the image alone brings it back, and a
    disk created from it in such a run would find none.
    """
    requested = serve(monkeypatch, lzma.compress(PAYLOAD))
    warned = warnings_of(monkeypatch)
    path = tmp_path / 'talos.raw'
    provider = talos_factory.FactoryImageProvider()
    declared = props('https://factory.invalid/a.raw.xz', path)
    original = provider.create(declared)
    until_the_clock_passes(path)
    replacement = provider.create(declared)
    assert len(requested) == 1, 'the replacement reuses the file rather than fetching it again'

    provider.delete(str(original.id), original.outs or {})

    assert path.read_bytes() == PAYLOAD
    assert len(warned) == 1 and str(path) in warned[0], warned
    # The replacement's own delete, when it comes, is the one that removes it.
    provider.delete(str(replacement.id), replacement.outs or {})
    assert not path.exists()


def test_a_refresh_keeps_the_identity_its_create_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh reads an original awaiting its delete too, and must not hand it the replacement's write."""
    _ = serve(monkeypatch, lzma.compress(PAYLOAD))
    _ = warnings_of(monkeypatch)
    path = tmp_path / 'talos.raw'
    provider = talos_factory.FactoryImageProvider()
    declared = props('https://factory.invalid/a.raw.xz', path)
    original = provider.create(declared)
    until_the_clock_passes(path)
    _ = provider.create(declared)

    refreshed = provider.read(str(original.id), original.outs or {})
    provider.delete(str(original.id), refreshed.outs or {})

    assert path.read_bytes() == PAYLOAD


def test_a_resource_created_with_no_identity_recorded_deletes_the_file(tmp_path: Path) -> None:
    # A resource created without a recorded identity keeps none, since the
    # provider has no update to give it one, and its delete removes the file.
    path = tmp_path / 'talos.raw'
    _ = path.write_bytes(PAYLOAD)

    talos_factory.FactoryImageProvider().delete('an-id', props('https://factory.invalid/a.raw.xz', path))

    assert not path.exists()


def test_the_provider_carries_no_state_of_its_own() -> None:
    # A dynamic provider is pickled into the stack's state and revived in
    # another process, so anything it closed over would travel with it.
    provider: dynamic.ResourceProvider = talos_factory.FactoryImageProvider()
    assert vars(provider) == {}
