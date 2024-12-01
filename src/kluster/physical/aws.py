"""
Setups up resources on AWS.
"""

import ipaddress
import itertools
from typing import List

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx


def setup():
    """Setup with pulumi resources"""
    # Create a VPC with 1 availibility zone
    vpc = aws.ec2.Vpc(
        'kluster-vpc',
        aws.ec2.VpcArgs(
            cidr_block='10.0.0.0/16',
            assign_generated_ipv6_cidr_block=True,
            enable_dns_hostnames=True,
            enable_dns_support=True,
        ),
    )

    def get_subnets(
        cidr: ipaddress.IPv4Network | ipaddress.IPv6Network,
    ) -> List[str]:
        return [s.with_prefixlen for s in itertools.islice(cidr.subnets(new_prefix=64), 3)]

    subnet_cidrs = vpc.ipv6_cidr_block.apply(ipaddress.ip_network).apply(get_subnets)

    azs = aws.get_availability_zones(state='available').names[:1]
    for idx, az in enumerate(azs):
        subnet = aws.ec2.Subnet(
            f'kluster-vpc-subnet-{idx}',
            aws.ec2.SubnetArgs(
                vpc_id=vpc.id,
                availability_zone=az,
                map_public_ip_on_launch=False,
                assign_ipv6_address_on_creation=True,
                enable_resource_name_dns_aaaa_record_on_launch=True,
                ipv6_cidr_block=subnet_cidrs[0],
                ipv6_native=True,
                private_dns_hostname_type_on_launch='resource-name',
            ),
        )

    pulumi.export('vpcId', vpc.id)
