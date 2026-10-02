"""Inside the cluster: its address ranges, its ports, its mesh's MTU, its routing session, its pools, its namespaces, its storage classes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Network, IPv6Network
from typing import Literal, NamedTuple

from kluster.conventions.cloud import USER_VOLUME_ROOT
from kluster.conventions.identity import LABEL_DOMAIN

#: Talos/Cilium pod and service ranges, IPv4 first (architecture.md §1.3).
#: Deliberately not 10.42/10.43: those are the legacy k3s cluster's, and the
#: two clusters are routed to each other for the length of the migration.
POD_CIDR_V4 = IPv4Network('10.244.0.0/16')
POD_CIDR_V6 = IPv6Network('fd00:10:244::/56')
SERVICE_CIDR_V4 = IPv4Network('10.96.0.0/12')
SERVICE_CIDR_V6 = IPv6Network('fd00:10:96::/112')


class ManagementPorts(NamedTuple):
    """The two APIs the cluster is managed through, which three declarations agree on.

    Both terminate on the nodes themselves and belong to the cluster rather
    than to any workload. The balancer forwards each to every control plane,
    the node firewall opens each to the internet, and the cluster endpoint the
    machine configuration names is the balancer's address on the Kubernetes
    one -- so the endpoint, the backend set and the opening are one structure,
    and none of them can name a port the others do not. Iterating it yields
    the set the balancer and the firewall enumerate.
    """

    kubernetes: int
    """kube-apiserver, a hostNetwork static pod: the cluster endpoint's port.

    Talos's default `localAPIServerPort`, which this installation leaves
    unset -- the number is upstream's, and editing it here moves the
    listener, the opening and the endpoint but not the server.
    """
    talos: int
    """apid, the machine API, on the port Talos fixes it at and exposes no knob for.

    Day 1 and every configuration change go over it.
    """


MANAGEMENT_PORTS = ManagementPorts(kubernetes=6443, talos=50000)

#: KubePrism — the node-local kube-apiserver front the Cilium datapath uses
#: (there is no kube-proxy to fall back on). Node-local, so it is neither a
#: management port above nor a public one below.
KUBEPRISM_PORT = 7445

#: BGP (cluster-infra.md §2). The UDM's FRR is AS 65000; the cluster peers
#: from a distinct private ASN so the session is eBGP.
UDM_ASN = 65000
CLUSTER_ASN = 65001

#: The `internet` pool holds on-the-wire node addresses — private IPv4s (OCI
#: 1:1-NATs the public v4, so a node's public literal never matches) and v6
#: GUAs — and the balancer's two public addresses, which no arriving traffic
#: carries and which answer connections that start in the cluster (rfc-007
#: §4.4). Its membership is a physical-stack output, not a constant; the `lan`
#: pool's range is `site.LAN_POOL`, which is a decision of this program.
POOL_INTERNET = 'internet'

#: A Service opts into a pool by carrying this label; the pools'
#: `serviceSelector` matches on it.
LB_POOL_LABEL = f'{LABEL_DOMAIN}/lb-pool'

GATEWAY_NAMESPACE = 'gateways'
GATEWAY_INTERNET = 'internet-gw'
GATEWAY_LAN = 'lan-gw'
#: Same shape as lan-gw on its own VIP; attaching a route here *is* the
#: decision "reachable from the IoT VLAN" (cluster-infra.md §2).
GATEWAY_MEDIA = 'media-gw'

#: The namespaces a sealed value is bound to (`conventions.sealed`): `kubeseal`
#: seals each value strict, its namespace part of the ciphertext, so the
#: command that seals it and the stack that installs what reads it have to
#: agree on the name.
#:
#: The sealed-secrets controller's name and namespace are the two `kubeseal`
#: looks for unless told otherwise, so the chart is installed under them
#: (rfc-007 §6.1) and the certificate a value is sealed to is fetched from
#: them.
SEALING_CONTROLLER = 'sealed-secrets-controller'
SEALING_NAMESPACE = 'kube-system'
#: cert-manager's own: a cluster issuer's credentials are read from the
#: controller's namespace under the chart's defaults (rfc-007 §5.2).
CERT_MANAGER_NAMESPACE = 'cert-manager'
#: Cilium's secrets namespace for BGP, the release's default (rfc-007 §4.6).
BGP_SECRETS_NAMESPACE = 'kube-system'
#: The monitoring stack's, alertmanager's among it (rfc-007 §7).
MONITORING_NAMESPACE = 'monitoring'

#: The MTU of KubeSpan's WireGuard link, which the machine configuration
#: states; rfc-007 §4.1 has `k8s-base` size Cilium from it too. Cilium's own MTU setting
#: is the underlying network's, and the network its tunnel crosses is this
#: link, which Talos selects by a firewall mark rather than by a route, so
#: detection would size it from the node's interface instead. Talos' default
#: (`constants.KubeSpanLinkMTU`), stated rather than inherited so that a moved
#: default cannot leave the two apart.
KUBESPAN_MTU = 1420


class Transports(Enum):
    """The transport protocols a public port is served on, which one listener carries together."""

    TCP = 'tcp'
    UDP = 'udp'
    TCP_AND_UDP = 'tcp+udp'

    @property
    def protocols(self) -> tuple[Literal['tcp', 'udp'], ...]:
        """Each protocol on its own, as a firewall rule names one."""
        match self:
            case Transports.TCP:
                return ('tcp',)
            case Transports.UDP:
                return ('udp',)
            case Transports.TCP_AND_UDP:
                return ('tcp', 'udp')


class Front(Enum):
    """Where a public port's traffic enters the installation."""

    BALANCER = 'balancer'
    """The network load balancer, which forwards to every cloud node."""
    DEDICATED_VIP = 'dedicated-vip'
    """The dedicated VIP, which one node holds (`cloud.DEDICATED_VIP_NODE`) and
    no listener forwards to."""


class Answerer(Enum):
    """What takes a public port's traffic once it reaches a node."""

    GATEWAYS = 'gateways'
    """The Gateways, whose Envoy runs on the host: the datapath hands the
    packet up the host's stack, through the node firewall, so the port is one
    the firewall opens."""
    PODS = 'pods'
    """A LoadBalancer Service's pods, which the datapath reaches ahead of the
    node firewall, so the port is one the firewall never sees."""


@dataclass(frozen=True)
class PublicPort:
    """One row of the public port census: a port the internet reaches, and how."""

    name: str
    """What the port serves, never its number: the balancer's listener and
    backend set are named after it, and after it and the family for IPv6."""
    port: int
    transports: Transports
    front: Front
    answerer: Answerer


#: Public port census — every port the internet reaches a service of the
#: cluster on through the cloud: at the balancer, or at the dedicated VIP. A
#: node's own internet-facing ports are not rows (the management ports,
#: `MANAGEMENT_PORTS`, and KubeSpan's, which the Talos component opens), and
#: the home site's is `QBITTORRENT_PEER_PORT` below. Read by `physical` for
#: the node firewall's openings of the rows the Gateways answer; rfc-007 §5.3
#: makes `k8s-base` (the Gateways' ports) and `apps` (the raw Services) its
#: readers too. No security rule names a port: the cloud subnet admits
#: everything and the node's firewall is the filter (physical.md §1–2). It is
#: the firewall-audit reference too, and what the recorded fallback copies
#: into machine configuration for the rows a Service's pods answer.
PUBLIC_PORT_CENSUS: tuple[PublicPort, ...] = (
    # Redirects to HTTPS and nothing else.
    PublicPort('http', 80, Transports.TCP, Front.BALANCER, Answerer.GATEWAYS),
    PublicPort('https', 443, Transports.TCP, Front.BALANCER, Answerer.GATEWAYS),
    # syncthing's discovery server, which terminates its own TLS.
    PublicPort('syncthing-discovery', 8443, Transports.TCP, Front.BALANCER, Answerer.PODS),
    PublicPort('syncthing', 22000, Transports.TCP_AND_UDP, Front.BALANCER, Answerer.PODS),
    PublicPort('hath', 60011, Transports.TCP, Front.DEDICATED_VIP, Answerer.PODS),
)

#: The bulk-transfer peer port, which the *site* gateway terminates rather than
#: the balancer above: inbound peer traffic reaches the worker VM through the
#: home site, and the census above is the cloud half of the same subject. Two
#: firewall declarations name it — the IPv6 pinhole and the IPv4 forward
#: (physical/gateway.md §4.2) — and they have to agree, which is what makes it a
#: convention rather than a setting (rfc-002 §11).
QBITTORRENT_PEER_PORT = 53363

SC_LOCAL_PATH = 'local-path'
SC_NAS = 'nas'
SC_CLOUD_BLOCK = 'cloud-block'

#: The Talos user volume `local-path` hands directories out of, on every
#: node: a `directory` volume, which has no disk of its own and lives on the
#: system disk's EPHEMERAL partition (physical.md §2; storage.md §2 says what
#: that makes its lifetime). It shares Talos' one namespace of user-volume
#: names with the node volumes (`cloud.NODE_VOLUMES`), so no row may take it.
LOCAL_PATH_VOLUME = 'storage'

#: Where that volume is mounted, and so the directory the provisioner is
#: pointed at: Talos mounts a user volume at its root under the volume's
#: name. The StorageClass above is k8s-base's.
LOCAL_PATH_ROOT = f'{USER_VOLUME_ROOT}/{LOCAL_PATH_VOLUME}'
