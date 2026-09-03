"""Cluster-wide conventions: the names, labels, and addresses every stack agrees on.

Conventions are code, not stack outputs (declarative/README.md §2): a
cross-stack-referenced singleton gets an explicit name from this package with
autonaming disabled, so `apps` can address a `k8s-base` gateway (or a
`physical` bucket layout) without a StackReference. StackReferences carry only
machine facts — kubeconfig, node IPs, zone IDs.

What belongs here: a value the program must agree on with itself. What does
not: machine facts (OCIDs, generated names, IPs the cloud hands out) — those
are stack outputs — and per-app values, which live with their app.

One module per domain, and values that are only correct together are one
structure rather than a flat namespace, so using one without its siblings does
not parse (rfc-002 §10.1). Most of the surface is re-exported here, so a reader
says `conventions.X` and does not have to know which domain owns `X`.

**Three domains are read qualified instead**: `conventions.gateway`,
`conventions.overlay` and `conventions.forge`. The first two's names used to
carry a `GW_`/`ZT_` prefix so that a flat namespace could hold them, and the
prefix is what the module path now says (rfc-002 §3.1) —
`conventions.overlay.ROSTER`, `conventions.gateway.SERVICES`. It is also the
distinction the naming rules care about most: which network a name belongs to
is never a thing to guess. `forge` is qualified from the other side: its names
are common nouns — `Repository`, `Environment`, `Account` — that mean one
particular thing only while the forge stands beside them.
"""

from __future__ import annotations

from kluster.conventions import forge, gateway, overlay
from kluster.conventions.backup import (
    BACKUP_VERSION_RETENTION_DAYS,
    BUCKET_BACKUP,
    BULKY,
    ETCD_SNAPSHOT_PREFIX,
    PRECIOUS,
    RETENTION_CLASSES,
    STANDARD,
    STATE_DUMP_PREFIX,
    RetentionClass,
    barman_repo_path,
    volsync_repo_path,
)
from kluster.conventions.cloud import (
    ALL_NODES,
    CLOUD_NODES,
    DEDICATED_VIP_NODE,
    FOLLOWS_DEDICATED_VIP,
    NODE_BOOT_VOLUME_GB,
    NODE_MEMORY_GB,
    NODE_OCPUS,
    NODE_VOLUME_VPUS,
    NODE_VOLUMES,
    VCN_CIDR,
    VCN_SUBNET_CIDR,
    FollowsDedicatedVip,
    NodeVolume,
)
from kluster.conventions.cluster import (
    CLUSTER_ASN,
    GATEWAY_INTERNET,
    GATEWAY_LAN,
    GATEWAY_MEDIA,
    GATEWAY_NAMESPACE,
    KUBEPRISM_PORT,
    LB_POOL_LABEL,
    LOCAL_PATH_ROOT,
    POD_CIDR_V4,
    POD_CIDR_V6,
    POOL_INTERNET,
    PUBLIC_PORT_CENSUS,
    QBITTORRENT_PEER_PORT,
    SC_CLOUD_BLOCK,
    SC_LOCAL_PATH,
    SC_NAS,
    SERVICE_CIDR_V4,
    SERVICE_CIDR_V6,
    UDM_ASN,
)
from kluster.conventions.dns import (
    ALL_ZONES,
    ANCHOR_CLUSTER,
    ANCHOR_LABEL,
    ANCHOR_TTL,
    ANCHOR_VIP1,
    PRIMARY_ONLY,
    PUBLIC_ALL,
    ZONE_FAMILY,
    ZONE_MIRRORS,
    ZONE_PRIMARY,
    ZONE_SHORT,
    ZT_LABEL,
)
from kluster.conventions.homelab import (
    HOMELAB_BRIDGE,
    HOMELAB_HOST_KEY,
    HOMELAB_MEMORY_GIB,
    HOMELAB_NODE,
    HOMELAB_NODE_IPV4,
    HOMELAB_STORAGE_DIR,
    HOMELAB_VCPUS,
)
from kluster.conventions.identity import CLUSTER_NAME, LABEL_DOMAIN, PHYSICAL, STATE_BACKEND
from kluster.conventions.providers import (
    B2_ACCOUNT,
    OCI_SEED_USER_EMAIL,
    OCI_TENANCY,
    B2Account,
    Compartment,
    CompartmentMissing,
    OciTenancy,
)
from kluster.conventions.site import (
    CLUSTER_VLAN,
    CONTAINER_VLAN,
    IOT_VLAN,
    LAN_POOL,
    SERVER_LAN,
    SITE_NETWORKS,
    SITE_ULA,
    AddressPool,
    SiteNetwork,
    Vip,
    ula_subnet,
)

__all__ = (
    'ALL_NODES',
    'ALL_ZONES',
    'ANCHOR_CLUSTER',
    'ANCHOR_LABEL',
    'ANCHOR_TTL',
    'ANCHOR_VIP1',
    'B2_ACCOUNT',
    'BACKUP_VERSION_RETENTION_DAYS',
    'BUCKET_BACKUP',
    'BULKY',
    'CLOUD_NODES',
    'CLUSTER_ASN',
    'CLUSTER_NAME',
    'CLUSTER_VLAN',
    'CONTAINER_VLAN',
    'DEDICATED_VIP_NODE',
    'ETCD_SNAPSHOT_PREFIX',
    'FOLLOWS_DEDICATED_VIP',
    'GATEWAY_INTERNET',
    'GATEWAY_LAN',
    'GATEWAY_MEDIA',
    'GATEWAY_NAMESPACE',
    'HOMELAB_BRIDGE',
    'HOMELAB_HOST_KEY',
    'HOMELAB_MEMORY_GIB',
    'HOMELAB_NODE',
    'HOMELAB_NODE_IPV4',
    'HOMELAB_STORAGE_DIR',
    'HOMELAB_VCPUS',
    'IOT_VLAN',
    'KUBEPRISM_PORT',
    'LABEL_DOMAIN',
    'LAN_POOL',
    'LB_POOL_LABEL',
    'LOCAL_PATH_ROOT',
    'NODE_BOOT_VOLUME_GB',
    'NODE_MEMORY_GB',
    'NODE_OCPUS',
    'NODE_VOLUMES',
    'NODE_VOLUME_VPUS',
    'OCI_SEED_USER_EMAIL',
    'OCI_TENANCY',
    'PHYSICAL',
    'POD_CIDR_V4',
    'POD_CIDR_V6',
    'POOL_INTERNET',
    'PRECIOUS',
    'PRIMARY_ONLY',
    'PUBLIC_ALL',
    'PUBLIC_PORT_CENSUS',
    'QBITTORRENT_PEER_PORT',
    'RETENTION_CLASSES',
    'SC_CLOUD_BLOCK',
    'SC_LOCAL_PATH',
    'SC_NAS',
    'SERVER_LAN',
    'SERVICE_CIDR_V4',
    'SERVICE_CIDR_V6',
    'SITE_NETWORKS',
    'SITE_ULA',
    'STANDARD',
    'STATE_BACKEND',
    'STATE_DUMP_PREFIX',
    'UDM_ASN',
    'VCN_CIDR',
    'VCN_SUBNET_CIDR',
    'ZONE_FAMILY',
    'ZONE_MIRRORS',
    'ZONE_PRIMARY',
    'ZONE_SHORT',
    'ZT_LABEL',
    'AddressPool',
    'B2Account',
    'Compartment',
    'CompartmentMissing',
    'FollowsDedicatedVip',
    'NodeVolume',
    'OciTenancy',
    'RetentionClass',
    'SiteNetwork',
    'Vip',
    'barman_repo_path',
    'forge',
    'gateway',
    'overlay',
    'ula_subnet',
    'volsync_repo_path',
)
