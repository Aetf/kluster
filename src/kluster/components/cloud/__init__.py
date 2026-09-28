"""The cloud installation: everything declared against the OCI account.

The area holds the network below, the nodes and their load balancer
(`nodes`), the tenancy-level quotas and budget that keep the account inside
the free envelope (`guardrails`), and the block storage attached to a node
(`storage`).

**The network** (docs/declarative/physical.md §1).

One dual-stack VCN with a single public subnet: the nodes are the ingress, so
there is no private tier to protect and no NAT gateway to pay attention to.
Two gateways hang off it — the internet gateway, and a **service gateway** so
node ↔ Object Storage traffic takes the in-region path that costs nothing
instead of leaving through the IGW.

The subnet admits everything, on purpose. A subnet that names no security
list carries the VCN's default one, whose rules are OCI's choice, so the
subnet names one list of its own that admits every protocol in both
directions and both families, statelessly. The filter is on the node: Talos'
ingress firewall decides every host-network port (physical.md §1–2), and
what Cilium's datapath answers ahead of it is the raw TCP/UDP Services
the cluster declares, while no Service allocates a `NodePort`
(cluster-infra.md §2).
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci
from pulumi_oci.core.outputs import GetServicesServiceResult

from kluster import conventions
from putils import Component, async_output, resolve

#: The whole internet, one block per family.
ANYWHERE = ('0.0.0.0/0', '::/0')


class CloudNetwork(Component, pulumi_type='kluster:cloud:CloudNetwork'):
    """The VCN, its gateways, its route table, and its one public subnet with its one security list."""

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
            dns_label=conventions.CLUSTER_NAME,
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

        # Stateless, because a rule that admits every packet both ways has
        # nothing to decide by a connection's state, and tracking it costs a
        # table on every VNIC in the subnet that drops new connections when
        # full.
        self.security_list = oci.core.SecurityList(
            f'{name}-security',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            display_name=f'{name}-security',
            ingress_security_rules=[
                oci.core.SecurityListIngressSecurityRuleArgs(
                    protocol='all', source=cidr, source_type='CIDR_BLOCK', stateless=True
                )
                for cidr in ANYWHERE
            ],
            egress_security_rules=[
                oci.core.SecurityListEgressSecurityRuleArgs(
                    protocol='all', destination=cidr, destination_type='CIDR_BLOCK', stateless=True
                )
                for cidr in ANYWHERE
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
            # This list alone: the default one is not in it.
            security_list_ids=[self.security_list.id],
            display_name=f'{name}-subnet',
            dns_label='nodes',
            prohibit_public_ip_on_vnic=False,
            opts=self.child_opts(),
        )

        self.register_outputs({})

    async def _object_storage_service(self) -> GetServicesServiceResult:
        """The regional Object Storage service entry a service gateway wants."""
        # Parented, which is how an invoke inherits a provider: given a parent
        # it signs with that parent's, and given neither it would fall to the
        # default one — which this program disables.
        services = await resolve(oci.core.get_services_output(opts=pulumi.InvokeOptions(parent=self)))
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
