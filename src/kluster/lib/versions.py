"""Version pins: one configuration namespace, the kind in the key.

Everything this repository pins is the same kind of fact — a build somebody
else produced, selected by version — so the Talos release, the Helm charts, the
container images and the gateway's container root filesystems share one
`versions:` namespace and differ by a prefix on the key (rfc-002 §11.1):

    versions:talos: v1.13.9
    versions:chart-cert-manager: https://charts.jetstack.io:v1.19.1
    versions:image-adguard: docker.io/adguard/adguardhome:v0.107.68
    versions:rootfs-gateway-caddy: rootfs-1:e154a141364c60cc…

The prefix is what lets one renovate manager per kind match its own entries and
nothing else. The keys live in the project-level `config:` block of
`Pulumi.yaml` rather than in a stack's file, so five stack programs read one
copy and a stack overrides a pin only when it deliberately runs a different
version from the rest; `pulumi config set` cannot write there, which is what a
renovate-maintained pin wants anyway.

Each accessor returns a parsed value rather than the raw string, and each
refuses a missing or malformed pin by naming the key.
"""

from __future__ import annotations

import re
from typing import NamedTuple, final

import pulumi

#: The one namespace every pin lives in.
NAMESPACE = 'versions'

#: The whole key of the one pin there is exactly one of.
TALOS = 'talos'

#: A hex-encoded SHA-256 digest, which is what a root filesystem is pinned by.
_DIGEST = re.compile(r'[0-9a-f]{64}')

#: The namespace, read once: a `Config` holds a name and reads the runtime at
#: every call, so one is all the accessors below need between them.
_CONFIG = pulumi.Config(NAMESPACE)


class ChartVersion(NamedTuple):
    """A Helm chart: the repository serving it, and the version to install."""

    repo: str
    version: str


class RootfsPin(NamedTuple):
    """A container root filesystem: the release that published it, and its digest.

    The digest is the pin and the release is only where the bytes were found;
    the URL the two produce is a rule in `conventions`, so a change of
    publisher is an edit to that rule rather than to four configured URLs.
    """

    release: str
    sha256: str


class _Kind:
    """One kind of pin, read as `versions:<kind>-<name>`."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    def raw(self, name: str) -> str:
        """The pin as configured, or a `KeyError` naming the key that is absent."""
        key = f'{self._kind}-{name}'
        try:
            return _CONFIG.require(key)
        except pulumi.ConfigMissingError as error:
            raise KeyError(f'nothing pins {name}: set {NAMESPACE}:{key} in Pulumi.yaml') from error

    def malformed(self, name: str, why: str) -> ValueError:
        """A refusal that names the key, so the operator knows which line to fix."""
        return ValueError(f'{NAMESPACE}:{self._kind}-{name} {why}')


@final
class ImageVersions(_Kind):
    """Container images, pinned as a full reference: registry, repository, tag."""

    def __init__(self) -> None:
        super().__init__('image')

    def __getitem__(self, name: str) -> str:
        return self.raw(name)


@final
class ChartVersions(_Kind):
    """Helm charts, pinned as `<repository>:<version>`."""

    def __init__(self) -> None:
        super().__init__('chart')

    def __getitem__(self, name: str) -> ChartVersion:
        repo, separator, version = self.raw(name).rpartition(':')
        if not separator or not repo or not version:
            raise self.malformed(name, 'is not a `<repository>:<version>` pin')
        return ChartVersion(repo, version)


@final
class RootfsVersions(_Kind):
    """Container root filesystems, pinned as `<release>:<sha256>`.

    A scalar rather than the structure this used to be: a pin is a scalar
    everywhere else in the repository, and what a release calls its assets is a
    convention rather than a URL beside every digest (rfc-002 §11.1). The
    digest's shape is checked here, at the boundary, so a truncated paste is a
    configuration error naming its key instead of a push that reaches the
    device and fails there.
    """

    def __init__(self) -> None:
        super().__init__('rootfs')

    def __getitem__(self, name: str) -> RootfsPin:
        release, separator, digest = self.raw(name).rpartition(':')
        if not separator or not release:
            raise self.malformed(name, 'is not a `<release>:<sha256>` pin')
        if not _DIGEST.fullmatch(digest):
            raise self.malformed(name, 'does not end in a hex sha256 digest')
        return RootfsPin(release, digest)


@final
class Versions:
    """Every pin the repository holds, by kind."""

    chart = ChartVersions()
    image = ImageVersions()
    rootfs = RootfsVersions()

    @property
    def talos(self) -> str:
        """The Talos release the whole fleet runs.

        One pin and not one per node: a cluster's machine configurations, its
        installer image and its worker's disk image are one version by
        construction, and a second key would be a second place for it to be
        wrong. It has no `<name>` because there is one of it, which is also why
        it is the one kind that is a whole key rather than a prefix.
        """
        try:
            return _CONFIG.require(TALOS)
        except pulumi.ConfigMissingError as error:
            raise KeyError(f'nothing pins the Talos release: set {NAMESPACE}:{TALOS} in Pulumi.yaml') from error


versions = Versions()
