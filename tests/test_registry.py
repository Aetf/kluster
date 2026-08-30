"""Pulling a root filesystem out of a registry, against a registry that is not there.

What is doubled is the wire and nothing above it: the token dance, the digest
arithmetic, the media-type handling and the flattening are the shipped code.
The double is a real `requests.Response` built the way an adapter builds one,
so the module meets the same object it meets in production -- a stub with three
attributes would not exercise `raise_for_status` or the header casing a
challenge arrives in. No test opens a socket.

The images here are two- and three-layer toys rather than fixtures of a real
one, because what each test is about is a property of the layering: which layer
wins, what a whiteout removes, which bytes are checked against what. The
property holds at three layers or it does not hold.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, final

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPResponse

from kluster.providers.device_files import registry

REPOSITORY = 'registry.invalid/estate/adguard'
REALM = 'https://registry.invalid/token'
GRANTED = 'a-token-the-realm-handed-out'

GZIP_LAYER = 'application/vnd.oci.image.layer.v1.tar+gzip'
PLAIN_LAYER = 'application/vnd.oci.image.layer.v1.tar'
IMAGE_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
IMAGE_INDEX = 'application/vnd.oci.image.index.v1+json'


@final
@dataclass(frozen=True)
class Entry:
    """One file a layer carries, as little of it as a test needs to name."""

    name: str
    content: bytes = b''
    mode: int = 0o644
    linkname: str | None = None
    pax: Mapping[str, str] | None = None


def layer(*entries: Entry, compressed: bool = True) -> bytes:
    """One layer, as a registry stores it: a tar, gzipped unless asked otherwise."""
    sink = io.BytesIO()
    with tarfile.open(fileobj=sink, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry.name)
            info.mode = entry.mode
            if entry.linkname is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = entry.linkname
            else:
                info.size = len(entry.content)
            if entry.pax is not None:
                info.pax_headers = dict(entry.pax)
            archive.addfile(info, None if entry.linkname is not None else io.BytesIO(entry.content))
    raw = sink.getvalue()
    return gzip.compress(raw) if compressed else raw


def digest_of(data: bytes) -> str:
    return f'sha256:{hashlib.sha256(data).hexdigest()}'


def unpack(archive: bytes) -> dict[str, tarfile.TarInfo]:
    """The flattened archive as a reader of it sees: every entry, by name."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
        return {member.name: member for member in tar}


def contents(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
        return {
            member.name: extracted.read()
            for member in tar
            if member.isreg() and (extracted := tar.extractfile(member)) is not None
        }


@final
@dataclass
class Registry:
    """A registry that challenges once, then serves what it was given.

    `served` is keyed by digest, which is how both endpoints this module uses
    address content, and `asked` records every request in order -- the whole
    point of several tests below is *what was not fetched*.
    """

    served: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    presented: list[str | None] = field(default_factory=list)
    challenge: str | None = f'Bearer realm="{REALM}",service="registry.invalid",scope="repository:estate/adguard:pull"'
    token: str = GRANTED

    def store(self, media_type: str, data: bytes) -> str:
        """Put content in, and answer with the digest that now names it."""
        digest = digest_of(data)
        self.served[digest] = (media_type, data)
        return digest

    def manifest(self, layers: Sequence[str], *, media_type: str = IMAGE_MANIFEST, config: bytes = b'{}') -> str:
        """Store a manifest over stored layers, and answer with its digest."""
        document = {
            'schemaVersion': 2,
            'mediaType': media_type,
            'config': {
                'mediaType': 'application/vnd.oci.image.config.v1+json',
                'digest': self.store('application/vnd.oci.image.config.v1+json', config),
                'size': len(config),
            },
            'layers': [
                {'mediaType': self.served[digest][0], 'digest': digest, 'size': len(self.served[digest][1])}
                for digest in layers
            ],
        }
        return self.store(media_type, json.dumps(document).encode())

    def request(
        self,
        url: str,
        *,
        accept: str,
        token: str | None,
        timeout: int,
        params: Mapping[str, str] | None = None,
    ) -> requests.Response:
        _ = accept, timeout
        self.asked.append(url)
        self.presented.append(token)
        if url == REALM:
            return self._answer(200, json.dumps({'token': self.token}).encode(), 'application/json')
        if self.challenge is not None and token != self.token:
            return self._answer(401, b'{"errors":[]}', 'application/json', {'WWW-Authenticate': self.challenge})
        _ = params
        digest = url.rsplit('/', 1)[-1]
        if digest not in self.served:
            return self._answer(404, b'{"errors":[]}', 'application/json')
        media_type, data = self.served[digest]
        return self._answer(200, data, media_type)

    def _answer(
        self,
        status: int,
        body: bytes,
        media_type: str,
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        raw = HTTPResponse(
            body=io.BytesIO(body),
            status=status,
            headers={'Content-Type': media_type, **(headers or {})},
            preload_content=False,
        )
        return HTTPAdapter().build_response(requests.Request('GET', 'https://registry.invalid/').prepare(), raw)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """The registry the module reaches instead of a real one."""
    fake = Registry()
    monkeypatch.setattr(registry, 'request', fake.request)
    return fake


def image(digest: str, repository: str = REPOSITORY) -> registry.Image:
    return registry.Image(repository=repository, digest=digest)


# ---------------------------------------------------------------------------
# What the pin verifies
# ---------------------------------------------------------------------------


def test_a_manifest_the_registry_serves_under_the_wrong_digest_is_refused(served: Registry) -> None:
    """The first link of the chain, and the one the whole pin rests on.

    A registry answering a digest with content that does not hash to it is
    either broken or hostile, and either way nothing below it may be parsed --
    the layer digests a pull would trust next come out of these very bytes.
    """
    manifest = served.manifest([served.store(GZIP_LAYER, layer(Entry('etc/hosts')))])
    served.served[manifest] = (IMAGE_MANIFEST, b'{"schemaVersion":2,"layers":[]}')

    with pytest.raises(registry.DigestMismatch) as raised:
        _ = registry.rootfs(image(manifest))

    assert raised.value.expected == manifest


def test_a_layer_the_registry_substitutes_is_refused(served: Registry) -> None:
    """The second link: the manifest is trusted, so what it says a layer is, is the pin."""
    blob = served.store(GZIP_LAYER, layer(Entry('etc/hosts', b'the reviewed one')))
    manifest = served.manifest([blob])
    served.served[blob] = (GZIP_LAYER, layer(Entry('etc/hosts', b'something else entirely')))

    with pytest.raises(registry.DigestMismatch) as raised:
        _ = registry.rootfs(image(manifest))

    assert raised.value.expected == blob


def test_nothing_beyond_the_manifest_and_its_layers_is_fetched(served: Registry) -> None:
    """A root filesystem is the layers; the image configuration is not read.

    Asserted because it is the difference between a pull whose every byte the
    pin covers and one that also runs on content nobody checked.
    """
    config = b'{"config":{"Entrypoint":["/sbin/init"]}}'
    blob = served.store(GZIP_LAYER, layer(Entry('etc/hosts')))
    manifest = served.manifest([blob], config=config)

    _ = registry.rootfs(image(manifest))

    # A set, because the manifest is asked for twice: once to provoke the
    # challenge and once with what the realm granted.
    fetched = {url.rsplit('/', 1)[-1] for url in served.asked if url != REALM}
    assert fetched == {manifest, blob}
    assert digest_of(config) not in fetched


# ---------------------------------------------------------------------------
# Anonymous authorization
# ---------------------------------------------------------------------------


def test_the_challenge_is_answered_once_and_the_token_covers_every_layer(served: Registry) -> None:
    """A registry scopes a token to the repository, so one pull needs one.

    The first request is unauthorized on purpose -- the module carries no
    credential and learns the realm from the challenge itself -- and everything
    after it presents what the realm granted.
    """
    blobs = [served.store(GZIP_LAYER, layer(Entry(f'etc/{name}'))) for name in ('hosts', 'resolv.conf')]
    manifest = served.manifest(blobs)

    _ = registry.rootfs(image(manifest))

    assert served.asked.count(REALM) == 1
    assert served.presented[0] is None, 'the challenge has to be provoked before there is a realm to ask'
    assert served.presented[2:] == [GRANTED] * (len(served.presented) - 2)


def test_a_registry_that_needs_no_token_is_never_sent_to_a_realm(served: Registry) -> None:
    served.challenge = None
    manifest = served.manifest([served.store(GZIP_LAYER, layer(Entry('etc/hosts')))])

    _ = registry.rootfs(image(manifest))

    assert REALM not in served.asked


def test_a_challenge_naming_no_realm_is_refused_by_reference(served: Registry) -> None:
    """There is nowhere to go and no credential to fall back on, which is the design."""
    served.challenge = 'Basic realm-less nonsense'
    manifest = served.manifest([served.store(GZIP_LAYER, layer(Entry('etc/hosts')))])

    with pytest.raises(registry.UnsupportedImage, match=REPOSITORY):
        _ = registry.rootfs(image(manifest))


# ---------------------------------------------------------------------------
# What this module refuses to guess about
# ---------------------------------------------------------------------------


def test_a_multi_platform_index_is_refused_rather_than_chosen_from(served: Registry) -> None:
    """One device, one architecture: the pin should be naming the image directly.

    Choosing a platform here would make the pin mean whatever this module's
    idea of the device's architecture is, which is a fact it does not have.
    """
    manifest = served.manifest([served.store(GZIP_LAYER, layer(Entry('etc/hosts')))], media_type=IMAGE_INDEX)

    with pytest.raises(registry.UnsupportedImage, match='not a single image'):
        _ = registry.rootfs(image(manifest))


def test_a_layer_packed_in_a_way_this_cannot_read_is_refused(served: Registry) -> None:
    """Guessing at a compression that is wrong produces a tree rather than an error."""
    manifest = served.manifest([served.store('application/vnd.oci.image.layer.v1.tar+brotli', b'whatever')])

    with pytest.raises(registry.UnsupportedImage, match='brotli'):
        _ = registry.rootfs(image(manifest))


def test_an_uncompressed_layer_is_read_as_it_is(served: Registry) -> None:
    """The media type is what says so, rather than the bytes being sniffed."""
    manifest = served.manifest([served.store(PLAIN_LAYER, layer(Entry('etc/hosts', b'plain'), compressed=False))])

    assert contents(registry.rootfs(image(manifest))) == {'etc/hosts': b'plain'}


@pytest.mark.parametrize(
    ('repository', 'digest'),
    [
        ('estate/adguard', f'sha256:{"a" * 64}'),
        ('adguard', f'sha256:{"a" * 64}'),
        (REPOSITORY, 'a' * 64),
        (REPOSITORY, f'sha256:{"a" * 8}'),
        (REPOSITORY, f'sha256:{"A" * 64}'),
    ],
    ids=['no registry host', 'bare name', 'unqualified digest', 'truncated', 'upper case'],
)
def test_a_reference_that_is_not_a_whole_pin_is_refused_at_construction(repository: str, digest: str) -> None:
    """Before any request: an image fetched from a default nobody named, or by a
    digest that cannot be compared byte for byte against a device's marker, is
    the failure the pin exists to prevent."""
    with pytest.raises(ValueError, match='registry host|digest'):
        _ = registry.Image(repository=repository, digest=digest)


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def test_a_later_layer_replaces_the_same_path_in_an_earlier_one(served: Registry) -> None:
    manifest = served.manifest(
        [
            served.store(GZIP_LAYER, layer(Entry('etc/hosts', b'from the base'), Entry('etc/motd', b'kept'))),
            served.store(GZIP_LAYER, layer(Entry('etc/hosts', b'from the build'))),
        ]
    )

    assert contents(registry.rootfs(image(manifest))) == {
        'etc/hosts': b'from the build',
        'etc/motd': b'kept',
    }


def test_a_whiteout_removes_what_it_covers_and_does_not_reach_the_device(served: Registry) -> None:
    """`tar` has no notion of a deletion, so a marker that travelled would land
    inside the container as a file the image meant to have removed."""
    manifest = served.manifest(
        [
            served.store(GZIP_LAYER, layer(Entry('etc/hosts', b'base'), Entry('etc/motd', b'kept'))),
            served.store(GZIP_LAYER, layer(Entry('etc/.wh.hosts'))),
        ]
    )

    assert contents(registry.rootfs(image(manifest))) == {'etc/motd': b'kept'}


def test_a_whiteout_over_a_directory_takes_everything_under_it(served: Registry) -> None:
    manifest = served.manifest(
        [
            served.store(GZIP_LAYER, layer(Entry('opt/old/bin/tool', b'gone'), Entry('opt/keep', b'kept'))),
            served.store(GZIP_LAYER, layer(Entry('opt/.wh.old'))),
        ]
    )

    assert set(unpack(registry.rootfs(image(manifest)))) == {'opt/keep'}


def test_an_opaque_marker_empties_the_directory_its_layer_found(served: Registry) -> None:
    """The other whiteout: not "this path is gone" but "everything here is"."""
    manifest = served.manifest(
        [
            served.store(GZIP_LAYER, layer(Entry('var/cache/a', b'gone'), Entry('var/cache/b', b'gone'))),
            served.store(
                GZIP_LAYER, layer(Entry(f'var/cache/{registry.OPAQUE_WHITEOUT}'), Entry('var/cache/c', b'new'))
            ),
        ]
    )

    assert contents(registry.rootfs(image(manifest))) == {'var/cache/c': b'new'}


def test_a_path_a_whiteout_removed_comes_back_if_a_later_layer_writes_it(served: Registry) -> None:
    """Order is the whole semantics: a removal is an event, not a permanent verdict."""
    manifest = served.manifest(
        [
            served.store(GZIP_LAYER, layer(Entry('etc/hosts', b'first'))),
            served.store(GZIP_LAYER, layer(Entry('etc/.wh.hosts'))),
            served.store(GZIP_LAYER, layer(Entry('etc/hosts', b'third'))),
        ]
    )

    assert contents(registry.rootfs(image(manifest))) == {'etc/hosts': b'third'}


def test_an_entry_keeps_its_own_metadata_across_the_flattening(served: Registry) -> None:
    """What the device unpacks has to be the tree the image builder produced.

    The PAX header is the case that would go silently wrong: file capabilities
    travel as one, and a container whose `ping` lost `cap_net_raw` fails in a
    way nothing about this pull would explain.
    """
    capability = {'SCHILY.xattr.security.capability': 'a capability blob'}
    manifest = served.manifest(
        [
            served.store(
                GZIP_LAYER,
                layer(
                    Entry('bin/ping', b'#!/bin/sh\n', mode=0o755, pax=capability),
                    Entry('bin/sh', linkname='busybox'),
                ),
            )
        ]
    )

    flattened = unpack(registry.rootfs(image(manifest)))

    assert flattened['bin/ping'].mode == 0o755
    assert flattened['bin/ping'].pax_headers == capability
    assert flattened['bin/sh'].issym()
    assert flattened['bin/sh'].linkname == 'busybox'


def test_flattening_no_layers_at_all_is_an_empty_archive() -> None:
    """A degenerate manifest is not a crash: it is an image with nothing in it."""
    assert unpack(registry.flatten([])) == {}


def test_an_image_renders_as_the_reference_a_reader_would_type(served: Registry) -> None:
    """Which is what every refusal above is named by."""
    _ = served
    reference = image(f'sha256:{"a" * 64}')

    assert str(reference) == f'{REPOSITORY}@sha256:{"a" * 64}'
    assert reference.host == 'registry.invalid'
    assert reference.path == 'estate/adguard'


def test_a_registry_error_is_raised_rather_than_swallowed(served: Registry) -> None:
    """A missing manifest is not an empty root filesystem."""
    with pytest.raises(requests.HTTPError):
        _ = registry.rootfs(image(f'sha256:{"b" * 64}'))

    assert served.asked, 'the pull was attempted'


def test_the_flattened_archive_is_a_tar_the_device_can_unpack(served: Registry) -> None:
    """The contract with the device: one plain archive, `tar -xf` and nothing more.

    A concatenation of layer archives would also be "a tar" and would stop at
    the first end-of-archive marker, so this asserts one archive holding every
    entry rather than merely that the bytes parse.
    """
    manifest = served.manifest(
        [
            served.store(GZIP_LAYER, layer(Entry('sbin/init', b'#!/bin/sh\n', mode=0o755))),
            served.store(GZIP_LAYER, layer(Entry('etc/caddy/Caddyfile', b'a site block'))),
        ]
    )

    archive = registry.rootfs(image(manifest))

    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
        names = tar.getnames()
    assert names == ['sbin/init', 'etc/caddy/Caddyfile']


def test_the_seam_is_the_whole_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request goes through `request`, the trip to a realm included.

    Asserted because a second, un-seamed call would open a socket in a test run
    that believes it has none -- and would do it only on the authorization
    path, which the passing tests above never reach.
    """
    calls: list[str] = []

    def refuse(url: str, **_: Any) -> requests.Response:
        calls.append(url)
        raise AssertionError(f'unexpected request to {url}')

    monkeypatch.setattr(registry, 'request', refuse)
    monkeypatch.setattr(requests, 'get', refuse)

    with pytest.raises(AssertionError):
        _ = registry.rootfs(image(f'sha256:{"c" * 64}'))

    assert len(calls) == 1
