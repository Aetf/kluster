"""Version pins: one namespace, the kind in the key, a parsed value out.

What is under test is the boundary (rfc-002 §11.1). Every pin in this
repository is a string an operator or a renovate branch edited, and the
accessor is the one place that turns it into something typed and refuses a
missing or malformed one by naming the key rather than failing further in.
"""

from __future__ import annotations

from collections.abc import Callable

import pulumi
import pytest

from kluster.lib.versions import ChartVersion, ImagePin, versions

DIGEST = f'sha256:{"a" * 64}'
REPOSITORY = 'ghcr.io/aetf/homelab-containers/caddy'

PINS = {
    'versions:talos': 'v1.13.9',
    'versions:chart-cert-manager': 'https://charts.jetstack.io:v1.19.1',
    'versions:chart-registry-only': 'oci://example.invalid/charts/thing:0.4.0',
    'versions:image-gateway-caddy': f'{REPOSITORY}:3@{DIGEST}',
}


@pytest.fixture(autouse=True)
def pinned() -> None:
    pulumi.runtime.set_all_config(dict(PINS))


def test_every_kind_shares_one_namespace_and_differs_by_key_prefix() -> None:
    """Which is what lets one renovate manager per kind match its own entries.

    Three kinds and one `versions:` namespace, read the same way from any stack
    because the keys are project-level configuration rather than five stacks'
    copies of the same value. The gateway's root filesystems are in the `image`
    kind and not one of their own: they are registry images, so an image
    reference is what pins them (rfc-002 §11.1).
    """
    assert versions.talos == 'v1.13.9'
    assert versions.chart['cert-manager'] == ChartVersion('https://charts.jetstack.io', 'v1.19.1')
    assert versions.image['gateway-caddy'] == ImagePin(REPOSITORY, '3', DIGEST)


def test_a_chart_pinned_from_a_registry_keeps_the_scheme_in_its_repository() -> None:
    # The separator is the last colon, not the first: an `oci://` reference
    # carries one of its own and splitting on it would name no repository.
    assert versions.chart['registry-only'] == ChartVersion('oci://example.invalid/charts/thing', '0.4.0')


@pytest.mark.parametrize(
    ('missing', 'read'),
    [
        ('the Talos release', lambda: versions.talos),
        ('versions:chart-nowhere', lambda: versions.chart['nowhere']),
        ('versions:image-nowhere', lambda: versions.image['nowhere']),
    ],
    ids=['talos', 'chart', 'image'],
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


def test_a_chart_pin_that_names_no_version_at_all_is_refused_by_name() -> None:
    """The repository alone is not a pin, and an unpinned chart is a moving one.

    How far the check goes is limited by the shape: `<repository>:<version>` is
    split at the last colon, so a value carrying one is two halves whatever
    they mean. What is unambiguous is a value with no colon in it.
    """
    pulumi.runtime.set_all_config(dict(PINS) | {'versions:chart-cert-manager': 'charts.jetstack.io'})

    with pytest.raises(ValueError, match='versions:chart-cert-manager'):
        _ = versions.chart['cert-manager']
