"""The cloud site's block volumes.

The properties here are the ones no later diff can show, because a diff shows
what changed and these are about what must never change: that a volume holding
a dataset outside every backup regime cannot be deleted or detached without an
explicit unprotect, and that its attachment asks nothing of a guest that ships
no agent.
"""

from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio

from kluster.components.cloud.storage import NodeVolume

COMPARTMENT_ID = 'ocid1.compartment.oc1..test'
AVAILABILITY_DOMAIN = 'ZRbp:PHX-AD-1'
INSTANCE_ID = 'ocid1.instance.oc1.phx.node'
SIZE_GB = 50


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        return args.name + '_id', dict(cast('dict[str, Any]', args.inputs))

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)


def node_volume() -> NodeVolume:
    return NodeVolume(
        'kluster-hath-cache',
        compartment_id=COMPARTMENT_ID,
        availability_domain=AVAILABILITY_DOMAIN,
        instance_id=INSTANCE_ID,
        size_gb=SIZE_GB,
    )


@pytest.mark.asyncio
async def test_the_volume_and_its_attachment_both_need_an_unprotect() -> None:
    volume = node_volume()
    # The data is outside every backup regime, so a destroy is unrecoverable;
    # a detach proposed by a node replacement is the same loss by another name.
    assert volume.volume._protect is True  # pyright: ignore[reportPrivateUsage]
    assert volume.attachment._protect is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_the_volume_is_sized_as_asked_and_on_the_budgeted_tier() -> None:
    volume = node_volume()
    assert await volume.volume.size_in_gbs.future() == str(SIZE_GB)
    # 0 VPUs/GB is Lower Cost, the tier the storage budget is written against.
    assert await volume.volume.vpus_per_gb.future() == '0'


@pytest.mark.asyncio
async def test_the_attachment_needs_no_agent_in_the_guest() -> None:
    volume = node_volume()
    # An iSCSI attachment expects the guest to perform a login; Talos ships
    # nothing that would.
    assert await volume.attachment.attachment_type.future() == 'paravirtualized'
    assert await volume.attachment.volume_id.future() == await volume.volume.id.future()
    assert await volume.attachment.instance_id.future() == INSTANCE_ID
