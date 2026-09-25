"""Cluster-wide conventions: the names, labels, and addresses every stack agrees on.

Conventions are code, not stack outputs (declarative/README.md §2): a
cross-stack-referenced singleton gets an explicit name from this package with
autonaming disabled, so `apps` can address a `k8s-base` gateway (or a
`physical` bucket layout) without a StackReference. StackReferences carry only
machine facts — kubeconfig, node IPs, zone IDs.

What belongs here: a value the program must agree on with itself. What does
not: machine facts (OCIDs, generated names, IPs the cloud hands out) — those
are stack outputs — and per-app values, which live with their app.

One module per subject, and values that are only correct together are one
structure rather than a flat namespace, so using one without its siblings does
not parse (rfc-002 §10.1). Most of the surface is re-exported here, so a reader
says `conventions.X` and does not have to know which module owns `X`.

**A module whose names need its own name beside them is read qualified
instead** — `conventions.<module>.X`, never re-exported. Today that is
`gateway`, `overlay`, `forge`, `routes` and `alert`, for one of two reasons.
For `gateway` and `overlay` the module path carries what a prefix otherwise
would (rfc-002 §3.1) — `conventions.overlay.ROSTER`,
`conventions.gateway.SERVICES` — and it is the distinction the naming rules
care about most: which network a name belongs to is never a thing to guess.
`forge`, `routes` and `alert` are qualified from the other side: their names
are common nouns — `Repository`, `Environment`, `Account`; `Route`, `Extra`,
`SELF`; `EVENT`, `Tier`, `FIELDS` — that mean one particular thing only while
the forge, the census or the alert stands beside them.

Glossary
--------

The vocabulary the naming rules produce — one term per concept, descriptive
over metaphorical (style/README.md under "Naming") — kept here where the style
reviewer reads a diff against it (rfc-002 §3). Every entry is collected from
where the tree already uses the term that way, and the pointer beside it is
that place. A `Not:` line lists the words a diff does not introduce for the
concept: this package uses none of them, and `test_conventions` holds it to
that. A word a term is merely distinguished from stays in the prose.

overlay
    The ZeroTier network every unattended run reaches the home site over
    (`conventions.overlay`; rfc-002 §3.2). The adjective for what is on it —
    an overlay address, an overlay member, an overlay route — and never
    "network" unqualified. The wire label `zt` is a value (`dns.OVERLAY_LABEL`),
    not a word.
    Not: zt.

site
    The home LANs the gateway routes, collectively (`conventions.site`;
    rfc-002 §3.2): a site network is one of them (`SiteNetwork`,
    `SITE_NETWORKS`), and the untagged one is the server LAN (`SERVER_LAN`).

LAN
    On its own, the side an application is reachable from, and the pool that
    side is steered to (`routes.Exposure`, `cluster.GATEWAY_LAN`,
    `site.LAN_POOL`); with a site network's name, that network (`SERVER_LAN`).
    Not one of rfc-002 §3.2's adjectives, which say which network an address
    is on.

container VLAN
    The one site network the container services sit on (`site.CONTAINER_VLAN`;
    rfc-002 §3.2), named as such because a bridged service holds an address
    there and the host-network one deliberately does not
    (`gateway.BridgedService`, `gateway.HostNetworkService`).

container service
    A service the device runs, bridged or in the host's own network namespace
    (`gateway.SERVICES`, `gateway.ContainerService`). The set as a whole is
    what the device runs, never a figure of speech for it (rfc-002 §3.1).
    Not: estate.

gateway
    The machine at the middle, named for what it does for the site, and the
    component (`conventions.gateway`; rfc-002 §3.2). The cluster's own
    gateways — `cluster.GATEWAY_INTERNET`, `GATEWAY_LAN`, `GATEWAY_MEDIA` —
    are Gateway API objects, and a sentence that could mean either says "the
    site gateway" (`cluster.QBITTORRENT_PEER_PORT`). Neither is a prefix:
    rfc-002 §3.1 retired `GW_*`.
    Not: gw.

device
    The same machine as what a provider writes to: the thing with a userland,
    a host key and a firmware update (`gateway.HOST_KEY`, `gateway.DATA_ROOT`;
    rfc-002 §3.2).

UDM
    The same machine as the appliance, where the sentence is about hardware
    or the controller that ships with it (`cluster.UDM_ASN`, `overlay.UDM`;
    rfc-002 §3.2).

member
    What has joined the overlay, one roster entry each (`overlay.ROSTER`,
    `overlay.member`). Its identity keeps ZeroTier's own word, node id
    (`EnrolledMember.node_id`); a node unqualified is a Talos node
    (`cloud.ALL_NODES`); the homelab host is the machine the worker runs on,
    itself a member (`overlay.MEMBER_HOMELAB`, `conventions.homelab`).

resolver
    One of the two AdGuard instances every lease on the LAN names, the site's
    name service (`gateway.RESOLVERS`, `gateway.resolver_api_url`;
    declarative/dns.md §3). A zone's authoritative servers are its name
    servers, which is a different thing (physical/gateway.md §1).

zone
    One of the DNS zones the installation holds (`conventions.dns`;
    declarative/dns.md §2): served (`WEB_ZONES`) or parked (`PARKED_ZONES`),
    and a record is published in a zone only where something answers for the
    name there — nothing mirrors (rfc-003 §11).
    Not: mirrored, alias zone.

installation
    This deployment as a whole, what every census here is written in the
    terms of (style/pulumi.md under "Data"; rfc-003 §11). "Estate" survives in
    one sense only, the operator's personal holdings and their succession
    (credentials.md), and does not reach this package.
    Not: estate.

census
    A table two programs read, which is what places it in this package
    (style/pulumi.md under "Data"; declarative/README.md §2). A component
    receives the census it acts on, and the entries it is handed are its roll
    (style/pulumi.md). Two censuses carry names of their own: the roster and
    the register.

roster
    The overlay's census, one entry per member (`overlay.ROSTER`; rfc-002
    §3.1).

register
    The credential register, credentials.md: the inventory of every
    credential, one row each, which the `credentials` command is the
    executable form of (`scripts/credentials`). This package names it once,
    where `conventions.forge` says whose rows push into the Environments.

initial state
    The configuration installed into a state directory that has never held
    one (`gateway.ADGUARD_API_PORT`; `components.gateway.container.InitialState`;
    rfc-002 §3.1). "Seed" is not a synonym but a reserved word, the nocloud
    seed image and the credential seed kit, which this package names in that
    sense alone (`homelab.HOMELAB_STORAGE_DIR`, `providers.OCI_SEED_USER_EMAIL`).

dedicated VIP node
    The node holding the dedicated VIP, one capability of a machine
    (`cloud.DEDICATED_VIP_NODE`; rfc-002 §3.1), apart from the volumes the
    same machine happens to hold.
    Not: augmented.

node volume
    A block volume attached to a node (`cloud.NODE_VOLUMES`,
    `cloud.NodeVolumeEntry`; rfc-002 §3.1). What it is for belongs to whatever
    claims it, so an entry is named after its claimant and the type is not.

CI
    This repository's own GitHub Actions, the deployment pipeline
    (cluster/architecture.md §4.3; `overlay.Role.CI`, `overlay.CI_MEMBERS`).
    A workflow in any other repository is named with its repository and is
    not CI.
"""

from __future__ import annotations

from kluster.conventions import alert, forge, gateway, overlay, routes
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
    NodeVolumeEntry,
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
    MANAGEMENT_PORTS,
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
    ManagementPorts,
)
from kluster.conventions.dns import (
    ALL_ZONES,
    ANCHOR_CLUSTER,
    ANCHOR_LABEL,
    ANCHOR_TTL,
    ANCHOR_VIP1,
    OVERLAY_DOMAIN,
    OVERLAY_LABEL,
    PARKED_ZONES,
    PRIMARY_ONLY,
    WEB_ZONES,
    ZONE_FAMILY,
    ZONE_PRIMARY,
    ZONE_SHORT,
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
from kluster.conventions.identity import CLUSTER_NAME, DRILL, LABEL_DOMAIN, PHYSICAL, STATE_BACKEND
from kluster.conventions.outputs import PHYSICAL_OUTPUTS, PhysicalOutputs
from kluster.conventions.providers import (
    B2_ACCOUNT,
    CLOUDFLARE_ACCOUNT,
    OCI_SEED_USER_EMAIL,
    OCI_TENANCY,
    B2Account,
    CloudflareAccount,
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
    'CLOUDFLARE_ACCOUNT',
    'CLOUD_NODES',
    'CLUSTER_ASN',
    'CLUSTER_NAME',
    'CLUSTER_VLAN',
    'CONTAINER_VLAN',
    'DEDICATED_VIP_NODE',
    'DRILL',
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
    'MANAGEMENT_PORTS',
    'NODE_BOOT_VOLUME_GB',
    'NODE_MEMORY_GB',
    'NODE_OCPUS',
    'NODE_VOLUMES',
    'NODE_VOLUME_VPUS',
    'OCI_SEED_USER_EMAIL',
    'OCI_TENANCY',
    'OVERLAY_DOMAIN',
    'OVERLAY_LABEL',
    'PARKED_ZONES',
    'PHYSICAL',
    'PHYSICAL_OUTPUTS',
    'POD_CIDR_V4',
    'POD_CIDR_V6',
    'POOL_INTERNET',
    'PRECIOUS',
    'PRIMARY_ONLY',
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
    'WEB_ZONES',
    'ZONE_FAMILY',
    'ZONE_PRIMARY',
    'ZONE_SHORT',
    'AddressPool',
    'B2Account',
    'CloudflareAccount',
    'Compartment',
    'CompartmentMissing',
    'FollowsDedicatedVip',
    'ManagementPorts',
    'NodeVolumeEntry',
    'OciTenancy',
    'PhysicalOutputs',
    'RetentionClass',
    'SiteNetwork',
    'Vip',
    'alert',
    'barman_repo_path',
    'forge',
    'gateway',
    'overlay',
    'routes',
    'ula_subnet',
    'volsync_repo_path',
)
