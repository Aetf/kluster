"""Each cloud node has one physical link, which its machine configuration depends on.

The Talos component names a node's link by an alias that a fixed-name
`LinkAliasConfig` assigns only when exactly one physical link matches
(`talos.uplink_alias_document`). A second VNIC on an instance would leave the
alias unassigned and the node without an address, so the invariant is held
here against the whole physical program rather than assumed. The homelab
worker's half is `test_homelab.py`'s, on its domain's interfaces.
"""

from __future__ import annotations

import pytest
from mock_monitor import declaring

# The physical program's installation stand-in and the fixture that runs it;
# importing the fixture is what makes it this module's too.
from test_physical_stack import Installation, setup  # noqa: F401  # pyright: ignore[reportUnusedImport]

from kluster import conventions
from kluster.stacks import physical


@pytest.mark.asyncio
async def test_every_cloud_node_has_exactly_one_vnic(setup: Installation) -> None:  # noqa: F811
    async with declaring():
        await physical.main()

    instances = setup.of_type('oci:Core/instance:Instance')
    # Not vacuous: every cloud node is here, each with its primary VNIC.
    assert len(instances) == len(conventions.CLOUD_NODES) > 0
    for instance in instances:
        assert instance.inputs['createVnicDetails']
    # And no second one: a VNIC beyond the primary is an attachment.
    assert setup.of_type('oci:Core/vnicAttachment:VnicAttachment') == []
