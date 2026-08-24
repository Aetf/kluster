"""The physical program as a whole.

A smoke test with teeth: it declares the entire stack against mocks, which is
what catches wiring mistakes — a resource argument the provider would reject,
or a dependency ordered so that the endpoint is needed before it exists.
"""

from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio

LB_ADDRESS = '203.0.113.10'
VNIC_ID = 'ocid1.vnic.oc1.phx.augmented'


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'oci:Core/vcn:Vcn':
            outputs['ipv6cidrBlocks'] = ['2603:c020:8000:1200::/56']
        if args.typ == 'oci:NetworkLoadBalancer/networkLoadBalancer:NetworkLoadBalancer':
            outputs['ipAddresses'] = [{'ipAddress': LB_ADDRESS, 'isPublic': True}]
        if args.typ == 'talos:machine/secrets:Secrets':
            outputs['machineSecrets'] = {'cluster': {'id': 'test'}}
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        match args.token:
            case 'oci:Core/getServices:getServices':
                return {'services': [{'id': 'ocid1.service.os', 'name': 'Object Storage', 'cidrBlock': 'oci-os'}]}, []
            case 'oci:Core/getVnicAttachments:getVnicAttachments':
                return {'vnicAttachments': [{'vnicId': VNIC_ID}]}, []
            case 'oci:Identity/getAvailabilityDomains:getAvailabilityDomains':
                return {'availabilityDomains': [{'name': 'ZRbp:PHX-AD-1'}]}, []
            case 'oci:Identity/getFaultDomains:getFaultDomains':
                return {'faultDomains': [{'name': f'FAULT-DOMAIN-{n}'} for n in (1, 2, 3)]}, []
            case 'talos:machine/getConfiguration:getConfiguration':
                return {'machineConfiguration': 'machine: {}'}, []
            case 'talos:imagefactory/getUrls:getUrls':
                return {'urls': {'diskImage': 'https://factory.talos.dev/image/test/v1.11.0/oracle-arm64.qcow2'}}, []
            case _:
                return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup() -> None:
    pulumi.runtime.set_all_config(
        {
            'kluster:compartmentId': 'ocid1.compartment.test',
            'kluster:talosVersion': 'v1.11.0',
        }
    )
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)


@pytest.mark.asyncio
async def test_the_stack_declares() -> None:
    from kluster.stacks import physical

    await physical.main()
