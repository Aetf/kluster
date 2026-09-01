"""Version pins: one configuration namespace, the kind in the key.

Everything this repository pins is the same kind of fact — a build somebody
else produced, selected by version — so the Talos release, the Helm charts and
the container images share one `versions:` namespace and differ by a prefix on
the key (rfc-002 §11.1):

    versions:talos: v1.13.9
    versions:chart-cert-manager: https://charts.jetstack.io:v1.19.1
    versions:image-gateway-caddy: ghcr.io/aetf/homelab-containers/caddy:3@sha256:8258d234…

The gateway's container root filesystems are in that third kind rather than a
kind of their own: they are published as registry images, so an image reference
is what pins them and there is nothing left that made them special (rfc-002
§11.1).

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

#: A registry digest, in the one form a reference carries it: algorithm-qualified
#: and lower case, because that is what a registry serves and what a comparison
#: against a device's marker is made byte for byte against.
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def is_digest(value: str) -> bool:
    """Whether `value` is a registry digest, in the one spelling a registry uses.

    Read here and by the provider that pulls by one
    (`providers.device_files.provider`), so that the shape a pin is accepted in
    and the shape a pull is performed by cannot drift apart. The provider's
    `check` is the other boundary: it holds a digest to this same spelling and a
    repository to one naming its registry host, because the device resolves the
    reference itself and compares the marker beside its tree byte for byte.
    """
    return _DIGEST.fullmatch(value) is not None


#: The namespace, read once: a `Config` holds a name and reads the runtime at
#: every call, so one is all the accessors below need between them.
_CONFIG = pulumi.Config(NAMESPACE)


class ChartVersion(NamedTuple):
    """A Helm chart: the repository serving it, and the version to install."""

    repo: str
    version: str


class ImagePin(NamedTuple):
    """A container image, pinned as the whole reference it is pulled by.

    The digest is the identity and the repository and tag are where those bytes
    were found, but the pin carries all three, because a reference is what an
    image *is* — it is the form renovate maintains natively, the form a reader
    recognizes, and the form a third-party image can be pinned in at all. An
    estate that also decides where its own builds are published expresses that
    as a check against the pin rather than as a value the pin has to omit.
    """

    repository: str
    tag: str
    digest: str

    def __str__(self) -> str:
        return f'{self.repository}:{self.tag}@{self.digest}'


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
    """Container images, pinned as `<repository>:<tag>@sha256:<digest>`.

    The whole reference, because that is what an image is named by everywhere
    else and what one renovate data source maintains end to end: it bumps the
    tag and the digest together and reads the repository out of the same line.
    A tag alone would be a moving pin, a digest alone a pin nobody can read,
    and a reference without its repository would be a kind that only an image
    this estate publishes could belong to.

    Where a build *should* come from is a separate question with a separate
    answer: a caller that has an opinion — the gateway does, since two of its
    services must run one build — checks this pin against it and refuses a
    mismatch by name. That keeps a change of publisher a reviewed edit to a
    rule and a pin together, without making the pin unable to say where it
    points.

    The shape is checked here, at the boundary, so a truncated paste is a
    configuration error naming its key instead of a pull that reaches a
    registry and is refused there.
    """

    def __init__(self) -> None:
        super().__init__('image')

    def __getitem__(self, name: str) -> ImagePin:
        reference, separator, digest = self.raw(name).partition('@')
        if not separator:
            raise self.malformed(name, 'is not a `<repository>:<tag>@sha256:<digest>` reference')
        if not is_digest(digest):
            raise self.malformed(name, 'does not end in a lower-case `sha256:` digest')
        # The last colon, so a registry named with a port keeps it: the tag is
        # the part after it, and a tag never contains a slash.
        repository, colon, tag = reference.rpartition(':')
        if not colon or not repository or not tag or '/' in tag:
            raise self.malformed(name, 'names no `<repository>:<tag>` before its digest')
        return ImagePin(repository, tag, digest)


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
class Versions:
    """Every pin the repository holds, by kind."""

    chart = ChartVersions()
    image = ImageVersions()

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
