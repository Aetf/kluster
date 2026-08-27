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

**Every domain of the design appears below, implemented or not.** A domain
that has no implementation yet is still called, and says so by raising with
its own name: the stack is the inventory, so what is missing is visible in
the program rather than only in a tracker. This is why a run of the stack
currently stops partway — deliberately, and at a named place.
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci

from kluster import conventions, gateway
from kluster.gateway import estate as gw_estate
from kluster.gateway import zerotier as gw_zerotier
from kluster.physical import homelab
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

    placements = async_output(lambda: _placements(compartment_id))

    nodes = CloudNodes(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        subnet_id=network.subnet.id,
        image_id=image.image.id,
        machine_configs=cluster.machine_configs,
        ocpus=conventions.NODE_OCPUS,
        memory_gb=conventions.NODE_MEMORY_GB,
        boot_volume_gb=conventions.NODE_BOOT_VOLUME_GB,
        placements=placements,
        augmented=conventions.AUGMENTED_NODE,
        load_balancer=load_balancer,
    )

    # Machine facts only: the downstream stacks read addresses and ids, never
    # conventions — those they share as code. The rest of the census
    # (kubeconfig, talosconfig, bucket names and endpoints) is exported by the
    # domains below as they come to exist.
    pulumi.export('cluster_endpoint', load_balancer.address)
    pulumi.export('vip1', nodes.reserved_ip.ip_address)
    pulumi.export('vip1_private', nodes.secondary_ip.ip_address)
    pulumi.export('node_private_ips', {node: instance.private_ip for node, instance in nodes.instances.items()})
    pulumi.export('node_public_ips', {node: instance.public_ip for node, instance in nodes.instances.items()})

    # The rest of the design, written or not. A domain with no implementation
    # is still called and refuses by name; the first one reached ends the run,
    # which is why the domains below it are unreachable today rather than
    # absent.
    _declare_storage(compartment_id=compartment_id, nodes=nodes)
    _declare_guardrails(compartment_id=compartment_id)
    _declare_talos_day1(cluster=cluster, nodes=nodes)
    homelab.declare(
        conventions.CLUSTER_NAME,
        cluster=cluster,
        connection_uri=config.require('libvirtUri'),
        storage_dir=config.require('libvirtStorageDir'),
        bridge=conventions.HOMELAB_BRIDGE,
        vcpus=conventions.HOMELAB_VCPUS,
        memory_gib=conventions.HOMELAB_MEMORY_GIB,
        disk_gb=conventions.HOMELAB_DISK_GB,
        haos_domain_uuid=config.require('haosDomainUuid'),
    )
    declare_gateway(config)


def declare_gateway(config: pulumi.Config) -> None:
    """§4: the gateway, through the three doors it is configured by.

    The device's own desired state over SSH, the controller's firewall over its
    API, and the overlay's configuration over ZeroTier Central's — three
    credentials, because they authorize three different things and no one of
    them should imply the others.

    Everything read here is a site fact: what the images were built as, where
    the resolvers sit, which nodes are on the overlay. The decisions — the
    estate's shape, the firewall census, the roster's roles and the rules that
    confine a run — are code, and the configuration is checked against them.
    """
    addresses = gw_estate.parse_addresses(config.require_object('gatewayAddresses'))
    resolvers = [addresses[instance] for instance in sorted(gw_estate.VHOST_ADGUARD)]

    gateway.declare_estate(
        conventions.CLUSTER_NAME,
        host=str(conventions.ZT_UDM),
        host_key=config.require_secret('gatewayHostKey'),
        private_key=config.require_secret('gatewayPrivateKey'),
        bgp_neighbour=conventions.HOMELAB_NODE_IPV4,
        bgp_password=config.require_secret('gatewayBgpPassword'),
        acme_token=config.require_secret('gatewayAcmeToken'),
        rootfs=gw_estate.parse_rootfs(config.require_object('gatewayRootfs')),
        addresses=addresses,
    )
    gateway.declare_firewall(
        conventions.CLUSTER_NAME,
        api_url=config.require('unifiApiUrl'),
        api_key=config.require_secret('unifiApiKey'),
        site=conventions.UNIFI_SITE,
        worker_gua=config.require('workerGua'),
        peer_port=config.require_int('qbittorrentPeerPort'),
    )
    gateway.declare_zerotier(
        conventions.CLUSTER_NAME,
        api_token=config.require_secret('zerotierApiToken'),
        network_id=config.require('zerotierNetworkId'),
        members=gw_zerotier.parse_members(config.require_object('zerotierMembers')),
        adguard=resolvers,
    )


def _declare_storage(*, compartment_id: str, nodes: CloudNodes) -> None:
    """§1 and §5: the block volume and both object buckets.

    The augmented node's block volume, the chunk bucket that sits in-region
    with the cloud nodes, and the backup bucket that deliberately does not —
    a backup kept at the provider whose loss it insures is not a backup —
    together with their scoped keys and the version-retention rule that makes
    a deletion by automation recoverable.
    """
    raise NotImplementedError(
        'physical §1/§5 storage: the block volume, the chunk bucket, the backup bucket and '
        'their keys are not declared yet — kluster-ops#27, docs/declarative/physical.md §1 '
        'and §5, docs/cluster/storage.md §4'
    )


def _declare_guardrails(*, compartment_id: str) -> None:
    """§1: the spend limits.

    Compartment quotas that refuse to create anything outside the free
    envelope, and a budget whose alerts arrive before a bill does. The quota
    is the load-bearing half: an alert tells you afterwards.
    """
    raise NotImplementedError(
        'physical §1 guardrails: compartment quotas and the budget alert rules are not '
        'declared yet — kluster-ops#27, docs/declarative/physical.md §1, '
        'docs/cluster/nodes.md §3.2'
    )


def _declare_talos_day1(*, cluster: TalosCluster, nodes: CloudNodes) -> None:
    """§2: the tail of the Talos chain.

    Secrets and per-node configuration already stand, and the cloud nodes read
    that configuration from instance metadata at first boot. What is missing
    is everything after first boot: applying subsequent configuration changes
    over the machine API, bootstrapping etcd on one node, gating on cluster
    health, and surfacing the two credentials the later stacks are built on.
    """
    raise NotImplementedError(
        'physical §2 Talos day-1: configuration apply, bootstrap, the health gate and the '
        'kubeconfig/talosconfig outputs are not declared yet — kluster-ops#26, '
        'docs/declarative/physical.md §2'
    )


async def _placements(compartment_id: str) -> list[tuple[str, str]]:
    """(availability domain, fault domain) pairs, in the order nodes take them.

    Availability domain first, fault domain only as the tiebreak. An AD is an
    independent failure domain where a fault domain is a rack, but the reason
    this is not merely nicer is capacity: A1 capacity is per-AD, so a fleet
    packed into one AD draws its replacements from a single pool -- and
    "replace at leisure" after losing a node (nodes.md §5, tier 3) assumes a
    pool that has something in it.

    Both lists are regional facts read at apply time. A region offering one AD
    degrades to plain fault-domain spread, which is what this used to do
    unconditionally.
    """
    domains = await oci.identity.get_availability_domains_output(compartment_id=compartment_id).future()
    assert domains is not None
    availability = [str(domain.name) for domain in domains.availability_domains]

    faults: list[list[str]] = []
    for name in availability:
        found = await oci.identity.get_fault_domains_output(
            compartment_id=compartment_id,
            availability_domain=name,
        ).future()
        assert found is not None
        faults.append([str(domain.name) for domain in found.fault_domains])

    # Column-major: every AD is used once before any AD is used twice.
    depth = max(len(domains) for domains in faults)
    return [
        (name, faults[position][level % len(faults[position])])
        for level in range(depth)
        for position, name in enumerate(availability)
    ]
