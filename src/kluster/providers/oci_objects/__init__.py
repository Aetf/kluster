# The OCI SDK ships no type information (no py.typed, no stubs), so every
# value that crosses its boundary is Unknown to a strict checker. Suppressing
# that here, in the one module of this package that touches the SDK, keeps
# the rest of the codebase under the full standard.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""A compressed artifact, decompressed and uploaded as an object to OCI Object Storage.

OCI imports a custom image only from an object, and only uncompressed, while
the publishers of the images this installation boots ship them compressed
(rfc-006 §4.1). `ArtifactObject` is the step between: it fetches an `xz`
artifact, checks the compressed bytes against the digest its publisher
states, decompresses it on the machine running the program, and uploads the
result. The image is then an ordinary `oci.core.Image` imported from that
object, declared by whoever declares this.

A dynamic provider because no provider uploads an object from a URL, and the
same detour `kluster.providers.talos_factory` takes for a disk image the
libvirt provider cannot decompress: the download is checked before anything
leaves the machine, and the seam a test replaces is a Python function.

**The credential is the provider's own** (`kluster.providers.configured`): the
OCI signing configuration's three secrets, read in `configure` from the
stack's configuration under the keys below. The account's tenancy and region
are not secrets and are declared inputs, as is everything that says where
the object goes. `check` stamps the session and this module's version.

**Every declared input but the tenancy is the object's identity**, so a
change to any of them is a new object and the old one deleted after it: a
release bump uploads the new artifact before the image that imported the old
one is replaced. A change that keeps the object's name -- a digest corrected
for the same release -- deletes the old object first, since the two are one
object. The tenancy only says which account the session signs into,
and a change to it, like a rotation, re-stamps the resource and uploads
nothing. `read` is the inherited one and reports no drift: an object deleted
behind the state is found by the image import that needs it.
"""

from __future__ import annotations

import hashlib
import logging
import lzma
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, final

import oci
import pulumi
import pulumi.dynamic as dynamic
import requests

from kluster.providers.configured import STAMPS, ConfiguredProvider, declared_change, has_unknowns

__all__ = (
    'CHUNK_BYTES',
    'COMPARED',
    'DECLARED',
    'FETCH_TIMEOUT',
    'FINGERPRINT_CONFIG',
    'IDENTITY',
    'PRIVATE_KEY_CONFIG',
    'USER_CONFIG',
    'VERSION',
    'ArtifactObject',
    'ArtifactObjectProvider',
    'ArtifactRefused',
    'DigestMismatch',
    'TruncatedArtifact',
    'fetch',
    'materialize',
    'upload',
)

log = logging.getLogger(__name__)

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

#: This module's version, bumped by hand when an operation's behavior changes
#: (`configured`).
VERSION = '1'


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


def upload(config: Mapping[str, str], props: Mapping[str, Any], path: Path) -> None:
    """Upload the file at `path` as the object `props` names. The seam a test replaces."""
    client = oci.object_storage.ObjectStorageClient(dict(config))
    log.info('uploading %s to %s/%s (%.1f GiB)', props['object_name'], props['namespace'], props['bucket'], _gib(path))
    _ = oci.object_storage.UploadManager(client, allow_parallel_uploads=True).upload_file(
        str(props['namespace']), str(props['bucket']), str(props['object_name']), str(path)
    )


def _gib(path: Path) -> float:
    return path.stat().st_size / 2**30


def _object_id(props: Mapping[str, Any]) -> str:
    return '/'.join(str(props[key]) for key in ('namespace', 'bucket', 'object_name'))


@final
class ArtifactObjectProvider(ConfiguredProvider):
    """One artifact as one object: create uploads it, delete removes it, update re-stamps."""

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

    def _version(self) -> str:
        return VERSION

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
        replaces = [key for key in IDENTITY if olds.get(key) != news.get(key)]
        return dynamic.DiffResult(
            changes=declared_change(olds, news, COMPARED),
            replaces=replaces,
            # A new object is uploaded before the old one goes, so the image
            # that imported the old one never points at nothing -- unless the
            # two are one object, whose delete would then remove what the
            # create had just uploaded.
            delete_before_replace=_object_id(olds) == _object_id(news),
        )

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        with tempfile.TemporaryDirectory(prefix='artifact-') as scratch:
            path = Path(scratch) / 'artifact'
            log.info('fetching %s (about a gigabyte compressed) and checking it against its digest', props['url'])
            materialize(str(props['url']), str(props['sha256']), path)
            upload(self._signing(props), props, path)
        return dynamic.CreateResult(id_=_object_id(props), outs=props)

    def update(self, _id: str, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        # Reached only when a stamp or the tenancy moved (`diff`): the object
        # is the one declared, so this records the new stamps and uploads
        # nothing.
        return dynamic.UpdateResult(outs=news)

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        client = oci.object_storage.ObjectStorageClient(self._signing(props))
        try:
            _ = client.delete_object(str(props['namespace']), str(props['bucket']), str(props['object_name']))
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise


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
