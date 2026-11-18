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
addresses instead, over apid, on top of its day-0 rendering.

The homelab worker's address runs the other way. Nothing assigns it — the
cluster VLAN it sits on carries no DHCP server at all, by design, because the
gateway's BGP neighbor statement names that address as a constant — so the
worker states its own address, and states it in the configuration it boots
with.

Day-2 is deliberately `talosctl` — upgrades and `upgrade-k8s` are imperative
operations, and wrapping them in fake-declarative command resources would buy
drift detection that isn't real. The scheduled etcd snapshot is no `talosctl`
call: it is the cluster's to take, with a certificate Talos issues for that one
role to one namespace (`control_plane_patch`; storage.md §5, which says what of
it is declared).

Machine configuration is composed from typed Python dicts and handed to the
provider as patches. JSON is emitted rather than YAML because it *is* YAML,
and the program then needs no serializer dependency to state its own
configuration.

A patch is either a strategic merge into the `v1alpha1` document or a
document of its own kind, which the provider's patch loader appends to the
configuration beside `v1alpha1`. Whatever the pinned Talos release has moved
out of `v1alpha1` is stated in its own document: at v1.13 that is every field
of `machine.network`, so KubeSpan, the node's hostname and its link
addressing travel as network documents (`KubeSpanConfig`, `HostnameConfig`,
`LinkAliasConfig`, `LinkConfig`, `DHCPv4Config`) the way the ingress
firewall always has
(`NetworkDefaultActionConfig`, `NetworkRuleConfig`). A node volume
travels as a `UserVolumeConfig`, the document v1.13 names in place of the
deprecated partitions of `machine.disks`, and so does the directory
`local-path` hands out, a `UserVolumeConfig` of its own on every node in
place of a kubelet mount: a host path a pod reaches is a user volume, and
the configuration carries no `machine.kubelet.extraMounts`.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, IPv6Network
from typing import Any, Literal, Protocol, cast

import pulumi
from pulumiverse_talos import client, machine
from pulumiverse_talos.cluster import (
    GetHealthClientConfigurationArgs,
    Kubeconfig,
    KubeconfigClientConfigurationArgs,
    get_health_output,
)
from pulumiverse_talos.machine.outputs import CertificateResult, ClientConfiguration, MachineSecretsResult

from kluster import conventions
from kluster.conventions.cluster import KUBECONFIG_RENEWAL
from putils import Component, async_output, resolve

#: What a node is to Kubernetes. The cloud fleet is control planes that also
#: carry workloads (architecture.md §1.1); the homelab machine is a worker.
Role = Literal['controlplane', 'worker']


class Refusal(enum.Enum):
    """Which rule the rolls handed to a `TalosCluster` or `TalosDay1` broke."""

    #: No control-plane node at all.
    NO_CONTROL_PLANE = enum.auto()
    #: A node named as both a control plane and a worker; `nodes` are those.
    BOTH_ROLES = enum.auto()
    #: A node named more than once within one roll; `nodes` are those.
    NAMED_TWICE = enum.auto()
    #: BGP peers keyed by nodes the cluster does not have.
    BGP_PEER_STRANGER = enum.auto()
    #: Installers keyed by nodes the cluster does not have.
    INSTALLER_STRANGER = enum.auto()
    #: Nodes with no installer of their own.
    INSTALLER_MISSING = enum.auto()
    #: Day 1 addresses keyed by nodes the cluster does not have.
    ADDRESS_STRANGER = enum.auto()
    #: Day 1 secondary addresses keyed by nodes the cluster does not have.
    SECONDARY_ADDRESS_STRANGER = enum.auto()
    #: Nodes day 1 has no address for.
    ADDRESS_MISSING = enum.auto()
    #: Volumes attached to nodes the cluster does not have; `volumes` are those.
    VOLUME_STRANGER = enum.auto()
    #: Nodes that more than one volume attaches to; `volumes` are theirs.
    VOLUMES_CROWDED = enum.auto()


class RollRefused(ValueError):
    """Rolls a `TalosCluster` or `TalosDay1` refused: `refusal` says which rule, `nodes` which nodes broke it.

    A `ValueError`, so a caller that catches one is unaffected; the message
    is for a person, and the fields are what a program tells refusals apart
    by. `volumes` names the volumes of a volume refusal.
    """

    def __init__(
        self, message: str, *, refusal: Refusal, nodes: Sequence[str] = (), volumes: Sequence[str] = ()
    ) -> None:
        super().__init__(message)
        self.refusal: Refusal = refusal
        self.nodes: tuple[str, ...] = tuple(nodes)
        self.volumes: tuple[str, ...] = tuple(volumes)


#: Kubelet reservations exist so eviction has something to work with: an
#: unreserved node starves its own control plane before the kubelet notices.
SYSTEM_RESERVED = {'cpu': '200m', 'memory': '512Mi', 'ephemeral-storage': '1Gi'}

#: The write rate, in MB/s, a control plane's boot volume takes beside etcd
#: with etcd's syncs intact: writes held to it kept an etcd-shaped
#: `fdatasync` at a p99 of 1.6 ms, where an image unpack's shape at the
#: volume's full rate stretched it to 15.6 s (nodes.md §1.2).
BOOT_VOLUME_SAFE_WRITE_MBPS = 12.0

#: The rate, in MB/s, one writer alone drives a control plane's boot volume
#: to: one sequential writer held it for the whole run, and four writers of
#: an image unpack's shape together reached no more (nodes.md §1.2).
BOOT_VOLUME_ONE_WRITER_MBPS = 51.7


def image_pull_bound(safe_mbps: float, writer_mbps: float) -> int:
    """How many images the kubelet may pull at once for the pulls to write within `safe_mbps` together.

    Each pull is counted at `writer_mbps`, what one writer drives the disk
    to, since no reading bounds a pull's own rate below that. The kubelet
    refuses a bound under one, so where a single pull may exceed `safe_mbps`
    the bound is one: the fewest pulls at once the kubelet can be held to,
    with that one pull's own rate beyond what the kubelet bounds.
    """
    return max(1, math.floor(safe_mbps / writer_mbps))


#: KubeSpan's WireGuard listen port. Talos fixes it (`KubeSpanDefaultPort`)
#: and `KubeSpanConfig` has no field to move it; WireGuard runs over UDP.
KUBESPAN_PORT = 51820

#: The kubelet's API, which the apiserver and metrics-server call.
KUBELET_PORT = 10250

#: The DHCPv6 client's port (RFC 8415 §7.2). A cloud node's IPv6 address is
#: leased over DHCPv6, and the server's Reply comes from its own address rather
#: than the multicast the Solicit went to, so it matches no connection-tracking
#: entry and needs an opening of its own.
DHCPV6_CLIENT_PORT = 546

#: IPv6 link-local addresses, which never leave the link. The DHCPv6 client
#: sends from its link-local address, and a server answering a link-local
#: destination answers from a link-local source (RFC 6724 §5, rule 2).
LINK_LOCAL = 'fe80::/10'

#: BGP. Only the homelab worker speaks it, and only with the gateway
#: (cluster-infra.md §2), so it is never one of the host openings below.
BGP_PORT = 179

#: The whole internet, both families — what a management port on a public
#: node is exposed to whether or not it is written down.
ANYWHERE: tuple[str, ...] = ('0.0.0.0/0', '::/0')

#: The cluster's own ranges: the cloud nodes' network, the homelab worker's
#: VLAN, and both pod ranges. A pod's call to a node's host network carries a
#: pod-range source; a node's call over a path that is not tunnelled carries a
#: node-network source. Traffic that rides KubeSpan needs none of them, since
#: it enters on the `kubespan` interface, which the ingress chain accepts ahead
#: of every rule.
CLUSTER_RANGES: tuple[str, ...] = tuple(
    str(network)
    for network in (
        conventions.VCN_CIDR,
        conventions.CLUSTER_VLAN.v4,
        conventions.POD_CIDR_V4,
        conventions.POD_CIDR_V6,
    )
)

#: The cluster's service subnets, IPv4 first: the cluster is dual-stack with
#: v4 primary (architecture.md §1.3). `node_patch` hands them to Kubernetes,
#: and their families are the ones the kubelet takes a node address in
#: (`kubelet_node_ip_patch`).
SERVICE_SUBNETS: tuple[IPv4Network | IPv6Network, ...] = (conventions.SERVICE_CIDR_V4, conventions.SERVICE_CIDR_V6)

#: The name this program gives a node's one physical link. Talos' network
#: documents address a link by name, and the name the kernel gives it
#: (`eth0`, `ens3`, `enp1s0`) is a property of the PCI topology the platform
#: builds and of the kernel's naming policy, neither of which this program
#: decides — so `uplink_alias_document` attaches this alias to whichever link
#: is physical, and every document that configures the link names the alias.
UPLINK = 'uplink'


@dataclass(frozen=True)
class Opening:
    """One hole in the node-local firewall: a port, its protocol, and who may come through.

    Talos' ingress firewall is default-deny, so an `Opening` is the only way
    traffic reaches a listener in the host network namespace. Neither the
    protocol nor the sources has a default: each opening states both, because
    a default is a guess about the listener, and on a public node the guess is
    the whole of what stands between the port and the internet.
    """

    port: int
    protocol: Literal['tcp', 'udp']
    subnets: tuple[str, ...]

    def document(self) -> dict[str, Any]:
        return {
            'apiVersion': 'v1alpha1',
            'kind': 'NetworkRuleConfig',
            # One port can be opened on both protocols, and Talos requires
            # each rule's name to be unique.
            'name': f'port-{self.port}-{self.protocol}',
            'portSelector': {'ports': [self.port], 'protocol': self.protocol},
            'ingress': [{'subnet': subnet} for subnet in self.subnets],
        }


#: The host ports a pod calls besides the kubelet's: the metrics endpoints the
#: scraper, a pod, reads off processes on the host network. Cilium's agent
#: (9962), operator (9963) and Envoy (9964), and Hubble's metrics server
#: (9965), at the defaults of the pinned chart (Cilium v1.20.4 `values.yaml`
#: L1501, L2661, L3077, L3405; the port table of `system_requirements.rst`
#: L437–457), every one of them on the host network; and the node exporter
#: (9100), on the host network in the monitoring chart's pinned
#: `prometheus-node-exporter` 4.55.1 (`values.yaml` L131, L382). The ports
#: are the pinned releases' rather than this program's decision, as the
#: kubelet's is Kubernetes'.
POD_REACHED_PORTS: tuple[int, ...] = (9962, 9963, 9964, 9965, 9100)

#: What terminates in the host network namespace, and who may reach it. The
#: firewall is the only filter on ports in front of a public node: the cloud
#: subnet's list narrows no port (declarative/physical.md §1–2).
#:
#: The management APIs (`conventions.MANAGEMENT_PORTS`), which the balancer
#: forwards from the same structure, are open to the internet; so is KubeSpan,
#: whose peers include the homelab worker at a home address nothing declares;
#: and so are the Gateways' listeners (`conventions.GATEWAY_PORTS`), over TCP,
#: on every node: a Gateway's packets are handed up the host's stack to the
#: node's Envoy, so they cross this firewall (rfc-007 §4.3); on a cloud node
#: those ports are the internet's by design and nothing on the host binds them
#: but through the proxy, and the worker's site gateway forwards none of them
#: from outside the site. The kubelet and the other ports a pod calls are
#: open to the cluster alone, the pod ranges included: a pod's call to its own
#: node arrives on the pod's device from the pod's address, while one to
#: another node is masqueraded to a node address and enters on `kubespan`.
#: The DHCPv6 client is open to the link alone. etcd and trustd have no opening: their
#: peers reach them over KubeSpan, which enters on `kubespan`. Nor has etcd's
#: metrics listener (`conventions.ETCD_METRICS_PORT`). Its readers arrive over KubeSpan or
#: on `lo`, both accepted ahead of every rule: the API server's node proxy
#: calls another control plane over KubeSpan and its own node on `lo`, and
#: vmagent, which runs on the worker alone (`components.monitoring`, its
#: placement held by a test), calls each control plane over KubeSpan,
#: masqueraded to the worker's address. Nothing outside the cluster reaches the
#: port. Inside it, more than the readers do: every KubeSpan peer, the
#: homelab worker's host among them; once Cilium runs, a pod on another node,
#: masqueraded to its node's address over KubeSpan; and a pod calling a host
#: address inside the pod range on its own node, which the chain's pod-range
#: accept admits on any port. That reach is accepted under threat-model.md §4,
#: failing its second clause: the listener serves counters, histograms and
#: health, never a key or a value, the class of host metrics rfc-007 §4.3
#: already lets a pod read. A scraper on a control plane, reading its own
#: node's listener from a pod address, would need an opening from the
#: cluster's ranges; keeping vmagent off the control planes is what keeps the
#: port without one.
#:
#: A raw port's (`conventions.routes.Port`) is absent: Cilium's BPF datapath
#: answers its LoadBalancer Service at tc ingress, ahead of nftables, so it
#: serves without a firewall entry.
HOST_OPENINGS: tuple[Opening, ...] = (
    *(Opening(port, 'tcp', ANYWHERE) for port in conventions.MANAGEMENT_PORTS),
    Opening(KUBESPAN_PORT, 'udp', ANYWHERE),
    *(Opening(port, 'tcp', ANYWHERE) for port in conventions.GATEWAY_PORTS),
    Opening(KUBELET_PORT, 'tcp', CLUSTER_RANGES),
    *(Opening(port, 'tcp', CLUSTER_RANGES) for port in POD_REACHED_PORTS),
    Opening(DHCPV6_CLIENT_PORT, 'udp', (LINK_LOCAL,)),
)

#: The ports `HOST_OPENINGS` opens, whatever the protocol or the sources.
HOST_PORTS: tuple[int, ...] = tuple(opening.port for opening in HOST_OPENINGS)


def etcd_metrics_url() -> str:
    """Where a control plane's etcd serves its metrics: every address of the node, both families.

    etcd opens the URL with a `tcp` listener, and on `tcp` an unspecified
    host is one dual-stack socket, so `0.0.0.0` answers on every IPv4 and
    IPv6 address alike. The host is unspecified rather than a node address
    because the patch is shared by every control plane, and each of its
    readers (`conventions.ETCD_METRICS_PORT`) — the API server's node proxy, and
    vmagent's node scrape from the worker over KubeSpan — dials the node's
    internal address, where a loopback listener would not answer. The
    firewall admits nothing to the port, so nothing outside the cluster
    reaches it.
    """
    return f'http://{IPv4Address(0)}:{conventions.ETCD_METRICS_PORT}'


def node_patch() -> dict[str, Any]:
    """What every machine in the cluster carries, whatever its role."""
    return {
        'machine': {
            'features': {
                # There is no kube-proxy to fall back on, so the node-local
                # apiserver front is mandatory rather than an optimization.
                'kubePrism': {'enabled': True, 'port': conventions.KUBEPRISM_PORT},
                # The cluster DNS forwards to Talos' own resolver on the host,
                # at a link-local address the baseline network policy's
                # metadata deny is shaped around (rfc-007 §4.5). The generated
                # configuration already turns both on; the design depends on
                # them, so they are stated rather than inherited, and
                # forwarding is refused without the resolver it forwards to.
                'hostDNS': {'enabled': True, 'forwardKubeDNSToHost': True},
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
                'serviceSubnets': [str(subnet) for subnet in SERVICE_SUBNETS],
            },
        },
    }


def installer_patch(installer: str) -> dict[str, Any]:
    """Name the node's own factory installer as the image it installs from.

    `installer` is the installer the Image Factory builds from the schematic
    the node's boot image was built from (`TalosArtifact.installer`), so it
    carries what that image carries, system extensions included. Without
    this patch the provider writes the stock `ghcr.io/siderolabs/installer`
    of the Talos release it was built against, which carries none.

    What reads the field, at v1.13.9, is narrower than its name suggests.
    Talos reads it in one place: the install task, which runs only on a
    machine with no META partition — a blank disk booted from an ISO or over
    PXE (`v1alpha1_sequencer_tasks.go`, `Install`). Every node here boots a
    disk image that already carries META, so none of them reads it at boot.
    An upgrade does not read it either: `talosctl upgrade` sends the image
    its `--image` flag names, whose default is the stock installer, and the
    node refuses a request that names none (`internal/app/lifecycle`,
    `Upgrade`). So the field is where the node's configuration states which
    installer is its own, and the upgrade runbook reads it from there
    (operations.md §2.1). Changing it alone needs no reboot:
    `machine.install` is among the fields Talos applies immediately
    (`CanApplyImmediate`). An apply that carries a reboot-requiring change
    beside it is staged whole, and the node's `persistent` configuration
    names the new installer until the next reboot makes it active.
    """
    return {'machine': {'install': {'image': installer}}}


def kubespan_document() -> dict[str, Any]:
    """KubeSpan on, which every machine in the cluster carries, at the MTU the design sizes Cilium from.

    `enabled` and the link's MTU are stated. The MTU is
    `conventions.KUBESPAN_MTU`, which rfc-007 §4.1 has Cilium's own MTU
    setting read too, because Cilium's tunnel crosses this link; it equals
    Talos' default, and is stated so that a moved default cannot leave the two
    apart. Every other field keeps the default Talos gives it: pod networks
    are not advertised over the mesh (Cilium's tunnel carries pod traffic),
    traffic to a peer that is down does not bypass the mesh, and no extra
    endpoints are harvested.

    `filters` is unset as well, so a node advertises every address it holds
    as its peers' allowed IPs, in both families alike: KubeSpan carries every
    packet addressed to a node's address, the GUA as the private IPv4, in
    both directions of a flow. Its one cost is a node's connection to the
    balancer's IPv6 front, whose answer, addressed to the caller's GUA, a
    backend sends over the mesh rather than back through the balancer, and
    nothing a node runs dials that front (declarative/physical.md §2,
    cluster/architecture.md §3.2). Excluding the GUAs
    (`excludeAdvertisedNetworks`) would make IPv6 a case of its own, and move
    node-to-node IPv6 off the mesh: onto the internet between the worker and
    the cloud nodes, and into the ingress firewall, whose cluster ranges name
    no GUA block and cannot name the worker's, which is the home provider's.

    It is a document of its own because the pinned release deprecates
    `machine.network.kubespan` for it, and a configuration carrying both is
    refused: the document's own validation rejects a `v1alpha1` that also
    configures KubeSpan.
    """
    return {'apiVersion': 'v1alpha1', 'kind': 'KubeSpanConfig', 'enabled': True, 'mtu': conventions.KUBESPAN_MTU}


def hostname_document(hostname: str) -> dict[str, Any]:
    """Name the machine `hostname`, whatever its platform or Talos itself would call it.

    The hostname is the name etcd knows a control plane's member by and the
    name the kubelet registers the node under, so it is the declaration's
    (`TalosCluster` passes the node's own name) rather than whatever the
    machine is handed. A static hostname outranks every other source Talos
    reads one from: the hostname OCI's VCN gives a cloud instance, which
    carries a suffix when another instance still holds the label, the nocloud
    seed's `local-hostname`, and the name Talos generates. So a re-created
    node takes its predecessor's name, and a control plane joining etcd with
    an empty data directory first removes any member carrying its own
    hostname, the one its predecessor left behind among them. One that joins
    while that predecessor still runs removes a live member, so the program
    creates no instance beside its predecessor on its own: every node's
    instance is protected, and the engine refuses its replacement
    (`components.cloud.nodes`, declarative/physical.md §1).

    `auto` is stated `off` because the configuration the provider generates
    for v1.13 already carries a `HostnameConfig` with `auto: stable`; a patch
    of the same kind merges into it, and the document refuses `hostname`
    beside any `auto` but `off`. It is a document of its own because v1.13
    deprecates `machine.network.hostname` for it.

    A changed hostname needs no reboot: whether an apply can go in without
    one is decided on the `v1alpha1` document alone (`CanApplyImmediate`),
    and this document is not part of it. The kubelet restarts under the new
    name, which registers a second Node object beside the old one; etcd
    takes the new name when it next starts.
    """
    return {'apiVersion': 'v1alpha1', 'kind': 'HostnameConfig', 'auto': 'off', 'hostname': hostname}


def control_plane_patch(*, cert_sans: Sequence[str], secretbox_secret: str | None = None) -> dict[str, Any]:
    """What only a control plane has: the apiserver, etcd and its disk, the CNI, the service proxy, apid's SANs, the Talos grant.

    A worker's configuration has no apiserver to harden and no etcd to
    encrypt or read, and the CNI and kube-proxy are installed by the control
    plane, so none of this belongs in the shared patch.

    etcd's disk is the boot volume, which the images the kubelet pulls are
    unpacked onto as well, and a burst of unpacks at the volume's full rate
    stretches etcd's syncs into seconds (nodes.md §1.2). So a control plane's
    kubelet pulls at most `image_pull_bound` images at once, sized from the
    rate etcd's syncs survive beside it, `BOOT_VOLUME_SAFE_WRITE_MBPS`, and
    the rate one writer reaches, `BOOT_VOLUME_ONE_WRITER_MBPS`. Unbounded,
    as Talos leaves the kubelet, every image a burst of scheduling asks for
    unpacks at once. The bound applies to a running node without a reboot:
    `machine.kubelet` is among the fields Talos applies immediately
    (`CanApplyImmediate`), and the kubelet restarts under the new
    configuration. A worker's disk holds no etcd, so its pulls stay unbounded.

    `cert_sans` go into two certificates, which read two fields: the
    Kubernetes API server's (`cluster.apiServer.certSANs`) and apid's
    (`machine.certSANs`). Nothing else puts a NAT address into apid's
    certificate. Talos fills it from the addresses the node holds on its
    links, loopback and its hostnames, and Talos' `oracle` platform reports no
    external address, so an address the cloud maps onto the node from outside
    is in neither certificate unless it is listed here. The management address
    every Talos call is dialed at (`TalosCluster`) is such an address. A worker
    renders neither field: nothing dials a worker's apid directly, since a
    call reaches it through a control plane.

    Talos ships no CNI and no kube-proxy here: Cilium, which `k8s-base`
    installs, is both (rfc-007 §4.2). kube-proxy is off from the first boot
    because turning it off later is a procedure rather than a configuration
    change: Talos applies a bootstrap manifest only while its object is
    missing and never deletes one, so the DaemonSet stays until a `talosctl
    upgrade-k8s` prunes it, and the rules kube-proxy programmed stay on each
    node until it reboots.

    The Talos grant is Talos API access from Kubernetes
    (`machine.features.kubernetesTalosAPIAccess`): a pod in an allowed
    namespace asks for a certificate through a `talos.dev/v1alpha1`
    `ServiceAccount`, and Talos issues it, renews it, and refuses a role or a
    namespace the field does not allow. It allows one grant,
    `conventions.ETCD_SNAPSHOT_GRANT`: one role, which the etcd snapshot reads
    with, in one namespace, `conventions.ETCD_BACKUP_NAMESPACE`, which no
    workload outside the etcd backup shares (storage.md §5; credentials.md
    §1, rule 3). Talos refuses the feature on a worker and
    runs its controller on a control plane alone, so a worker states none of
    it. The field is among those Talos applies immediately
    (`CanApplyImmediate`), so turning it on reboots no node. With it on,
    Talos applies the `serviceaccounts.talos.dev` CRD as a bootstrap
    manifest and keeps the `talos` Service in `default` pointing at the
    control planes, which is why a stack that declares such a
    `ServiceAccount` previews only once `physical` has applied this.
    """
    config: dict[str, Any] = {
        'machine': {
            'certSANs': list(cert_sans),
            'features': {
                'kubernetesTalosAPIAccess': {
                    'enabled': True,
                    'allowedRoles': [conventions.ETCD_SNAPSHOT_GRANT.role],
                    'allowedKubernetesNamespaces': [conventions.ETCD_SNAPSHOT_GRANT.namespace],
                },
            },
            'kubelet': {
                'extraConfig': {
                    'maxParallelImagePulls': image_pull_bound(BOOT_VOLUME_SAFE_WRITE_MBPS, BOOT_VOLUME_ONE_WRITER_MBPS),
                },
            },
        },
        'cluster': {
            'allowSchedulingOnControlPlanes': True,
            'network': {'cni': {'name': 'none'}},
            'proxy': {'disabled': True},
            'apiServer': {
                'certSANs': list(cert_sans),
                # A public 6443 warrants both, defaults notwithstanding.
                'extraArgs': {'anonymous-auth': 'false', 'audit-log-path': '/var/log/audit/kube/kube-apiserver.log'},
            },
            'etcd': {
                'advertisedSubnets': [str(conventions.VCN_CIDR)],
                # etcd's metrics listener (`etcd_metrics_url`).
                'extraArgs': {'listen-metrics-urls': etcd_metrics_url()},
            },
        },
    }
    if secretbox_secret is not None:
        # Encryption at rest for Kubernetes secrets. etcd lives in a $0-trust
        # tenancy, and its snapshots ship off-site hourly (storage.md §5; the
        # snapshot job is declared and takes none until bring-up stage 6 seals
        # its key — nodes.md §5 Tier 0), so the key material has to be stated
        # here rather than inherited from whatever the generator happened to
        # do.
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

    The node-side half of `node_volume_document`, and what the pod consuming
    the volume's class selects, and a copy's destination onto it, since a
    claim of the class holds no pod anywhere by itself (rfc-002 §10.5). A
    strategic merge into `v1alpha1`, because v1.13 has no document for node
    labels.
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


def kubelet_node_ip_patch(excluded: Sequence[str]) -> dict[str, Any]:
    """Keep the kubelet's node address off each of `excluded`, IPv4 addresses as `secondary_address_documents` adds.

    The kubelet's node IP is, per family, the first of the node's routed
    addresses that falls inside `machine.kubelet.nodeIP.validSubnets`, in
    Talos' default address order, which compares addresses numerically
    (`controllers/k8s/nodeip.go`; this program sets no
    `nodeAddressSortAlgorithm`). An address added beside the lease's is one
    more candidate, so without this patch the node IP is whichever of the two
    the cloud happened to assign lower.

    The subnets are the whole of each family the service subnets give, which
    is the set Talos takes when the field is unset (`nodeip_config.go`), and
    a negation of each excluded address as a host. So the candidates are exactly the
    default's less the excluded ones, and the kubelet keeps the address the
    node's lease gives it.
    """
    admitted = [str(type(subnet)((0, 0))) for subnet in SERVICE_SUBNETS]
    negated = [f'!{address}/32' for address in excluded]
    return {'machine': {'kubelet': {'nodeIP': {'validSubnets': [*admitted, *negated]}}}}


def etcd_advertised_patch(excluded: Sequence[str]) -> dict[str, Any]:
    """Keep each of `excluded` out of the addresses a control plane's etcd member advertises.

    With `cluster.etcd.advertisedSubnets` set, Talos advertises every current
    address of the node inside it, not the first one, and writes them all
    into the member's peer URLs (`controllers/etcd/spec.go`). The health
    check requires every peer-URL host of every member to be among the
    control-plane addresses it is given (`EtcdControlPlaneNodesAssertion`),
    and those are the names day 1 gives the nodes (`TalosDay1`). An address
    added inside the node network beside the lease's would be a second peer
    URL, and the check would fail on it until it timed out.

    The patch carries the negations alone. Talos appends the list a later
    patch carries to the one before it, and `patches` lists
    `control_plane_patch`, which names the node network, ahead of the
    document that carries this one, so the rendered list is the node network
    and then each negation. Talos folds the list in order, a negation
    dropping from what the subnets before it kept, so the negations have to
    come last.
    """
    return {'cluster': {'etcd': {'advertisedSubnets': [f'!{address}/32' for address in excluded]}}}


def secondary_address_documents(address: str, *, role: Role) -> list[dict[str, Any]]:
    """Put an extra address on the node's physical link, and keep the kubelet and etcd off it.

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

    The address is also excluded from the kubelet's node IP
    (`kubelet_node_ip_patch`), in this function rather than beside it, so
    that the address cannot be added without the exclusion. The node's
    Kubernetes address is then the lease's, which is the name Talos calls and
    the health gate know the node by (`TalosDay1`). On a control plane it is
    excluded from etcd's advertised addresses as well
    (`etcd_advertised_patch`), for the same reason and in the same place: the
    member's peer URLs are what the health gate holds to those names.
    """
    documents = [
        uplink_alias_document(),
        {'apiVersion': 'v1alpha1', 'kind': 'LinkConfig', 'name': UPLINK, 'addresses': [{'address': f'{address}/32'}]},
        {'apiVersion': 'v1alpha1', 'kind': 'DHCPv4Config', 'name': UPLINK, 'clientIdentifier': 'none'},
        kubelet_node_ip_patch([address]),
    ]
    if role == 'controlplane':
        documents.append(etcd_advertised_patch([address]))
    return documents


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
    Talos' own defaults, since no platform layer names any here. A cloud node
    is different: Talos' `oracle` platform gives it the cloud's resolver at
    `169.254.169.254`, and a platform's resolvers outrank the defaults.
    IPv6 is untouched, because the GUA this design expects is SLAAC and SLAAC
    is the kernel's, not DHCP's. So is the link-local, which the kernel forms
    from the NIC's MAC as an EUI-64 — nothing here sets `addr_gen_mode` or a
    `stable_secret` — and which the gateway routes the `lan` pool's IPv6 VIPs
    through: it holds only while the MAC does, which is why the worker's is
    declared (`conventions.HOMELAB_NODE_MAC`).

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
    openings = [*HOST_OPENINGS, *extra]
    return [
        {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'},
        *(opening.document() for opening in openings),
    ]


def patches(
    *,
    installer: str,
    role: Role = 'controlplane',
    hostname: str | None = None,
    cert_sans: Sequence[str] = (),
    secretbox_secret: str | None = None,
    static_address: StaticAddress | None = None,
    secondary_address: str | None = None,
    bgp_peer: str | None = None,
    volume: str | None = None,
) -> list[str]:
    """The patch list for one node, as the provider wants it.

    `installer` is the node's own factory installer (`installer_patch`). It
    has no default, because the default the provider fills in is the stock
    installer, which would drop the node's extensions on the upgrade that
    took it.

    `hostname` is the name the node takes (`hostname_document`). The
    program's own call, `TalosCluster`, passes every node's; a list rendered
    without one leaves the node to whatever name Talos generates.

    `cert_sans` are a control plane's certificate SANs (`control_plane_patch`);
    a worker renders none.

    `volume` is the name of the node volume attached to this node, if one is.

    `bgp_peer` is a subnet, not a host: it is who may open a BGP session with
    this node, and the answer for the homelab worker is the gateway alone.

    `static_address` is the node's own address where no platform assigns one,
    and `secondary_address` an extra address on top of whichever it already
    has; no node has both, and a node with neither is configured by its lease.
    """
    documents: list[Mapping[str, Any]] = [
        node_patch(),
        installer_patch(installer),
        kubespan_document(),
        local_path_volume_document(),
    ]
    if hostname is not None:
        documents.append(hostname_document(hostname))
    if volume is not None:
        documents.append(node_volume_label_patch(volume))
    if role == 'controlplane':
        documents.append(control_plane_patch(cert_sans=cert_sans, secretbox_secret=secretbox_secret))
    if static_address is not None:
        documents += static_address_documents(static_address)
    if secondary_address is not None:
        documents += secondary_address_documents(secondary_address, role=role)
    if volume is not None:
        documents.append(node_volume_document(volume))
    extra = [Opening(BGP_PORT, 'tcp', (bgp_peer,))] if bgp_peer is not None else []
    documents += ingress_firewall_documents(extra)
    return [json.dumps(document) for document in documents]


class TalosCluster(Component, pulumi_type='kluster:talos:TalosCluster'):
    """The cluster PKI and the configuration each machine boots with.

    The secrets land in Pulumi state — the provider's ephemeral resources do
    not bridge — which is why the state backend is passphrase-encrypted behind
    client-certificate TLS.

    Day 0 delivers each machine its configuration out of band: `user_data` in
    OCI instance metadata for the cloud nodes, a cloud-init seed for the
    homelab VM. Everything that needs a machine to answer — applying later
    changes, bootstrapping etcd, the health gate, the two credentials the
    later stacks are built on — is `TalosDay1`.

    :param management_address: the one address the machine API is dialed at
        (`TalosDay1`): the balancer's, which forwards apid to every control
        plane, as it forwards the Kubernetes API that `endpoint` names. Every
        control plane renders it into both certificates (`control_plane_patch`),
        in the configuration it boots with and so in every one applied to it,
        because a call verifies the certificate against the address it
        dialed and nothing else would put a NAT address there.
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
    :param installers: node name to the installer image of the artifact
        that node boots (`TalosArtifact.installer`), which its configuration
        names as its own (`installer_patch`). Every node needs one, and from
        the artifact it boots: the installer is built from a schematic, and
        only the boot image's schematic carries what the node runs.
    :param volumes: the node volumes, by name (`conventions.NODE_VOLUMES`).
        Each is mounted, and its node labelled, in the configuration of the
        node it attaches to, so the volume is part of what that machine boots
        with: the census is known at day 0, while the attachment itself can
        only be declared once the instance exists. A volume on a node outside
        the cluster, or two volumes on one node, are refused — the disk
        selector finds a node's data disk as the one disk that is not the
        boot disk, so it cannot tell two data disks apart.

    A node's name in the rolls is also its hostname (`hostname_document`), in
    the configuration it boots with and in every one applied to it. A node
    named in `STATIC_ADDRESSES` also boots with its own address, its subnet's
    prefix and a default route, rather than with whatever a DHCP server
    offers it.
    """

    def __init__(
        self,
        name: str,
        *,
        cluster_name: str,
        endpoint: pulumi.Input[str],
        management_address: pulumi.Input[str],
        control_plane_nodes: Sequence[str],
        worker_nodes: Sequence[str],
        talos_version: str,
        bgp_peers: Mapping[str, pulumi.Input[str]],
        installers: Mapping[str, pulumi.Input[str]],
        volumes: Mapping[str, conventions.NodeVolumeEntry],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        if not control_plane_nodes:
            raise RollRefused('a cluster needs at least one control-plane node', refusal=Refusal.NO_CONTROL_PLANE)
        self.control_plane_nodes = tuple(control_plane_nodes)
        self.worker_nodes = tuple(worker_nodes)
        self.roles: dict[str, Role] = {
            **{node: cast('Role', 'controlplane') for node in self.control_plane_nodes},
            **{node: cast('Role', 'worker') for node in self.worker_nodes},
        }
        if len(self.roles) != len(self.control_plane_nodes) + len(self.worker_nodes):
            both = sorted(set(self.control_plane_nodes) & set(self.worker_nodes))
            if both:
                raise RollRefused(
                    f'a node is both a control plane and a worker: {both}', refusal=Refusal.BOTH_ROLES, nodes=both
                )
            twice = sorted(
                {
                    node
                    for roll in (self.control_plane_nodes, self.worker_nodes)
                    for node in roll
                    if roll.count(node) > 1
                }
            )
            raise RollRefused(
                f'a node is named once in its roll, and {twice} are named more than once',
                refusal=Refusal.NAMED_TWICE,
                nodes=twice,
            )

        self.cluster_name = cluster_name
        self._endpoint = endpoint
        self._talos_version = talos_version
        #: Where every Talos call is dialed, and what every control plane's
        #: certificates carry for it.
        self.management_address: pulumi.Output[str] = pulumi.Output.from_input(management_address)
        self._bgp_peers = dict(bgp_peers)
        unknown = sorted(set(self._bgp_peers) - set(self.roles))
        if unknown:
            raise RollRefused(
                f'BGP peers name nodes that are not in the cluster: {unknown}',
                refusal=Refusal.BGP_PEER_STRANGER,
                nodes=unknown,
            )
        self._installers = dict(installers)
        unknown = sorted(set(self._installers) - set(self.roles))
        if unknown:
            raise RollRefused(
                f'installers name nodes that are not in the cluster: {unknown}',
                refusal=Refusal.INSTALLER_STRANGER,
                nodes=unknown,
            )
        missing = sorted(set(self.roles) - set(self._installers))
        if missing:
            raise RollRefused(
                f'every node names its own installer, and {missing} name none',
                refusal=Refusal.INSTALLER_MISSING,
                nodes=missing,
            )
        self._volumes = _volumes_by_node(volumes, self.roles)

        self.secrets = machine.Secrets(
            f'{name}-secrets',
            talos_version=talos_version,
            # Regenerating cluster PKI is a rebuild, not an update.
            opts=self.child_opts(protect=True),
        )
        # Part of the workaround layer at the end of this module.
        self._machine_secrets = self.secrets.machine_secrets.apply(_machine_secrets_input)

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
            machine_secrets=self._machine_secrets,
            talos_version=self._talos_version,
            # The endpoint is the load balancer's address, which exists only
            # once that resource does — so the patches are computed inside an
            # async_output rather than at declaration time.
            config_patches=async_output(partial(self._patches, node, role, secondary_address)),
            opts=pulumi.InvokeOutputOptions(parent=self),
        )

    async def _patches(self, node: str, role: Role, secondary_address: pulumi.Input[str] | None) -> list[str]:
        return patches(
            installer=str(await resolve(self._installers[node])),
            role=role,
            # The node's name in the declaration is its hostname.
            hostname=node,
            cert_sans=[str(await resolve(self.management_address))],
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


class TalosDay1(Component, pulumi_type='kluster:talos:TalosDay1'):
    """Apply, bootstrap, health, and the credentials the rest of the world reads.

    Everything here talks to machines that already run, over apid on port
    50000, which is why it is a component of its own: it is declared with the
    addresses those machines hold, and those exist only once the instances
    and the worker VM do.

    A call over apid carries two addresses that are not the same thing: the
    *endpoint* it opens a connection to and verifies the certificate of, and
    the *node* it names in the request. Every call here is dialed at one
    endpoint, the cluster's management address (`TalosCluster`), which every
    control plane's apid certificate carries. A control plane's apid forwards
    a call to the node it names, over port 50000 with the machine PKI,
    verifying that node's certificate against the name; it does so without
    etcd or Kubernetes, and for a name that is its own as much as for
    another's. So the bootstrap, the first contact of all, goes the way every
    apply does, and so does a call to the worker, which no endpoint outside
    the site reaches directly.

    A node is therefore named by an address it holds on an interface, which
    its certificate carries whatever its configuration says. A cloud node's
    public IPv4 is a NAT mapping on no interface and in no certificate, and
    it is no address Kubernetes reports for the node either, so the health
    gate could not match it to one.

    :param cluster: the PKI and the day-0 configuration: what a machine boots with
        when it is launched, and what is applied to it here. Its management
        address is what every call is dialed at.
    :param addresses: node name to the address the node is *named* by, one
        it holds on an interface. Every node of the cluster needs one — a
        cluster is not healthy because the nodes somebody listed are.
    :param forward: what carries a call from the management address to apid:
        the listener on 50000 and the backends behind it. Every call here
        depends on them. The address exists before they do, so no input
        carries the order.
    :param secondary_addresses: node name to an extra address to put on its
        physical link (the node's dedicated VIP).
    """

    def __init__(
        self,
        name: str,
        *,
        cluster: TalosCluster,
        addresses: Mapping[str, pulumi.Input[str]],
        forward: Sequence[pulumi.Resource],
        secondary_addresses: Mapping[str, pulumi.Input[str]] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.cluster = cluster
        self._addresses = dict(addresses)
        self._secondary_addresses = dict(secondary_addresses or {})
        for label, keyed, refusal in (
            ('addresses', self._addresses, Refusal.ADDRESS_STRANGER),
            ('secondary addresses', self._secondary_addresses, Refusal.SECONDARY_ADDRESS_STRANGER),
        ):
            unknown = sorted(set(keyed) - set(cluster.roles))
            if unknown:
                raise RollRefused(
                    f'{label} name nodes that are not in the cluster: {unknown}', refusal=refusal, nodes=unknown
                )
        missing = sorted(set(cluster.roles) - set(self._addresses))
        if missing:
            raise RollRefused(
                f'day 1 needs an address for every node, and {missing} have none',
                refusal=Refusal.ADDRESS_MISSING,
                nodes=missing,
            )
        endpoint = cluster.management_address
        forward = list(forward)

        # What is applied is the current day-0 rendering plus whatever only
        # exists now: the secondary private IP behind the dedicated VIP. A node
        # launched before a configuration change booted an older rendering, and
        # this apply brings it current.
        self.configurations = {
            node: cluster.configuration(node, secondary_address=self._secondary_addresses.get(node))
            for node in cluster.roles
        }

        # Every `_client_configuration_input` below is part of the workaround
        # layer at the end of this module.
        generated = cluster.secrets.client_configuration
        client_configuration = _client_configuration_input(generated, machine.ClientConfigurationArgs)

        # Node-serial by construction: each apply waits for the one before it,
        # so a change that reboots the machine never takes the quorum with it.
        self.applies: dict[str, machine.ConfigurationApply] = {}
        previous: list[pulumi.Resource] = []
        for node in cluster.roles:
            applied = machine.ConfigurationApply(
                f'{name}-{node}-config',
                node=self._addresses[node],
                endpoint=endpoint,
                client_configuration=client_configuration,
                machine_configuration_input=self.configurations[node].machine_configuration,
                # A change needing a reboot is staged rather than applied, so
                # the reboot is an operator's decision and not a side effect.
                apply_mode='staged_if_needing_reboot',
                # `reset` wipes STATE and EPHEMERAL — every partition the node
                # has (provider issue #205). It stays off on every node,
                # whatever it carries: replacing a node is an explicit
                # procedure, in which a cloud node's instance leaves the
                # state only through a refresh that reads it gone
                # (operations.md §3.1), never something a destroy of this
                # resource does on its own.
                on_destroy=machine.ConfigurationApplyOnDestroyArgs(reset=False, graceful=True, reboot=False),
                opts=self.child_opts(depends_on=[*forward, *previous]),
            )
            self.applies[node] = applied
            previous = [applied]

        first = cluster.control_plane_nodes[0]
        # Bootstrap is a once-per-cluster operation on one node: running it on
        # a second control plane would try to start a second etcd cluster.
        self.bootstrap = machine.Bootstrap(
            f'{name}-bootstrap',
            node=self._addresses[first],
            endpoint=endpoint,
            client_configuration=client_configuration,
            opts=self.child_opts(depends_on=[*forward, self.applies[first]]),
        )

        # The client certificate is issued again by whichever apply runs
        # within the renewal window of its expiry. The window is stated, so
        # the alert on the copies' expiry is held against the one this
        # resource takes (`conventions.cluster.KUBECONFIG_ALERT_MARGIN`).
        self.kubeconfig_source = Kubeconfig(
            f'{name}-kubeconfig',
            node=self._addresses[first],
            endpoint=endpoint,
            client_configuration=_client_configuration_input(generated, KubeconfigClientConfigurationArgs),
            certificate_renewal_duration=_go_duration(KUBECONFIG_RENEWAL),
            opts=self.child_opts(depends_on=[*forward, self.bootstrap]),
        )

        # Every node is nameable, by the names the applies use, through the
        # one endpoint they are dialed at — the file a human then holds.
        self._client_configuration = client.get_configuration_output(
            cluster_name=cluster.cluster_name,
            client_configuration=_client_configuration_input(generated, client.GetConfigurationClientConfigurationArgs),
            endpoints=endpoint.apply(lambda address: [address]),
            nodes=async_output(partial(self._addresses_of, tuple(cluster.roles))),
            opts=pulumi.InvokeOutputOptions(parent=self),
        )

        # The gate. This data source does not return until the cluster reports
        # healthy, so anything that resolves it is ordered behind a working
        # cluster rather than behind a resource that merely finished.
        #
        # It names each node as the applies do, which is the address the
        # node's kubelet reports to Kubernetes, and the check compares the two.
        self.health = get_health_output(
            client_configuration=_client_configuration_input(generated, GetHealthClientConfigurationArgs),
            control_plane_nodes=async_output(partial(self._addresses_of, cluster.control_plane_nodes)),
            worker_nodes=async_output(partial(self._addresses_of, cluster.worker_nodes)),
            endpoints=endpoint.apply(lambda address: [address]),
            opts=pulumi.InvokeOutputOptions(parent=self, depends_on=[*forward, self.bootstrap, *self.applies.values()]),
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


def _go_duration(delta: dt.timedelta) -> str:
    """A length of time as Go's `time.ParseDuration` reads it, in whole hours, the unit the provider's default is in."""
    hours, rest = divmod(delta, dt.timedelta(hours=1))
    if rest:
        raise ValueError(f'{delta} is not a whole number of hours')
    return f'{hours}h'


def _volumes_by_node(volumes: Mapping[str, conventions.NodeVolumeEntry], nodes: Mapping[str, Role]) -> dict[str, str]:
    """Node name to the name of the one volume attached to it, sentinels resolved."""
    unknown = sorted(name for name, entry in volumes.items() if entry.attached_node not in nodes)
    if unknown:
        raise RollRefused(
            f'volumes attach to nodes that are not in the cluster: {unknown}',
            refusal=Refusal.VOLUME_STRANGER,
            nodes=sorted({volumes[name].attached_node for name in unknown}),
            volumes=unknown,
        )
    by_node: dict[str, list[str]] = {}
    for name, entry in sorted(volumes.items()):
        by_node.setdefault(entry.attached_node, []).append(name)
    crowded = {node: names for node, names in by_node.items() if len(names) > 1}
    if crowded:
        raise RollRefused(
            f'a node carries at most one volume, because its disk selector cannot tell two data disks apart: {crowded}',
            refusal=Refusal.VOLUMES_CROWDED,
            nodes=sorted(crowded),
            volumes=sorted(name for names in crowded.values() for name in names),
        )
    return {node: names[0] for node, names in by_node.items()}


async def _resolved(inputs: Sequence[pulumi.Input[str]]) -> list[str]:
    if not inputs:
        return []
    values = await resolve(*inputs)
    return [str(value) for value in (values if len(inputs) > 1 else (values,))]


async def _resolved_one(value: pulumi.Input[str] | None) -> str | None:
    return None if value is None else str(await resolve(value))


# -- workaround: the provider's output types, as its input types ------------
#
# A layer of its own, around an upstream defect: an output of one resource
# whose type is a structure cannot be handed on as another's input
# (pulumi/pulumi#15883; for this provider, pulumiverse/pulumi-talos#93 and
# #200). The secrets bundle and the client configuration are read back from
# the `Secrets` resource as the provider's output types, and an output type is
# a mapping keyed by Python names (`k8s_aggregator`). The SDK serializes a
# mapping it has no type for key by key, as it stands, and an invoke gives it
# no type, so the provider would receive the Python spellings in place of its
# own (`k8sAggregator`) and refuse every multi-word field as missing. An input
# type is serialized by its own field metadata, under the provider's names,
# wherever it is handed. The SDK's `…ArgsDict` form is keyed by Python names
# too, so an invoke sends it the way it sends an output type; only an `…Args`
# instance carries the provider's names. So each hand-off builds the `…Args`
# its consumer declares.
#
# The layer is:
#   - `TalosCluster._machine_secrets`, which `_render` hands on;
#   - in `TalosDay1.__init__`, each `_client_configuration_input(generated,
#     …)` that feeds the applies, the bootstrap, the kubeconfig, the client
#     configuration invoke and the health invoke;
#   - this section: `_machine_secrets_input`, `_certificate_input`,
#     `_ClientConfigurationInput` and `_client_configuration_input`.
# Removing it means handing `self.secrets.machine_secrets` and
# `cluster.secrets.client_configuration` on directly, and deleting this
# section.
#
# It goes once an output of `Secrets`, handed unchanged to an invoke, reaches
# the provider under the provider's names and type-checks against the
# consumer's parameter. `test_the_sdk_still_hands_an_output_type_to_an_invoke_under_python_names`
# in `tests/test_talos_day1.py` is the tripwire: it goes red the day the first
# half holds.
#
# Each is an `apply` on the output itself rather than a `resolve`: what a
# resolve returns is the plain mapping, without the attributes read here.


def _machine_secrets_input(bundle: MachineSecretsResult) -> machine.MachineSecretsArgs:
    """The secrets bundle `Secrets` generated, as the input `get_configuration` takes."""
    certs = bundle.certs
    return machine.MachineSecretsArgs(
        certs=machine.CertificatesArgs(
            etcd=_certificate_input(certs.etcd),
            k8s=_certificate_input(certs.k8s),
            k8s_aggregator=_certificate_input(certs.k8s_aggregator),
            k8s_serviceaccount=machine.KeyArgs(key=certs.k8s_serviceaccount.key),
            os=_certificate_input(certs.os),
        ),
        cluster=machine.ClusterArgs(id=bundle.cluster.id, secret=bundle.cluster.secret),
        secrets=machine.KubernetesSecretsArgs(
            bootstrap_token=bundle.secrets.bootstrap_token,
            secretbox_encryption_secret=bundle.secrets.secretbox_encryption_secret,
            aescbc_encryption_secret=bundle.secrets.aescbc_encryption_secret,
        ),
        trustdinfo=machine.TrustdInfoArgs(token=bundle.trustdinfo.token),
    )


def _certificate_input(certificate: CertificateResult) -> machine.CertificateArgs:
    return machine.CertificateArgs(cert=certificate.cert, key=certificate.key)


class _ClientConfigurationInput[T](Protocol):
    """The constructor of one of the provider's client-configuration input types, which share their fields."""

    def __call__(self, *, ca_certificate: str, client_certificate: str, client_key: str) -> T: ...


def _client_configuration_input[T](
    configuration: pulumi.Output[ClientConfiguration], as_input: _ClientConfigurationInput[T]
) -> pulumi.Output[T]:
    """The client configuration `Secrets` generated, as the input type `as_input` builds."""
    return configuration.apply(
        lambda it: as_input(
            ca_certificate=it.ca_certificate, client_certificate=it.client_certificate, client_key=it.client_key
        )
    )
