"""The `physical` stack: everything that exists before the Kubernetes API.

OCI network and nodes, the Talos day-1 chain, the homelab worker VM and the
adopted HAOS domain, the UDM's gw-config and firewall, and the B2 buckets —
declared per docs/declarative/physical.md. The state-backend appliance is
deliberately *not* here: it is this program's own prerequisite
(docs/physical/state-backend.md).

Order is dictated by the endpoint. A node's machine configuration names the
cluster endpoint, which is the load balancer's address, so the balancer is
declared before the configuration that names it and before the nodes that
carry that configuration; the backends pointing back at those nodes come
last.
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci

from kluster import conventions
from kluster.physical.cloud import CloudNetwork
from kluster.physical.image import TalosImage
from kluster.physical.nodes import CloudNodes, NodeLoadBalancer
from kluster.physical.talos import TalosCluster
from putils import async_output

#: Talos' own API port, and the endpoint scheme the machine config expects.
KUBE_API_PORT = 6443


async def main() -> None:
    config = pulumi.Config()
    compartment_id = config.require('compartmentId')
    talos_version = config.require('talosVersion')

    network = CloudNetwork(conventions.CLUSTER_NAME, compartment_id=compartment_id)
    image = TalosImage(conventions.CLUSTER_NAME, compartment_id=compartment_id, talos_version=talos_version)

    load_balancer = NodeLoadBalancer(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        subnet_id=network.subnet.id,
    )

    cluster = TalosCluster(
        conventions.CLUSTER_NAME,
        cluster_name=conventions.CLUSTER_NAME,
        endpoint=load_balancer.address.apply(lambda address: f'https://{address}:{KUBE_API_PORT}'),
        cert_sans=[load_balancer.address],
        control_plane_nodes=conventions.CLOUD_NODES,
        talos_version=talos_version,
    )

    availability_domain = async_output(lambda: _first_availability_domain(compartment_id))
    fault_domains = async_output(lambda: _fault_domains(compartment_id))

    nodes = CloudNodes(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        subnet_id=network.subnet.id,
        image_id=image.image.id,
        machine_configs=cluster.machine_configs,
        ocpus=conventions.NODE_OCPUS,
        memory_gb=conventions.NODE_MEMORY_GB,
        boot_volume_gb=conventions.NODE_BOOT_VOLUME_GB,
        fault_domains=fault_domains,
        availability_domain=availability_domain,
        augmented=conventions.AUGMENTED_NODE,
        load_balancer=load_balancer,
    )

    # Machine facts only: the downstream stacks read addresses and ids, never
    # conventions — those they share as code.
    pulumi.export('cluster_endpoint', load_balancer.address)
    pulumi.export('vip1', nodes.reserved_ip.ip_address)
    pulumi.export('node_private_ips', {node: instance.private_ip for node, instance in nodes.instances.items()})
    pulumi.export('node_public_ips', {node: instance.public_ip for node, instance in nodes.instances.items()})


async def _first_availability_domain(compartment_id: str) -> str:
    domains = await oci.identity.get_availability_domains_output(compartment_id=compartment_id).future()
    assert domains is not None
    return domains.availability_domains[0].name


async def _fault_domains(compartment_id: str) -> list[str]:
    """Every fault domain of the availability domain the fleet lives in.

    Read rather than hard-coded: the count is a regional fact, and the nodes
    are spread across whatever is actually offered.
    """
    availability_domain = await _first_availability_domain(compartment_id)
    domains = await oci.identity.get_fault_domains_output(
        compartment_id=compartment_id,
        availability_domain=availability_domain,
    ).future()
    assert domains is not None
    return [str(domain.name) for domain in domains.fault_domains]
