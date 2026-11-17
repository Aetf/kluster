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

The bound is read as `test_conda_pins.py` reads gawk's. No test module
imports another (framework/testing.md §2), so the two readings are each
module's own.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pinned_tools
import process_sessions
import pytest
from renovate_text import listed, package_rules, scalar

ROOT = Path(__file__).parent.parent

#: The `[tools]` key `mise.toml` pins kubectl under, which is also the name
#: renovate's mise manager gives the dependency.
KUBECTL = 'kubectl'

#: Every pin held to a line, by its `[tools]` key.
CLIENTS = (KUBECTL, 'cmctl')

#: The bound one `talosctl images default` runs under; it reads the release's
#: own constants and takes well under a second.
TIMEOUT = 12


def pins() -> dict[str, Any]:
    return tomllib.loads((ROOT / 'mise.toml').read_text())['tools']


def minor(version: str) -> str:
    """The `<major>.<minor>` of a version, with or without its leading `v`."""
    major, minor_, *_ = version.removeprefix('v').split('.')
    return f'{major}.{minor_}'


def next_line(version: str) -> str:
    """The first release of the line after `version`'s: its next minor."""
    major, minor_ = minor(version).split('.')
    return f'{major}.{int(minor_) + 1}.0'


def bound(key: str) -> re.Pattern[str]:
    """The `allowedVersions` of the one rule naming `key` alone, as a regular expression."""
    named = [
        written
        for rule in package_rules((ROOT / 'renovate.json5').read_text())
        if listed(rule, 'matchDepNames') == [key] and (written := scalar(rule, 'allowedVersions')) is not None
    ]
    assert len(named) == 1, f'{len(named)} rules bound {key}; renovate needs one'
    unescaped = named[0].replace('\\\\', '\\')
    assert unescaped.startswith('/') and unescaped.endswith('/'), f'allowedVersions {named[0]} is not a pattern'
    return re.compile(unescaped[1:-1])


def test_kubectl_is_on_the_minor_the_pinned_talos_release_defaults_to() -> None:
    talosctl = pinned_tools.located('talosctl')
    images = process_sessions.run([talosctl, 'images', 'default'], text=True, timeout=TIMEOUT, check=True).stdout
    found = re.search(r'/kube-apiserver:(v\d+\.\d+\.\d+)$', images, re.MULTILINE)
    assert found is not None, f'{talosctl} names no kube-apiserver image:\n{images}'
    assert minor(str(pins()[KUBECTL])) == minor(found[1]), f'the pinned Talos release defaults to Kubernetes {found[1]}'


@pytest.mark.parametrize('key', CLIENTS)
def test_a_clients_bound_admits_its_release_and_not_the_next_line(key: str) -> None:
    version = str(pins()[key])
    line = bound(key)
    assert line.search(version), f'{key}: the bound {line.pattern} refuses the pinned {version}'
    assert not line.search(next_line(version)), f'{key}: the bound {line.pattern} admits {next_line(version)}'


@pytest.mark.parametrize(('version', 'following'), [('1.36.6', '1.37.0'), ('v1.36.3', '1.37.0'), ('2.9.1', '2.10.0')])
def test_the_next_line_is_the_next_minor(version: str, following: str) -> None:
    assert next_line(version) == following
