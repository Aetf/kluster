"""The clients a hand reading of the cluster runs stay on the cluster's lines.

`kubectl` is supported within one minor of the API server it talks to, and
the cluster runs the Kubernetes the pinned Talos release defaults to, which
`talosctl images default` reads off the release itself, with no network. So
the `kubectl` pin's minor is that one: a Talos bump onto a release that
defaults to another minor is red here until its pull request moves the pin.

Renovate offers each such client only releases on its pin's line: a rule in
`renovate.json5` names the client and carries an `allowedVersions` that
admits the pinned release and not the next line's first, so a toolchain pull
request never carries a release off the line. `cmctl` holds the line built
against the cert-manager line of the cluster's chart (its comment in
`mise.toml`); no offline source names that line, so only its bound is held
here.

The bound is read by `renovate_text`, as `test_conda_pins.py` reads gawk's.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from renovate_text import allowed_versions, listed, next_line, package_rules
from talos_images import default_images

ROOT = Path(__file__).parent.parent

#: The `[tools]` key `mise.toml` pins kubectl under, which is also the name
#: renovate's mise manager gives the dependency.
KUBECTL = 'kubectl'

#: Every pin held to a line, by its `[tools]` key.
CLIENTS = (KUBECTL, 'cmctl')


def pins() -> dict[str, Any]:
    return tomllib.loads((ROOT / 'mise.toml').read_text())['tools']


def minor(version: str) -> str:
    """The `<major>.<minor>` of a version, with or without its leading `v`."""
    major, minor_, *_ = version.removeprefix('v').split('.')
    return f'{major}.{minor_}'


def bound(key: str) -> re.Pattern[str]:
    """The `allowedVersions` of the one rule naming `key` alone, as a regular expression."""
    named = [
        line
        for rule in package_rules((ROOT / 'renovate.json5').read_text())
        if listed(rule, 'matchDepNames') == [key] and (line := allowed_versions(rule)) is not None
    ]
    assert len(named) == 1, f'{len(named)} rules bound {key}; renovate needs one'
    return named[0]


def test_kubectl_is_on_the_minor_the_pinned_talos_release_defaults_to() -> None:
    release = default_images()['kube-apiserver'].tag
    assert minor(str(pins()[KUBECTL])) == minor(release), f'the pinned Talos release defaults to Kubernetes {release}'


@pytest.mark.parametrize('key', CLIENTS)
def test_a_clients_bound_admits_its_release_and_not_the_next_line(key: str) -> None:
    version = str(pins()[key])
    line = bound(key)
    assert line.search(version), f'{key}: the bound {line.pattern} refuses the pinned {version}'
    assert not line.search(next_line(version)), f'{key}: the bound {line.pattern} admits {next_line(version)}'
