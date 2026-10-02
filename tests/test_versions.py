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
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pulumi
import pytest

from kluster.lib.versions import (
    CHART,
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
def pinned() -> None:
    pulumi.runtime.set_all_config(dict(PINS))


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
    # Not vacuous: the block pins both kinds.
    assert charts
    assert manifests
    for name in charts:
        assert from_program.chart[name] == from_file.chart[name], name
    for name in manifests:
        assert from_program.manifest[name] == from_file.manifest[name], name
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
