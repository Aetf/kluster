# The OCI SDK ships no type information (no py.typed, no stubs), so every
# value that crosses its boundary is Unknown to a strict checker. Suppressing
# that here, in the one module of this package that touches the SDK, keeps
# the rest of the codebase under the full standard.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""The objects OCI imports a custom image from, uploaded to OCI Object Storage.

OCI imports a custom image only from an object, and only uncompressed, while
the publishers of the images this installation boots ship them compressed or
loose (rfc-006 §4.1). Each resource here is one way of getting from what a
publisher serves to the object an `oci.core.Image` is imported from, declared
by whoever declares this:

-   **`ArtifactObject`** fetches an `xz` artifact, checks the compressed bytes
    against the digest its publisher states, decompresses it on the machine
    running the program, and uploads the result: the state-backend
    appliance's Fedora CoreOS image.
-   **`ImageArchive`** packages a disk image already on the machine running the
    program with the `image_metadata.json` that says how OCI launches it, as
    one gzipped tar, and uploads that: the archive Talos's Oracle guide
    prescribes for the cloud nodes, whose disk image the factory serves with
    no checksum (`kluster.providers.talos_factory` fetches it). OCI reads the
    launch options and the compatible shapes out of the archive, so the image
    imported from it names no `sourceImageType` (the API's `ImageSourceDetails`:
    the field "is not used for exported Oracle images with the OCI image
    format").

**Nothing of the machine or the moment enters the archive.** Its members
carry no time, owner or mode of the machine that built it, and the gzip header
carries no file name and no time, so packaging one disk image with one
document twice under one build of the compression library gives one archive;
another build may compress the same input differently. Nothing depends on the
bytes being equal: the resource is identified by its declared inputs, and no
digest of the archive is recorded or compared.

Dynamic providers because no provider uploads an object from a URL, and
`oci.objectstorage.StorageObject`'s local `source` is a file the plan itself
needs: the provider fails a plan on any machine without the file, and records
the file's modification time beside its path, so a file fetched again plans a
replacement (terraform-provider-oci, `GetSourceFileState` and
`ValidateSourceValue`). The disk image is a build artifact that a CI runner
never holds. Here the object is what was declared -- its source and where it
goes -- and the file is read only when the object is created.

**The credential is the provider's own** (`kluster.providers.configured`): the
OCI signing configuration's three secrets, read in `configure` from the
stack's configuration under the keys below. The account's tenancy and region
are not secrets and are declared inputs, as is everything that says where
the object goes. `check` stamps the session and this module's version.

**Every declared input but the tenancy is the object's identity**, so a
change to any of them is a new object and the old one deleted after it: a
release bump uploads the new artifact before the image that imported the old
one is replaced. An `ImageArchive`'s identity is the disk image's path, its
name inside the archive and the metadata document, so a disk image that
changes behind an unchanged path is not one it notices; its caller names the
path by what the file holds. A change that keeps the object's name -- a
digest corrected for the same release -- deletes the old object first, since
the two are one object. The tenancy only says which account the session signs
into, and a change to it, like a rotation, re-stamps the resource and uploads
nothing. `read` is the inherited one and reports no drift: an object deleted
behind the state is found by the image import that needs it.

**The delete removes only the object its own create wrote.** The create
records `etag`, the ETag OCI answers the upload with, and the delete is
conditioned on it (`if-match`). OCI's ETag names a write rather than its
bytes, so a replacement the engine creates before it deletes the original --
a `--replace`, which asks no diff -- uploads the same artifact under the
same name with an ETag of its own, and the original's delete then answers
`412` and leaves the replacement where it is, with a warning rather than a
failure: an original whose delete fails stays in state, and that delete is
retried, and fails again, on every later run. A refresh carries the
recorded ETag rather than reading the object's: it refreshes that original
too, which a read would hand the replacement's ETag. The ETag is in the
clear in the committed checkpoint: it names a write, not a principal. A
resource that recorded none deletes unconditionally, and its first re-stamp
takes the object's current ETag; a re-stamp that finds no object records
`NO_WRITE` instead and warns, and the delete then removes nothing.

**Each long step says what it is doing before it starts** -- the fetch, the
decompression, the packaging, the upload -- through `pulumi.log`, which
`pulumi` draws on the stack's row as the run goes. The standard `logging`
module would reach nobody: nothing configures it in the process a dynamic
provider runs in.
"""

from __future__ import annotations

import abc
import gzip
import hashlib
import io
import lzma
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, final

import oci
import pulumi
import pulumi.dynamic as dynamic
import requests

from kluster.providers.configured import STAMPS, ConfiguredProvider, declared_change, has_unknowns

__all__ = (
    'ARCHIVE_COMPARED',
    'ARCHIVE_IDENTITY',
    'ARCHIVE_VERSION',
    'CHUNK_BYTES',
    'COMPARED',
    'DECLARED',
    'ETAG',
    'FETCH_TIMEOUT',
    'FINGERPRINT_CONFIG',
    'IDENTITY',
    'IMAGE_METADATA',
    'NO_WRITE',
    'PRIVATE_KEY_CONFIG',
    'USER_CONFIG',
    'VERSION',
    'ArtifactObject',
    'ArtifactObjectProvider',
    'ArtifactRefused',
    'DigestMismatch',
    'DiskImageMissing',
    'ImageArchive',
    'ImageArchiveProvider',
    'TruncatedArtifact',
    'fetch',
    'materialize',
    'package',
    'upload',
)

#: How long one read from the publisher may stall before the fetch is
#: abandoned: a bound on a link that has stopped moving, not on the transfer.
FETCH_TIMEOUT = 60

#: Bytes pulled from the network per step. Neither form of the artifact is
#: ever held in memory whole.
CHUNK_BYTES = 1 << 20

#: The stack-configuration keys the OCI signing configuration is read from.
USER_CONFIG = 'ociUserOcid'
FINGERPRINT_CONFIG = 'ociFingerprint'
PRIVATE_KEY_CONFIG = 'ociPrivateKey'

#: What the object is: where it comes from, the digest it is held to, and
#: where it goes.
IDENTITY = ('url', 'sha256', 'region', 'namespace', 'bucket', 'object_name')

#: What a caller declares: the identity, and the account the session signs into.
DECLARED = (*IDENTITY, 'tenancy')

#: What `diff` compares: the declared properties and the two stamps.
COMPARED = (*DECLARED, *STAMPS)

#: The output that records the ETag of the write the resource's create made,
#: which its delete is conditioned on.
ETAG = 'etag'

#: What `etag` records for a resource whose re-stamp found no object: no write
#: of the resource's stands, and no later write can be one, so its delete
#: removes nothing. No upload is answered with it, OCI's ETags being UUIDs. It
#: is stored in state, so renaming it is a migration.
NO_WRITE = 'no-write-of-this-resource-stands'

#: What an `ImageArchive` is: the disk image it packages and the name it has
#: inside the archive, the document packaged beside it, and where the archive
#: goes.
ARCHIVE_IDENTITY = ('path', 'member', 'image_metadata', 'region', 'namespace', 'bucket', 'object_name')

#: What `diff` compares for an `ImageArchive`: as `COMPARED`, over its identity.
ARCHIVE_COMPARED = (*ARCHIVE_IDENTITY, 'tenancy', *STAMPS)

#: The name OCI reads an archive's launch options and compatible shapes from.
IMAGE_METADATA = 'image_metadata.json'

#: This module's version for `ArtifactObject`, bumped by hand when one of its
#: operations' behavior changes (`configured`).
VERSION = '2'

#: The same, for `ImageArchive`.
ARCHIVE_VERSION = '1'


class ArtifactRefused(Exception):
    """The artifact fetched is not the one declared, and nothing was uploaded."""


@final
class DigestMismatch(ArtifactRefused):
    """The compressed bytes do not have the digest the artifact is declared with."""

    def __init__(self, url: str, *, found: str, declared: str) -> None:
        super().__init__(f'{url} has sha256 {found}, and is declared with {declared}: nothing was uploaded')


@final
class TruncatedArtifact(ArtifactRefused):
    """The compressed stream ended before the artifact did."""

    def __init__(self, url: str) -> None:
        super().__init__(f'{url} ended mid-stream: the artifact is incomplete, and nothing was uploaded')


def fetch(url: str) -> requests.Response:
    """Open the artifact's byte stream. The seam a test replaces."""
    response = requests.get(url, stream=True, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    return response


def materialize(url: str, sha256: str, path: Path) -> None:
    """Leave the artifact at `url` decompressed at `path`, or refuse having left nothing there.

    **Nothing is decompressed until the digest has vouched for it.** The
    compressed bytes are written beside `path` and digested as they arrive,
    which is what a publisher states a digest over; an artifact that differs
    is refused there, before a byte of it is parsed. Only then is it
    decompressed, a bounded amount per step, so neither form is ever held in
    memory whole. A stream whose digest matches and which still ends before
    the `xz` stream does is refused as truncated: what the digest was taken
    over was incomplete too.
    """
    compressed = path.with_name(f'{path.name}.xz')
    try:
        hasher = hashlib.sha256()
        with compressed.open('wb') as sink, fetch(url) as response:
            for chunk in response.iter_content(CHUNK_BYTES):
                hasher.update(chunk)
                _ = sink.write(chunk)
        if (found := hasher.hexdigest()) != sha256:
            raise DigestMismatch(url, found=found, declared=sha256)
        pulumi.log.info(f'{url} matches its digest; decompressing it ({_gib(compressed):.1f} GiB compressed)')
        _decompress(url, compressed, path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        compressed.unlink(missing_ok=True)


def _decompress(url: str, source: Path, path: Path) -> None:
    """`source`'s `xz` stream into `path`, at most `CHUNK_BYTES` out of each call."""
    decompressor = lzma.LZMADecompressor()
    with source.open('rb') as compressed, path.open('wb') as sink:
        while not decompressor.eof:
            chunk = b''
            if decompressor.needs_input:
                chunk = compressed.read(CHUNK_BYTES)
                if not chunk:
                    raise TruncatedArtifact(url)
            _ = sink.write(decompressor.decompress(chunk, max_length=CHUNK_BYTES))


@final
class DiskImageMissing(Exception):
    """The disk image an archive packages is not on the machine creating it, and nothing was uploaded."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f'{path} is not on this machine: the resource that fetches it holds it on the machine that '
            'created that resource, and replacing that resource here fetches it again; nothing was uploaded'
        )


def package(image: Path, member: str, image_metadata: str, archive: Path) -> None:
    """Write the disk image at `image`, as `member`, and `image_metadata` to `archive` as a gzipped tar.

    The disk image goes first, under the name its caller gives it, and the
    document second, as `IMAGE_METADATA` -- the order of Talos's Oracle guide
    (`tar zcf oracle-arm64.oci oracle-arm64.qcow2 image_metadata.json`), and
    its member names where the caller passes the artifact's own name. The
    name is the caller's rather than the file's because the file is named for
    a cache: a member name longer than a tar header's 100 characters is
    written behind an extra GNU long-name record ahead of the disk image,
    which changes the archive's structure.
    Nothing of the machine that builds it enters the archive: each member's
    time, owner and mode are fixed, and the gzip header names no file and no
    time. The disk image is streamed through, never held in memory.
    """
    document = image_metadata.encode()
    with (
        archive.open('wb') as sink,
        gzip.GzipFile(filename='', mode='wb', fileobj=sink, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode='w|', format=tarfile.GNU_FORMAT) as tar,
        image.open('rb') as disk,
    ):
        tar.addfile(_member(member, image.stat().st_size), disk)
        tar.addfile(_member(IMAGE_METADATA, len(document)), io.BytesIO(document))


def _member(name: str, size: int) -> tarfile.TarInfo:
    """A regular file's header with nothing in it but its name and size."""
    member = tarfile.TarInfo(name)
    member.size = size
    member.mtime = 0
    member.mode = 0o644
    member.uid = member.gid = 0
    member.uname = member.gname = ''
    return member


def upload(config: Mapping[str, str], props: Mapping[str, Any], path: Path) -> str:
    """Upload the file at `path` as the object `props` names, and answer the write's ETag. The seam a test replaces.

    `upload_file` answers with the multipart upload's commit or, for a file
    too small to go multipart, the put, and either carries the ETag the
    write left on the object.
    """
    client = oci.object_storage.ObjectStorageClient(dict(config))
    response = oci.object_storage.UploadManager(client, allow_parallel_uploads=True).upload_file(
        str(props['namespace']), str(props['bucket']), str(props['object_name']), str(path)
    )
    return str(response.headers['etag'])


def _gib(path: Path) -> float:
    return path.stat().st_size / 2**30


def _object_id(props: Mapping[str, Any]) -> str:
    return '/'.join(str(props[key]) for key in ('namespace', 'bucket', 'object_name'))


class _ObjectProvider(ConfiguredProvider, abc.ABC):
    """One file as one object: create uploads it, delete removes what that upload wrote, update re-stamps.

    The half both resources share. A subclass names its identity and its
    version, and supplies `_prepare`, which leaves the file to upload in a
    scratch directory the create removes afterwards.
    """

    #: The declared inputs that are the object: a change to any is a new one.
    identity: ClassVar[tuple[str, ...]]
    #: What `diff` compares: the declared inputs and the two stamps.
    compared: ClassVar[tuple[str, ...]]

    user: str
    fingerprint: str
    private_key: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.user = str(config.require(USER_CONFIG))
        self.fingerprint = str(config.require(FINGERPRINT_CONFIG))
        self.private_key = str(config.require(PRIVATE_KEY_CONFIG))

    def _credential(self) -> str:
        return f'{self.user}:{self.fingerprint}:{self.private_key}'

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return f'{props.get("region")}/{props.get("namespace")}/{props.get("bucket")}'

    def _signing(self, props: Mapping[str, Any]) -> dict[str, str]:
        """The signing configuration the SDK takes: the credential, and the account it is declared against."""
        return {
            'user': self.user,
            'fingerprint': self.fingerprint,
            'key_content': self.private_key,
            'tenancy': str(props['tenancy']),
            'region': str(props['region']),
        }

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return self._stamp(news, [])

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        replaces = [key for key in self.identity if olds.get(key) != news.get(key)]
        return dynamic.DiffResult(
            changes=declared_change(olds, news, self.compared),
            replaces=replaces,
            # A new object is uploaded before the old one goes, so the image
            # that imported the old one never points at nothing -- unless the
            # two are one object, which the old one's delete makes room for
            # first, so that this replacement does not rest on the ETag the
            # delete is conditioned on.
            delete_before_replace=_object_id(olds) == _object_id(news),
        )

    @abc.abstractmethod
    def _prepare(self, props: Mapping[str, Any], scratch: Path) -> Path:
        """Leave the file the object is to hold in `scratch`, and answer where it is."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        with tempfile.TemporaryDirectory(prefix='artifact-') as scratch:
            path = self._prepare(props, Path(scratch))
            pulumi.log.info(
                f'uploading {props["object_name"]} to {props["namespace"]}/{props["bucket"]} ({_gib(path):.1f} GiB)'
            )
            etag = upload(self._signing(props), props, path)
        return dynamic.CreateResult(id_=_object_id(props), outs={**props, ETAG: etag})

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        # Reached only when a stamp or the tenancy moved (`diff`): the object
        # is the one declared, so this records the new stamps, keeps the
        # recorded ETag, and uploads nothing. A resource that recorded none
        # takes the object's current one.
        etag = olds.get(ETAG) or self._current_etag(news)
        return dynamic.UpdateResult(outs={**news, ETAG: etag})

    def _current_etag(self, props: dict[str, Any]) -> str:
        """The ETag the object `props` names carries now, or `NO_WRITE` where the read finds no object.

        A `404` is the object missing or this credential unable to read it
        (OCI's `NotAuthorizedOrNotFound`). Either way no write this resource
        made is known to stand, and recording nothing would make its delete
        unconditional for good, removing whatever a later replacement
        uploads; failing would be retried, and fail again, on every later run.
        """
        client = oci.object_storage.ObjectStorageClient(self._signing(props))
        try:
            response = client.head_object(str(props['namespace']), str(props['bucket']), str(props['object_name']))
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise
            pulumi.log.warn(
                f'{_object_id(props)} is missing, or this credential cannot read it; '
                'recording that no write of this resource stands, so its delete will remove nothing'
            )
            return NO_WRITE
        # The SDK's code paths let a checker infer `None` for its answer; a
        # `head_object` that succeeds answers with the object's headers.
        return str(response.headers['etag'])  # pyright: ignore[reportOptionalMemberAccess]

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        etag = props.get(ETAG)
        if etag == NO_WRITE:
            return
        client = oci.object_storage.ObjectStorageClient(self._signing(props))
        condition = {} if etag is None else {'if_match': str(etag)}
        try:
            _ = client.delete_object(
                str(props['namespace']), str(props['bucket']), str(props['object_name']), **condition
            )
        except oci.exceptions.ServiceError as exc:
            if exc.status == 412:
                # Another write holds the name: a replacement that uploaded
                # the artifact again before this delete ran. It is left, and
                # the delete returns, so the engine drops this resource
                # rather than retrying it on every later run.
                pulumi.log.warn(
                    f'{_object_id(props)} was written again after this resource uploaded it '
                    f'(ETag {etag}); leaving it in place'
                )
            elif exc.status != 404:
                raise


@final
class ArtifactObjectProvider(_ObjectProvider):
    """A publisher's `xz` artifact as one object, checked against its digest and decompressed before the upload."""

    identity = IDENTITY
    compared = COMPARED

    def _version(self) -> str:
        return VERSION

    def _prepare(self, props: Mapping[str, Any], scratch: Path) -> Path:
        path = scratch / 'artifact'
        pulumi.log.info(f'fetching {props["url"]} (about a gigabyte compressed) and checking it against its digest')
        materialize(str(props['url']), str(props['sha256']), path)
        return path


@final
class ImageArchiveProvider(_ObjectProvider):
    """A local disk image and its `image_metadata.json` as one object: the archive OCI imports an image from."""

    identity = ARCHIVE_IDENTITY
    compared = ARCHIVE_COMPARED

    def _version(self) -> str:
        return ARCHIVE_VERSION

    def _prepare(self, props: Mapping[str, Any], scratch: Path) -> Path:
        image = Path(str(props['path']))
        if not image.is_file():
            raise DiskImageMissing(str(image))
        archive = scratch / str(props['object_name'])
        pulumi.log.info(f'packaging {image} ({_gib(image):.1f} GiB) with {IMAGE_METADATA} into {archive.name}')
        package(image, str(props['member']), str(props['image_metadata']), archive)
        return archive


@final
class ArtifactObject(dynamic.Resource, module='oci_objects', name='ArtifactObject'):
    """A compressed artifact at `url`, uploaded decompressed as `object_name` in `bucket`."""

    url: pulumi.Output[str]
    sha256: pulumi.Output[str]
    region: pulumi.Output[str]
    tenancy: pulumi.Output[str]
    namespace: pulumi.Output[str]
    bucket: pulumi.Output[str]
    object_name: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        url: pulumi.Input[str],
        sha256: pulumi.Input[str],
        region: pulumi.Input[str],
        tenancy: pulumi.Input[str],
        namespace: pulumi.Input[str],
        bucket: pulumi.Input[str],
        object_name: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `object_name` in `bucket` holds the decompressed artifact at `url`, whose compressed digest is `sha256`."""
        super().__init__(
            ArtifactObjectProvider(),
            name,
            {
                'url': url,
                'sha256': sha256,
                'region': region,
                'tenancy': tenancy,
                'namespace': namespace,
                'bucket': bucket,
                'object_name': object_name,
            },
            opts,
        )


@final
class ImageArchive(dynamic.Resource, module='oci_objects', name='ImageArchive'):
    """The disk image at `path`, named `member`, and `image_metadata`, uploaded as one archive, `object_name`."""

    path: pulumi.Output[str]
    member: pulumi.Output[str]
    image_metadata: pulumi.Output[str]
    region: pulumi.Output[str]
    tenancy: pulumi.Output[str]
    namespace: pulumi.Output[str]
    bucket: pulumi.Output[str]
    object_name: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        path: pulumi.Input[str],
        member: pulumi.Input[str],
        image_metadata: pulumi.Input[str],
        region: pulumi.Input[str],
        tenancy: pulumi.Input[str],
        namespace: pulumi.Input[str],
        bucket: pulumi.Input[str],
        object_name: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `object_name` in `bucket` holds the archive of the disk image at `path` and `image_metadata`.

        `path` is read on the machine that creates the resource, and only
        there: it names a file another resource left on that machine, named
        by what it holds, so a disk image that changes is a new path and a new
        archive. `member` is the disk image's name inside the archive, which
        is the caller's to choose (`package`).
        """
        super().__init__(
            ImageArchiveProvider(),
            name,
            {
                'path': path,
                'member': member,
                'image_metadata': image_metadata,
                'region': region,
                'tenancy': tenancy,
                'namespace': namespace,
                'bucket': bucket,
                'object_name': object_name,
            },
            opts,
        )
