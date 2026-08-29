"""The cluster VCN's shape, asserted against Pulumi's mock provider.

What matters here is what cannot be seen by reading a diff later: that the
subnet's IPv6 block is derived from the prefix OCI assigns rather than
declared, and that Object Storage traffic is routed at the service gateway
instead of out through the internet gateway.
"""

from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio

from kluster import conventions

VCN_IPV6_PREFIX = '2603:c020:8000:1200::/56'
OBJECT_STORAGE_SERVICE_ID = 'ocid1.service.oc1.phx.objectstorage'
OBJECT_STORAGE_CIDR = 'oci-phx-objectstorage'


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'oci:Core/vcn:Vcn':
            outputs['ipv6cidrBlocks'] = [VCN_IPV6_PREFIX]
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token == 'oci:Core/getServices:getServices':
            return {
                'services': [
                    {'id': 'ocid1.service.oc1.phx.other', 'name': 'All PHX Services', 'cidrBlock': 'all-phx'},
                    {
                        'id': OBJECT_STORAGE_SERVICE_ID,
                        'name': 'OCI PHX Object Storage',
                        'cidrBlock': OBJECT_STORAGE_CIDR,
                    },
                ]
            }, []
        return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)


@pytest.mark.asyncio
async def test_vcn_is_dual_stack() -> None:
    from kluster.components.cloud import CloudNetwork

    network = CloudNetwork('kluster', compartment_id='ocid1.compartment.test')
    assert await network.vcn.cidr_blocks.future() == [str(conventions.VCN_CIDR)]
    assert await network.vcn.is_ipv6enabled.future() is True


@pytest.mark.asyncio
async def test_subnet_ipv6_is_derived_from_the_assigned_prefix() -> None:
    from kluster.components.cloud import CloudNetwork

    network = CloudNetwork('kluster', compartment_id='ocid1.compartment.test')
    assert await network.subnet.ipv6cidr_block.future() == '2603:c020:8000:1200::/64'


@pytest.mark.asyncio
async def test_object_storage_rides_the_service_gateway() -> None:
    from kluster.components.cloud import CloudNetwork

    network = CloudNetwork('kluster', compartment_id='ocid1.compartment.test')

    services = await network.service_gateway.services.future()
    assert services is not None
    assert [s.service_id for s in services] == [OBJECT_STORAGE_SERVICE_ID]

    rules = await network.route_table.route_rules.future()
    assert rules is not None
    by_destination = {rule.destination: rule.destination_type for rule in rules}
    assert by_destination['0.0.0.0/0'] == 'CIDR_BLOCK'
    assert by_destination['::/0'] == 'CIDR_BLOCK'
    assert by_destination[OBJECT_STORAGE_CIDR] == 'SERVICE_CIDR_BLOCK'
