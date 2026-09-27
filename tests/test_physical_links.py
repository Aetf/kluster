"""Each cloud node has one physical link, which its machine configuration depends on.

The Talos component names a node's link by an alias that a fixed-name
`LinkAliasConfig` assigns only when exactly one physical link matches
(`talos.uplink_alias_document`). A second VNIC on an instance would leave the
alias unassigned and the node without an address, so the invariant is held
here against the whole physical program rather than assumed. The homelab
worker's half is `test_homelab.py`'s, on its domain's interfaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from mock_monitor import declaring
from physical_installation import Installation, install

from kluster import conventions
from kluster.stacks import physical


@pytest_asyncio.fixture(autouse=True)
async def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Installation:
    return await install(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_every_cloud_node_has_exactly_one_vnic(setup: Installation) -> None:
    async with declaring():
        await physical.main()

    instances = setup.of_type('oci:Core/instance:Instance')
    # Not vacuous: every cloud node is here, each with its primary VNIC.
    assert len(instances) == len(conventions.CLOUD_NODES) > 0
    for instance in instances:
        assert instance.inputs['createVnicDetails']
    # And no second one: a VNIC beyond the primary is an attachment.
    assert setup.of_type('oci:Core/vnicAttachment:VnicAttachment') == []
