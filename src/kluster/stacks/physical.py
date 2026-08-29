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

**Every domain of the design appears below, and every one of them is
written.** The stack is the inventory: a domain with no implementation would
still be called here and would refuse by naming itself, so what is missing is
visible in the program rather than only in a tracker. Nothing is missing
today, and a run therefore goes all the way through.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import pulumi
import pulumi_oci as oci

from kluster import conventions
from kluster.components import gateway, homelab
from kluster.components.backup import BackupBucket
from kluster.components.cloud import CloudNetwork
from kluster.components.cloud.guardrails import Guardrails
from kluster.components.cloud.nodes import CloudNodes, NodeLoadBalancer
from kluster.components.cloud.storage import NodeVolume
from kluster.components.gateway import estate as gw_estate
from kluster.components.talos import TalosCluster, TalosDay1
from kluster.components.talos.image import TalosImage, TalosNocloudImage
from putils import async_output

#: Talos' own API port, and the endpoint scheme the machine config expects.
KUBE_API_PORT = 6443

#: The tenancy the OCI credential signs for, read out of the provider's own
#: configuration rather than restated as a key of this program's. It is the
#: account the key belongs to, written beside the key by the mint that issued
#: it (credentials.md §3), and the two guardrail resources need it because both
#: are tenancy-level objects that *name* a compartment rather than living in
#: one: the quota policy and the budget.
OCI_NAMESPACE = 'oci'
OCI_TENANCY_KEY = 'tenancyOcid'

#: The first-bring-up knob: a LAN address for the gateway, set only while the
#: gateway is not yet on the overlay. Absent — the steady state — every client
#: of the gateway derives its address from `conventions.ZT_UDM`. One of two
#: optional keys, and the other is its mirror image: this one is set only
#: during the ceremony, `workerGua` only after it.
GATEWAY_BOOTSTRAP_HOST = 'gatewayBootstrapHost'

#: The client half of the libvirt session, and the only part of that session
#: this stack is configured with. Where it lands on the machine running the
#: program, and the URI that names it there, are derived
#: (`components/homelab/`).
LIBVIRT_PRIVATE_KEY = 'libvirtPrivateKey'

#: The export each continuous-integration identity's key material leaves under,
#: by the roster name of the member that carries it. The names are half of a
#: contract: `credentials derived sync` reads this stack's state by them and
#: pushes each into the `ZEROTIER_IDENTITY` secret of the Environments whose
#: jobs join with that identity (credentials.md §3, gateway.md §2.6), which is
#: one Environment set per identity domain — `physical` and `dns` — because an
#: identity live in two jobs at once flaps. A roster rename that this mapping
#: does not follow fails the run rather than quietly renaming the contract.
CI_IDENTITY_OUTPUTS = {
    'ci-physical': 'ci_zerotier_identity_physical',
    'ci-dns': 'ci_zerotier_identity_dns',
}


async def main() -> None:
    config = pulumi.Config()
    # A convention rather than a config key: the compartment is the boundary
    # this stack's own credential is confined to, decided here and created by
    # the mint that issues that credential (credentials.md §3). A stack whose
    # compartment does not exist yet refuses by naming that command.
    compartment_id = conventions.OCI_TENANCY.compartments[conventions.PHYSICAL].require()
    tenancy_id = pulumi.Config(OCI_NAMESPACE).require_secret(OCI_TENANCY_KEY)
    talos_version = config.require('talosVersion')

    network = CloudNetwork(conventions.CLUSTER_NAME, compartment_id=compartment_id)
    image = TalosImage(conventions.CLUSTER_NAME, compartment_id=compartment_id, talos_version=talos_version)
    # The worker's own artefact: the same Talos version, a schematic of its
    # own (x86, and the i915 firmware the GPU cutover wants present from day
    # 0), and a disk image on this machine rather than in a cloud catalogue.
    worker_image = TalosNocloudImage(f'{conventions.CLUSTER_NAME}-worker', talos_version=talos_version)

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
        worker_nodes=(conventions.HOMELAB_NODE,),
        talos_version=talos_version,
        # Who may open a BGP session with the worker: the gateway's leg on the
        # cluster VLAN, as a /32. Scoping the opening to that one address
        # rather than to the VLAN is what keeps every other node on it out of
        # the worker's routing table. It is derived rather than configured
        # because the VLAN and its gateway are this program's own decision
        # (`conventions`), and a second place to state it is a second place
        # for it to be wrong.
        bgp_peers={conventions.HOMELAB_NODE: f'{conventions.CLUSTER_VLAN.require_gateway()}/32'},
    )

    placements = async_output(lambda: _placements(compartment_id))

    nodes = CloudNodes(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        subnet_id=network.subnet.id,
        image_id=image.image.id,
        # The cloud half of the fleet only: the worker's configuration reaches
        # it on a seed image under libvirt, not as instance metadata.
        machine_configs={node: cluster.machine_configs[node] for node in conventions.CLOUD_NODES},
        ocpus=conventions.NODE_OCPUS,
        memory_gb=conventions.NODE_MEMORY_GB,
        boot_volume_gb=conventions.NODE_BOOT_VOLUME_GB,
        placements=placements,
        augmented=conventions.DEDICATED_VIP_NODE,
        load_balancer=load_balancer,
    )

    # Machine facts only: the downstream stacks read addresses and ids, never
    # conventions — those they share as code. The rest of the census
    # (kubeconfig, talosconfig, bucket names and endpoints) is exported by the
    # domains below as they come to exist.
    #
    # Both families of the balancer are published, because the cluster anchor
    # in `dns` carries an A and an AAAA; the VIP below is IPv4 only, and that
    # is a property of the address rather than an omission here.
    pulumi.export('cluster_endpoint', load_balancer.address)
    pulumi.export('cluster_endpoint_v6', load_balancer.address_v6)
    pulumi.export('vip1', nodes.reserved_ip.ip_address)
    pulumi.export('vip1_private', nodes.secondary_ip.ip_address)
    pulumi.export('node_private_ips', {node: instance.private_ip for node, instance in nodes.instances.items()})
    pulumi.export('node_public_ips', {node: instance.public_ip for node, instance in nodes.instances.items()})

    _declare_talos_day1(cluster=cluster, nodes=nodes, cluster_endpoint=load_balancer.address)
    _declare_storage(compartment_id=compartment_id, nodes=nodes)
    _declare_guardrails(config=config, compartment_id=compartment_id, tenancy_id=tenancy_id)

    # The two domains that are not the cloud: the host under libvirt, and the
    # gateway through the three doors it is configured by. Both are reached
    # over the overlay, whose roster is code (`conventions.ZT_ROSTER`).
    homelab.declare(
        conventions.CLUSTER_NAME,
        cluster=cluster,
        # Derived, not recorded: the address comes from the overlay roster and
        # the rest of the URI is a property of the machine running this
        # program, which is the one thing committed configuration cannot know
        # (`homelab.connection_uri`). The key is read in the clear rather than
        # as a secret Output because it is written to a file before any
        # resource exists — it reaches no resource input, and so no state.
        connection_uri=homelab.connection_uri(
            host=str(conventions.zt_member(conventions.ZT_MEMBER_HOMELAB).address),
            private_key=config.require(LIBVIRT_PRIVATE_KEY),
        ),
        storage_dir=config.require('libvirtStorageDir'),
        bridge=conventions.HOMELAB_BRIDGE,
        vcpus=conventions.HOMELAB_VCPUS,
        memory_gib=conventions.HOMELAB_MEMORY_GIB,
        image_path=worker_image.path,
        haos_domain_uuid=config.require('haosDomainUuid'),
    )
    declare_gateway(config)


def declare_gateway(config: pulumi.Config) -> None:
    """§4: the gateway, through the three doors it is configured by.

    The device's own desired state over SSH, the controller's firewall over its
    API, and the overlay's configuration over ZeroTier Central's — three
    credentials, because they authorize three different things and no one of
    them should imply the others.

    Everything read here is a site fact: what the images were built as and what
    the worker's global address turned out to be. The decisions — the service
    census and the addresses on it, the firewall census, the overlay roster and
    the rules that confine a run — are code.

    **`gatewayBootstrapHost` is the first-bring-up knob**, and it answers one
    question: where does the device answer today. Both providers that reach it
    dial that address instead of the overlay one — the estate over SSH and the
    controller over its API — because the daemon that answers at
    `conventions.ZT_UDM` is a container of the estate this run is delivering,
    so until the delivery has happened the overlay address answers nothing.
    Absent, which is the steady state, both derive from `ZT_UDM`. Whether the
    gateway is a member at all is a separate question with a separate answer:
    the roster carries an entry for it or it does not, and the ceremony that
    gets from one to the other is physical/gateway.md §2.5.

    The pinned host key is not affected either way: it is stored as a bare
    `ssh-ed25519 <blob>` line with no host name in front of it, so it matches
    the device at whichever address the session dials (`providers/device_files/ssh.py`).
    """
    gateway_host = config.get(GATEWAY_BOOTSTRAP_HOST) or str(conventions.ZT_UDM)

    gateway.declare_estate(
        conventions.CLUSTER_NAME,
        host=gateway_host,
        host_key=config.require_secret('gatewayHostKey'),
        private_key=config.require_secret('gatewayPrivateKey'),
        bgp_neighbour=conventions.HOMELAB_NODE_IPV4,
        bgp_password=config.require_secret('gatewayBgpPassword'),
        acme_token=config.require_secret('gatewayAcmeToken'),
        rootfs=gw_estate.parse_rootfs(config.require_object('gatewayRootfs')),
    )
    gateway.declare_firewall(
        conventions.CLUSTER_NAME,
        # The same address the estate's SSH goes to, for the same reason: the
        # controller answers on the gateway's own overlay address, which this
        # program assigns in the ZeroTier roster above and therefore already
        # knows (physical/gateway.md §2.3). Recording it beside the API key
        # would be a second copy of a stated constant, free to disagree with
        # the roster that decides it — which is also why the bootstrap knob
        # moves both doors at once rather than one endpoint being overridable.
        api_url=f'https://{gateway_host}',
        api_key=config.require_secret('unifiApiKey'),
        site=conventions.UNIFI_SITE,
        # Optional, and absent on the first apply of all: the worker's global
        # address is a SLAAC address formed from the router advertisement of
        # the VLAN this same call declares, so it comes into being one boot
        # after this run rather than before it. Absent, the pinhole is not
        # declared and the worker's IPv6 is outbound-only — the degraded stage
        # the design already accepts (physical/gateway.md §4.2) — and the
        # bring-up ceremony sets the key once the address can be read off the
        # advertisement (physical/gateway.md §2.5).
        worker_gua=config.get('workerGua'),
        peer_port=config.require_int('qbittorrentPeerPort'),
    )
    network = gateway.declare_zerotier(
        conventions.CLUSTER_NAME,
        api_token=config.require_secret('zerotierApiToken'),
        network_id=config.require('zerotierNetworkId'),
        adguard=[resolver.address for resolver in gw_estate.resolvers()],
    )
    # The key material of the two identities generated above, which is how a
    # continuous-integration job joins the overlay at all: the value is the
    # member's `identity.secret`, written to that path on the runner before
    # the daemon starts (`.github/actions/zerotier`). It leaves as a secret,
    # because an export that lost the marking would print a join credential
    # into a deployment log, and it is read back out of state — with
    # `--show-secrets` — by `credentials derived sync`.
    for member, output in CI_IDENTITY_OUTPUTS.items():
        pulumi.export(output, pulumi.Output.secret(network.identities[member].private_key))


@dataclass(frozen=True)
class Storage:
    """What §1 and §5 declare, returned as one handle.

    The block volumes and the bucket belong together because the rule that
    separates them is the point: the volumes live on the cloud provider beside
    the nodes, and the bucket is somewhere else on purpose.
    """

    volumes: Mapping[str, NodeVolume]
    backup: BackupBucket


def _declare_storage(*, compartment_id: str, nodes: CloudNodes) -> Storage:
    """§1 and §5: the fleet's block volumes and the backup bucket.

    One volume per entry of `conventions.NODE_VOLUMES`, attached to the node
    that entry names — for the following volume, whichever node holds the
    dedicated VIP. The bucket is on the other provider deliberately: a backup
    kept at the provider whose loss it insures is not a backup, and the
    version-retention rule beside it is what makes a deletion by automation
    recoverable.
    """
    volumes = {
        name: NodeVolume(
            f'{conventions.CLUSTER_NAME}-{name}',
            compartment_id=compartment_id,
            # Both read off the instance rather than restated: a volume
            # attaches only within its own availability domain, and the node's
            # domain is itself a regional fact chosen at apply time
            # (`_placements`).
            availability_domain=nodes.instances[volume.attached_node].availability_domain,
            instance_id=nodes.instances[volume.attached_node].id,
            size_gb=volume.size_gb,
        )
        for name, volume in sorted(conventions.NODE_VOLUMES.items())
    }

    backup = BackupBucket(
        conventions.CLUSTER_NAME,
        region=conventions.B2_ACCOUNT.region,
        bucket_name=conventions.BUCKET_BACKUP,
    )

    # Names, endpoints and credentials, because that is what a consumer of a
    # bucket is configured with (physical.md §0). The per-namespace VolSync and
    # barman keys are not here: the census of namespaces belongs to the stack
    # that declares them, and each arrives as a scope on this bucket when it
    # does. What exists without any application is the etcd snapshot key.
    pulumi.export('backup_bucket', backup.bucket_name)
    pulumi.export('backup_endpoint', backup.endpoint)
    pulumi.export(
        'backup_keys',
        {scope: {'id': backup.key_id(scope), 'secret': backup.key_secret(scope)} for scope in backup.keys},
    )

    return Storage(volumes=volumes, backup=backup)


def _declare_guardrails(*, config: pulumi.Config, compartment_id: str, tenancy_id: pulumi.Input[str]) -> Guardrails:
    """§1: the spend limits.

    Compartment quotas that refuse to create anything outside the free
    envelope, and a budget whose alerts arrive before a bill does. The quota
    is the load-bearing half: an alert tells you afterwards.

    The compartment is passed twice on purpose. A budget targets it by OCID,
    while quota statements have no OCID form at all and name it by name —
    which is why the name is a convention this program decides rather than
    something read back from the tenancy.
    """
    compartment = conventions.OCI_TENANCY.compartments[conventions.PHYSICAL]
    return Guardrails(
        conventions.CLUSTER_NAME,
        tenancy_id=tenancy_id,
        compartment_id=compartment_id,
        compartment_name=compartment.name,
        recipients=_budget_recipients(config),
    )


def _budget_recipients(config: pulumi.Config) -> tuple[str, ...]:
    """Who the budget alerts go to, as `budgetAlertRecipients`.

    A list rather than one address: a budget alert is the only signal this
    stack emits that does not go through the cluster, so it has to survive the
    cluster being the thing that is broken, and a second address is the
    cheapest form of that. An empty list is refused by the component — an
    alert with no audience is a rule that runs and tells nobody.

    Addresses are ordinary configuration and may be set with `--secret`; a
    secret value reads back through this call unchanged.
    """
    recipients = cast('object', config.require_object('budgetAlertRecipients'))
    if not isinstance(recipients, list):
        raise TypeError(f'budgetAlertRecipients must be a list of email addresses, not {recipients!r}')
    entries = cast('list[object]', recipients)
    if not all(isinstance(entry, str) for entry in entries):
        raise TypeError(f'budgetAlertRecipients must be a list of email addresses, not {recipients!r}')
    return tuple(cast('list[str]', entries))


def _declare_talos_day1(*, cluster: TalosCluster, nodes: CloudNodes, cluster_endpoint: pulumi.Input[str]) -> TalosDay1:
    """§2: the tail of the Talos chain, and the two credentials it produces.

    The cloud nodes read their configuration from instance metadata at first
    boot and the worker reads it from a seed image; this is everything after
    that. Subsequent configuration changes go over the machine API one node at
    a time, the first control plane bootstraps etcd, and the kubeconfig is
    released only once the cluster reports healthy.

    Which address reaches which machine is two answers, because apid routes by
    the node a call names rather than by the connection it arrives on:

    -   The cloud nodes are dialled at their own public addresses. A call with
        no cluster to route through has nothing else to use — bootstrap is the
        first contact of all — and a balancer would pick whichever backend it
        liked.
    -   The worker is named by its cluster-VLAN address and dialled at the
        cluster endpoint, which the balancer forwards on 50000. Whichever
        control plane answers proxies the call to the worker over KubeSpan, so
        the backend the balancer chose does not matter and nothing outside the
        site needs a route to the worker's LAN address.

    The worker's *first* configuration is not applied at all — it rides the
    seed image the VM boots from — so the proxy path is needed only once the
    node is in the mesh, which is where a change to a running worker finds it.
    """
    day1 = TalosDay1(
        conventions.CLUSTER_NAME,
        cluster=cluster,
        addresses={
            **{node: instance.public_ip for node, instance in nodes.instances.items()},
            conventions.HOMELAB_NODE: str(conventions.HOMELAB_NODE_IPV4),
        },
        endpoints={conventions.HOMELAB_NODE: cluster_endpoint},
        # The dedicated VIP, which the node holding it does not otherwise
        # answer for: OCI assigns the address to the VNIC and leaves the guest
        # alone.
        secondary_addresses={conventions.DEDICATED_VIP_NODE: nodes.secondary_ip.ip_address},
    )
    # Both are cluster-admin credentials, and both are marked secret at the
    # source; `k8s-base` and `apps` read them from here rather than from a file
    # anybody has to hold.
    pulumi.export('kubeconfig', day1.kubeconfig)
    pulumi.export('talosconfig', day1.talosconfig)
    return day1


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
