"""Talos day-1: secrets, machine configuration, apply, bootstrap, health.

The provider chain of declarative/physical.md §2, one resource each: the
cluster PKI, a machine configuration per node, the configuration apply that
carries later changes over apid, the one-time bootstrap of the first control
plane, the kubeconfig and client configuration, and the health check that
gates everything downstream.

The chain is two components because the day it describes is two days.
`TalosCluster` is day 0: the PKI and the configuration each machine boots
with, delivered out of band as instance metadata or a seed image, and
therefore knowable before any machine exists. `TalosDay1` is what happens
once they do: it needs the address each machine answers apid on, which is a
fact about a running machine rather than a decision this program makes.

Splitting them is not an aesthetic choice. An OCI instance's `user_data` *is*
its machine configuration, so a configuration naming something the cloud only
assigns to the finished instance — the dedicated VIP's secondary private IP —
would wait on the instance that is waiting on it. Day 1 carries those
addresses instead, over apid, on top of the configuration the machine booted.

The homelab worker's address runs the other way. Nothing assigns it — the
cluster VLAN it sits on carries no DHCP server at all, by design, because the
gateway's BGP neighbor statement names that address as a constant — so the
worker states its own address, and states it in the configuration it boots
with.

Day-2 is deliberately `talosctl` — upgrades, `upgrade-k8s` and etcd snapshots
are imperative operations, and wrapping them in fake-declarative command
resources would buy drift detection that isn't real.

Machine configuration is composed from typed Python dicts and handed to the
provider as patches. JSON is emitted rather than YAML because it *is* YAML,
and the program then needs no serializer dependency to state its own
configuration.

A patch is either a strategic merge into the `v1alpha1` document or a
document of its own kind, which the provider's patch loader appends to the
configuration beside `v1alpha1`. Whatever the pinned Talos release has moved
out of `v1alpha1` is stated in its own document: at v1.13 that is every field
of `machine.network`, so KubeSpan and the node's link addressing travel as
network documents (`KubeSpanConfig`, `LinkAliasConfig`, `LinkConfig`,
`DHCPv4Config`) the way the ingress firewall always has
(`NetworkDefaultActionConfig`, `NetworkRuleConfig`). A node volume
travels as a `UserVolumeConfig`, the document v1.13 names in place of the
deprecated partitions of `machine.disks`, and so does the directory
`local-path` hands out, a `UserVolumeConfig` of its own on every node in
place of a kubelet mount: a host path a pod reaches is a user volume, and
the configuration carries no `machine.kubelet.extraMounts`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from ipaddress import IPv4Address, IPv4Interface
from typing import Any, Literal, cast

import pulumi
from pulumiverse_talos import client, machine
from pulumiverse_talos.cluster import Kubeconfig, get_health_output

from kluster import conventions
from putils import Component, async_output, resolve

#: What a node is to Kubernetes. The cloud fleet is control planes that also
#: carry workloads (architecture.md §1.1); the homelab machine is a worker.
Role = Literal['controlplane', 'worker']

#: Kubelet reservations exist so eviction has something to work with: an
#: unreserved node starves its own control plane before the kubelet notices.
SYSTEM_RESERVED = {'cpu': '200m', 'memory': '512Mi', 'ephemeral-storage': '1Gi'}

#: Ports that terminate in the host network namespace: the management APIs
#: (`conventions.MANAGEMENT_PORTS`), which the balancer forwards from the same
#: structure, and the two the cluster speaks to itself on. Service ports are deliberately absent:
#: LoadBalancer traffic is answered by Cilium's BPF datapath at tc ingress,
#: ahead of nftables, so declared frontends serve without a firewall entry
#: while undeclared ports fall through to default-deny
#: (declarative/physical.md §2).
HOST_PORTS: tuple[int, ...] = (
    *conventions.MANAGEMENT_PORTS,
    51820,  # KubeSpan
    10250,  # kubelet, intra-cluster
)

#: BGP. Only the homelab worker speaks it, and only with the gateway
#: (cluster-infra.md §2), so it is never part of the host-port census above.
BGP_PORT = 179

#: The whole internet, both families — what a management port on a public
#: node is exposed to whether or not it is written down.
ANYWHERE: tuple[str, ...] = ('0.0.0.0/0', '::/0')

#: The name this program gives a node's one physical link. Talos' network
#: documents address a link by name, and the name the kernel gives it
#: (`eth0`, `ens3`, `enp1s0`) is a property of the PCI topology the platform
#: builds and of the kernel's naming policy, neither of which this program
#: decides — so `uplink_alias_document` attaches this alias to whichever link
#: is physical, and every document that configures the link names the alias.
UPLINK = 'uplink'


@dataclass(frozen=True)
class Opening:
    """One hole in the node-local firewall: a port, and who may come through.

    Talos' ingress firewall is default-deny, so an `Opening` is the only way
    traffic reaches a listener in the host network namespace.
    """

    port: int
    subnets: tuple[str, ...] = ANYWHERE
    protocol: str = 'tcp'

    def document(self) -> dict[str, Any]:
        return {
            'apiVersion': 'v1alpha1',
            'kind': 'NetworkRuleConfig',
            'name': f'port-{self.port}',
            'portSelector': {'ports': [self.port], 'protocol': self.protocol},
            'ingress': [{'subnet': subnet} for subnet in self.subnets],
        }


def node_patch() -> dict[str, Any]:
    """What every machine in the cluster carries, whatever its role."""
    return {
        'machine': {
            'features': {
                # There is no kube-proxy to fall back on, so the node-local
                # apiserver front is mandatory rather than an optimization.
                'kubePrism': {'enabled': True, 'port': conventions.KUBEPRISM_PORT},
            },
            'kubelet': {
                'extraConfig': {'systemReserved': SYSTEM_RESERVED},
            },
        },
        'cluster': {
            # KubeSpan peers find each other through discovery; without it the
            # mesh `kubespan_document` turns on has no membership.
            'discovery': {'enabled': True},
            'network': {
                # IPv4 first: the cluster is dual-stack with v4 primary
                # (architecture.md §1.3).
                'podSubnets': [str(conventions.POD_CIDR_V4), str(conventions.POD_CIDR_V6)],
                'serviceSubnets': [str(conventions.SERVICE_CIDR_V4), str(conventions.SERVICE_CIDR_V6)],
            },
        },
    }


def kubespan_document() -> dict[str, Any]:
    """KubeSpan on, which every machine in the cluster carries.

    Only `enabled` is stated. Every other field keeps the default Talos gives
    it: pod networks are not advertised over the mesh (the CNI carries pod
    traffic), traffic to a peer that is down does not bypass the mesh, no
    extra endpoints are harvested, and the link MTU is Talos' own.

    It is a document of its own because the pinned release deprecates
    `machine.network.kubespan` for it, and a configuration carrying both is
    refused: the document's own validation rejects a `v1alpha1` that also
    configures KubeSpan.
    """
    return {'apiVersion': 'v1alpha1', 'kind': 'KubeSpanConfig', 'enabled': True}


def control_plane_patch(*, cert_sans: Sequence[str], secretbox_secret: str | None = None) -> dict[str, Any]:
    """The parts only a control plane has: the apiserver, etcd, and the CNI.

    A worker's configuration has no apiserver to harden and no etcd to
    encrypt, and the CNI is installed by the control plane, so none of this
    belongs in the shared patch.
    """
    config: dict[str, Any] = {
        'cluster': {
            'allowSchedulingOnControlPlanes': True,
            'network': {'cni': {'name': 'none'}},
            'apiServer': {
                'certSANs': list(cert_sans),
                # A public 6443 warrants both, defaults notwithstanding.
                'extraArgs': {'anonymous-auth': 'false', 'audit-log-path': '/var/log/audit/kube/kube-apiserver.log'},
            },
            'etcd': {
                'advertisedSubnets': [str(conventions.VCN_CIDR)],
            },
        }
    }
    if secretbox_secret is not None:
        # Encryption at rest for Kubernetes secrets. etcd lives in a $0-trust
        # tenancy, and its snapshots are designed to ship off-site hourly
        # (storage.md §5; the snapshot is not built — nodes.md §5 Tier 0), so
        # the key material has to be stated here rather than inherited from
        # whatever the generator happened to do.
        config['cluster']['secretboxEncryptionSecret'] = secretbox_secret
    return config


def local_path_volume_document() -> dict[str, Any]:
    """The directory the `local-path` StorageClass hands out (storage.md §2).

    The provisioner is `k8s-base`'s; the path underneath it is machine
    configuration, because the kubelet sees `/var/mnt` bound read-only and
    so can create nothing inside a plain directory there. A user volume is a
    mount of its own at `/var/mnt/<name>`, and a mount made under `/var/mnt`
    reaches the kubelet writable — the mechanism a node volume uses, and the
    one Talos names as the replacement for a kubelet mount.

    A `directory` volume has no disk: Talos creates the directory on the
    system disk's EPHEMERAL partition and bind-mounts it onto itself, and a
    directory already at that path keeps its contents. So the document
    states nothing but its type, and carries no `provisioning`,
    `filesystem`, `encryption` or `mount` block, each of which Talos refuses
    for this type. Nor is it a node volume: with no disk to select, it is
    outside the rule of one node volume per node, which exists for
    `DATA_DISK_SELECTOR`'s sake.
    """
    return {
        'apiVersion': 'v1alpha1',
        'kind': 'UserVolumeConfig',
        'name': conventions.LOCAL_PATH_VOLUME,
        'volumeType': 'directory',
    }


#: How a node volume's disk is found the first time, before Talos has written
#: anything to it: by exclusion, as the disk that is not the one Talos booted
#: from. It matches exactly one disk because a node carries at most one volume,
#: and on every later boot Talos finds the volume by the partition label it
#: wrote (`u-<name>`) instead, so attachment order, device path and serial
#: never enter into it.
DATA_DISK_SELECTOR = '!system_disk'


def node_volume_document(name: str) -> dict[str, Any]:
    """Mount the node volume `name` at `conventions.node_volume_mount(name)`.

    A `UserVolumeConfig` of type `partition`: one partition filling the
    disk the selector finds, formatted XFS and mounted at Talos' user-volume
    root under the volume's name. The type is stated rather than defaulted,
    because a `disk` volume is located by its selector alone, with
    `system_disk` unbound, so it could never select by exclusion. XFS is
    stated for the same kind of reason: a volume found carrying another
    filesystem is refused as a mismatch rather than mounted, so a default
    Talos changed would strand every existing volume.

    Talos provisions only while it finds no partition labelled `u-<name>`,
    and then onto the selected disk when its prober recognizes nothing on
    it, or onto free GPT space. The prober knows no partition table but
    GPT, so an MBR-partitioned disk, or one holding a filesystem the
    prober does not know, reads as empty and is repartitioned and
    formatted; a located partition on which it finds no filesystem is
    formatted too. The rule that keeps other data off both paths is
    storage.md §6.

    There is no encryption block: every key source Talos offers either binds
    the dataset to a machine that a rebuild replaces or puts the key in the
    instance's own metadata. Mount options stay Talos' defaults,
    `nosuid,nodev` among them.
    """
    return {
        'apiVersion': 'v1alpha1',
        'kind': 'UserVolumeConfig',
        'name': name,
        'volumeType': 'partition',
        'provisioning': {
            'diskSelector': {'match': DATA_DISK_SELECTOR},
            # A partition volume must be bounded; this bound is the whole
            # disk, and `grow` takes a resized disk at the node's next boot.
            'maxSize': '100%',
            'grow': True,
        },
        'filesystem': {'type': 'xfs'},
    }


def node_volume_label_patch(name: str) -> dict[str, Any]:
    """Label the node carrying the node volume `name` with it.

    The node-side half of `node_volume_document`, and what a `local`
    PersistentVolume's node affinity selects on (rfc-002 §10.5). A strategic
    merge into `v1alpha1`, because v1.13 has no document for node labels.
    """
    return {'machine': {'nodeLabels': {conventions.NODE_VOLUME_LABEL: name}}}


def uplink_alias_document() -> dict[str, Any]:
    """Name the node's physical link `UPLINK`, whatever the kernel called it.

    A `LinkAliasConfig` selector is only ever shown physical links — an
    Ethernet link of no logical kind, not a bond, bridge, VLAN or tunnel — so
    a selector that is simply `true` matches the physical link, the same test
    `deviceSelector: {physical: true}` makes. An alias with a fixed name must
    match exactly one link, so a machine with two physical links gets no
    alias, and every document that names `UPLINK` names nothing. Because a
    link document also switches off Talos' default DHCP, such a machine boots
    with no address on any link, reachable only on its console. That is why
    each node this program configures has exactly one physical link, and why
    the tests hold both kinds of node to it: one VNIC per cloud instance, one
    interface on the homelab worker's domain.
    """
    return {'apiVersion': 'v1alpha1', 'kind': 'LinkAliasConfig', 'name': UPLINK, 'selector': {'match': 'true'}}


def secondary_address_documents(address: str) -> list[dict[str, Any]]:
    """Put an extra address on the node's physical link.

    OCI hands the node a secondary private IP on its VNIC but does
    not configure the guest, so without this the dedicated VIP is an address
    the machine never answers for (architecture.md §3.2). It is added as a
    host route (/32) on purpose: the subnet route already arrives over DHCP,
    and a second one for the same prefix is a conflict, not a redundancy.

    The lease is stated alongside the address, because configuring any link
    in a network document switches off the DHCP Talos otherwise runs on every
    physical link by default. The request sends no client identifier
    (`clientIdentifier: none`), which is what a `machine.network.interfaces`
    entry with `dhcp: true` sends; the document's own default would be the
    MAC, and moving off the deprecated field is meant to change nothing the
    node does.
    """
    return [
        uplink_alias_document(),
        {'apiVersion': 'v1alpha1', 'kind': 'LinkConfig', 'name': UPLINK, 'addresses': [{'address': f'{address}/32'}]},
        {'apiVersion': 'v1alpha1', 'kind': 'DHCPv4Config', 'name': UPLINK, 'clientIdentifier': 'none'},
    ]


@dataclass(frozen=True)
class StaticAddress:
    """A node's own addressing on a network that will not hand it out.

    `address` carries its prefix rather than being a bare host, because the
    prefix is what makes the subnet a connected route; `gateway` is the next hop
    for everything else.
    """

    address: IPv4Interface
    gateway: IPv4Address


#: Nodes whose machine configuration has to state their address, because
#: nothing else will. Only the homelab worker qualifies. A cloud node is
#: handed its address by the platform it boots on, but the worker is a VM on
#: the cluster VLAN, which runs no DHCP server — and three other places
#: already name its address as a constant: the gateway's FRR neighbor
#: statement, the gateway's port forward for the qbittorrent peer port, and
#: the node day 1 names in the apid calls that configure it. A lease would
#: make all three a guess (physical/homelab-host.md §2).
#:
#: This is a table rather than a constructor input on purpose. The address is
#: not a decision a caller makes: a stack free to pass one could tell the
#: machine an address the gateway was never told about, which is the failure
#: the constant exists to prevent.
STATIC_ADDRESSES: Mapping[str, StaticAddress] = {
    conventions.HOMELAB_NODE: StaticAddress(
        address=IPv4Interface(f'{conventions.HOMELAB_NODE_IPV4}/{conventions.CLUSTER_VLAN.v4.prefixlen}'),
        gateway=conventions.CLUSTER_VLAN.require_gateway(),
    ),
}


def static_address_documents(static: StaticAddress) -> list[dict[str, Any]]:
    """Configure the node's link itself: address, subnet, default route.

    The counterpart of `secondary_address_documents` for a machine no
    platform configures on its behalf. Two differences from that one, which
    are the same decision twice: there is no DHCP document, and the address
    carries the subnet's prefix instead of /32. With no lease there is no
    subnet route to conflict with, and with no subnet route the address has
    to bring one. The default route is then explicit, because carrying it
    was the lease's other job. Talos' default DHCP stays off without being
    told to: configuring any link in a network document is what switches it
    off.

    Whatever else the lease carried goes with it. Resolvers fall back to
    Talos' own defaults, which is what the cloud nodes effectively use too;
    IPv6 is untouched, because the GUA this design expects is SLAAC and SLAAC
    is the kernel's, not DHCP's.

    The link is found the way `secondary_address_documents` finds it — as
    the one physical link, through `uplink_alias_document`, not by a name. A
    virtio NIC presents as an ordinary Ethernet link, and the worker has
    exactly one.
    """
    return [
        uplink_alias_document(),
        {
            'apiVersion': 'v1alpha1',
            'kind': 'LinkConfig',
            'name': UPLINK,
            'addresses': [{'address': str(static.address)}],
            # No destination is how a `LinkConfig` route spells the default
            # route: one for the gateway's address family. Spelled out as
            # `0.0.0.0/0`, the document refuses it as an unspecified prefix.
            'routes': [{'gateway': str(static.gateway)}],
        },
    ]


def ingress_firewall_documents(extra: Sequence[Opening] = ()) -> list[dict[str, Any]]:
    """The node-local firewall: default-deny plus one rule per opening.

    Talos expresses this as separate configuration documents rather than as
    v1alpha1 fields, so they travel as their own patches.
    """
    openings = [*(Opening(port) for port in HOST_PORTS), *extra]
    return [
        {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'},
        *(opening.document() for opening in openings),
    ]


def patches(
    *,
    role: Role = 'controlplane',
    cert_sans: Sequence[str] = (),
    secretbox_secret: str | None = None,
    static_address: StaticAddress | None = None,
    secondary_address: str | None = None,
    bgp_peer: str | None = None,
    volume: str | None = None,
) -> list[str]:
    """The patch list for one node, as the provider wants it.

    `volume` is the name of the node volume attached to this node, if one is.

    `bgp_peer` is a subnet, not a host: it is who may open a BGP session with
    this node, and the answer for the homelab worker is the gateway alone.

    `static_address` is the node's own address where no platform assigns one,
    and `secondary_address` an extra address on top of whichever it already
    has; no node has both, and a node with neither is configured by its lease.
    """
    documents: list[Mapping[str, Any]] = [node_patch(), kubespan_document(), local_path_volume_document()]
    if volume is not None:
        documents.append(node_volume_label_patch(volume))
    if role == 'controlplane':
        documents.append(control_plane_patch(cert_sans=cert_sans, secretbox_secret=secretbox_secret))
    if static_address is not None:
        documents += static_address_documents(static_address)
    if secondary_address is not None:
        documents += secondary_address_documents(secondary_address)
    if volume is not None:
        documents.append(node_volume_document(volume))
    extra = [Opening(BGP_PORT, (bgp_peer,))] if bgp_peer is not None else []
    documents += ingress_firewall_documents(extra)
    return [json.dumps(document) for document in documents]


class TalosCluster(Component, pulumi_type='kluster:physical:TalosCluster'):
    """The cluster PKI and the configuration each machine boots with.

    The secrets land in Pulumi state — the provider's ephemeral resources do
    not bridge — which is why the state backend is passphrase-encrypted behind
    client-certificate TLS.

    Day 0 delivers each machine its configuration out of band: `user_data` in
    OCI instance metadata for the cloud nodes, a cloud-init seed for the
    homelab VM. Everything that needs a machine to answer — applying later
    changes, bootstrapping etcd, the health gate, the two credentials the
    later stacks are built on — is `TalosDay1`.

    :param control_plane_nodes: node names, in the order they take the
        cluster; the first one is the node that bootstraps etcd.
    :param worker_nodes: node names that get a worker configuration. Required
        even when there are none, like every roll a component receives: a
        default here would let a call site that lost the argument declare a
        cluster of zero workers, and the only objection would be the BGP
        check below, which bites only while a peer names the missing worker.
    :param bgp_peers: node name to the subnet allowed to open a BGP session
        with it (the homelab worker's gateway). This is a site fact rather
        than a machine one, so it is part of what the machine boots with.
        Required like the node rolls above, and the one roll nothing else
        would catch: a call site that lost it would boot a worker that
        refuses its gateway's session, and no check or test would object.
    :param volumes: the node volumes, by name (`conventions.NODE_VOLUMES`).
        Each is mounted, and its node labelled, in the configuration of the
        node it attaches to, so the volume is part of what that machine boots
        with: the census is known at day 0, while the attachment itself can
        only be declared once the instance exists. A volume on a node outside
        the cluster, or two volumes on one node, are refused — the disk
        selector finds a node's data disk as the one disk that is not the
        boot disk, so it cannot tell two data disks apart.

    A node named in `STATIC_ADDRESSES` also boots with its own address, its
    subnet's prefix and a default route, rather than with whatever a DHCP
    server offers it.
    """

    def __init__(
        self,
        name: str,
        *,
        cluster_name: str,
        endpoint: pulumi.Input[str],
        cert_sans: Sequence[pulumi.Input[str]],
        control_plane_nodes: Sequence[str],
        worker_nodes: Sequence[str],
        talos_version: str,
        bgp_peers: Mapping[str, pulumi.Input[str]],
        volumes: Mapping[str, conventions.NodeVolumeEntry],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        if not control_plane_nodes:
            raise ValueError('a cluster needs at least one control-plane node')
        self.control_plane_nodes = tuple(control_plane_nodes)
        self.worker_nodes = tuple(worker_nodes)
        self.roles: dict[str, Role] = {
            **{node: cast('Role', 'controlplane') for node in self.control_plane_nodes},
            **{node: cast('Role', 'worker') for node in self.worker_nodes},
        }
        if len(self.roles) != len(self.control_plane_nodes) + len(self.worker_nodes):
            raise ValueError('a node is both a control plane and a worker')

        self.cluster_name = cluster_name
        self._endpoint = endpoint
        self._talos_version = talos_version
        self._cert_sans = tuple(cert_sans)
        self._bgp_peers = dict(bgp_peers)
        unknown = sorted(set(self._bgp_peers) - set(self.roles))
        if unknown:
            raise ValueError(f'BGP peers name nodes that are not in the cluster: {unknown}')
        self._volumes = _volumes_by_node(volumes, self.roles)

        self.secrets = machine.Secrets(
            f'{name}-secrets',
            talos_version=talos_version,
            # Regenerating cluster PKI is a rebuild, not an update.
            opts=self.child_opts(protect=True),
        )

        self.configurations = {node: self._render(node) for node in self.roles}

        self.register_outputs({})

    # -- configuration ------------------------------------------------------

    def configuration(
        self,
        node: str,
        *,
        secondary_address: pulumi.Input[str] | None = None,
    ) -> pulumi.Output[machine.GetConfigurationResult]:
        """One node's machine configuration, optionally with an extra address.

        Without `secondary_address` this is what the machine boots with,
        rendered once. With one it is a second rendering of the same
        configuration that additionally puts that address on the node's
        physical link — a configuration that only exists to be applied over apid,
        because the address it names is assigned to an instance that the first
        rendering is an input to.
        """
        if secondary_address is None:
            return self.configurations[node]
        return self._render(node, secondary_address=secondary_address)

    def _render(
        self,
        node: str,
        *,
        secondary_address: pulumi.Input[str] | None = None,
    ) -> pulumi.Output[machine.GetConfigurationResult]:
        role = self.roles[node]
        return machine.get_configuration_output(
            cluster_name=self.cluster_name,
            cluster_endpoint=self._endpoint,
            machine_type=role,
            # The provider's input and output types describe the same
            # structure under different names.
            machine_secrets=cast('Any', self.secrets.machine_secrets),
            talos_version=self._talos_version,
            # The endpoint is the load balancer's address, which exists only
            # once that resource does — so the patches are computed inside an
            # async_output rather than at declaration time.
            config_patches=async_output(partial(self._patches, node, role, secondary_address)),
            opts=pulumi.InvokeOutputOptions(parent=self),
        )

    async def _patches(self, node: str, role: Role, secondary_address: pulumi.Input[str] | None) -> list[str]:
        return patches(
            role=role,
            cert_sans=await _resolved(self._cert_sans),
            secretbox_secret=await self._secretbox_secret() if role == 'controlplane' else None,
            static_address=STATIC_ADDRESSES.get(node),
            secondary_address=await _resolved_one(secondary_address),
            bgp_peer=await _resolved_one(self._bgp_peers.get(node)),
            volume=self._volumes.get(node),
        )

    async def _secretbox_secret(self) -> str | None:
        """The generated key that encrypts Kubernetes secrets in etcd.

        Talos generates one with the rest of the bundle; naming it in a patch
        is what makes encryption at rest a property this program states rather
        than one it inherits. A bundle that arrives without one is reported as
        an error — the deployment fails rather than quietly bringing up a
        cluster whose secrets are readable in every etcd snapshot.
        """
        # The provider's result types are mappings that also expose their
        # fields as attributes, and only the mapping half survives being
        # resolved here — so the bundle is read by key. The SDK types every
        # field as present; what the provider returned is what decides.
        bundle = cast('Mapping[str, Any]', await resolve(self.secrets.machine_secrets))
        secret = cast('Mapping[str, Any]', bundle.get('secrets') or {}).get('secretbox_encryption_secret')
        if not secret:
            pulumi.error(
                'the machine secrets carry no secretbox key: etcd would hold Kubernetes secrets in clear', self
            )
            return None
        return str(secret)

    # -- outputs ------------------------------------------------------------

    @property
    def machine_configs(self) -> dict[str, pulumi.Output[str]]:
        """Per-node configuration, ready to become `user_data` or a seed ISO."""
        return {node: configuration.machine_configuration for node, configuration in self.configurations.items()}


class TalosDay1(Component, pulumi_type='kluster:physical:TalosDay1'):
    """Apply, bootstrap, health, and the credentials the rest of the world reads.

    Everything here talks to machines that already run, over apid on port
    50000, which is why it is a component of its own: it is declared with the
    addresses those machines answer on, and those exist only once the
    instances and the worker VM do.

    A call over apid carries two addresses that are not the same thing: the
    *endpoint* it opens a connection to, and the *node* it names in the
    request. apid routes by node — a member that is not the named one proxies
    the call across the cluster — so an endpoint only has to be a way in.
    Where a node is not directly reachable, that is what reaches it: it is
    named as the node and dialed at an endpoint that is.

    Not every call has that to lean on. Bootstrap is the cluster's first
    contact, before there is a cluster to route through, so it dials the node
    it names; so does the kubeconfig read that follows it. Those are
    control-plane operations on the first control plane, which is directly
    reachable, and they stay that way.

    :param cluster: the PKI and the day-0 configuration these machines booted.
    :param addresses: node name to the address the node is *named* by. Every
        node of the cluster needs one — a cluster is not healthy because the
        nodes somebody listed are.
    :param endpoints: node name to the address a call to it is *dialed* at,
        for the nodes where that differs from the address above. A node with
        no entry is dialed at its own address.
    :param secondary_addresses: node name to an extra address to put on its
        physical link (the node's dedicated VIP).
    """

    def __init__(
        self,
        name: str,
        *,
        cluster: TalosCluster,
        addresses: Mapping[str, pulumi.Input[str]],
        endpoints: Mapping[str, pulumi.Input[str]] | None = None,
        secondary_addresses: Mapping[str, pulumi.Input[str]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.cluster = cluster
        self._addresses = dict(addresses)
        self._secondary_addresses = dict(secondary_addresses or {})
        for label, keyed in (
            ('addresses', self._addresses),
            ('endpoints', dict(endpoints or {})),
            ('secondary addresses', self._secondary_addresses),
        ):
            unknown = sorted(set(keyed) - set(cluster.roles))
            if unknown:
                raise ValueError(f'{label} name nodes that are not in the cluster: {unknown}')
        missing = sorted(set(cluster.roles) - set(self._addresses))
        if missing:
            raise ValueError(f'day 1 needs an address for every node, and {missing} have none')
        #: Where each node is dialed, which is its own address unless the
        #: caller named a way in to reach it with.
        self._endpoints = {node: (endpoints or {}).get(node, self._addresses[node]) for node in cluster.roles}

        # What is applied is the booted configuration plus whatever only exists
        # now: the secondary private IP behind the dedicated VIP.
        self.configurations = {
            node: cluster.configuration(node, secondary_address=self._secondary_addresses.get(node))
            for node in cluster.roles
        }

        client_configuration = cast('Any', cluster.secrets.client_configuration)

        # Node-serial by construction: each apply waits for the one before it,
        # so a change that reboots the machine never takes the quorum with it.
        self.applies: dict[str, machine.ConfigurationApply] = {}
        previous: list[pulumi.Resource] = []
        for node in cluster.roles:
            applied = machine.ConfigurationApply(
                f'{name}-{node}-config',
                node=self._addresses[node],
                # Not the same address for every node: an apply is routed by
                # the node it names, so a node behind the mesh is dialed
                # wherever the cluster answers.
                endpoint=self._endpoints[node],
                client_configuration=client_configuration,
                machine_configuration_input=self.configurations[node].machine_configuration,
                # A change needing a reboot is staged rather than applied, so
                # the reboot is an operator's decision and not a side effect.
                apply_mode='staged_if_needing_reboot',
                # `reset` wipes STATE and EPHEMERAL — every partition the node
                # has (provider issue #205). It stays off on every node,
                # whatever it carries: replacing a node is an explicit
                # procedure (drain, etcd leave, destroy, recreate), never
                # something a destroy of this resource does on its own.
                on_destroy=machine.ConfigurationApplyOnDestroyArgs(reset=False, graceful=True, reboot=False),
                opts=self.child_opts(depends_on=previous),
            )
            self.applies[node] = applied
            previous = [applied]

        first = cluster.control_plane_nodes[0]
        # Bootstrap is a once-per-cluster operation on one node: running it on
        # a second control plane would try to start a second etcd cluster. It
        # is also the call with nothing to route through — there is no cluster
        # yet — so it dials the node it names, and so does the kubeconfig read
        # that waits on it.
        self.bootstrap = machine.Bootstrap(
            f'{name}-bootstrap',
            node=self._addresses[first],
            endpoint=self._addresses[first],
            client_configuration=client_configuration,
            opts=self.child_opts(depends_on=[self.applies[first]]),
        )

        self.kubeconfig_source = Kubeconfig(
            f'{name}-kubeconfig',
            node=self._addresses[first],
            endpoint=self._addresses[first],
            client_configuration=client_configuration,
            opts=self.child_opts(depends_on=[self.bootstrap]),
        )

        # Every node is nameable, and the control planes are what an operator
        # dials to reach any of them — the same split as the applies above, in
        # the file a human then holds.
        self._client_configuration = client.get_configuration_output(
            cluster_name=cluster.cluster_name,
            client_configuration=client_configuration,
            endpoints=async_output(partial(self._addresses_of, cluster.control_plane_nodes)),
            nodes=async_output(partial(self._addresses_of, tuple(cluster.roles))),
            opts=pulumi.InvokeOutputOptions(parent=self),
        )

        # The gate. This data source does not return until the cluster reports
        # healthy, so anything that resolves it is ordered behind a working
        # cluster rather than behind a resource that merely finished.
        #
        # It reaches the workers the same way: they are named as nodes, and the
        # client talks to control-plane endpoints only.
        self.health = get_health_output(
            client_configuration=client_configuration,
            control_plane_nodes=async_output(partial(self._addresses_of, cluster.control_plane_nodes)),
            worker_nodes=async_output(partial(self._addresses_of, cluster.worker_nodes)),
            endpoints=async_output(partial(self._addresses_of, cluster.control_plane_nodes)),
            opts=pulumi.InvokeOutputOptions(parent=self, depends_on=[self.bootstrap, *self.applies.values()]),
        )
        self._kubeconfig = pulumi.Output.secret(async_output(self._healthy_kubeconfig))

        self.register_outputs({})

    # -- inputs prepared asynchronously -------------------------------------

    async def _healthy_kubeconfig(self) -> str:
        """The kubeconfig, resolved behind the health check rather than beside it."""
        _, raw = await resolve(self.health, self.kubeconfig_source.kubeconfig_raw)
        return str(raw)

    async def _addresses_of(self, nodes: Sequence[str]) -> list[str]:
        return await _resolved([self._addresses[node] for node in nodes])

    # -- outputs ------------------------------------------------------------

    @property
    def kubeconfig(self) -> pulumi.Output[str]:
        """Cluster-admin credentials, released only once the cluster is healthy."""
        return self._kubeconfig

    @property
    def talosconfig(self) -> pulumi.Output[str]:
        """The talosctl client configuration: the same PKI, for the machine API."""
        return pulumi.Output.secret(self._client_configuration.apply(lambda config: config.talos_config))


def _volumes_by_node(volumes: Mapping[str, conventions.NodeVolumeEntry], nodes: Mapping[str, Role]) -> dict[str, str]:
    """Node name to the name of the one volume attached to it, sentinels resolved."""
    unknown = sorted(name for name, entry in volumes.items() if entry.attached_node not in nodes)
    if unknown:
        raise ValueError(f'volumes attach to nodes that are not in the cluster: {unknown}')
    by_node: dict[str, list[str]] = {}
    for name, entry in sorted(volumes.items()):
        by_node.setdefault(entry.attached_node, []).append(name)
    crowded = {node: names for node, names in by_node.items() if len(names) > 1}
    if crowded:
        raise ValueError(
            f'a node carries at most one volume, because its disk selector cannot tell two data disks apart: {crowded}'
        )
    return {node: names[0] for node, names in by_node.items()}


async def _resolved(inputs: Sequence[pulumi.Input[str]]) -> list[str]:
    if not inputs:
        return []
    values = await resolve(*inputs)
    return [str(value) for value in (values if len(inputs) > 1 else (values,))]


async def _resolved_one(value: pulumi.Input[str] | None) -> str | None:
    return None if value is None else str(await resolve(value))
