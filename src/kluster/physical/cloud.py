"""The cluster's OCI network (docs/declarative/physical.md §1).

One dual-stack VCN with a single public subnet: the nodes are the ingress, so
there is no private tier to protect and no NAT gateway to pay attention to.
Two gateways hang off it — the internet gateway, and a **service gateway** so
node ↔ Object Storage traffic takes the in-region path that costs nothing
instead of leaving through the IGW.

Security rules are derived, not enumerated: what lives here is the platform
baseline, while per-service ingress is emitted beside the service that needs
it. A hand-kept port list in a design doc would only ever be stale.
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci
from pulumi_oci.core.outputs import GetServicesServiceResult

from kluster import conventions
from putils import Component, async_output, resolve


class CloudNetwork(Component):
    """The VCN, its gateways, its route table and its one public subnet."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.compartment_id = compartment_id

        self.vcn = oci.core.Vcn(
            f'{name}-vcn',
            compartment_id=compartment_id,
            cidr_blocks=[str(conventions.VCN_CIDR)],
            # OCI assigns the /56 GUA; the nodes' v6 addresses come out of it.
            is_ipv6enabled=True,
            display_name=f'{name}-vcn',
            dns_label='kluster',
            opts=self.child_opts(),
        )

        self.internet_gateway = oci.core.InternetGateway(
            f'{name}-igw',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            enabled=True,
            display_name=f'{name}-igw',
            opts=self.child_opts(),
        )

        self.service_gateway = oci.core.ServiceGateway(
            f'{name}-sgw',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            services=[oci.core.ServiceGatewayServiceArgs(service_id=async_output(self._object_storage_service_id))],
            display_name=f'{name}-sgw',
            opts=self.child_opts(),
        )

        self.route_table = oci.core.RouteTable(
            f'{name}-routes',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            display_name=f'{name}-routes',
            route_rules=[
                oci.core.RouteTableRouteRuleArgs(
                    destination='0.0.0.0/0',
                    destination_type='CIDR_BLOCK',
                    network_entity_id=self.internet_gateway.id,
                ),
                oci.core.RouteTableRouteRuleArgs(
                    destination='::/0',
                    destination_type='CIDR_BLOCK',
                    network_entity_id=self.internet_gateway.id,
                ),
                # Object Storage and OCIR by service CIDR label, so the
                # in-region path is taken without hard-coding addresses.
                oci.core.RouteTableRouteRuleArgs(
                    destination=async_output(self._object_storage_cidr),
                    destination_type='SERVICE_CIDR_BLOCK',
                    network_entity_id=self.service_gateway.id,
                ),
            ],
            opts=self.child_opts(),
        )

        self.subnet = oci.core.Subnet(
            f'{name}-subnet',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            cidr_block=str(conventions.VCN_SUBNET_CIDR),
            ipv6cidr_block=async_output(self._subnet_ipv6_cidr),
            route_table_id=self.route_table.id,
            display_name=f'{name}-subnet',
            dns_label='nodes',
            prohibit_public_ip_on_vnic=False,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    async def _object_storage_service(self) -> GetServicesServiceResult:
        """The regional Object Storage service entry a service gateway wants."""
        services = await oci.core.get_services_output().future()
        assert services is not None
        for service in services.services:
            if 'Object Storage' in service.name:
                return service
        raise ValueError('no Object Storage service in this region')

    async def _object_storage_service_id(self) -> str:
        return (await self._object_storage_service()).id

    async def _object_storage_cidr(self) -> str:
        return (await self._object_storage_service()).cidr_block

    async def _subnet_ipv6_cidr(self) -> str:
        """The first /64 of the VCN's assigned /56.

        OCI hands out the prefix, so the subnet's block is derived from it
        rather than declared — the design owns the shape, the platform owns
        the addresses.
        """
        blocks = await resolve(self.vcn.ipv6cidr_blocks)
        prefix = str(blocks[0])
        network, _, _ = prefix.partition('::/')
        return f'{network}::/64'
