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

The subnet's list narrows IPv6 alone. A subnet that names no security list
carries the VCN's default one, whose rules are OCI's choice, so the subnet
names one list of its own. It admits every IPv4 protocol from anywhere, IPv6
TCP and UDP from anywhere, and the ICMPv6 below; it lets every protocol out in
both families; and every rule is stateless. The filter on ports is the node's:
Talos' ingress firewall decides every host-network port (physical.md §1–2),
and what Cilium's datapath answers ahead of it is the raw TCP/UDP Services the
cluster declares, while no Service allocates a `NodePort` (cluster-infra.md
§2).

ICMPv6 is the exception because the node cannot bound it. Talos admits every
ICMPv6 packet through one rate allowance shared by every source, and
neighbor discovery needs that allowance too, so a flood from the internet at a
node's public IPv6 address would cut the node's IPv6 (security-audit.md M3,
rfc-005 §9.2). The list therefore admits from the internet only the ICMPv6
types a node must hear from it — the error messages and echo, so ping still
answers — and every ICMPv6 type from link-local sources, which no sender off
the node's own link has, and from the subnet's own IPv6 block, which the
subnet's instances send from. Nothing OCI documents stops a sender on the
internet from forging a source in that block, since the source/destination
check asks only whether the VNIC is a packet's source or destination, so the
rule admits such a packet as well. A flood therefore still reaches the
allowance: of echo or of the error types from anywhere — an error comes from
whichever router is on the path, and echo cannot be scoped to the operator's
address, which is dynamic — or of any type from a source forged into the
subnet's block. That residual is accepted (security-audit.md M3): it gives a
sender on the internet nothing an echo flood does not already give it.
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci
from pulumi_oci.core.outputs import GetServicesServiceResult

from kluster import conventions
from putils import Component, async_output, resolve

#: The whole internet, one block per family.
ANYWHERE = ('0.0.0.0/0', '::/0')
ANYWHERE_V4, ANYWHERE_V6 = ANYWHERE

#: IPv6 link-local addresses, which a router never forwards (RFC 4291
#: §2.5.6): a packet from one was sent on the node's own link. The VCN's
#: virtual router speaks to a node from one — its router advertisements, which
#: carry the node's IPv6 default route, and neighbor discovery — and OCI
#: documents no exemption for IPv6 traffic (its one documented exemption is
#: IPv4's 169.254.0.0/16), so the list admits all ICMPv6 from this block.
LINK_LOCAL_V6 = 'fe80::/10'

#: IANA protocol numbers, the form a security rule names a protocol in.
TCP, UDP, ICMPV6 = '6', '17', '58'

#: The ICMPv6 types the internet may send a node (RFC 4443): the four error
#: messages — destination unreachable, packet too big (path MTU discovery,
#: which a stateless list must admit for TCP to cross a smaller link), time
#: exceeded, parameter problem — and echo request and reply, so the node
#: answers ping and hears the answers to its own. Every other type, neighbor
#: discovery's included, is admitted from `LINK_LOCAL_V6` and from the subnet's
#: own IPv6 block alone. Widening this is what M3 of security-audit.md weighs.
ICMPV6_FROM_ANYWHERE = (1, 2, 3, 4, 128, 129)


def ingress_rules(subnet_ipv6: pulumi.Input[str]) -> list[oci.core.SecurityListIngressSecurityRuleArgs]:
    """What the subnet admits: all of IPv4, and IPv6's TCP, UDP and the ICMPv6 a node needs.

    A security list only admits, and admits a packet any one rule matches,
    so ICMPv6 is narrowed by naming the other IPv6 protocols the nodes serve
    and each ICMPv6 type the internet may send, one rule per type.

    `subnet_ipv6` is the subnet's own IPv6 block, from which every ICMPv6 type
    is admitted. A node resolving another node's global address on the subnet
    solicits from its own global address — Linux sources a neighbor
    solicitation from the packet that prompted it. OCI does not document
    whether it carries neighbor discovery between instances through the list;
    where it does, that solicitation arrives from the block rather than from
    `LINK_LOCAL_V6`.
    """

    def rule(
        protocol: str, source: pulumi.Input[str], icmp_type: int | None = None
    ) -> oci.core.SecurityListIngressSecurityRuleArgs:
        return oci.core.SecurityListIngressSecurityRuleArgs(
            protocol=protocol,
            source=source,
            source_type='CIDR_BLOCK',
            stateless=True,
            icmp_options=None
            if icmp_type is None
            else oci.core.SecurityListIngressSecurityRuleIcmpOptionsArgs(type=icmp_type),
        )

    return [
        rule('all', ANYWHERE_V4),
        rule(TCP, ANYWHERE_V6),
        rule(UDP, ANYWHERE_V6),
        *(rule(ICMPV6, ANYWHERE_V6, icmp_type) for icmp_type in ICMPV6_FROM_ANYWHERE),
        rule(ICMPV6, LINK_LOCAL_V6),
        rule(ICMPV6, subnet_ipv6),
    ]


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

        # The subnet's IPv6 block, which the list admits ICMPv6 from as well as
        # the subnet carrying it. OCI assigns the VCN's prefix at apply time, so
        # the block is an output derived from it, and the list cannot read it
        # off the subnet, which names the list and so is created after it.
        self.subnet_ipv6 = async_output(self._subnet_ipv6_cidr)

        # Stateless, because a rule that decides by protocol, type and source
        # has nothing to decide by a connection's state, and tracking it
        # costs a table on every VNIC in the subnet that drops new
        # connections when full. Egress is never narrowed, so a node's own
        # connections leave freely; their answers come back as TCP, UDP or
        # an ICMPv6 type the ingress rules admit.
        self.security_list = oci.core.SecurityList(
            f'{name}-security',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            display_name=f'{name}-security',
            ingress_security_rules=ingress_rules(self.subnet_ipv6),
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
            ipv6cidr_block=self.subnet_ipv6,
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
