"""Renovate reads every conda pin of `mise.toml` from the channel mise installs it from, and within the line it holds.

A pin that names a `channel` is one mise installs through its conda backend.
Renovate's mise manager hands such a pin to the conda datasource under the
bare package name, dropping the channel, and api.anaconda.org answers a
package only as `<channel>/<name>`: so each needs a rule naming it that way,
or its lookup finds nothing and the pin never moves. The package a pin
installs is the name after `conda:`, or for a pin by a short name, the one
mise's own registry resolves the name to.

A pin that holds a line other than its package's newest -- the local gawk,
which follows the appliance's -- carries an `allowedVersions` on its rule,
and the bound admits the pinned release and not the next line's first.
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

#: The prefix a pin by mise's conda backend carries in `mise.toml`, and that
#: `mise registry` answers a short name with.
CONDA = 'conda:'


def channel_pins() -> dict[str, dict[str, Any]]:
    """Every `mise.toml` pin that names a `channel`, by the key the mise manager names the dependency."""
    tools: dict[str, Any] = tomllib.loads((ROOT / 'mise.toml').read_text())['tools']
    return {key: pin for key, pin in tools.items() if isinstance(pin, dict) and 'channel' in pin}


def package(key: str) -> str:
    """The conda package the pin `key` installs: its own name after `conda:`, or what mise's registry resolves it to."""
    if key.startswith(CONDA):
        return key.removeprefix(CONDA)
    resolved = process_sessions.run(
        [pinned_tools.located('mise'), 'registry', key], text=True, check=True, timeout=60
    ).stdout.split()
    conda = [backend for backend in resolved if backend.startswith(CONDA)]
    assert conda, f'mise registry resolves {key} to {resolved}, and no conda backend among them'
    return conda[0].removeprefix(CONDA)


def _rule_for(config: str, key: str) -> str:
    named = [
        rule
        for rule in package_rules(config)
        if listed(rule, 'matchDatasources') == ['conda']
        and listed(rule, 'matchDepNames') == [key]
        and scalar(rule, 'overridePackageName') is not None
    ]
    assert len(named) == 1, f'{len(named)} rules name the conda package of {key}; renovate needs one'
    return named[0]


def allowed(rule: str) -> re.Pattern[str] | None:
    """The rule's `allowedVersions`, where it sets one as a regular expression."""
    written = scalar(rule, 'allowedVersions')
    if written is None:
        return None
    unescaped = written.replace('\\\\', '\\')
    assert unescaped.startswith('/') and unescaped.endswith('/'), f'allowedVersions {written} is not a pattern'
    return re.compile(unescaped[1:-1])


def next_line(version: str) -> str:
    """The first release of the line after `version`'s: its next minor."""
    major, minor = version.split('.')[:2]
    return f'{major}.{int(minor) + 1}.0'


def test_every_conda_pin_is_read_from_its_channel() -> None:
    config = (ROOT / 'renovate.json5').read_text()
    pins = channel_pins()

    # Not vacuous: mise.toml pins conda packages today.
    assert pins
    for key, pin in pins.items():
        rule = _rule_for(config, key)
        assert scalar(rule, 'overridePackageName') == f'{pin["channel"]}/{package(key)}', key


def test_a_conda_pins_bound_admits_its_release_and_not_the_next_line() -> None:
    config = (ROOT / 'renovate.json5').read_text()
    bounded = {
        key: (pin['version'], bound)
        for key, pin in channel_pins().items()
        if (bound := allowed(_rule_for(config, key))) is not None
    }

    # Not vacuous: one pin holds a line today, gawk's.
    assert bounded
    for key, (version, bound) in bounded.items():
        assert bound.search(version), f'{key}: the bound {bound.pattern} refuses the pinned {version}'
        assert not bound.search(next_line(version)), f'{key}: the bound {bound.pattern} admits {next_line(version)}'


@pytest.mark.parametrize(('version', 'following'), [('5.3.1', '5.4.0'), ('17.11', '17.12.0'), ('0.9.3', '0.10.0')])
def test_the_next_line_is_the_next_minor(version: str, following: str) -> None:
    assert next_line(version) == following
