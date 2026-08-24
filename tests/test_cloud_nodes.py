"""The node fleet's shape.

The properties asserted here are the ones a later `pulumi diff` cannot show
because they are structural: that no two nodes share a fault domain, that the
legacy metadata endpoint is off on every one of them, and that the dedicated
VIP is a reserved address attached to a secondary private IP rather than an
ephemeral one.
"""

from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio

VNIC_ID = 'ocid1.vnic.oc1.phx.augmented'


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        return args.name + '_id', dict(cast('dict[str, Any]', args.inputs))

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token == 'oci:Core/getVnicAttachments:getVnicAttachments':
            return {'vnicAttachments': [{'vnicId': VNIC_ID}]}, []
        return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)


def build() -> Any:
    from kluster.physical.nodes import CloudNodes, NodeLoadBalancer

    load_balancer = NodeLoadBalancer(
        'kluster',
        compartment_id='ocid1.compartment.test',
        subnet_id='ocid1.subnet.test',
    )
    return CloudNodes(
        'kluster',
        compartment_id='ocid1.compartment.test',
        subnet_id='ocid1.subnet.test',
        image_id='ocid1.image.test',
        machine_configs={'cp1': 'config-1', 'cp2': 'config-2', 'cp3': 'config-3'},
        ocpus=1,
        memory_gb=8,
        boot_volume_gb=50,
        fault_domains=['FAULT-DOMAIN-1', 'FAULT-DOMAIN-2', 'FAULT-DOMAIN-3'],
        availability_domain='ZRbp:PHX-AD-1',
        augmented='cp1',
        load_balancer=load_balancer,
    )


@pytest.mark.asyncio
async def test_nodes_do_not_share_a_fault_domain() -> None:
    nodes = build()
    domains = [await instance.fault_domain.future() for instance in nodes.instances.values()]
    assert len(set(domains)) == len(domains) == 3


@pytest.mark.asyncio
async def test_legacy_imds_is_disabled_everywhere() -> None:
    nodes = build()
    for instance in nodes.instances.values():
        options = await instance.instance_options.future()
        assert options is not None
        assert options.are_legacy_imds_endpoints_disabled is True


@pytest.mark.asyncio
async def test_each_node_carries_its_own_machine_config() -> None:
    nodes = build()
    configs = {node: (await instance.metadata.future())['user_data'] for node, instance in nodes.instances.items()}
    assert configs == {'cp1': 'config-1', 'cp2': 'config-2', 'cp3': 'config-3'}


@pytest.mark.asyncio
async def test_the_vip_is_reserved_and_secondary() -> None:
    nodes = build()
    assert await nodes.reserved_ip.lifetime.future() == 'RESERVED'
    assert await nodes.secondary_ip.vnic_id.future() == VNIC_ID
    # The reserved address points at the secondary private IP, not the node's
    # primary one, so the workload's address survives a node rebuild.
    assert await nodes.reserved_ip.private_ip_id.future() == await nodes.secondary_ip.id.future()


@pytest.mark.asyncio
async def test_management_ports_preserve_the_client_address() -> None:
    from kluster.physical.nodes import MANAGEMENT_PORTS, NodeLoadBalancer

    balancer = NodeLoadBalancer('lb', compartment_id='ocid1.compartment.test', subnet_id='ocid1.subnet.test')
    assert set(balancer.backend_sets) == set(MANAGEMENT_PORTS)
    for backend_set in balancer.backend_sets.values():
        assert await backend_set.is_preserve_source.future() is True

    # Every node backs every management port.
    nodes = build()
    assert len(nodes.backends) == len(MANAGEMENT_PORTS) * 3
