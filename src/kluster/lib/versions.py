"""Version pins: one configuration namespace, the kind in the key.

Every pin a stack program reads is the same kind of fact — a build somebody
else produced, selected by version — so the Talos release, the Helm charts, the
release manifests and the container images share one `versions:` namespace and
differ by a prefix on the key (docs/framework/pulumi.md §3.2):

    versions:talos: v1.13.9
    versions:image-gateway-caddy: ghcr.io/aetf/homelab-containers/caddy:3@sha256:8258d234…
    versions:chart-cert-manager:
      value:
        repository: oci://quay.io/jetstack/charts
        version: v1.21.1
        digest: sha256:15c0b46d…
        definitions: true
    versions:manifest-gateway-api:
      value:
        repository: kubernetes-sigs/gateway-api
        release: v1.6.1
        asset: experimental-install.yaml
        sha256: d7fa7765…

The gateway's container root filesystems are in the image kind rather than a
kind of their own: they are published as registry images, so an image reference
is what pins them and there is nothing left that made them special.

The prefix is what lets one renovate manager per kind match its own entries and
nothing else. The keys live in the project-level `config:` block of
`Pulumi.yaml` rather than in a stack's file, so five stack programs read one
copy; `pulumi config set` cannot write there, which is what a
renovate-maintained pin wants anyway.

**One parser, two sources.** A stack program reads the block through its
configuration (`ProgramConfig`), and a script reads it out of `Pulumi.yaml`
itself (`ProjectFile`), so a pin a program and a script both read is read the
same way by both and lives in the block whatever reads it. The two sources
hand a value over in different shapes — the engine passes a structured pin to
the program as the JSON text of its `value:`, the file holds the object under
that key — and each source undoes its own shape, so `Versions` parses one.

Each accessor checks the pin's shape and returns it parsed, and each refuses a
missing or malformed pin by naming the key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol, cast, final

import pulumi

#: The one namespace every pin a stack program reads lives in.
NAMESPACE = 'versions'

#: The whole key of the one pin there is exactly one of.
TALOS = 'talos'

#: The key prefix of each kind with a name after it: `versions:<kind>-<name>`.
CHART = 'chart'
IMAGE = 'image'
MANIFEST = 'manifest'

#: A Talos release as upstream tags one: `v<major>.<minor>.<patch>`. That
#: spelling is what the image factory's paths and the image names built from the
#: pin carry, so a bare `1.13.9` or a minor line such as `v1.13` names nothing
#: the factory serves. A pre-release is refused too: the renovate manager for
#: this key reads `v[\d.]+`, so it would track a pre-release as the release it
#: precedes and rewrite the pin into a tag that does not exist.
_TALOS_RELEASE = re.compile(r'v\d+\.\d+\.\d+')

#: A registry digest, in the one form a reference carries it: algorithm-qualified
#: and lower case, because that is what a registry serves and what a comparison
#: against a device's marker is made byte for byte against.
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}')

#: A file's sha256 as a release's checksum file and `sha256sum` print it: bare,
#: lower-case hex, which is the spelling renovate's `github-release-attachments`
#: data source writes back on a bump.
_SHA256 = re.compile(r'[0-9a-f]{64}')

#: A GitHub repository, `<owner>/<name>`.
_GITHUB_REPOSITORY = re.compile(r'[\w.-]+/[\w.-]+')

#: The two schemes a chart repository is reached by. An OCI registry serves a
#: chart by digest, and Helm checks one written into the reference; an HTTP
#: repository serves an archive Helm checks against nothing it can be pinned
#: to.
OCI_SCHEME = 'oci://'
HTTP_SCHEME = 'https://'


def is_digest(value: str) -> bool:
    """Whether `value` is a registry digest, in the one spelling a registry uses.

    Read here, where an image or chart pin is parsed, and by the `check` of the
    provider that pulls by one (`providers.device_files.provider`), which
    imports it rather than spelling its own: the shape a pin is accepted in and
    the shape a pull is performed by are then one function and cannot drift
    apart. That `check` is the other boundary -- it holds a digest to this
    spelling and a repository to one naming its registry host, because the
    device resolves the reference itself and compares the marker beside its
    tree byte for byte.
    """
    return _DIGEST.fullmatch(value) is not None


# --- The two sources --------------------------------------------------------


class PinSource(Protocol):
    """Where the `versions:` block is read from, keyed by the name inside the namespace.

    A plain pin and a structured one are asked for separately because the
    engine hands them to a program differently: a plain value as itself, an
    object as JSON text. `None` is a pin nobody wrote.
    """

    def plain(self, key: str) -> object | None:
        """The value of a pin written as a scalar."""
        ...

    def structured(self, key: str) -> object | None:
        """The value of a pin written as an object under `value:`."""
        ...


@final
class ProgramConfig:
    """The block as a running stack program's configuration hands it over.

    The engine passes a project-level `value:` object to the program as the
    JSON text of that object, which is what the SDK's own
    `Config.get_object` decodes. A value that is not JSON is handed back as
    the string it is, so the kind that wanted an object refuses it by name.
    """

    def __init__(self) -> None:
        # A `Config` holds a name and reads the runtime at every call, so one
        # is all the accessors need between them.
        self._config = pulumi.Config(NAMESPACE)

    def plain(self, key: str) -> object | None:
        return self._config.get(key)

    def structured(self, key: str) -> object | None:
        raw = self._config.get(key)
        if raw is None:
            return None
        try:
            return cast('object', json.loads(raw))
        except json.JSONDecodeError:
            return raw


@final
class ProjectFile:
    """The block as `Pulumi.yaml` holds it: the file's `config:` mapping, loaded.

    A structured value is written under `value:`, the one form Pulumi's project
    schema accepts for an object; written directly under the key, the object is
    refused by the CLI as an invalid type declaration. So an object here is
    accepted only in that form, and refused by name otherwise, before a script
    reads a pin the program could never be handed.
    """

    def __init__(self, config: Mapping[str, object]) -> None:
        self._config = config

    def names(self, kind: str) -> list[str]:
        """The `<name>` of every `versions:<kind>-<name>` key the file writes, in its order."""
        prefix = f'{NAMESPACE}:{kind}-'
        return [key.removeprefix(prefix) for key in self._config if key.startswith(prefix)]

    def plain(self, key: str) -> object | None:
        return self._unwrapped(key)

    def structured(self, key: str) -> object | None:
        return self._unwrapped(key)

    def _unwrapped(self, key: str) -> object | None:
        value = self._config.get(f'{NAMESPACE}:{key}')
        if not isinstance(value, Mapping):
            return value
        if set(cast('Mapping[str, object]', value)) != {'value'}:
            raise ValueError(
                f'{NAMESPACE}:{key} is an object written outside `value:`, which Pulumi refuses as an invalid '
                'type declaration; write it as `value:` followed by the object'
            )
        return cast('Mapping[str, object]', value)['value']


# --- The kinds ----------------------------------------------------------------


class ImagePin(NamedTuple):
    """A container image, pinned as the whole reference it is pulled by.

    The digest is the identity and the repository and tag are where those bytes
    were found, but the pin carries all three, because a reference is what an
    image *is* — it is the form renovate maintains natively, the form a reader
    recognizes, and the form a third-party image can be pinned in at all. An
    installation that also decides where its own builds are published
    expresses that as a check against the pin rather than as a value the pin
    has to omit.
    """

    repository: str
    tag: str
    digest: str

    def __str__(self) -> str:
        return f'{self.repository}:{self.tag}@{self.digest}'


@dataclass(frozen=True, kw_only=True)
class Floor:
    """The operator version a chart may not fall below, and the document that says so."""

    operator: str
    """The lowest operator version, checked against the `appVersion` the chart
    itself declares: several charts are versioned apart from what they ship
    (`cloudnative-pg` 0.29.0 ships CNPG 1.30.0)."""

    document: str
    """The section that states the floor, and why."""


@dataclass(frozen=True, kw_only=True)
class ChartPin:
    """A Helm chart: where it is served, the version to install, and what `update_crds` renders of it."""

    name: str
    """The chart's name in its repository, which is the `<name>` of its key."""

    repository: str
    """An `oci://` registry path or an `https://` chart repository."""

    version: str

    digest: str | None
    """The OCI manifest's digest, which Helm checks the pulled chart against;
    `None` exactly when the repository is an HTTP one, which offers nothing to
    check."""

    definitions: bool
    """Whether `update_crds` renders the chart for CustomResourceDefinitions."""

    render_values: Mapping[str, str] = field(default_factory=dict[str, str])
    """`--set` values that make the chart render its definitions. They are what
    `update_crds` renders with, and nothing the stack installs with."""

    floor: Floor | None = None

    @property
    def oci(self) -> bool:
        return self.repository.startswith(OCI_SCHEME)

    @property
    def reference(self) -> str:
        """The chart as Helm locates it: the full digest-pinned reference, or the name within the repository."""
        return f'{self.repository}/{self.name}@{self.digest}' if self.oci else self.name


@dataclass(frozen=True, kw_only=True)
class ManifestPin:
    """A YAML bundle published as a GitHub release asset, with the asset's sha256."""

    name: str
    repository: str
    """The GitHub repository, `<owner>/<name>`."""

    release: str
    """The release tag."""

    asset: str
    sha256: str

    @property
    def key(self) -> str:
        return f'{NAMESPACE}:{MANIFEST}-{self.name}'

    @property
    def url(self) -> str:
        return f'https://github.com/{self.repository}/releases/download/{self.release}/{self.asset}'


class _Fields:
    """One structured pin's fields, each read once and refused by key and field name."""

    def __init__(self, key: str, value: object) -> None:
        self.key = key
        if not isinstance(value, Mapping):
            raise ValueError(f'{key} is not an object written under `value:`')
        self._value = cast('Mapping[str, object]', value)
        self._read: set[str] = set()

    def malformed(self, why: str) -> ValueError:
        return ValueError(f'{self.key} {why}')

    def text(self, name: str, *, required: bool = True) -> str | None:
        self._read.add(name)
        value = self._value.get(name)
        if value is None and not required:
            return None
        # A YAML scalar such as `1.20` arrives as a number, so a version that
        # was not quoted is refused rather than compared as `1.2`.
        if not isinstance(value, str) or not value:
            raise self.malformed(f'has no `{name}` string')
        return value

    def flag(self, name: str) -> bool:
        self._read.add(name)
        value = self._value.get(name)
        if not isinstance(value, bool):
            raise self.malformed(f'has no `{name}` boolean')
        return value

    def mapping(self, name: str) -> Mapping[str, object] | None:
        self._read.add(name)
        value = self._value.get(name)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise self.malformed(f'`{name}` is not an object')
        return cast('Mapping[str, object]', value)

    def done(self) -> None:
        """Refuse a field nothing read, which is a misspelling or a field this kind has not got."""
        unknown = sorted(set(self._value) - self._read)
        if unknown:
            raise self.malformed(f'carries fields this kind has not got: {", ".join(unknown)}')


class _Kind:
    """One kind of pin, read as `versions:<kind>-<name>`."""

    def __init__(self, source: PinSource, kind: str) -> None:
        self._source = source
        self._kind = kind

    def key(self, name: str) -> str:
        return f'{NAMESPACE}:{self._kind}-{name}'

    def _missing(self, name: str) -> KeyError:
        return KeyError(f'nothing pins {name}: set {self.key(name)} in Pulumi.yaml')

    def malformed(self, name: str, why: str) -> ValueError:
        """A refusal that names the key, so the operator knows which line to fix."""
        return ValueError(f'{self.key(name)} {why}')

    def plain(self, name: str) -> str:
        """The pin as configured, or a `KeyError` naming the key that is absent."""
        value = self._source.plain(f'{self._kind}-{name}')
        if value is None:
            raise self._missing(name)
        if not isinstance(value, str):
            raise self.malformed(name, 'is not a string')
        return value

    def structured(self, name: str) -> _Fields:
        value = self._source.structured(f'{self._kind}-{name}')
        if value is None:
            raise self._missing(name)
        return _Fields(self.key(name), value)


@final
class ImageVersions(_Kind):
    """Container images, pinned as `<repository>:<tag>@sha256:<digest>`.

    The whole reference, because that is what an image is named by everywhere
    else and what one renovate data source maintains end to end: it bumps the
    tag and the digest together and reads the repository out of the same line.
    A tag alone would be a moving pin, a digest alone a pin nobody can read,
    and a reference without its repository would be a kind that only an image
    this installation publishes could belong to.

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

    def __init__(self, source: PinSource) -> None:
        super().__init__(source, IMAGE)

    def __getitem__(self, name: str) -> ImagePin:
        reference, separator, digest = self.plain(name).partition('@')
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
    """Helm charts, each an object under `value:`.

    The repository decides what the pin can hold. An `oci://` registry serves
    the chart by digest, and Helm refuses a pulled chart whose digest is not
    the one written into its reference, so an OCI pin carries the digest and
    is refused without it. An `https://` repository serves an archive Helm
    checks against nothing but an optional provenance signature, so there is
    no digest to write and a pin naming one is refused as a mistake.
    """

    def __init__(self, source: PinSource) -> None:
        super().__init__(source, CHART)

    def __getitem__(self, name: str) -> ChartPin:
        fields = self.structured(name)
        repository = cast('str', fields.text('repository'))
        version = cast('str', fields.text('version'))
        digest = fields.text('digest', required=False)
        definitions = fields.flag('definitions')
        render_values = fields.mapping('render-values') or {}
        floor = fields.mapping('floor')
        fields.done()

        if repository.startswith(OCI_SCHEME):
            if digest is None or not is_digest(digest):
                raise fields.malformed('is served from an OCI registry and carries no lower-case `sha256:` digest')
        elif repository.startswith(HTTP_SCHEME):
            if digest is not None:
                raise fields.malformed('is served from an HTTP repository, which offers no digest to check')
        else:
            raise fields.malformed(f'names a repository that is neither `{OCI_SCHEME}` nor `{HTTP_SCHEME}`')

        if render_values and not definitions:
            raise fields.malformed('carries `render-values` for definitions it does not render')
        if not all(isinstance(value, str) for value in render_values.values()):
            raise fields.malformed('carries a `render-values` value that is not a string')

        return ChartPin(
            name=name,
            repository=repository,
            version=version,
            digest=digest,
            definitions=definitions,
            render_values=cast('Mapping[str, str]', render_values),
            floor=self._floor(fields, floor),
        )

    @staticmethod
    def _floor(fields: _Fields, floor: Mapping[str, object] | None) -> Floor | None:
        if floor is None:
            return None
        inner = _Fields(f'{fields.key} floor', floor)
        parsed = Floor(operator=cast('str', inner.text('operator')), document=cast('str', inner.text('document')))
        inner.done()
        return parsed


@final
class ManifestVersions(_Kind):
    """Release manifests, each an object under `value:`: the repository, release, asset and its sha256."""

    def __init__(self, source: PinSource) -> None:
        super().__init__(source, MANIFEST)

    def __getitem__(self, name: str) -> ManifestPin:
        fields = self.structured(name)
        pin = ManifestPin(
            name=name,
            repository=cast('str', fields.text('repository')),
            release=cast('str', fields.text('release')),
            asset=cast('str', fields.text('asset')),
            sha256=cast('str', fields.text('sha256')),
        )
        fields.done()
        if _GITHUB_REPOSITORY.fullmatch(pin.repository) is None:
            raise fields.malformed('names no `<owner>/<name>` GitHub repository')
        if _SHA256.fullmatch(pin.sha256) is None:
            raise fields.malformed('carries no lower-case hexadecimal sha256')
        return pin


@final
class Versions:
    """Every pin in the `versions:` block, by kind, read from one source."""

    def __init__(self, source: PinSource) -> None:
        self._source = source
        self.chart = ChartVersions(source)
        self.image = ImageVersions(source)
        self.manifest = ManifestVersions(source)

    @property
    def talos(self) -> str:
        """The Talos release the whole fleet runs.

        One pin and not one per node: a cluster's machine configurations, its
        installer image and its worker's disk image are one version by
        construction, and a second key would be a second place for it to be
        wrong. It has no `<name>` because there is one of it, which is also why
        it is the one kind that is a whole key rather than a prefix.

        The value is the release tag itself, since that is what every reader
        passes on, and it is checked to be one before any of them does.
        """
        release = self._source.plain(TALOS)
        if release is None:
            raise KeyError(f'nothing pins the Talos release: set {NAMESPACE}:{TALOS} in Pulumi.yaml')
        if not isinstance(release, str) or _TALOS_RELEASE.fullmatch(release) is None:
            raise ValueError(f'{NAMESPACE}:{TALOS} is not a Talos release tag (`v<major>.<minor>.<patch>`)')
        return release


#: The pins a running stack program reads.
versions = Versions(ProgramConfig())
