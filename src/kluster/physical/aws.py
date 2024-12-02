"""
Setups up resources on AWS.
"""

import ipaddress
import itertools
from typing import List

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx


def create_subnet(
    name: str, vpc: aws.ec2.Vpc, az: str, ipv6_cidr: pulumi.Input[str], gw: pulumi.Input[str] | None = None
) -> tuple[aws.ec2.Subnet, aws.ec2.RouteTable]:
    subnet = aws.ec2.Subnet(
        name,
        vpc_id=vpc.id,
        availability_zone=az,
        map_public_ip_on_launch=False,
        assign_ipv6_address_on_creation=True,
        enable_resource_name_dns_aaaa_record_on_launch=True,
        ipv6_cidr_block=ipv6_cidr,
        ipv6_native=True,
        private_dns_hostname_type_on_launch='resource-name',
        opts=pulumi.ResourceOptions(parent=vpc),
    )
    rtb = aws.ec2.RouteTable(name, vpc_id=vpc.id, opts=pulumi.ResourceOptions(parent=subnet))
    aws.ec2.RouteTableAssociation(
        name, route_table_id=rtb.id, subnet_id=subnet.id, opts=pulumi.ResourceOptions(parent=rtb)
    )
    return subnet, rtb


def setup_networks():
    """Setup networks and vpc"""
    # Create a VPC with 1 availibility zone
    vpc = aws.ec2.Vpc(
        'kluster-vpc',
        cidr_block='10.0.0.0/16',
        assign_generated_ipv6_cidr_block=True,
        enable_dns_hostnames=True,
        enable_dns_support=True,
    )

    dhcp_options = aws.ec2.VpcDhcpOptions(
        'kluster-vpc',
        domain_name_servers=['2a01:4f8:c2c:123f::1', '2a00:1098:2b::1', '2a00:1098:2c::1'],
        opts=pulumi.ResourceOptions(parent=vpc),
    )
    aws.ec2.VpcDhcpOptionsAssociation(
        'kluster-vpc', vpc_id=vpc.id, dhcp_options_id=dhcp_options.id, opts=pulumi.ResourceOptions(parent=dhcp_options)
    )

    def get_subnets(
        cidr: ipaddress.IPv4Network | ipaddress.IPv6Network,
    ) -> List[str]:
        return [s.with_prefixlen for s in itertools.islice(cidr.subnets(new_prefix=64), 3)]

    subnet_cidrs = vpc.ipv6_cidr_block.apply(ipaddress.ip_network).apply(get_subnets)

    az = aws.get_availability_zones(state='available').names[0]
    control_subnet, control_rtb = create_subnet('kluster-vpc-ctrl', vpc, az, subnet_cidrs[0])
    work_subnet, work_rtb = create_subnet('kluster-vpc-work', vpc, az, subnet_cidrs[1])

    igw = aws.ec2.InternetGateway('kluster-vpc', vpc_id=vpc.id, opts=pulumi.ResourceOptions(parent=vpc))
    aws.ec2.Route(
        'kluster-vpc-ctrl',
        route_table_id=control_rtb.id,
        gateway_id=igw.id,
        destination_ipv6_cidr_block='::/0',
        opts=pulumi.ResourceOptions(parent=igw),
    )

    eigw = aws.ec2.EgressOnlyInternetGateway('kluster-vpc', vpc_id=vpc.id, opts=pulumi.ResourceOptions(parent=vpc))
    aws.ec2.Route(
        'kluster-vpc-work',
        route_table_id=work_rtb.id,
        egress_only_gateway_id=eigw.id,
        destination_ipv6_cidr_block='::/0',
        opts=pulumi.ResourceOptions(parent=eigw),
    )

    aws.ec2.VpcEndpoint(
        'kluster-vpc-s3',
        vpc_id=vpc.id,
        service_name='com.amazonaws.us-west-2.s3',
        route_table_ids=[control_rtb.id, work_rtb.id],
        auto_accept=True,
        opts=pulumi.ResourceOptions(parent=vpc),
    )

    pulumi.export('vpcId', vpc.id)


def setup_vms():
    pass


def setup():
    setup_networks()
    setup_vms()
