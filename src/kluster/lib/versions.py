"""Version pins: one configuration namespace, the kind in the key.

Everything this repository pins is the same kind of fact — a build somebody
else produced, selected by version — so the Talos release, the Helm charts and
the container images share one `versions:` namespace and differ by a prefix on
the key (rfc-002 §11.1):

    versions:talos: v1.13.9
    versions:chart-cert-manager: https://charts.jetstack.io:v1.19.1
    versions:image-gateway-caddy: 3@sha256:8258d234b66696ef…

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

#: The namespace, read once: a `Config` holds a name and reads the runtime at
#: every call, so one is all the accessors below need between them.
_CONFIG = pulumi.Config(NAMESPACE)


class ChartVersion(NamedTuple):
    """A Helm chart: the repository serving it, and the version to install."""

    repo: str
    version: str


class ImagePin(NamedTuple):
    """A container image: the tag it was published under, and its manifest digest.

    The digest is the pin and the tag is only where those bytes were found;
    which repository serves them is a rule in `conventions`, so a change of
    publisher is an edit to that rule rather than to as many configured
    references as there are pins.
    """

    tag: str
    digest: str


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
    """Container images, pinned as `<tag>@sha256:<digest>`.

    Both halves, because a digest is the only identity renovate maintains end
    to end while a tag is what a human reads and what a data source bumps; one
    data source moves the pair together (rfc-002 §11.1). A tag alone would be a
    moving pin, and a digest alone would be a pin nobody can read.

    What is *not* here is the repository. Which registry publishes a build is a
    decision of the estate rather than a value repeated in every pin that names
    the same publisher, so it is a rule in `conventions` applied to this pin —
    the same ruling that kept the release URL out of configuration before these
    became registry images.

    The digest's shape is checked here, at the boundary, so a truncated paste
    is a configuration error naming its key instead of a pull that reaches a
    registry and is refused there.
    """

    def __init__(self) -> None:
        super().__init__('image')

    def __getitem__(self, name: str) -> ImagePin:
        tag, separator, digest = self.raw(name).partition('@')
        if not separator or not tag:
            raise self.malformed(name, 'is not a `<tag>@sha256:<digest>` pin')
        if not _DIGEST.fullmatch(digest):
            raise self.malformed(name, 'does not end in a lower-case `sha256:` digest')
        return ImagePin(tag, digest)


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
