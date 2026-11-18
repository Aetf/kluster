"""What the cloud fleet's real declaration registers, for an engine case's stand-ins to declare with.

An engine case runs stand-ins under `physical`'s own URNs (framework/testing.md
§8), and what the engine does with a resource's replacement or delete is
decided by three of its options: `protect`, `delete_before_replace` and
`retain_on_delete`. A stand-in that stated them itself would hold what the
case's author wrote rather than what the program declares. So they are read
here, off the registrations the real `NodeLoadBalancer`, `CloudNodes` and
`NodeVolume` make under the mock monitor, declared as the `physical` stack
declares them, and a stand-in takes the ones its own type and logical name
were registered with. So a runbook's commands run against the options the
program declares, and a stand-in named for a resource the fleet does not
register fails the program that declares it.
"""

from __future__ import annotations

from typing import Any, cast

import pulumi
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.cloud.nodes import CloudNodes, NodeLoadBalancer
from kluster.components.cloud.storage import NodeVolume

#: Placements for the three nodes, one AD apiece; a placement is an input, so
#: the fleet is declared without the region's lookups.
PLACEMENTS = [(f'AD-{n}', 'FAULT-DOMAIN-1') for n in (1, 2, 3)]


class Account(Recorder):
    """Answers the two lookups `CloudNodes` makes per node: its primary VNIC, and the one GUA that VNIC holds."""

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        called = cast('dict[str, Any]', args.args)
        match args.token:
            case 'oci:Core/getVnicAttachments:getVnicAttachments':
                return {'vnicAttachments': [{'vnicId': f'vnic-{called["instanceId"]}'}]}
            case 'oci:Core/getVnic:getVnic':
                return {'ipv6addresses': [f'2001:db8::{len(str(called["vnicId"]))}']}
            case _:
                return {}


def key(typ: str, name: str) -> str:
    """How a stand-in finds its options: its type and logical name, which together the fleet registers once."""
    return f'{typ}::{name}'


def options(request: Any) -> dict[str, bool]:
    """The options of one registration request that decide whether, and in what order, a replacement or delete runs.

    Each is named as `pulumi.ResourceOptions` takes it, so a stand-in passes
    the mapping as keyword arguments. `protect` and `retain_on_delete` are
    optional in the request, and one the declaration left unset reads `False`,
    which is what the engine makes of it on a resource whose parent sets none.
    """
    return {
        'protect': bool(request.protect),
        'delete_before_replace': bool(request.deleteBeforeReplace),
        'retain_on_delete': bool(request.retainOnDelete),
    }


async def fleet_options() -> dict[str, dict[str, bool]]:
    """Every resource the fleet's declaration registers, by `key`, and its `options`.

    The fleet is the stack's: every cloud node, the dedicated VIP on the node
    the stack names, and a `NodeVolume` per entry of the volume table on the
    node it attaches to, each component under the name the stack gives it.
    """
    monitor = await run_with(Account(), stack='physical')
    async with declaring():
        balancer = NodeLoadBalancer(conventions.CLUSTER_NAME, compartment_id='compartment', subnet_id='subnet')
        nodes = CloudNodes(
            conventions.CLUSTER_NAME,
            compartment_id='compartment',
            subnet_id='subnet',
            image_id='image',
            machine_configs={node: f'config-{node}' for node in conventions.CLOUD_NODES},
            ocpus=conventions.NODE_OCPUS,
            memory_gb=conventions.NODE_MEMORY_GB,
            boot_volume_gb=conventions.NODE_BOOT_VOLUME_GB,
            placements=PLACEMENTS,
            dedicated_vip_node=conventions.DEDICATED_VIP_NODE,
            load_balancer=balancer,
        )
        for volume_name, entry in sorted(conventions.NODE_VOLUMES.items()):
            instance = nodes.instances[entry.attached_node]
            _ = NodeVolume(
                f'{conventions.CLUSTER_NAME}-{volume_name}',
                compartment_id='compartment',
                availability_domain=instance.availability_domain,
                instance_id=instance.id,
                size_gb=entry.size_gb,
                vpus_per_gb=conventions.NODE_VOLUME_VPUS,
            )
    return {key(request.type, request.name): options(request) for request in monitor.requested if request.custom}
