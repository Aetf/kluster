"""Pulling a container image's root filesystem out of a registry, verified.

The device this package writes to cannot pull from a registry: it runs
`systemd-nspawn` over a directory, not a container engine, and its user space
is the cut-down one a router ships. So the runner does the pulling and hands
the device a plain archive — which is the same division of labour a release
tarball got, with the archive now assembled here instead of downloaded whole.

**The reference is the pin, and the pin verifies everything.** An image is
named by its manifest digest, and every byte below it is reachable only through
content this module has already checked:

1.  the manifest is fetched *by* that digest and hashed against it, so a
    registry serving different bytes under the same name is caught before
    anything is parsed;
2.  each layer named in that verified manifest is hashed against the digest the
    manifest gives for it, so a substituted blob is caught before it is
    decompressed;
3.  nothing else is read at all — the image configuration is not fetched,
    because a root filesystem is the layers and nothing in the configuration
    changes what is on disk.

That chain is what a URL plus a `sha256` bought before, one link longer and
maintained by the same machinery that maintains any other image pin.

**Flattening is `podman export` in Python.** Layers are applied in order and
the result is one tar: a path a later layer carries replaces the same path in
an earlier one, and an overlay whiteout removes what it covers rather than
travelling to the device as a `.wh.` file the container would then see. Entries
are copied with their own metadata — mode, ownership, times, link targets and
the PAX headers that carry file capabilities — so what the device unpacks is
the tree the image builder produced.

The archive is *content*-identical to what `podman export` writes, not
byte-identical: tar padding and the order of equal entries are a writer's
choice. Nothing depends on the bytes matching, because what the device records
beside the tree is the manifest digest — the pin — and never a checksum of the
archive lying next to it.

**Only anonymous, public pulls.** This module carries no credential and takes
none: it answers a registry's `Bearer` challenge with the token that challenge
hands out for free. An image that needs a login would fail here rather than
silently reach for an ambient one, which is the right failure for a
provider whose whole input is a public reference.

The one seam a test replaces is `request`, so the token dance, the digest
arithmetic, the media-type handling and the flattening are all the shipped
code — as with the transport in `ssh`, no test opens a socket.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, final

import requests
import zstandard

__all__ = (
    'ACCEPT',
    'BLOB_TIMEOUT',
    'DECOMPRESSORS',
    'MANIFEST_TIMEOUT',
    'MANIFEST_TYPES',
    'OPAQUE_WHITEOUT',
    'TOKEN_ACCEPT',
    'WHITEOUT_PREFIX',
    'DigestMismatch',
    'Image',
    'UnsupportedImage',
    'flatten',
    'request',
    'rootfs',
)

#: The manifest media types this module knows how to read: a single image, in
#: either of the two spellings registries serve. A multi-platform index is
#: deliberately absent — see `UnsupportedImage`.
MANIFEST_TYPES = (
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
)

#: What a manifest request asks for. Sent as one header so a registry that
#: would otherwise fall back to a legacy schema answers with the modern one.
ACCEPT = ', '.join(MANIFEST_TYPES)

#: What the trip to an authorization realm asks for. A realm answers JSON and
#: nothing else, so this says so rather than repeating the manifest's list.
TOKEN_ACCEPT = 'application/json'


def _stored(data: bytes) -> bytes:
    """An uncompressed layer: the archive is already the archive."""
    return data


def _ungzip(data: bytes) -> bytes:
    return gzip.decompress(data)


def _unzstd(data: bytes) -> bytes:
    # `stream_reader` rather than `decompress`: the latter refuses a frame whose
    # header omits the uncompressed size, which is a legal thing for a
    # compressor to write and not something a consumer gets to require.
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True) as reader:
        return reader.read()


#: How a layer's bytes are packed, by the media type that declares it. The
#: media type is read rather than the bytes sniffed: it comes out of a manifest
#: this module has already verified, so it is a statement the pin covers, and
#: an unknown one is refused instead of guessed at.
DECOMPRESSORS: Mapping[str, Callable[[bytes], bytes]] = {
    'application/vnd.oci.image.layer.v1.tar': _stored,
    'application/vnd.oci.image.layer.v1.tar+gzip': _ungzip,
    'application/vnd.oci.image.layer.v1.tar+zstd': _unzstd,
    'application/vnd.docker.image.rootfs.diff.tar': _stored,
    'application/vnd.docker.image.rootfs.diff.tar.gzip': _ungzip,
}

#: How an overlay layer says a path is gone: a marker file named after it, and
#: the special marker that empties a directory the earlier layers filled.
WHITEOUT_PREFIX = '.wh.'
OPAQUE_WHITEOUT = '.wh..wh..opq'

#: How long the runner may spend on one request. A manifest is a few kilobytes
#: and a layer is tens of megabytes, so they are not the same wait.
MANIFEST_TIMEOUT = 60
BLOB_TIMEOUT = 300

#: A registry reference's digest, in the one form registries use.
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}')

#: One `key="value"` of a `WWW-Authenticate` challenge.
_CHALLENGE = re.compile(r'(?P<key>[a-z_]+)="(?P<value>[^"]*)"')


@final
class DigestMismatch(Exception):
    """Content does not hash to the digest that named it, so nothing is used."""

    def __init__(self, what: str, expected: str, actual: str) -> None:
        super().__init__(f'{what} hashes to {actual}, not the pinned {expected}; refusing to use it')
        self.what: str = what
        self.expected: str = expected
        self.actual: str = actual


@final
class UnsupportedImage(Exception):
    """The reference resolves to something this module will not guess about.

    Two cases, and both are refusals rather than fallbacks. A **multi-platform
    index** would make this module choose a platform, and a root filesystem
    pushed to one device has exactly one right answer that the pin should be
    naming directly. An **unknown layer media type** would make it guess at a
    compression, and a guess that is wrong produces a tree rather than an
    error.
    """

    def __init__(self, reference: str, why: str) -> None:
        super().__init__(f'{reference} {why}')
        self.reference: str = reference
        self.why: str = why


@final
@dataclass(frozen=True)
class Image:
    """One image, as a pull needs it: where it lives and which bytes it is.

    `repository` carries its registry host, because a reference without one is
    only meaningful against a default this module refuses to have: an image
    fetched from somewhere the caller did not name is the failure the pin
    exists to prevent. `digest` is the manifest digest and is what the pull is
    performed by — a tag would be a name someone else can move.
    """

    repository: str
    digest: str

    def __post_init__(self) -> None:
        if '/' not in self.repository or not _is_registry(self.repository.split('/', 1)[0]):
            raise ValueError(f'{self.repository!r} does not begin with a registry host')
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError(f'{self.digest!r} is not a `sha256:<hex>` digest')

    def __str__(self) -> str:
        return f'{self.repository}@{self.digest}'

    @property
    def host(self) -> str:
        """The registry the pull is addressed to."""
        return self.repository.split('/', 1)[0]

    @property
    def path(self) -> str:
        """The repository within that registry, as the API path names it."""
        return self.repository.split('/', 1)[1]

    def manifest_url(self) -> str:
        return f'https://{self.host}/v2/{self.path}/manifests/{self.digest}'

    def blob_url(self, digest: str) -> str:
        return f'https://{self.host}/v2/{self.path}/blobs/{digest}'


def request(
    url: str,
    *,
    accept: str,
    token: str | None,
    timeout: int,
    params: Mapping[str, str] | None = None,
) -> requests.Response:
    """Perform one HTTP request. The one seam a test replaces.

    Every request this module makes goes through here, the trip to an
    authorization realm included, so a test that replaces it has replaced the
    network and not merely most of it.

    Redirects are followed, because a registry answers a blob with one to
    wherever it actually stores the bytes; `requests` drops the authorization
    header when that redirect crosses hosts, which is what should happen to a
    token minted for the registry.
    """
    headers = {'Accept': accept}
    if token is not None:
        headers['Authorization'] = f'Bearer {token}'
    return requests.get(url, headers=headers, params=params, timeout=timeout, allow_redirects=True)


def rootfs(image: Image) -> bytes:
    """The image's root filesystem, as one plain archive the device can unpack.

    Everything the archive is made of is verified against the pin on the way
    in; see this module's docstring for the chain and for what is deliberately
    not fetched.
    """
    token: str | None = None
    raw, token = _get(image, image.manifest_url(), accept=ACCEPT, timeout=MANIFEST_TIMEOUT, token=token)
    _verify(str(image), image.digest, raw)

    manifest: Mapping[str, Any] = json.loads(raw)
    media_type = str(manifest.get('mediaType', ''))
    if media_type not in MANIFEST_TYPES:
        # An index lands here too, which is the point: it is a list of images
        # rather than an image, and picking one of them is not this module's
        # decision to make.
        raise UnsupportedImage(str(image), f'is a {media_type or "manifest of no declared type"}, not a single image')

    layers: list[bytes] = []
    for layer in manifest['layers']:
        digest = str(layer['digest'])
        packing = str(layer['mediaType'])
        decompress = DECOMPRESSORS.get(packing)
        if decompress is None:
            raise UnsupportedImage(str(image), f'has a layer packed as {packing}, which this cannot unpack')
        blob, token = _get(image, image.blob_url(digest), accept=packing, timeout=BLOB_TIMEOUT, token=token)
        _verify(f'{image} layer {digest}', digest, blob)
        # Decompressed only after the manifest's digest for it has been
        # checked, so what is unpacked is a function of bytes already vouched
        # for -- the same ordering the device push has always used.
        layers.append(decompress(blob))
    return flatten(layers)


def flatten(layers: Sequence[bytes]) -> bytes:
    """Apply layer archives in order and write the result as one archive.

    A path in a later layer replaces the same path in an earlier one, and a
    whiteout removes what it names instead of being carried through: the device
    unpacks this with `tar`, which has no notion of a deletion, so a marker
    that reached it would appear inside the container as a file the image meant
    to have removed.

    Entries keep their own metadata, PAX headers included, so file
    capabilities and long names survive; the output is written as PAX for the
    same reason. Replacing an entry keeps the position the first layer gave it,
    which keeps a directory ahead of what it contains.
    """
    entries: dict[str, tuple[tarfile.TarInfo, bytes | None]] = {}
    for layer in layers:
        with tarfile.open(fileobj=io.BytesIO(layer), mode='r:') as archive:
            for member in archive:
                covered = _whiteout(member.name)
                if covered is not None:
                    _erase(entries, covered)
                    continue
                extracted = archive.extractfile(member) if member.isreg() else None
                entries[member.name] = (member, None if extracted is None else extracted.read())

    sink = io.BytesIO()
    with tarfile.open(fileobj=sink, mode='w', format=tarfile.PAX_FORMAT) as out:
        for member, data in entries.values():
            out.addfile(member, None if data is None else io.BytesIO(data))
    return sink.getvalue()


def _get(image: Image, url: str, *, accept: str, timeout: int, token: str | None) -> tuple[bytes, str | None]:
    """One request, answering an authorization challenge once if it gets one.

    The token is threaded through the pull rather than fetched per request: a
    registry scopes it to the repository, so one covers the manifest and every
    layer under it, and the challenge is answered at most once per pull.
    """
    response = request(url, accept=accept, token=token, timeout=timeout)
    if response.status_code == requests.codes.unauthorized:
        token = _token(image, response)
        response = request(url, accept=accept, token=token, timeout=timeout)
    response.raise_for_status()
    return response.content, token


def _token(image: Image, challenged: requests.Response) -> str:
    """The bearer token a registry's own challenge says to go and get.

    Anonymous by construction: the challenge names a realm and a scope, the
    realm hands out a token for that scope to whoever asks, and this module
    sends no credential on the way. A registry that will not do that for this
    repository fails the pull, which is what a private image should do to a
    provider that was given nothing but a public reference.
    """
    challenge = challenged.headers.get('WWW-Authenticate', '')
    fields = {match['key']: match['value'] for match in _CHALLENGE.finditer(challenge)}
    realm = fields.pop('realm', None)
    if realm is None:
        raise UnsupportedImage(str(image), f'answered {challenged.status_code} with no bearer realm to ask')
    response = request(realm, accept=TOKEN_ACCEPT, token=None, timeout=MANIFEST_TIMEOUT, params=fields)
    response.raise_for_status()
    granted: Mapping[str, Any] = response.json()
    # `token` is the registry API's spelling and `access_token` OAuth2's; a
    # registry may answer with either and several answer with both.
    issued = granted.get('token') or granted.get('access_token')
    if not issued:
        raise UnsupportedImage(str(image), 'was granted no token by its own authorization realm')
    return str(issued)


def _verify(what: str, expected: str, data: bytes) -> None:
    """Refuse content that is not what the digest naming it says it is."""
    actual = f'sha256:{hashlib.sha256(data).hexdigest()}'
    if actual != expected:
        raise DigestMismatch(what, expected, actual)


def _whiteout(name: str) -> str | None:
    """What this entry deletes, if it is a whiteout rather than a file.

    A marker `.wh.x` beside `x` removes `x`; the opaque marker removes
    everything the directory holding it accumulated. Both answer with a path
    prefix, since removing a directory removes what is under it.
    """
    directory, _, base = name.rpartition('/')
    if base == OPAQUE_WHITEOUT:
        return directory
    if base.startswith(WHITEOUT_PREFIX):
        covered = base[len(WHITEOUT_PREFIX) :]
        return f'{directory}/{covered}' if directory else covered
    return None


def _erase(entries: dict[str, tuple[tarfile.TarInfo, bytes | None]], covered: str) -> None:
    """Drop a path and everything beneath it.

    An empty `covered` is an opaque marker at the archive's root, which empties
    the whole tree accumulated so far -- rare, legal, and the reason the prefix
    test is written as it is rather than as a bare `startswith`.
    """
    prefix = f'{covered}/' if covered else ''
    for name in _doomed(entries, covered, prefix):
        del entries[name]


def _doomed(entries: Mapping[str, object], covered: str, prefix: str) -> Iterable[str]:
    return [name for name in entries if name == covered or name.startswith(prefix)]


def _is_registry(candidate: str) -> bool:
    """Whether a reference's first component names a host rather than a path.

    The registry world's own rule: a first component with a dot or a port in it
    is a host, and `localhost` is one by exception. Everything else is a
    namespace under a default registry, and a default is exactly what this
    module refuses to have.
    """
    return candidate == 'localhost' or '.' in candidate or ':' in candidate
