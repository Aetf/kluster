"""Inside the cluster: its address ranges, its routing session, its pools, its storage classes."""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network

from kluster.conventions.identity import LABEL_DOMAIN

#: Talos/Cilium pod and service ranges, IPv4 first (architecture.md §1.3).
#: Deliberately not 10.42/10.43: those are the legacy k3s cluster's, and the
#: two clusters are routed to each other for the length of the migration.
POD_CIDR_V4 = IPv4Network('10.244.0.0/16')
POD_CIDR_V6 = IPv6Network('fd00:10:244::/56')
SERVICE_CIDR_V4 = IPv4Network('10.96.0.0/12')
SERVICE_CIDR_V6 = IPv6Network('fd00:10:96::/112')

#: KubePrism — the node-local kube-apiserver front the Cilium datapath uses
#: (there is no kube-proxy to fall back on).
KUBEPRISM_PORT = 7445

#: BGP (cluster-infra.md §2). The UDM's FRR is AS 65000; the cluster peers
#: from a distinct private ASN so the session is eBGP.
UDM_ASN = 65000
CLUSTER_ASN = 65001

#: The `internet` pool holds on-the-wire node addresses — private IPv4s (OCI
#: 1:1-NATs the public v4, so public literals never match) and v6 GUAs. Its
#: membership is a physical-stack output, not a constant; the `lan` pool's
#: range is `site.LAN_POOL`, which is a decision of this program.
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

#: Public port census — the ports the internet gateway and NLB terminate.
#: Listeners and security rules are derived beside the services that need
#: them (physical.md §1); this constant exists for the recorded fallback in
#: which the Talos ingress firewall must enumerate service ports, and as the
#: firewall-audit reference.
PUBLIC_PORT_CENSUS: tuple[tuple[int, str], ...] = (
    (80, 'tcp'),  # HTTP, redirect only
    (443, 'tcp'),  # HTTPS
    (8443, 'tcp'),  # matrix federation (terminates its own TLS)
    (22000, 'tcp'),  # syncthing
    (22000, 'udp'),  # syncthing
    (60011, 'tcp'),  # hath, on the dedicated VIP
)

SC_LOCAL_PATH = 'local-path'
SC_NAS = 'nas'
SC_CLOUD_BLOCK = 'cloud-block'

#: Backing directory for local-path on every node; a Talos machine-config
#: mount (physical.md §2), the StorageClass above is k8s-base's.
LOCAL_PATH_ROOT = '/var/mnt/storage'
