"""The `physical` stack: everything that exists before the Kubernetes API.

OCI network and nodes, the Talos day-1 chain, the homelab worker VM, the
UDM's gw-config and firewall, and the B2 buckets — declared per
docs/declarative/physical.md. The state-backend appliance is
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

**This program is wiring** (style/pulumi.md): it reads configuration, builds
the top-level components one call each, and exports. There are no `declare_*`
wrappers — a wrapper is a second name for a component and hides it from the
reader (rfc-002 §12) — and the private helpers that remain exist because their
component needs several reads to become one constructor call. The exports are
one block at the end, because they are this stack's whole contract with `dns`,
`k8s-base` and the credential machinery.
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci

from kluster import conventions
from kluster.components.gateway import (
    CaddyService,
    Gateway,
    OverlayDaemon,
    ResolverService,
    Rootfs,
    RoutingSession,
)
from kluster.components.backup import BackupBucket
from kluster.components.cloud import CloudNetwork
from kluster.components.cloud.guardrails import Guardrails
from kluster.components.cloud.nodes import CloudNodes, NodeLoadBalancer
from kluster.components.cloud.storage import NodeVolume
from kluster.components.homelab import HomelabHost
from kluster.components.overlay import Overlay
from kluster.components.overlay.flow_rules import flow_rules
from kluster.components.talos import TalosCluster, TalosDay1
from kluster.components.talos.image import TalosImage, TalosNocloudImage
from kluster.lib import config as lib_config
from kluster.lib.versions import NAMESPACE as VERSIONS, versions
from putils import async_output

#: Talos' own API port, and the endpoint scheme the machine config expects.
KUBE_API_PORT = 6443

#: What the cloud provider is built from. The account's own identifiers — its
#: region and its tenancy OCID — are facts and live in `conventions`; these
#: three are the secrets, and they are read at the line that builds the
#: provider and nowhere else (rfc-002 §8.1, §10.3).
OCI_USER_OCID = 'ociUserOcid'
OCI_FINGERPRINT = 'ociFingerprint'
OCI_PRIVATE_KEY = 'ociPrivateKey'

#: The first-bring-up knob: a LAN address for the gateway, set only while the
#: gateway is not yet on the overlay. Absent — the steady state — every client
#: of the gateway derives its address from `conventions.overlay.UDM`. One of two
#: optional keys, and the other is its mirror image: this one is set only
#: during the ceremony, `workerGua` only after it.
GATEWAY_BOOTSTRAP_HOST = 'gatewayBootstrapHost'

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
    compartment = conventions.OCI_TENANCY.compartments[conventions.PHYSICAL]
    compartment_id = compartment.require()
    # A convention for the same reason: the OCID names the account rather than
    # authenticating to it, and the mint that issues this stack's key proves
    # the key belongs to that account instead of writing the OCID beside it
    # (credentials.md §3).
    tenancy_id = conventions.OCI_TENANCY.tenancy_ocid
    # One pin for the fleet, in the namespace every version pin shares
    # (rfc-002 §11.1). Three declarations read it — the machine configurations,
    # the cloud image imported from the factory, and the worker's own disk
    # image — and they are one version by construction.
    talos_version = versions.talos

    # The one provider this program shares, and therefore the one the stack
    # program builds (rfc-002 §8.1): six components declare against this
    # account — the network, the balancer, the nodes, the guardrails, the block
    # volumes and the image import — and a provider built inside any of them
    # would be reached into by the rest. Its region and its tenancy come from
    # `conventions` because both are permanent properties of the account, so
    # what is read here is exactly the secrets.
    cloud = oci.Provider(
        f'{conventions.CLUSTER_NAME}-oci',
        region=conventions.OCI_TENANCY.region,
        tenancy_ocid=tenancy_id,
        user_ocid=config.require_secret(OCI_USER_OCID),
        fingerprint=config.require_secret(OCI_FINGERPRINT),
        private_key=config.require_secret(OCI_PRIVATE_KEY),
    )
    # Set on each top-level component that declares against the account, and
    # inherited from there by everything below it: nothing under these names
    # the provider, and no component takes it as an argument.
    on_cloud = pulumi.ResourceOptions(providers=[cloud])

    network = CloudNetwork(conventions.CLUSTER_NAME, compartment_id=compartment_id, opts=on_cloud)
    image = TalosImage(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        talos_version=talos_version,
        opts=on_cloud,
    )
    # The worker's own artefact: the same Talos version, a schematic of its
    # own (x86, and the i915 firmware the GPU cutover wants present from day
    # 0), and a disk image on this machine rather than in a cloud catalogue.
    # No cloud provider on it: nothing it declares reaches the account.
    worker_image = TalosNocloudImage(f'{conventions.CLUSTER_NAME}-worker', talos_version=talos_version)

    load_balancer = NodeLoadBalancer(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        subnet_id=network.subnet.id,
        opts=on_cloud,
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
        placements=async_output(lambda: _placements(compartment_id, cloud)),
        dedicated_vip_node=conventions.DEDICATED_VIP_NODE,
        load_balancer=load_balancer,
        opts=on_cloud,
    )

    # §2: the tail of the Talos chain, and the two credentials it produces. The
    # cloud nodes read their configuration from instance metadata at first boot
    # and the worker reads it from a seed image; this is everything after that.
    # Subsequent configuration changes go over the machine API one node at a
    # time, the first control plane bootstraps etcd, and the kubeconfig is
    # released only once the cluster reports healthy.
    #
    # Which address reaches which machine is two answers, because apid routes
    # by the node a call names rather than by the connection it arrives on:
    #
    # -   The cloud nodes are dialled at their own public addresses. A call
    #     with no cluster to route through has nothing else to use — bootstrap
    #     is the first contact of all — and a balancer would pick whichever
    #     backend it liked.
    # -   The worker is named by its cluster-VLAN address and dialled at the
    #     cluster endpoint, which the balancer forwards on 50000. Whichever
    #     control plane answers proxies the call to the worker over KubeSpan,
    #     so the backend the balancer chose does not matter and nothing outside
    #     the site needs a route to the worker's LAN address.
    #
    # The worker's *first* configuration is not applied at all — it rides the
    # seed image the VM boots from — so the proxy path is needed only once the
    # node is in the mesh, which is where a change to a running worker finds it.
    day1 = TalosDay1(
        conventions.CLUSTER_NAME,
        cluster=cluster,
        addresses={
            **{node: instance.public_ip for node, instance in nodes.instances.items()},
            conventions.HOMELAB_NODE: str(conventions.HOMELAB_NODE_IPV4),
        },
        endpoints={conventions.HOMELAB_NODE: load_balancer.address},
        # The dedicated VIP, which the node holding it does not otherwise
        # answer for: OCI assigns the address to the VNIC and leaves the guest
        # alone.
        secondary_addresses={conventions.DEDICATED_VIP_NODE: nodes.secondary_ip.ip_address},
    )

    # §1: one volume per entry of the census, attached to the node that entry
    # names — for the following volume, whichever node holds the dedicated VIP.
    # Both facts about placement are read off the instance rather than
    # restated: a volume attaches only within its own availability domain, and
    # the node's domain is itself a regional fact chosen at apply time
    # (`_placements`).
    for volume_name, volume in sorted(conventions.NODE_VOLUMES.items()):
        NodeVolume(
            f'{conventions.CLUSTER_NAME}-{volume_name}',
            compartment_id=compartment_id,
            availability_domain=nodes.instances[volume.attached_node].availability_domain,
            instance_id=nodes.instances[volume.attached_node].id,
            size_gb=volume.size_gb,
            opts=on_cloud,
        )

    # §5: the backup bucket, on the other account deliberately — a backup kept
    # at the provider whose loss it insures is not a backup, and the
    # version-retention rule beside it is what makes a deletion by automation
    # recoverable. It builds its own provider and reads its own key pair
    # (rfc-002 §8.1); its region is a property of that account rather than a
    # setting, so it comes from `conventions` like the cloud account's.
    backup = BackupBucket(
        conventions.CLUSTER_NAME,
        region=conventions.B2_ACCOUNT.region,
        bucket_name=conventions.BUCKET_BACKUP,
    )

    # §1: the spend limits. Compartment quotas that refuse to create anything
    # outside the free envelope, and a budget whose alerts arrive before a bill
    # does. The quota is the load-bearing half: an alert tells you afterwards.
    #
    # The compartment is passed twice on purpose. A budget targets it by OCID,
    # while quota statements have no OCID form at all and name it by name —
    # which is why the name is a convention this program decides rather than
    # something read back from the tenancy.
    Guardrails(
        conventions.CLUSTER_NAME,
        tenancy_id=tenancy_id,
        compartment_id=compartment_id,
        compartment_name=compartment.name,
        recipients=lib_config.strings(config.require_object('budgetAlertRecipients'), 'budgetAlertRecipients'),
        opts=on_cloud,
    )

    # §3: the worker VM under libvirt. No endpoint and no credential among the
    # arguments: the libvirt session is the component's own, so it builds its
    # provider and reads the key that opens it (rfc-002 §8.1). The
    # home-automation domain on the same host is declared nowhere here
    # (rfc-002 §13).
    HomelabHost(
        conventions.CLUSTER_NAME,
        cluster=cluster,
        storage_dir=conventions.HOMELAB_STORAGE_DIR,
        bridge=conventions.HOMELAB_BRIDGE,
        vcpus=conventions.HOMELAB_VCPUS,
        memory_gib=conventions.HOMELAB_MEMORY_GIB,
        image_path=worker_image.path,
    )

    # §4: the gateway and the overlay it is the site's member of. Two top-level
    # components, because the overlay is a network several machines belong to
    # and the gateway is one of them: what may join and what the rules are is
    # not the gateway's business (rfc-002 §6). The gateway's own outputs are on
    # the device, so nothing downstream reads a handle to it.
    _gateway(config)
    overlay = Overlay(
        conventions.CLUSTER_NAME,
        network_id=conventions.overlay.NETWORK_ID,
        # Composed here rather than inside the component, because what a run
        # may reach is a fact about how continuous integration gets to this
        # site rather than one about the network (rfc-002 §6).
        #
        # A run reaches the two resolvers at their LAN addresses on the
        # container VLAN, not at overlay addresses, because they have none:
        # they are containers on the device, not members of the overlay. The
        # packets are routed by the gateway, and a routed packet still carries
        # the destination it had before the forward, so the rule matches the
        # LAN address.
        flow_rules=flow_rules(
            gateway_overlay_address=conventions.overlay.UDM,
            homelab_overlay_address=conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address,
            resolver_site_addresses=[service.address for service in conventions.gateway.RESOLVERS],
        ),
    )

    ##
    ## The exports: everything another stack or the credential machinery reads
    ## out of this one (rfc-002 §12).
    ##

    # Machine facts only: the downstream stacks read addresses and ids, never
    # conventions — those they share as code.
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

    # Both are cluster-admin credentials, and both are marked secret at the
    # source; `k8s-base` and `apps` read them from here rather than from a file
    # anybody has to hold.
    pulumi.export('kubeconfig', day1.kubeconfig)
    pulumi.export('talosconfig', day1.talosconfig)

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

    # The key material of the two identities generated above, which is how a
    # continuous-integration job joins the overlay at all: the value is the
    # member's `identity.secret`, written to that path on the runner before
    # the daemon starts (`.github/actions/zerotier`). It leaves as a secret,
    # because an export that lost the marking would print a join credential
    # into a deployment log, and it is read back out of state — with
    # `--show-secrets` — by `credentials derived sync`.
    for member, output in CI_IDENTITY_OUTPUTS.items():
        pulumi.export(output, pulumi.Output.secret(overlay.identities[member].private_key))


def _gateway(config: pulumi.Config) -> Gateway:
    """The device and the controller behind it, from the four values it is told.

    Three credentials authorize three different things — the device's own
    desired state over SSH, the controller's firewall over its API, the
    overlay's configuration over ZeroTier Central's — and no one of them
    implies the others. **None of the three is read here.** The controller's
    API key and the overlay's administration token each configure a provider
    and nothing else, so each is read at the line that builds that provider,
    inside the component that owns the connection (rfc-002 §8.1). The device's
    SSH credential is the same rule for a provider with no such line: a dynamic
    provider reads its configuration in `configure`, in its own process, so
    `gatewayPrivateKey` is read there and by nothing else (§7.4).

    What is left is a knob, two secrets a file's content is rendered from, and
    one measurement. Where the device answers and which key it must present are
    not among them: the pin is a decision and lives in `conventions`, and the
    address derives from the roster unless the ceremony below overrides it.

    **`gatewayBootstrapHost` is the first-bring-up knob**, and it answers one
    question: where does the device answer today. Both doors dial that address
    instead of the overlay one, because the daemon that answers at
    `conventions.overlay.UDM` is one of the container services this run is
    delivering, so until the delivery has happened the overlay address answers
    nothing. Absent, which is the steady state, both derive from the roster's
    address for the gateway. Whether the gateway is a member at all is a
    separate question with a separate answer: the roster carries an entry for
    it or it does not, and the ceremony that gets from one to the other is
    physical/gateway.md §2.5.
    """
    return Gateway(
        conventions.CLUSTER_NAME,
        host=config.get(GATEWAY_BOOTSTRAP_HOST) or str(conventions.overlay.UDM),
        # One declaration per census entry, each holding the entry itself: the
        # gateway knows what a service's image is pinned at and what secret it
        # reads, and `conventions` knows the rest (rfc-002 §5.3).
        caddy=CaddyService(
            service=conventions.gateway.CADDY,
            pin=_rootfs(conventions.gateway.CADDY),
            # The gateway buys its own certificates with this, and nothing else
            # on the device reads it: its TLS has to keep renewing while the
            # cluster — and the cluster's issuer — is down.
            acme_token=config.require_secret('gatewayAcmeToken'),
        ),
        resolvers=tuple(
            ResolverService(service=service, pin=_rootfs(service)) for service in conventions.gateway.RESOLVERS
        ),
        overlay_daemon=OverlayDaemon(
            service=conventions.gateway.OVERLAY,
            pin=_rootfs(conventions.gateway.OVERLAY),
        ),
        routing=RoutingSession(
            neighbour=conventions.HOMELAB_NODE_IPV4,
            password=config.require_secret('gatewayBgpPassword'),
        ),
        site=conventions.gateway.UNIFI_SITE,
        # Optional, and absent on the first apply of all: the worker's global
        # address is a SLAAC address formed from the router advertisement of
        # the VLAN this same call declares, so it comes into being one boot
        # after this run rather than before it. Absent, the pinhole is not
        # declared and the worker's IPv6 is outbound-only — the degraded stage
        # the design already accepts (physical/gateway.md §4.2) — and the
        # bring-up ceremony sets the key once the address can be read off the
        # advertisement (physical/gateway.md §2.5).
        worker_gua=config.get('workerGua'),
    )


def _rootfs(service: conventions.gateway.ContainerService) -> Rootfs:
    """The root filesystem one service boots, from the pin that selects it.

    The pin is a whole image reference (`versions:image-gateway-…`), because
    that is what an image is named by (rfc-002 §11.1). What `conventions` owns
    is not the reference but an opinion about it: the census says which build a
    service runs, and one repository publishes that build, so a pin naming a
    different one is refused here by key. That is what keeps the two resolvers
    on one image — a state two independent references could otherwise reach —
    while leaving each of them a pin that can move on its own.
    """
    key = conventions.gateway.image_pin(service)
    pin = versions.image[key]
    published = conventions.gateway.image_repository(service.artifact)
    if pin.repository != published:
        raise ValueError(
            f'{VERSIONS}:image-{key} pins {pin.repository}, but {service.name} runs the '
            f'{service.artifact} build, which is published as {published}'
        )
    return Rootfs(repository=pin.repository, tag=pin.tag, digest=pin.digest)


async def _placements(compartment_id: str, cloud: oci.Provider) -> list[tuple[str, str]]:
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
    # These two calls are the stack program's own, made outside any component,
    # so there is no parent to inherit a provider from: they name it (rfc-002
    # §8.1). With default providers disabled a call that forgot would fail
    # rather than sign as nobody.
    signed = pulumi.InvokeOptions(provider=cloud)
    domains = await oci.identity.get_availability_domains_output(
        compartment_id=compartment_id,
        opts=signed,
    ).future()
    assert domains is not None
    availability = [str(domain.name) for domain in domains.availability_domains]

    faults: list[list[str]] = []
    for name in availability:
        found = await oci.identity.get_fault_domains_output(
            compartment_id=compartment_id,
            availability_domain=name,
            opts=signed,
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
