"""The pinned Fedora CoreOS release, and the renovate manager that moves it (rfc-006 ruling 7).

`kluster.lib.state_backend.settings` pins the release the `state-backend`
stack imports its image from and the digest its upload checks the download
against. Renovate moves the two as one pair, read from the stream's own
metadata through a custom data source. Held as text, because there is no
JSON5 parser here (`renovate_text`).
"""

from __future__ import annotations

import re
from pathlib import Path

from renovate_text import as_python_spells_it, package_rule

from kluster.lib.state_backend import settings

ROOT = Path(__file__).parent.parent
SETTINGS = ROOT / 'src' / 'kluster' / 'lib' / 'state_backend' / 'settings.py'

#: The manager's pattern, as Python writes it out: the release, and on the
#: next line its digest, captured together so a bump moves both.
MATCH_STRING = r"FCOS_RELEASE = '(?<currentValue>[\d.]+)'\nFCOS_ARTIFACT_SHA256 = '(?<currentDigest>[0-9a-f]{64})'"
#: The data source the manager reads, named in the file's `customDatasources`.
DATASOURCE = 'fedora-coreos-stable'


def _spelled(pattern: str) -> str:
    """`pattern` as `renovate.json5` holds it: double-quoted, since it holds single quotes, each backslash doubled."""
    return '"' + pattern.replace('\\', '\\\\') + '"'


def _config() -> str:
    return (ROOT / 'renovate.json5').read_text()


def test_one_manager_captures_the_release_and_its_digest_as_the_settings_hold_them() -> None:
    config = _config()
    assert config.count(_spelled(MATCH_STRING)) == 1

    (found,) = as_python_spells_it(MATCH_STRING).finditer(SETTINGS.read_text())

    assert found.group('currentValue') == settings.FCOS_RELEASE
    assert found.group('currentDigest') == settings.FCOS_ARTIFACT_SHA256


def test_the_manager_reads_the_stream_through_its_own_data_source() -> None:
    config = _config()
    # Its own entry's end rather than `package_rule`'s first brace, since the
    # pattern carries one.
    at = config.index(_spelled(MATCH_STRING))
    manager = config[config.rindex('    {\n', 0, at) : config.index('\n    },', at)]

    assert f"datasourceTemplate: 'custom.{DATASOURCE}'," in manager
    assert "'/^src/kluster/lib/state_backend/settings\\\\.py$/'," in manager

    datasources = config[config.index('customDatasources: {') :]
    entry = datasources[datasources.index(f"'{DATASOURCE}': {{") :]
    assert f"defaultRegistryUrlTemplate: '{settings.FCOS_STREAM_URL}'," in entry
    # The transform reads the release and the digest of the very artifact the
    # settings pin and the upload fetches: the compressed `oraclecloud` disk of
    # the x86_64 build, and when the stream last moved, which the release is
    # aged by.
    (template,) = re.findall(r"transformTemplates: \[\s*'([^']*)',", entry)
    artifact = 'architectures.x86_64.artifacts.oraclecloud'
    assert f'"version": {artifact}.release' in template
    assert f'"digest": {artifact}.formats.`qcow2.xz`.disk.sha256' in template
    assert '"releaseTimestamp": metadata.`last-modified`' in template


def test_a_release_bump_moves_the_url_the_upload_fetches() -> None:
    # The URL is built from the release, so the pair the manager moves is the
    # whole of what a bump changes.
    assert settings.FCOS_RELEASE in settings.FCOS_ARTIFACT_URL
    assert settings.FCOS_STREAM_URL.endswith(f'/streams/{settings.FCOS_STREAM}.json')


def test_the_manager_falls_in_the_appliances_group() -> None:
    # A bump imports a new image, so it travels with the appliance's other pins
    # rather than alone; the group rule matches the file.
    config = _config()
    appliance = "'src/kluster/lib/state_backend/settings.py'"
    _, rule = package_rule(config, config.index(appliance))
    assert "groupSlug: 'state-backend'," in rule
