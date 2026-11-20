"""The `physical` stack: everything that exists before the Kubernetes API.

OCI network and nodes, the Talos day-1 chain, the homelab worker VM, the
UDM's device files and firewall, and the B2 buckets — declared per
docs/declarative/physical.md. The state-backend appliance is
deliberately *not* here: it is this program's own prerequisite
(docs/physical/state-backend.md).

Declarations group by what they configure — the cloud account, the Talos
chain across the fleet, the homelab worker, the backup account, the site's
gateway and overlay — so a reader after one of those reads a run of adjacent
lines. Inside the fleet the endpoint dictates the rest: a node's machine
configuration names the cluster endpoint, which is the load balancer's
address, so the balancer is declared before the configuration that names it
and before the nodes that carry that configuration; the backends pointing
back at those nodes come last.

**Every area of the design appears below, and every one of them is
written.** The stack is the inventory: an area with no implementation would
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

from collections.abc import Callable, Mapping
from ipaddress import IPv4Address, IPv6Address, ip_address

import pulumi
import pulumi_oci as oci
import pulumiverse_talos

from kluster import conventions
from kluster.components.backup import (
    BackupBucket,
    Scope,
    barman_scope,
    etcd_reader_scope,
    etcd_scope,
    metrics_scope,
    volsync_scope,
)
from kluster.components.cloud import CloudNetwork
from kluster.components.cloud.guardrails import AlertRule, Guardrails
from kluster.components.cloud.nodes import CloudNodes, NodeLoadBalancer
from kluster.components.cloud.storage import NodeVolume
from kluster.components.gateway import (
    CaddyService,
    Gateway,
    OverlayDaemon,
    PublicKey,
    ResolverService,
    Rootfs,
    RoutingSession,
)
from kluster.components.homelab import HomelabHost
from kluster.components.overlay import Overlay
from kluster.components.overlay.flow_rules import flow_rules
from kluster.components.talos import TalosCluster, TalosDay1
from kluster.components.talos.image import TalosImage, TalosNocloudImage
from kluster.conventions import backup as backup_conventions
from kluster.conventions.backup import BackupFamily
from kluster.conventions.outputs import BOOT_VOLUME
from kluster.lib import config as lib_config
from kluster.lib.versions import NAMESPACE as VERSIONS
from kluster.lib.versions import versions

#: What the cloud provider is built from. The account's own identifiers — its
#: region and its tenancy OCID — are facts and live in `conventions`; these
#: three are the secrets, and they are read at the line that builds the
#: provider and nowhere else (rfc-002 §8.1, §10.3).
OCI_USER_OCID = 'ociUserOcid'
OCI_FINGERPRINT = 'ociFingerprint'
OCI_PRIVATE_KEY = 'ociPrivateKey'

#: Further stack configuration this program reads: the audience for the
#: budget's alerts, the certificate account the gateway buys its own TLS with,
#: the password on its BGP session, and whether qbittorrent has moved onto the
#: worker.
BUDGET_ALERT_RECIPIENTS = 'budgetAlertRecipients'
GATEWAY_ACME_TOKEN = 'gatewayAcmeToken'
GATEWAY_BGP_PASSWORD = 'gatewayBgpPassword'
QBITTORRENT_ON_WORKER = 'qbittorrentOnWorker'

#: The first-bring-up knob: a LAN address for the gateway, set only while the
#: ceremony brings it onto the overlay. Absent — the steady state — every client
#: of the gateway derives its address from `conventions.overlay.UDM`.
GATEWAY_BOOTSTRAP_HOST = 'gatewayBootstrapHost'


class BootstrapHostRefused(ValueError):
    """`GATEWAY_BOOTSTRAP_HOST` holds something other than a literal IP address."""


#: Static host entries on the gateway's own resolver. Empty, and that is the
#: design: the device name plane is DHCP-derived and served by that resolver,
#: while every service is named by its public hostname and steered by the
#: split-horizon rewrites. An entry belongs here only when a name must resolve
#: on the LAN with no lease behind it and no service plane to carry it — a
#: fully-qualified name mapped to one literal address.
GATEWAY_STATIC_HOSTS: Mapping[str, IPv4Address | IPv6Address] = {}

#: The budget's alerts, each answering a different question. Half-way through
#: the month is "something changed"; at the budget is "it already happened";
#: the forecast is the only one that can arrive while there is still time to
#: act.
BUDGET_ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(name='half', threshold=50, message='Half of the monthly cloud budget is spent.'),
    AlertRule(name='full', threshold=100, message='The monthly cloud budget is spent.'),
    AlertRule(
        name='forecast',
        threshold=100,
        message='This month is forecast to end over the cloud budget.',
        forecast=True,
    ),
)

#: The bucket's consumers that exist whether or not any application does: the
#: etcd snapshots' uploader, and what reads the snapshots back.
FIXED_SCOPES: tuple[Scope, ...] = (etcd_scope(), etcd_reader_scope())


#: The scope each backup family's writer key reaches, by the namespace it
#: backs up.
FAMILY_SCOPES: Mapping[BackupFamily, Callable[[str], Scope]] = {
    BackupFamily.VOLSYNC: volsync_scope,
    BackupFamily.BARMAN: barman_scope,
    BackupFamily.VM: metrics_scope,
}


def backup_scopes(backed: Mapping[str, frozenset[BackupFamily]]) -> tuple[Scope, ...]:
    """Every key the bucket carries: the fixed consumers', and a writer key per family of each backed namespace.

    `backed` is `conventions.backup.BACKED_NAMESPACES`, the census
    `credentials` seals the keys by too, so a namespace that starts backing up
    is one row there and a key here on the next apply.
    """
    return (
        *FIXED_SCOPES,
        *(FAMILY_SCOPES[family](namespace) for namespace, families in backed.items() for family in sorted(families)),
    )


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
    # One pin for the fleet, in the namespace every pin a stack program reads
    # shares (framework/pulumi.md §3.2). Three declarations read it — the
    # machine configurations, the cloud image imported from the factory, and
    # the worker's own disk image — and they are one version by construction.
    talos_version = versions.talos

    # The two providers this program shares, and therefore the two the stack
    # program builds (rfc-002 §8.1). The first is the cloud account's: six
    # components declare against it — the network, the balancer, the nodes,
    # the guardrails, the block volumes and the image import — and a provider
    # built inside any of them would be reached into by the rest. Its region
    # and its tenancy come from `conventions` because both are permanent
    # properties of the account, so what is read here is exactly the secrets.
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
    # The Talos chain's provider, shared the same way: the two images, the
    # machine configurations and day 1 all declare through it. It authenticates
    # to no account and is built from nothing — every call over apid carries
    # its client configuration as an input, and the image factory is the
    # public one — yet it is explicit like the rest, with the package's default
    # disabled, so a Talos call that misses it fails instead of reaching for a
    # provider nothing in the program names.
    talos = pulumiverse_talos.Provider(f'{conventions.CLUSTER_NAME}-talos')
    on_talos = pulumi.ResourceOptions(providers=[talos])

    network = CloudNetwork(conventions.CLUSTER_NAME, compartment_id=compartment_id, opts=on_cloud)
    # The one component on both: its schematic is the factory's, and the image
    # imported from it is the account's.
    image = TalosImage(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        talos_version=talos_version,
        opts=pulumi.ResourceOptions(providers=[cloud, talos]),
    )
    # The worker's own artifact: the fleet's Talos version, a schematic of its
    # own (x86, and the A380's driver and firmware, which the GPU cutover wants
    # present from day 0), and a file on this machine rather than an entry in a
    # cloud catalog. No cloud provider on it, only the chain's: nothing it
    # declares reaches the account. The disk it becomes is §3's.
    worker_image = TalosNocloudImage(f'{conventions.CLUSTER_NAME}-worker', talos_version=talos_version, opts=on_talos)
    load_balancer = NodeLoadBalancer(
        conventions.CLUSTER_NAME,
        compartment_id=compartment_id,
        subnet_id=network.subnet.id,
        opts=on_cloud,
    )

    cluster = TalosCluster(
        conventions.CLUSTER_NAME,
        cluster_name=conventions.CLUSTER_NAME,
        # The Kubernetes API at the balancer: the port is the one the balancer
        # forwards and the nodes open, from the same structure as both.
        endpoint=load_balancer.address.apply(
            lambda address: f'https://{address}:{conventions.MANAGEMENT_PORTS.kubernetes}'
        ),
        # The machine API at the same address, on the other port the balancer
        # forwards: what every Talos call is dialed at, and so what every
        # control plane's apid certificate carries.
        management_address=load_balancer.address,
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
        # Each node's installer is the one the factory builds from the
        # schematic of the image that node boots, so an upgrade that installs
        # it keeps what the image carries (operations.md §2.1).
        installers={
            **dict.fromkeys(conventions.CLOUD_NODES, image.installer),
            conventions.HOMELAB_NODE: worker_image.installer,
        },
        # Where each node is: the fleet this stack creates as OCI instances is
        # the cloud site, and the domain on the homelab host is the home one.
        sites={
            **dict.fromkeys(conventions.CLOUD_NODES, conventions.Site.CLOUD),
            conventions.HOMELAB_NODE: conventions.Site.HOME,
        },
        # The same table the attachments below are declared from: the machine
        # mounts each volume its node will be handed, and is labelled with it.
        # It is the table rather than the attachments because those need the
        # instances, and the instances boot this configuration.
        volumes=conventions.NODE_VOLUMES,
        opts=on_talos,
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
        placements=await _placements(compartment_id, cloud),
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
    # Every call is dialed at the balancer, which forwards 50000 to whichever
    # control plane it likes; that control plane forwards the call to the node
    # it names. So a node is named by an address it holds on its interface:
    # a cloud node by its private address, the worker by its cluster-VLAN
    # address, which a control plane reaches over KubeSpan. Nothing outside
    # the site needs a route to the worker, and a cloud node's public address,
    # which is on no interface, is in no certificate and names nothing here
    # (declarative/physical.md §2).
    #
    # The worker's *first* configuration is not applied at all — it rides the
    # seed image the VM boots from — so the path through a control plane is
    # needed only once the node is in the mesh, which is where a change to a
    # running worker finds it.
    day1 = TalosDay1(
        conventions.CLUSTER_NAME,
        cluster=cluster,
        addresses={
            **{node: instance.private_ip for node, instance in nodes.instances.items()},
            conventions.HOMELAB_NODE: str(conventions.HOMELAB_NODE_IPV4),
        },
        # The forward the calls go through: the balancer's listeners and the
        # backends behind them, the machine API's among them. Both components
        # hold these as lists rather than by port, so day 1 waits on every
        # one, which costs it nothing: it has no call to make before the
        # fleet is behind the balancer.
        forward=[load_balancer, *nodes.backends],
        # The dedicated VIP, which the node holding it does not otherwise
        # answer for: OCI assigns the address to the VNIC and leaves the guest
        # alone.
        secondary_addresses={conventions.DEDICATED_VIP_NODE: nodes.secondary_ip.ip_address},
        opts=on_talos,
    )

    # §1: one volume per entry of the census, attached to the node that entry
    # names — for the following volume, whichever node holds the dedicated VIP.
    # The census's other reader is the cluster above, which mounts each one.
    # Both facts about placement are read off the instance rather than
    # restated: a volume attaches only within its own availability domain, and
    # the node's domain is itself a regional fact chosen at apply time
    # (`_placements`). Each is kept for its id, which the volume map exported
    # below carries beside the node's boot volume.
    volumes: dict[str, NodeVolume] = {}
    for volume_name, entry in sorted(conventions.NODE_VOLUMES.items()):
        volumes[volume_name] = NodeVolume(
            f'{conventions.CLUSTER_NAME}-{volume_name}',
            compartment_id=compartment_id,
            availability_domain=nodes.instances[entry.attached_node].availability_domain,
            instance_id=nodes.instances[entry.attached_node].id,
            size_gb=entry.size_gb,
            vpus_per_gb=conventions.NODE_VOLUME_VPUS,
            opts=on_cloud,
        )

    # §1: the spend limits. Compartment quotas that refuse to create anything
    # outside the free envelope, and a budget whose alerts arrive before a bill
    # does. The quota is the load-bearing half: an alert tells you afterwards.
    #
    # The compartment is passed twice on purpose. A budget targets it by OCID,
    # while quota statements have no OCID form at all and name it by name —
    # which is why the name is a convention this program decides rather than
    # something read back from the tenancy.
    #
    # The addresses are private mailboxes, so they stay a secret from the key
    # to every alert rule, and their shape is checked inside the secret. A
    # value that is not JSON at all is refused here, by the key's name alone:
    # the SDK's own refusal quotes the value, and a run's error is logged.
    # `from None` keeps that refusal out of the traceback as well.
    try:
        recipients = config.require_secret_object(BUDGET_ALERT_RECIPIENTS)
    except pulumi.ConfigTypeError:
        raise lib_config.ShapeRefused(
            f'{BUDGET_ALERT_RECIPIENTS} must be a JSON list of addresses, and is not JSON',
            what=BUDGET_ALERT_RECIPIENTS,
            shape=lib_config.Shape.NOT_JSON,
        ) from None
    _ = Guardrails(
        conventions.CLUSTER_NAME,
        tenancy_id=tenancy_id,
        compartment_id=compartment_id,
        compartment_name=compartment.name,
        recipients=recipients.apply(lambda value: lib_config.strings(value, BUDGET_ALERT_RECIPIENTS)),
        alert_rules=BUDGET_ALERT_RULES,
        opts=on_cloud,
    )

    # §3: the VM the worker's disk image boots in. The image itself is
    # declared beside the cloud one above, because the cluster names its
    # installer.
    #
    # No endpoint and no credential among the VM's arguments: the libvirt
    # session is the component's own, so it builds its provider and reads the
    # key that opens it (rfc-002 §8.1). The home-automation domain on the same
    # host is declared nowhere here (rfc-002 §13).
    _ = HomelabHost(
        conventions.CLUSTER_NAME,
        cluster=cluster,
        host=str(conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address),
        storage_dir=conventions.HOMELAB_STORAGE_DIR,
        bridge=conventions.HOMELAB_BRIDGE,
        mac=conventions.HOMELAB_NODE_MAC,
        vcpus=conventions.HOMELAB_VCPUS,
        memory_gib=conventions.HOMELAB_MEMORY_GIB,
        disk_gib=conventions.HOMELAB_DISK_GIB,
        image_path=worker_image.path,
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
        retention_days=conventions.BACKUP_VERSION_RETENTION_DAYS,
        scopes=backup_scopes(backup_conventions.BACKED_NAMESPACES),
    )

    # §4: the gateway and the overlay it is the site's member of. Two top-level
    # components, because the overlay is a network several machines belong to
    # and the gateway is one of them: what may join and what the rules are is
    # not the gateway's business (rfc-002 §6). The gateway's own outputs are on
    # the device, apart from the worker's global address, which derives from
    # the cluster network's /64 of the delegated prefix, as the gateway reads
    # it, and is exported below.
    gateway = _gateway(config)
    overlay = Overlay(
        conventions.CLUSTER_NAME,
        network_id=conventions.overlay.NETWORK_ID,
        roster=conventions.overlay.ROSTER,
        # The overlay's own subnet and the default route via the gateway's
        # member, which only a device that opens the tunnel installs: no
        # route names a home prefix, so a member that has not opted in takes
        # nothing new. The run that writes the default route follows the one
        # that creates the gateway's member alone, an order the ceremony
        # keeps and no edge here can (physical/gateway.md §2.2, §2.5).
        managed_routes=conventions.overlay.MANAGED_ROUTES,
        # The tunnel's resolver: the primary zone, answered by the same two
        # resolvers the flow rules below admit a run to, at the same
        # container-VLAN addresses. Written by the same request as the routes
        # and the rules, with the token the component already holds; no
        # member applies it until its own setting is on (physical/gateway.md
        # §2.7).
        dns=conventions.overlay.MANAGED_DNS,
        # Composed here rather than inside the component, because what a run
        # may reach is a fact about how continuous integration gets to this
        # site rather than one about the network (rfc-002 §6). The program
        # names each continuous-integration identity by its node address,
        # which the component mints, so it arrives less those addresses and
        # the component renders it over them.
        #
        # A run reaches the two resolvers at their LAN addresses on the
        # container VLAN, not at overlay addresses, because they have none:
        # they are containers on the device, not members of the overlay. The
        # packets are routed by the gateway, over a host route each run
        # installs for itself (framework/ci.md §2), and a routed packet still carries
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
    ## out of this one (rfc-002 §12). The names are `conventions.PHYSICAL_OUTPUTS`,
    ## which every reader asks by — the `dns` and `k8s-base` StackReferences, the
    ## state reads of `credentials derived sync` — so a rename is one edit both
    ## sides see.
    ##
    outputs = conventions.PHYSICAL_OUTPUTS

    # Machine facts only: the downstream stacks read addresses and ids, never
    # conventions — those they share as code.
    #
    # Both families of the balancer are published, because the cluster anchor
    # in `dns` carries an A and an AAAA and both are `internet` pool members
    # (rfc-007 §4.4); the VIP below is IPv4 only, and that is a property of
    # the address rather than an omission here.
    pulumi.export(outputs.cluster_endpoint, load_balancer.address)
    pulumi.export(outputs.cluster_endpoint_v6, load_balancer.address_v6)
    pulumi.export(outputs.vip1, nodes.reserved_ip.ip_address)
    pulumi.export(outputs.vip1_private, nodes.secondary_ip.ip_address)
    pulumi.export(outputs.node_private_ips, {node: instance.private_ip for node, instance in nodes.instances.items()})
    # Each node's GUA beside its private address: with the dedicated VIP's
    # secondary private address (`vip1_private`) and the balancer's two
    # addresses, the `internet` pool's members, which `k8s-base` builds the
    # pool from (rfc-007 §4.4).
    pulumi.export(outputs.node_guas, nodes.guas)
    # The worker's GUA, derived from the cluster network's /64 this run read:
    # beside its IPv4, which is a convention, the worker's own pool in
    # `k8s-base` and the address a `SiteForward` port's Service asks for in
    # `apps` (rfc-008 §4.1). A run that reads a new /64 exports the new address, and the
    # readers follow in the same deploy chain. Each reader refuses an absent
    # value by name.
    pulumi.export(outputs.worker_gua, gateway.worker_gua)
    # Every cloud node's volumes by name, each to the OCID OCI generated for
    # it: the boot volume off the instance, and each block volume on the node
    # it is attached to. OCI's block-volume metrics name a volume by that OCID
    # alone, and this map is what turns one back into a node and a name
    # `conventions` declares; a reader takes it through
    # `kluster.lib.stack_volumes`, which refuses anything but such a map.
    pulumi.export(outputs.volume_ids, _volume_ids(nodes.instances, volumes, conventions.NODE_VOLUMES))

    # Both are cluster-admin credentials, and both are marked secret at the
    # source; `k8s-base` and `apps` read them from here rather than from a file
    # anybody has to hold.
    pulumi.export(outputs.kubeconfig, day1.kubeconfig)
    pulumi.export(outputs.talosconfig, day1.talosconfig)

    # Names, endpoints and credentials, because that is what a consumer of a
    # bucket is configured with (physical.md §0). The keys are one per scope:
    # the etcd snapshots' uploader and reader, and a writer key per family of
    # every backed namespace. `credentials derived b2-writer seal` reads each
    # writer key out of this stack's state by its scope and seals it into the
    # stack that declares what reads it, `apps` or `k8s-base`
    # (`conventions.sealed.WRITER_KEY_STACKS`), and `b2-reader seal` does the
    # same for the reader (`conventions.sealed.READER_KEYS_BY_SCOPE`).
    pulumi.export(outputs.backup_bucket, backup.bucket_name)
    pulumi.export(outputs.backup_endpoint, backup.endpoint)
    pulumi.export(
        outputs.backup_keys,
        {scope: {'id': backup.key_id(scope), 'secret': backup.key_secret(scope)} for scope in backup.keys},
    )

    # The key material of the two identities generated above, which is how a
    # continuous-integration job joins the overlay at all: the value is the
    # member's `identity.secret`, written to that path on the runner before
    # the daemon starts (`.github/actions/zerotier`). It leaves as a secret,
    # because an export that lost the marking would print a join credential
    # into a deployment log, and it is read back out of state — with
    # `--show-secrets` — by `credentials derived sync`. Indexing the overlay by
    # the roster name the table carries is what makes a roster rename the table
    # does not follow fail here rather than quietly rename the contract.
    for member, output in outputs.ci_identity.items():
        pulumi.export(output, pulumi.Output.secret(overlay.identities[member].private_key))


def _volume_ids(
    instances: Mapping[str, oci.core.Instance],
    volumes: Mapping[str, NodeVolume],
    rows: Mapping[str, conventions.NodeVolumeEntry],
) -> dict[str, dict[str, pulumi.Output[str]]]:
    """Cloud node → volume name → OCID: each node's boot volume, and each node volume on the node its row names."""
    ids: dict[str, dict[str, pulumi.Output[str]]] = {
        node: {BOOT_VOLUME: instance.boot_volume_id} for node, instance in instances.items()
    }
    for name, volume in volumes.items():
        ids[rows[name].attached_node][name] = volume.volume.id
    return ids


def _gateway(config: pulumi.Config) -> Gateway:
    """The device and the controller behind it, from what the stack configuration tells it.

    Three credentials authorize three different things — the device's own
    desired state over SSH, the controller's firewall over its API, the
    overlay's configuration over ZeroTier Central's — and no one of them
    implies the others. **None of the three is read here.** The controller's
    API key and the overlay's administration token each configure a provider
    and nothing else, so each is read where that provider takes it: at the line
    that builds it, inside the component that owns the connection (rfc-002
    §8.1), and for the controller's key in the `configure` of the order's
    dynamic provider as well. The device's
    SSH credential is the same rule for a provider with no such line: a dynamic
    provider reads its configuration in `configure`, in its own process, so
    `gatewayPrivateKey` is read there and by nothing else (framework/pulumi.md
    §5.2).

    What is left is a knob and two secrets a file's content is rendered from.
    Where the device answers and which key it must present are not among them: the pin is a decision and lives in `conventions`, and the
    address derives from the roster unless the ceremony below overrides it.

    **`gatewayBootstrapHost` is the first-bring-up knob**, and it answers one
    question: where does the device answer today. Both doors dial that address
    instead of the overlay one, because the daemon that answers at
    `conventions.overlay.UDM` is one of the container services this run is
    delivering, so until the delivery has happened the overlay address answers
    nothing. Absent, which is the steady state, both derive from the roster's
    address for the gateway. Whether the gateway is a member is a separate
    question with a separate answer: the roster's entry for it, which the
    ceremony recorded (physical/gateway.md §2.5).

    **The knob is a literal address, and that is checked here.** The one apply
    it exists for runs inside the cutover window, and that window stops both
    home resolvers for its whole length (physical/gateway-cutover.md §4), so a
    name is unresolvable at the only moment this value is read. Reading it is
    the last point at which the mistake is cheap: rejected here the run refuses
    before the gateway is declared, where a name that reached the providers
    would fail mid-window, on a device whose live state has already moved.
    """
    bootstrap_host = config.get(GATEWAY_BOOTSTRAP_HOST)
    if bootstrap_host is not None:
        try:
            _ = ip_address(bootstrap_host)
        except ValueError:
            raise BootstrapHostRefused(
                f'{GATEWAY_BOOTSTRAP_HOST} is {bootstrap_host!r}, which is not a literal IP '
                'address. The apply this key exists for runs inside the cutover window, which '
                'stops both home resolvers, so nothing on the LAN answers for a name at the '
                'moment this key is read (physical/gateway.md §2.5).'
            ) from None

    return Gateway(
        conventions.CLUSTER_NAME,
        host=bootstrap_host or str(conventions.overlay.UDM),
        # One declaration per census entry, each holding the entry itself: the
        # gateway knows what a service's image is pinned at and what secret it
        # reads, and `conventions` knows the rest (rfc-002 §5.3).
        caddy=CaddyService(
            service=conventions.gateway.CADDY,
            pin=_rootfs(conventions.gateway.CADDY),
            # The gateway buys its own certificates with this, and nothing else
            # on the device reads it: its TLS has to keep renewing while the
            # cluster — and the cluster's issuer — is down.
            acme_token=config.require_secret(GATEWAY_ACME_TOKEN),
            # What it serves: the services whose interfaces it fronts, and the
            # names it still answers for applications that have not migrated.
            # The second census empties by the end of Wave D, and the block it
            # renders goes with it (conventions.gateway.LEGACY_VHOSTS).
            vhosts=conventions.gateway.RESOLVERS,
            legacy=conventions.gateway.LEGACY_VHOSTS,
        ),
        resolvers=tuple(
            ResolverService(service=service, pin=_rootfs(service)) for service in conventions.gateway.RESOLVERS
        ),
        overlay_daemon=OverlayDaemon(
            service=conventions.gateway.OVERLAY,
            pin=_rootfs(conventions.gateway.OVERLAY),
        ),
        routing=RoutingSession(
            neighbor=conventions.HOMELAB_NODE_IPV4,
            neighbor_link_local=conventions.HOMELAB_NODE_LINK_LOCAL,
            password=config.require_secret(GATEWAY_BGP_PASSWORD),
        ),
        # The key this stack's own sessions present, and the only one this
        # program declares: the operator's own keys are on the device already
        # and the converger takes nothing away. Named for the credential it is
        # the public half of, which is this stack's.
        keys=(
            PublicKey(
                name=f'{conventions.CLUSTER_NAME}-{conventions.PHYSICAL}',
                key=conventions.gateway.CLIENT_KEY,
            ),
        ),
        site=conventions.gateway.UNIFI_SITE,
        static_hosts=GATEWAY_STATIC_HOSTS,
        # Optional, and absent until qbittorrent's own migration wave: the
        # legacy qbittorrent on the homelab host holds the peer port's WAN
        # side until then, by a UPnP lease or a rule kept by hand, and the
        # IPv4 forward that would take it is not declared. The wave reads the
        # device for what holds the port, clears it, and sets the key
        # (cluster/migration.md, Wave D; physical/gateway.md §4.2). A value
        # that is not a boolean refuses the run rather than reading as absent.
        peer_on_worker=config.get_bool(QBITTORRENT_ON_WORKER) or False,
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

    Both lists are regional facts read at apply time, and they consume no
    resource output, so this is awaited in `main` rather than wrapped in an
    output (framework/pulumi.md §1). A region offering one AD degrades to plain
    fault-domain spread.
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
