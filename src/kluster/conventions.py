"""Cluster-wide conventions: the names, labels, and addresses every stack agrees on.

Conventions are code, not stack outputs (framework/pulumi.md §3.1): a
cross-stack-referenced singleton gets an explicit name from this module with
autonaming disabled, so `apps` can address a `k8s-base` gateway (or a
`physical` bucket layout) without a StackReference. StackReferences carry only
machine facts — kubeconfig, node IPs, zone IDs.

What belongs here: a value the program must agree on with itself. What does
not: machine facts (OCIDs, generated names, IPs the cloud hands out) — those
are stack outputs — and per-app values, which live with their app.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

CLUSTER_NAME = 'kluster'

#: Prefix for every label/annotation key this program owns. A k8s label key
#: prefix must be a DNS subdomain; this one is a zone we control, so the keys
#: can never collide with an upstream chart's.
LABEL_DOMAIN = 'kluster.ucw.phd'

# ---------------------------------------------------------------------------
# Cloud site (OCI)
# ---------------------------------------------------------------------------

#: Home region — permanent per tenancy, and where the whole free envelope
#: (A1 OCPU-hours, the 200 GB boot+block allowance) is redeemable.
OCI_REGION = 'us-phoenix-1'

# ---------------------------------------------------------------------------
# Cluster networking
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# LoadBalancer pools
# ---------------------------------------------------------------------------

POOL_INTERNET = 'internet'
POOL_LAN = 'lan'

#: A Service opts into a pool by carrying this label; the pools'
#: `serviceSelector` matches on it.
LB_POOL_LABEL = f'{LABEL_DOMAIN}/lb-pool'

#: The `lan` pool: a dedicated dual-stack subnet, deliberately outside the
#: LAN's own 192.168.80.0/24, BGP-announced to the UDM (architecture.md §3.4).
#: The ULA /64 comes out of the site's existing fd1a:665f:8bcb::/48, whose
#: per-VLAN /64s are numbered after the v4 third octet (:80:: = LAN,
#: :90:: = IoT, :5:: = container VLAN) — so the pool is :70::.
LAN_POOL_V4 = IPv4Network('192.168.70.0/24')
LAN_POOL_V6 = IPv6Network('fd1a:665f:8bcb:70::/64')

#: Fixed VIPs out of the `lan` pool. They are literals rather than
#: pool-allocated because things outside the cluster name them: the UDM's
#: IoT→media firewall allow (physical/gateway.md §4.2) and the AdGuard
#: rewrites' targets (dns.md §3).
VIP_LAN_V4 = IPv4Address('192.168.70.1')
VIP_LAN_V6 = IPv6Address('fd1a:665f:8bcb:70::1')
VIP_MEDIA_V4 = IPv4Address('192.168.70.2')
VIP_MEDIA_V6 = IPv6Address('fd1a:665f:8bcb:70::2')

#: The `internet` pool holds on-the-wire node addresses — private IPv4s
#: (OCI 1:1-NATs the public v4, so public literals never match) and v6 GUAs.
#: Its membership is a physical-stack output, not a constant.

# ---------------------------------------------------------------------------
# Gateways (Gateway API, Cilium)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SC_LOCAL_PATH = 'local-path'
SC_NAS = 'nas'
SC_CLOUD_BLOCK = 'cloud-block'

#: Backing directory for local-path on every node; a Talos machine-config
#: mount (physical.md §2), the StorageClass above is k8s-base's.
LOCAL_PATH_ROOT = '/var/mnt/storage'

# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionClass:
    """A backup retention policy, shared by VolSync/restic and CNPG/barman.

    An app picks a class; nobody writes retain counts or cron lines inline.
    Changing a class is one diff that previews across every affected app.
    """

    name: str
    schedule: str
    """Cron expression for the recurring backup."""
    hourly: int | None = None
    daily: int | None = None
    weekly: int | None = None
    monthly: int | None = None
    max_age: str = ''
    """Freshness threshold for the central vmalert rule family."""


#: Daily, a month deep — the default every stateful app gets.
STANDARD = RetentionClass(name='standard', schedule='0 3 * * *', daily=30, max_age='36h')
#: Irreplaceable data: a month of dailies plus a year of monthlies.
PRECIOUS = RetentionClass(name='precious', schedule='0 3 * * *', daily=30, monthly=12, max_age='36h')
#: Large and slow-changing; weekly is enough and cheaper to store.
BULKY = RetentionClass(name='bulky', schedule='0 4 * * 0', weekly=4, max_age='9d')

RETENTION_CLASSES = (STANDARD, PRECIOUS, BULKY)


#: One bucket layout, so the backup inventory is derivable from the program.
def volsync_repo_path(namespace: str, pvc: str) -> str:
    return f'volsync/{namespace}/{pvc}'


def barman_repo_path(namespace: str, cluster: str) -> str:
    return f'cnpg/{namespace}/{cluster}'


ETCD_SNAPSHOT_PREFIX = 'etcd'
STATE_DUMP_PREFIX = 'pulumi-state'

# ---------------------------------------------------------------------------
# DNS (declarative/dns.md)
# ---------------------------------------------------------------------------

ZONE_PRIMARY = 'unlimited-code.works'
#: Mirrors of the primary zone: the same app records, fanned out by the
#: route helpers instead of copy-pasted.
ZONE_MIRRORS = ('unlimitedcodeworks.xyz', 'peifeng.phd', 'ucw.phd')
#: Family zones — estate records only, never app fan-out targets.
ZONE_FAMILY = ('jiahui.id', 'jiahui.love')

PUBLIC_ALL = (ZONE_PRIMARY, *ZONE_MIRRORS)
PRIMARY_ONLY = (ZONE_PRIMARY,)
ALL_ZONES = (*PUBLIC_ALL, *ZONE_FAMILY)

#: IP literals live only under the anchor namespace, with low TTLs; apps are
#: CNAMEs to an anchor, so a node rebuild touches exactly one record.
ANCHOR_LABEL = 'hosts'
ANCHOR_CLUSTER = f'kluster.{ANCHOR_LABEL}'
ANCHOR_VIP1 = f'vip1.{ANCHOR_LABEL}'
ANCHOR_TTL = 300

#: The ZeroTier host block — private addresses in public DNS, an existing
#: deliberate practice; its contents mirror the ZT member roster.
ZT_LABEL = 'zt'

# ---------------------------------------------------------------------------
# ZeroTier (physical/gateway.md §2)
# ---------------------------------------------------------------------------

ZT_SUBNET = IPv4Network('10.144.0.0/16')

#: Static managed addresses. The UDM is the nexthop of every managed route;
#: the two CI identities are confined by the tag-based flow rules to exactly
#: the four targets they need.
ZT_UDM = IPv4Address('10.144.1.1')
ZT_CI_DEPLOY = IPv4Address('10.144.2.1')
ZT_CI_PREVIEW = IPv4Address('10.144.2.2')

#: Role tags on the network (tag id 1000). `personal` is the permissive
#: default; membership itself is Pulumi-gated, so an undeclared member never
#: joins to receive it.
ZT_TAG_ROLE_ID = 1000
ZT_ROLE_PERSONAL = 0
ZT_ROLE_INFRA = 1
ZT_ROLE_CI = 2

#: LAN subnets the UDM member routes for ZT clients.
ZT_MANAGED_ROUTES = (
    IPv4Network('192.168.80.0/24'),  # server LAN (br0)
    IPv4Network('192.168.90.0/24'),  # IoT (br2)
    IPv4Network('10.0.5.0/24'),  # container VLAN (br5)
    LAN_POOL_V4,  # reached via the UDM's BGP-learned route
)
