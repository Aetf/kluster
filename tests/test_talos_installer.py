"""Every node names, as its installer, the one built from the schematic and release it boots.

A node boots its disk image once, and every later Talos release reaches it
through `talosctl upgrade`, which installs the installer image it is handed.
The runbook hands it the one the node's own configuration names
(operations.md §2.1), so the relation that keeps a node's system extensions
across an upgrade is this one: the installer each node's configuration names
is built from the same schematic, for the same platform, as the image that
node boots, at the same Talos release. The worker's schematic carries the
extensions its GPU needs and the cloud one carries none, so a node naming the
other class's installer -- or the stock one, which no schematic built -- loses
or gains them on its next upgrade. The release is the installer's tag, which
the runbook compares with the new pin before it upgrades, so a tag that
drifted from the release the fleet is pinned to would upgrade the node to
another release.

Held on the physical program as a whole, because the relation is the
program's wiring: the artifacts and the cluster are separate components, and
which artifact's installer reaches which node's configuration is decided only
where the program declares both. The harness's factory builds every URL from
the schematic and release it is asked about (`physical_installation`), so
each URL says which schematic and release it came from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from mock_monitor import declaring
from physical_installation import Installation, install

from kluster import conventions
from kluster.stacks import physical


@dataclass(frozen=True)
class Built:
    """What a factory artifact is built from: the schematic, the platform and the Talos release."""

    schematic: str
    platform: str
    version: str


def disk_image(url: str) -> Built:
    """What a factory disk-image URL names: `<factory>/image/<schematic>/<version>/<platform>-<arch>.<suffix>`."""
    _, kind, schematic, version, artifact = urlsplit(url).path.split('/')
    assert kind == 'image', url
    return Built(schematic=schematic, platform=artifact.split('-')[0], version=version)


def installer(reference: str) -> Built:
    """What a factory installer reference names: `<host>/<platform>-installer/<schematic>:<version>`."""
    _host, repository, tagged = reference.split('/')
    platform, suffix = repository.rsplit('-', 1)
    assert suffix == 'installer', reference
    schematic, version = tagged.split(':')
    return Built(schematic=schematic, platform=platform, version=version)


def installers_named(configuration: dict[str, Any]) -> list[str]:
    """The installer images one rendering's patches name."""
    patches = [cast('dict[str, Any]', json.loads(patch)) for patch in configuration['configPatches']]
    return [
        cast('dict[str, str]', patch['machine']['install'])['image']
        for patch in patches
        if 'kind' not in patch and 'install' in patch.get('machine', {})
    ]


@pytest_asyncio.fixture
async def ran(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Installation:
    installation = await install(monkeypatch, tmp_path)
    async with declaring():
        await physical.main()
    return installation


@pytest.mark.asyncio
async def test_every_node_names_the_installer_of_the_schematic_it_boots(ran: Installation) -> None:
    # The two images each node class boots, read off what the program handed
    # the providers: the cloud nodes' OCI import and the worker's local disk.
    cloud_source = ran.inputs_of(f'{conventions.CLUSTER_NAME}-image', 'oci:Core/image:Image')['imageSourceDetails']
    booted = {
        'controlplane': disk_image(str(cloud_source['sourceUri'])),
        'worker': disk_image(str(ran.inputs_of(f'{conventions.CLUSTER_NAME}-worker-nocloud')['url'])),
    }
    # The two classes boot different schematics, or a swap would pass below.
    assert booted['controlplane'].schematic != booted['worker'].schematic

    # Every rendering, the day-1 one the dedicated-VIP node is applied
    # included: each cloud node, the worker, and that second rendering.
    assert len(ran.configurations) == len(conventions.CLOUD_NODES) + 2
    for configuration in ran.configurations:
        role = str(configuration['machineType'])
        (named,) = installers_named(configuration)
        assert installer(named) == booted[role], f'a {role} names {named}, and boots {booted[role]}'
