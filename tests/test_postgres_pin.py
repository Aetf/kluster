"""Renovate reads the local Postgres client's pin, and moves its major with the appliance's.

`mise.toml`'s `postgres` and `settings.POSTGRES_IMAGE` are held to one major
(`tests/test_postgres_client.py`), so a major release has to move both in one
pull request: apart, each half fails that test on its own. The image is in
the appliance's group by its file; `renovate.json5` puts the client's major
there too, and leaves its minor releases in the toolchain group, since the
image's tag names no minor.

The client is read at all only through a rule of its own. The mise manager
resolves the pin to the conda datasource under the bare package name, which
api.anaconda.org does not answer, so the rule names the package as
`<channel>/<name>`, with the channel `mise.toml` writes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import cast

from renovate_text import group, listed, package_rule, package_rules, scalar

ROOT = Path(__file__).parent.parent

#: The key `mise.toml` pins the client under, which is the name the mise
#: manager gives the dependency.
PIN = 'postgres'

#: The conda package mise's registry resolves `postgres` to (`mise registry
#: postgres` answers `conda:postgresql`).
PACKAGE = 'postgresql'


def _for_the_client(rule: str) -> bool:
    return listed(rule, 'matchDatasources') == ['conda'] and listed(rule, 'matchDepNames') == [PIN]


def _client_rules(config: str) -> list[str]:
    return [rule for rule in package_rules(config) if _for_the_client(rule)]


def _matchers(rule: str) -> list[str]:
    """The keys a rule matches on: renovate tells them from the fields it applies by their prefix."""
    return [key for key in re.findall(r'^\s*(\w+):', rule, re.MULTILINE) if key.startswith(('match', 'exclude'))]


def test_renovate_reads_the_client_from_the_channel_mise_installs_it_from() -> None:
    """Held as text, because there is no JSON5 parser here (`renovate_text`)."""
    config = (ROOT / 'renovate.json5').read_text()
    pin = cast('dict[str, str]', tomllib.loads((ROOT / 'mise.toml').read_text())['tools'][PIN])

    named = [rule for rule in _client_rules(config) if scalar(rule, 'overridePackageName') is not None]
    assert len(named) == 1, 'no rule names the conda package, so renovate looks up a name nothing answers'
    (rule,) = named
    assert _matchers(rule) == ['matchDatasources', 'matchDepNames']
    assert scalar(rule, 'overridePackageName') == f'{pin["channel"]}/{PACKAGE}'


def test_renovate_moves_the_clients_major_with_the_appliance() -> None:
    """Held as text, because there is no JSON5 parser here (`renovate_text`)."""
    config = (ROOT / 'renovate.json5').read_text()
    appliance = "'src/kluster/lib/state_backend/settings.py'"
    assert config.count(appliance) == 1
    _, appliance_rule = package_rule(config, config.index(appliance))

    grouping = [
        (start, rule)
        for start, rule in {package_rule(config, hit.start()) for hit in re.finditer("'conda'", config)}
        if _for_the_client(rule) and group(rule)
    ]
    assert len(grouping) == 1, "no rule puts the client's major in the appliance's group"
    ((at, rule),) = grouping
    assert _matchers(rule) == ['matchDatasources', 'matchDepNames', 'matchUpdateTypes']
    assert listed(rule, 'matchUpdateTypes') == ['major']
    assert len(group(appliance_rule)) == 2
    assert group(rule) == group(appliance_rule)

    # The last match wins a field, so the rule sits after the one grouping the
    # whole mise manager as the toolchain.
    (toolchain_at,) = [
        start
        for start, found in (package_rule(config, hit.start()) for hit in re.finditer("'mise'", config))
        if not listed(found, 'matchDepNames')
    ]
    assert toolchain_at < at
