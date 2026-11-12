"""Version pins: one namespace, the kind in the key, a parsed value out.

What is under test is the boundary (docs/framework/pulumi.md §3.2). Every pin
is a string or an object an operator or a renovate branch edited, and the
parser is the one place that turns it into something typed and refuses a
missing or malformed one by naming the key rather than failing further in. It
reads two sources -- a program's configuration and the `Pulumi.yaml` a script
opens -- and they have to agree.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pulumi
import pytest
from renovate_text import as_python_spells_it, as_renovate_spells_it, listed, package_rules, scalar

from kluster.lib.versions import (
    CHART,
    IMAGE,
    MANIFEST,
    ChartPin,
    Floor,
    ImagePin,
    ManifestPin,
    ProgramConfig,
    ProjectFile,
    Versions,
    versions,
)
from kluster.scripts.update_crds import sources

ROOT = Path(__file__).parent.parent

DIGEST = f'sha256:{"a" * 64}'
REPOSITORY = 'ghcr.io/aetf/homelab-containers/caddy'

OCI_CHART = {
    'repository': 'oci://registry.example.invalid/charts',
    'version': 'v1.21.1',
    'digest': DIGEST,
    'definitions': True,
    'render-values': {'crds.enabled': 'true'},
}
HTTP_CHART = {
    'repository': 'https://charts.example.invalid/',
    'version': '0.29.0',
    'definitions': False,
    'floor': {'operator': '1.26', 'document': 'cluster-infra.md §1 item 5'},
}
MANIFEST_PIN = {
    'repository': 'kubernetes-sigs/gateway-api',
    'release': 'v1.6.1',
    'asset': 'experimental-install.yaml',
    'sha256': 'b' * 64,
}

#: The configuration a program is handed: a plain pin as itself, an object as
#: the JSON text of its `value:`, which is how the engine passes a
#: project-level object to the language host and what `Config.get_object`
#: decodes.
PINS = {
    'versions:talos': 'v1.13.9',
    'versions:chart-cert-manager': json.dumps(OCI_CHART),
    'versions:chart-cloudnative-pg': json.dumps(HTTP_CHART),
    'versions:manifest-gateway-api': json.dumps(MANIFEST_PIN),
    'versions:image-gateway-caddy': f'{REPOSITORY}:3@{DIGEST}',
}


@pytest.fixture(autouse=True)
def pinned() -> Iterator[None]:
    """The pins every case starts from, and whatever was configured before restored once it ends.

    The cases here set program configuration in pytest's main context, which
    pytest-asyncio copies into every async case after them, so a pin a case
    breaks on purpose would otherwise be what those read.
    """
    before = dict(pulumi.runtime.config.CONFIG.get())
    pulumi.runtime.set_all_config(dict(PINS))
    yield
    pulumi.runtime.set_all_config(before)


def project_block() -> dict[str, object]:
    """The `config:` block of the repository's own `Pulumi.yaml`, loaded as `update_crds` loads it."""
    return sources.read_config(ROOT / 'Pulumi.yaml')


def test_every_kind_shares_one_namespace_and_differs_by_key_prefix() -> None:
    """Which is what lets one renovate manager per kind match its own entries.

    Four kinds and one `versions:` namespace, read the same way from any stack
    because the keys are project-level configuration rather than one copy of
    the same value per stack.
    """
    assert versions.talos == 'v1.13.9'
    assert versions.image['gateway-caddy'] == ImagePin(REPOSITORY, '3', DIGEST)
    assert versions.chart['cert-manager'] == ChartPin(
        name='cert-manager',
        repository='oci://registry.example.invalid/charts',
        version='v1.21.1',
        digest=DIGEST,
        definitions=True,
        render_values={'crds.enabled': 'true'},
    )
    assert versions.chart['cloudnative-pg'] == ChartPin(
        name='cloudnative-pg',
        repository='https://charts.example.invalid/',
        version='0.29.0',
        digest=None,
        definitions=False,
        floor=Floor(operator='1.26', document='cluster-infra.md §1 item 5'),
    )
    assert versions.manifest['gateway-api'] == ManifestPin(name='gateway-api', **MANIFEST_PIN)


def test_a_chart_is_located_by_its_digest_where_its_registry_serves_one() -> None:
    """An OCI chart is pulled by its digest-pinned reference; an HTTP chart by its name in the repository."""
    assert versions.chart['cert-manager'].reference == f'oci://registry.example.invalid/charts/cert-manager@{DIGEST}'
    assert versions.chart['cloudnative-pg'].reference == 'cloudnative-pg'


def test_the_parser_reads_the_same_pins_from_a_program_and_from_the_file() -> None:
    """The program and `update_crds` read one block through one parser, and cannot disagree on a pin.

    Every pin in the repository's own `Pulumi.yaml`, read the way a script
    reads it -- the file's `config:` block -- and the way a program is handed
    it: a structured pin's `value:` as JSON text, a plain one as itself
    (`pulumi.Config.get_object` is the SDK's own decoder of that text). The
    two sources hand over different shapes and each undoes its own, so a
    source that did not would give a different pin or none.
    """
    block = project_block()
    project = ProjectFile(block)
    program: dict[str, str] = {}
    for key, value in block.items():
        if not key.startswith('versions:'):
            continue
        inner = cast('dict[str, object]', value)['value'] if isinstance(value, dict) else value
        program[key] = json.dumps(inner) if isinstance(inner, dict) else cast('str', inner)
    pulumi.runtime.set_all_config(program)
    from_file = Versions(project)
    from_program = Versions(ProgramConfig())

    charts = project.names(CHART)
    manifests = project.names(MANIFEST)
    images = project.names(IMAGE)
    # Not vacuous: the block pins every kind.
    assert charts
    assert manifests
    assert images
    for name in charts:
        assert from_program.chart[name] == from_file.chart[name], name
    for name in manifests:
        assert from_program.manifest[name] == from_file.manifest[name], name
    for name in images:
        assert from_program.image[name] == from_file.image[name], name
    assert from_program.talos == from_file.talos


def test_a_structured_pin_written_outside_value_is_refused_by_name() -> None:
    """Pulumi refuses an object written directly under a key, so a script reading the file does too.

    Otherwise the script would read a pin no program can ever be handed: the
    CLI fails the whole project file over it, `additionalProperties ...
    not allowed` against its config type declaration.
    """
    project = ProjectFile({'versions:chart-cert-manager': dict(OCI_CHART)})

    with pytest.raises(ValueError, match='versions:chart-cert-manager is an object written outside `value:`'):
        _ = Versions(project).chart['cert-manager']


@pytest.mark.parametrize(
    ('missing', 'read'),
    [
        ('the Talos release', lambda: versions.talos),
        ('versions:chart-nowhere', lambda: versions.chart['nowhere']),
        ('versions:manifest-nowhere', lambda: versions.manifest['nowhere']),
        ('versions:image-nowhere', lambda: versions.image['nowhere']),
    ],
    ids=['talos', 'chart', 'manifest', 'image'],
)
def test_a_pin_nothing_configures_is_refused_by_name(missing: str, read: Callable[[], object]) -> None:
    """A half-filled configuration is the ordinary state of a first run.

    So what matters is that the run stops naming the key an operator has to go
    and write, rather than somewhere downstream holding an empty string.
    """
    pulumi.runtime.set_all_config({})

    with pytest.raises(KeyError, match=missing):
        _ = read()


@pytest.mark.parametrize(
    ('pin', 'refusal'),
    [
        (OCI_CHART | {'digest': None}, 'carries no lower-case `sha256:` digest'),
        (OCI_CHART | {'digest': DIGEST.upper()}, 'carries no lower-case `sha256:` digest'),
        (HTTP_CHART | {'digest': DIGEST}, 'offers no digest to check'),
        (HTTP_CHART | {'repository': 'charts.example.invalid'}, 'neither `oci://` nor `https://`'),
        (HTTP_CHART | {'version': 1.2}, 'has no `version` string'),
        (HTTP_CHART | {'definitions': 'yes'}, 'has no `definitions` boolean'),
        (HTTP_CHART | {'render-values': {'crds.enabled': 'true'}}, 'for definitions it does not render'),
        (OCI_CHART | {'render-values': {'crds.enabled': True}}, 'not a string'),
        (HTTP_CHART | {'floor': {'operator': '1.26'}}, 'has no `document` string'),
        (HTTP_CHART | {'versoin': '0.30.0'}, 'fields this kind has not got: versoin'),
        ('https://charts.example.invalid:0.29.0', 'is not an object written under `value:`'),
    ],
    ids=[
        'oci without digest',
        'upper-case digest',
        'http with digest',
        'no scheme',
        'unquoted version',
        'definitions not a flag',
        'values without definitions',
        'value not a string',
        'floor without document',
        'misspelled field',
        'the old one-line form',
    ],
)
def test_a_chart_pin_of_the_wrong_shape_is_refused_by_name(pin: object, refusal: str) -> None:
    """Checked where the pin is read, so a mistake is a configuration error with a key on it.

    The repository decides the digest: an OCI registry serves one and Helm
    checks it, so an OCI pin without one would install whatever the tag names
    today, while an HTTP repository offers none, so a digest on one would read
    as a check that nothing makes.
    """
    pulumi.runtime.set_all_config(
        dict(PINS)
        | {
            'versions:chart-cert-manager': pin
            if isinstance(pin, str)
            else json.dumps({key: value for key, value in cast('dict[str, object]', pin).items() if value is not None})
        }
    )

    with pytest.raises(ValueError, match='versions:chart-cert-manager') as refused:
        _ = versions.chart['cert-manager']
    assert refusal in str(refused.value)


@pytest.mark.parametrize(
    ('pin', 'refusal'),
    [
        (MANIFEST_PIN | {'sha256': f'sha256:{"b" * 64}'}, 'carries no lower-case hexadecimal sha256'),
        (MANIFEST_PIN | {'repository': 'gateway-api'}, 'names no `<owner>/<name>` GitHub repository'),
        ({key: value for key, value in MANIFEST_PIN.items() if key != 'asset'}, 'has no `asset` string'),
    ],
    ids=['prefixed digest', 'no owner', 'no asset'],
)
def test_a_manifest_pin_of_the_wrong_shape_is_refused_by_name(pin: dict[str, str], refusal: str) -> None:
    pulumi.runtime.set_all_config(dict(PINS) | {'versions:manifest-gateway-api': json.dumps(pin)})

    with pytest.raises(ValueError, match='versions:manifest-gateway-api') as refused:
        _ = versions.manifest['gateway-api']
    assert refusal in str(refused.value)


@pytest.mark.parametrize(
    'value',
    [
        f'{REPOSITORY}:3',
        f'{REPOSITORY}:3@{DIGEST[:16]}',
        f'{REPOSITORY}:3@{DIGEST.upper()}',
        f'{REPOSITORY}@{DIGEST}',
        f'{REPOSITORY}:3@{"a" * 64}',
        f':3@{DIGEST}',
    ],
    ids=['no digest', 'truncated', 'upper case', 'no tag', 'unqualified digest', 'no repository'],
)
def test_an_image_pin_that_is_not_a_whole_reference_is_refused(value: str) -> None:
    """Checked here rather than at apply time.

    A truncated paste is then a configuration error with a key on it, instead
    of a pull that reaches a registry and is refused there. The digest has to
    carry its algorithm and be lower case, because that is the spelling a
    registry serves and the one a device's marker is compared byte for byte
    against — a differently-spelled digest is a pin that never matches.
    """
    pulumi.runtime.set_all_config(dict(PINS) | {'versions:image-gateway-caddy': value})

    with pytest.raises(ValueError, match='versions:image-gateway-caddy'):
        _ = versions.image['gateway-caddy']


def test_a_registry_named_with_a_port_keeps_it_out_of_the_tag() -> None:
    """The tag is what follows the *last* colon, and never contains a slash."""
    pulumi.runtime.set_all_config(
        dict(PINS) | {'versions:image-gateway-caddy': f'registry.invalid:5000/installation/caddy:3@{DIGEST}'}
    )

    assert versions.image['gateway-caddy'] == ImagePin('registry.invalid:5000/installation/caddy', '3', DIGEST)


@pytest.mark.parametrize(
    'value',
    ['1.13.9', 'v1.13', 'latest', 'v1.13.9 ', '', 'v1.14.0-beta.1'],
    ids=['no v', 'minor line', 'not a version', 'trailing space', 'empty', 'pre-release'],
)
def test_a_talos_pin_that_is_not_a_release_tag_is_refused_by_name(value: str) -> None:
    """The tag is what the image factory's paths and the image names carry.

    So a value in any other spelling is refused where it is read, naming the
    key, rather than reaching the factory as a release it does not serve. A
    pre-release is refused as well, because the renovate manager for the key
    would track it as the release it precedes.
    """
    pulumi.runtime.set_all_config(dict(PINS) | {'versions:talos': value})

    with pytest.raises(ValueError, match='versions:talos'):
        _ = versions.talos


# --------------------------------------------------------------------------
# Renovate moves every image pin.

#: The image manager's pattern, as `renovate.json5` writes it: the dependency
#: is the reference before the tag, the version the tag.
IMAGE_MATCH_STRING = r'versions:image-(?<key>[\w-]+):\s*(?<depName>\S+?):(?<currentValue>[^\s:@/]+)@(?<currentDigest>sha256:[0-9a-f]{64})'


def _image_lines() -> list[str]:
    return re.findall(r'^\s*versions:image-.*$', (ROOT / 'Pulumi.yaml').read_text(), re.MULTILINE)


def _read_by_renovate(line: str) -> re.Match[str]:
    found = as_python_spells_it(IMAGE_MATCH_STRING).search(line)
    assert found is not None, f'renovate reads nothing off {line.strip()!r}'
    return found


def test_renovate_reads_every_image_pin_whole() -> None:
    """Every image pin is one renovate reads, as the reference the program pulls by.

    Renovate reads its configuration from the default branch, so a pin its
    pattern does not reach, or reaches only in part, sits on its release
    without a word: a pin with no digest is not read at all, and one whose
    repository the pattern cut short would be looked up as another image.
    """
    config = (ROOT / 'renovate.json5').read_text()
    # The pattern without the key's group is the one the file holds.
    assert as_renovate_spells_it(IMAGE_MATCH_STRING.replace('(?<key>[\\w-]+)', '[\\w-]+')) in config

    project = ProjectFile(project_block())
    pins = Versions(project).image
    lines = _image_lines()
    assert len(lines) == len(project.names(IMAGE))
    for line in lines:
        found = _read_by_renovate(line)
        assert pins[found['key']] == ImagePin(found['depName'], found['currentValue'], found['currentDigest'])


# A model of the two versionings an image pin is read under, enough to say
# whether renovate would offer a release: how a tag parses, which tags may
# replace which, and how a step is classified. Each follows renovate's own
# source: `lib/modules/versioning/docker/index.ts`,
# `lib/modules/versioning/regex/index.ts` and
# `lib/workers/repository/process/lookup/update-type.ts`.


@dataclass(frozen=True)
class Parsed:
    release: tuple[int, ...]
    #: What a candidate has to share with the pinned tag to replace it.
    compatibility: tuple[object, ...]


def _docker(tag: str) -> Parsed | None:
    """A tag as docker versioning reads it: numbers, then everything past the first hyphen as a suffix.

    A candidate replaces the pin only with the same suffix and the same number
    of release parts, and a tag that looks like a commit hash is no version.
    """
    if re.fullmatch(r'[a-f0-9]{7,40}', tag) and not tag.isdigit():
        return None
    prefix, *suffix = re.sub(r'^[vV]', '', tag).split('-')
    found = re.fullmatch(r'(\d+(?:\.\d+)*)(\w*)', prefix)
    if found is None or found[2]:
        # A prerelease, which renovate does not offer in place of a release.
        return None
    release = tuple(int(part) for part in found[1].split('.'))
    return Parsed(release, ('-'.join(suffix), len(release)))


def _regex(pattern: str) -> Callable[[str], Parsed | None]:
    """A tag as a `regex:` versioning reads it: its named groups, build and revision after the patch."""
    compiled = as_python_spells_it(pattern)

    def parse(tag: str) -> Parsed | None:
        found = compiled.match(tag)
        if found is None:
            return None
        groups = found.groupdict()
        release = [int(groups.get(part) or 0) for part in ('major', 'minor', 'patch')]
        if groups.get('build'):
            release.append(int(groups['build']))
            if groups.get('revision'):
                release.append(int(groups['revision']))
        return Parsed(tuple(release), (groups.get('compatibility'),))

    return parse


def _update_type(before: Parsed, after: Parsed) -> str:
    if before.release[0] != after.release[0]:
        return 'major'
    if before.release[1:2] != after.release[1:2]:
        return 'minor'
    return 'patch'


def _rules_naming(config: str, dependency: str) -> list[str]:
    return [rule for rule in package_rules(config) if dependency in listed(rule, 'matchDepNames')]


def _versioning(config: str, dependency: str) -> Callable[[str], Parsed | None]:
    chosen = [value for rule in _rules_naming(config, dependency) if (value := scalar(rule, 'versioning'))]
    assert len(chosen) <= 1, f'{dependency} is given {len(chosen)} versionings'
    if not chosen:
        return _docker
    assert chosen[0].startswith('regex:'), chosen[0]
    # The file holds it as a JSON5 string, each backslash doubled.
    return _regex(chosen[0].removeprefix('regex:').replace('\\\\', '\\'))


def _offered(config: str, dependency: str, update: str) -> bool:
    """Whether renovate proposes an update of this type: a major on the dashboard, a minor outright, a patch by rule."""
    if update != 'patch':
        return True
    patches_off = re.search(r'^  patch: \{\s*(//[^\n]*\s*)*enabled: false,', config, re.MULTILINE)
    if patches_off is None:
        return True
    return any(
        'patch' in listed(rule, 'matchUpdateTypes') and re.search(r'^\s*enabled: true,$', rule, re.MULTILINE)
        for rule in _rules_naming(config, dependency)
    )


@dataclass(frozen=True)
class Scheme:
    """How a pinned image's publisher tags it, by two consecutive releases its registry lists.

    `step` is a release followed by the next one of the same line, chosen to
    show how that line moves: a minor where the line moves by minors, a patch
    where every release is one. `foreign` are tags the same repository
    publishes that are no release of the pinned line -- another flavour,
    another architecture, a prerelease, a commit -- and must never be offered
    in its place.
    """

    step: tuple[str, str]
    foreign: tuple[str, ...] = ()


#: The tagging scheme of each image pinned, read off each publisher's
#: registry. Every pinned dependency needs one; one whose pin is gone is
#: inert.
SCHEMES = {
    'ghcr.io/aetf/homelab-containers/caddy': Scheme(('15', '16'), ('sha-192765fe96e82be927e875b040869f40536698eb',)),
    'ghcr.io/aetf/homelab-containers/adguard': Scheme(('15', '16')),
    'ghcr.io/aetf/homelab-containers/zerotier': Scheme(('15', '16')),
    'docker.io/rancher/local-path-provisioner': Scheme(('v0.0.36', 'v0.0.37'), ('v0.0.x-head-arm64',)),
    'docker.io/library/busybox': Scheme(('1.37.0', '1.38.0')),
    'docker.io/devture/exim-relay': Scheme(('4.97.1-r0-1', '4.98-r0-0'), ('4.96-r1-0-arm64v8',)),
    'ghcr.io/authelia/authelia': Scheme(('4.38.19', '4.39.0')),
    'docker.io/curlimages/curl': Scheme(('8.22.0', '8.23.0')),
    'docker.io/nginxinc/nginx-unprivileged': Scheme(('1.30.6', '1.31.0'), ('1.31.0-alpine',)),
    'registry.k8s.io/git-sync/git-sync': Scheme(('v4.7.1', 'v4.8.0')),
    'ghcr.io/oss-apps/splitpro': Scheme(('v2.0.3', 'v2.1.0')),
    'ghcr.io/continuwuity/continuwuity': Scheme(('v26.8.1', 'v26.9.0')),
    'docker.io/syncthing/syncthing': Scheme(('2.0.16', '2.1.0')),
    'docker.io/mikefarah/yq': Scheme(('4.53.6', '4.54.1')),
    'docker.io/sigoden/dufs': Scheme(('v0.46.0', 'v0.47.0')),
    'docker.io/syncthing/discosrv': Scheme(('2.0.16', '2.1.0')),
    'docker.io/frosty5689/hath': Scheme(('1.6.5', '1.6.6')),
    'ghcr.io/donkie/spoolman': Scheme(('0.26.1', '0.27.0')),
    'docker.io/alpine/socat': Scheme(('1.8.0.3', '1.8.1.1'), ('1.7.4.4-r0',)),
    'ghcr.io/immich-app/immich-server': Scheme(('v3.3.6', 'v3.4.0'), ('v3.4.0-rc.1',)),
    'ghcr.io/immich-app/immich-machine-learning': Scheme(
        ('v3.3.6-openvino', 'v3.4.0-openvino'), ('v3.4.0', 'v3.4.0-cuda', 'v3.4.0-rc.1-openvino')
    ),
    'docker.io/library/redis': Scheme(('7.4.1', '7.4.2'), ('7.4.2-alpine',)),
    'docker.io/oliver006/redis_exporter': Scheme(('v1.96.0', 'v1.97.0'), ('v1.97.0-alpine',)),
    'ghcr.io/jellyfin/jellyfin': Scheme(('10.10.7', '10.11.0')),
    'docker.io/shokoanime/server': Scheme(('v5.2.5', 'v5.3.0')),
    'docker.io/alpine/k8s': Scheme(('1.36.6', '1.37.0')),
    'docker.io/qbittorrentofficial/qbittorrent-nox': Scheme(('5.1.4-2', '5.2.0-1'), ('5.3.0-lt1-2', '5.3.0-lt2-1')),
}


def _pinned() -> dict[str, list[str]]:
    """Every tag `Pulumi.yaml` pins, by the dependency renovate reads it as.

    A line renovate reads nothing off is left to the case that requires it to.
    """
    pinned: dict[str, list[str]] = {}
    for line in _image_lines():
        found = as_python_spells_it(IMAGE_MATCH_STRING).search(line)
        if found is not None:
            pinned.setdefault(found['depName'], []).append(found['currentValue'])
    return pinned


def test_the_image_pins_are_read() -> None:
    """The case below is parametrized by what `Pulumi.yaml` pins, so a block read as empty would hold nothing."""
    assert _pinned()


@pytest.mark.parametrize('dependency', sorted(_pinned()))
def test_renovate_offers_the_next_release_of_every_image_pin(dependency: str) -> None:
    """A later release of the pinned line is one renovate replaces the pin with, under the rules it reads.

    Two things leave a pin on its release for good, and renovate reports
    neither, since it reads its configuration from the default branch: a
    tag whose scheme the versioning cannot order -- docker versioning holds
    everything past the first hyphen fixed, so a tag carrying a packaging
    revision never has a successor -- and a line whose every release is a
    patch, which the global `patch: enabled: false` drops. So the next
    release is read the way renovate reads it, against the pinned tag, and
    the update it makes has to be one renovate proposes.

    The case reads no registry: `SCHEMES` is a transcription of each
    publisher's tagging scheme, so it is necessary and not sufficient. A
    publisher that renames a flavour or a variant leaves the case green
    until its scheme here is corrected.
    """
    assert dependency in SCHEMES, f'{dependency} is pinned with no tagging scheme recorded in SCHEMES'
    config = (ROOT / 'renovate.json5').read_text()
    scheme = SCHEMES[dependency]
    parse = _versioning(config, dependency)
    before, after = (parse(tag) for tag in scheme.step)
    assert before is not None and after is not None, f'{dependency}: {scheme.step} is no pair of versions'
    assert after.release > before.release, scheme.step
    for tag in _pinned()[dependency]:
        pinned = parse(tag)
        assert pinned is not None, f'{dependency}: the pinned {tag} is no version'
        assert after.compatibility == pinned.compatibility, f'{dependency}: {scheme.step[1]} cannot replace {tag}'
        for other in scheme.foreign:
            read = parse(other)
            assert read is None or read.compatibility != pinned.compatibility, f'{dependency}: {other} replaces {tag}'
    update = _update_type(before, after)
    assert _offered(config, dependency, update), f'{dependency}: a {update} update is never proposed'
