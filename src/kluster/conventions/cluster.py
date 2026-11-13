"""Inside the cluster: its address ranges, its ports, its mesh's MTU, its routing session, its pools, its namespaces, its storage classes."""

from __future__ import annotations

from enum import Enum
from ipaddress import IPv4Network, IPv6Network
from typing import Literal, NamedTuple

from kluster.conventions.cloud import NODE_VOLUMES, USER_VOLUME_ROOT
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
#: management port above nor a Gateway port below.
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

#: The worker's own pool: its IPv4 (`HOMELAB_NODE_IPV4`) and its GUA, which
#: is a physical-stack output, the same kind of member as the `internet`
#: pool's node addresses. A `SiteForward` port's Service asks for them,
#: because the site's forward and pinhole end at the worker's own address,
#: which is the address its outbound traffic wears, and no Service may hold a
#: host port (rfc-008 §4.1). Nothing announces it: the addresses are the
#: worker's own, routed to it as the node it is.
POOL_WORKER = 'worker'

#: A Service opts into a pool by carrying this label; the pools'
#: `serviceSelector` matches on it.
LB_POOL_LABEL = f'{LABEL_DOMAIN}/lb-pool'

#: The label a restic source carries its retention class's name under.
#: VolSync's own metrics carry no class, so a series that needs one takes it
#: from here (declarative/workloads.md §3).
RETENTION_LABEL = f'{LABEL_DOMAIN}/retention'

#: The fields of a namespace's sealed writer key: restic's own names for an S3
#: key's two halves, read off the mover's environment. A repository Secret
#: carries them as they are, and a key under any other field is refused.
WRITER_KEY_FIELDS = frozenset({'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'})

GATEWAY_NAMESPACE = 'gateways'
GATEWAY_INTERNET = 'internet-gw'
GATEWAY_LAN = 'lan-gw'
#: Same shape as lan-gw on its own VIP; attaching a route here *is* the
#: decision "reachable from the IoT VLAN" (cluster-infra.md §2).
GATEWAY_MEDIA = 'media-gw'

#: The namespaces a sealed value is bound to (`conventions.sealed`): `kubeseal`
#: seals a value strict unless its row says namespace-wide, as a writer key's
#: does, and either scope binds its namespace into the ciphertext, so the
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
    """The transport protocols a raw port is served on, which one listener carries together."""

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


class GatewayPorts(NamedTuple):
    """The ports every Gateway listens on, over TCP, which three declarations agree on.

    The Gateways' own and no application's: each Gateway answers its HTTPS
    listeners on one and redirects plain HTTP off the other (rfc-007 §5.2).
    The balancer forwards both to every cloud node, always, since the
    Gateways answer there and nowhere else on the internet; the node firewall
    opens both, since a Gateway's packets are handed up the host's stack to
    the node's Envoy (rfc-007 §4.3); and every Gateway's listeners take
    them. An application's raw port is a `conventions.routes.Port` instead,
    and none may claim one of these on a front a Gateway answers. Iterating
    it yields the set the balancer and the firewall enumerate, each named
    after its field.
    """

    http: int
    """Answered with a redirect to HTTPS and nothing else."""
    https: int


GATEWAY_PORTS = GatewayPorts(http=80, https=443)

SC_LOCAL_PATH = 'local-path'
#: The class a NAS claim's static PersistentVolume and its claim name, which no
#: StorageClass object answers to: it is what binds the one to the other, and
#: keeps every provisioner off both (`kluster.components.apps.storage`).
SC_NAS = 'nas'


def node_volume_class(row: str) -> str:
    """The `local-path` class served from the node volume `row` of `cloud.NODE_VOLUMES`.

    One per row, each a pool of its own in the provisioner's configuration at
    the row's mount (`cloud.node_volume_mount`), which `k8s-base` declares
    (storage.md §6). A row the table does not hold has no class, and is
    refused rather than named.
    """
    if row not in NODE_VOLUMES:
        raise ValueError(f'{row!r} is no node volume: the rows are {sorted(NODE_VOLUMES)}')
    return f'cloud-block-{row}'


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
