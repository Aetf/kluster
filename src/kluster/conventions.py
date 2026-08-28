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

#: The state-backend appliance (physical/state-backend.md), which is one name
#: in four places: the prefix on every cloud resource the box owns, the IAM
#: principal its provisioner signs as, the workstation slot that key lands in,
#: and the `credentials derived oci` subcommand that mints it. A name three
#: packages have to agree on is a convention, not a setting of any one of them.
STATE_BACKEND = 'state-backend'

#: The stack that owns the cloud estate (declarative/physical.md), which is
#: likewise one name in three places: the stack itself, the IAM principal it
#: signs as, and the compartment that principal administers.
PHYSICAL = 'physical'

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

#: The mail domain every OCI user this program creates is addressed in. An
#: identity-domains tenancy converts every legacy-IAM user into a domain user,
#: and the conversion refuses a user without a primary address; the address
#: must also be unique within the domain, so each user is named after itself
#: here rather than sharing one mailbox.
OCI_USER_EMAIL_DOMAIN = 'unlimited-code.works'

#: The seed user's primary email.
OCI_SEED_USER_EMAIL = f'pulumi@{OCI_USER_EMAIL_DOMAIN}'


class CompartmentMissing(LookupError):
    """A consumer's compartment is named here but does not exist in the tenancy yet."""


@dataclass(frozen=True)
class Compartment:
    """The OCI compartment one consumer administers, under both of its names.

    A compartment is the whole of what a §3 OCI key may touch
    (docs/credentials.md §3): each consumer administers its own and is a
    stranger outside it. That makes the boundary a decision of this program
    rather than a fact of the tenancy, so it is named here — and named twice,
    because the two halves are established at different moments.

    The **name** is a convention: it is chosen here, it is what the mint
    creates or adopts, and it is the only form OCI's quota statements accept
    (`physical/guardrails.py`). The **OCID** is the site fact that follows
    from creating it — an identifier the committed file may carry in the clear,
    for the reason `cloudflareAccountId` may: it names a container inside the
    tenancy rather than the account that owns it, and everything it admits is
    still behind a key.

    A compartment that has not been created yet therefore has a name and no
    OCID. That is a state rather than a gap: `credentials derived oci-<consumer>
    mint` creates it, prints the OCID, and the edit that records it here is one
    line to commit. Until then `require` refuses by naming that command, which
    is what keeps a stack from failing on a lookup instead.
    """

    #: The `credentials derived oci-<consumer>` row, and the stack or command
    #: the compartment belongs to.
    consumer: str
    #: What the compartment is called in the tenancy.
    name: str
    #: What OCI calls it, once it exists.
    ocid: str | None = None

    @property
    def mint(self) -> str:
        """The command that creates the compartment and mints the key confined to it."""
        return f'credentials derived oci-{self.consumer} mint'

    def require(self) -> str:
        """The OCID, or a refusal naming the command that produces one."""
        if self.ocid is None:
            raise CompartmentMissing(
                f'the {self.name} compartment does not exist yet, so nothing can be declared in it: '
                f'`{self.mint}` creates it and prints the OCID to record as the `{self.consumer}` entry '
                'of `conventions.OCI_COMPARTMENTS`'
            )
        return self.ocid


#: One compartment per consumer, which is what makes the §3 OCI rows
#: independent of each other.
#:
#: The appliance's is the tenancy's original compartment: it was made by hand
#: before this program existed, so it carries the estate's own name rather than
#: a per-consumer one, and the mint adopts it exactly as it adopts a user or a
#: group that is already there.
OCI_COMPARTMENTS: dict[str, Compartment] = {
    compartment.consumer: compartment
    for compartment in (
        Compartment(
            consumer=STATE_BACKEND,
            name=CLUSTER_NAME,
            ocid='ocid1.compartment.oc1..aaaaaaaaapllt64sf7e4gwnbka7l6d2hrblj6wvca7avtu6mrt6jaouallaq',
        ),
        Compartment(
            consumer=PHYSICAL,
            name=f'{CLUSTER_NAME}-{PHYSICAL}',
            ocid='ocid1.compartment.oc1..aaaaaaaajoaiz6cho6dnufutp6nrqyzhp6dswoi4hssa4o4sks276areztna',
        ),
    )
}

#: The cloud fleet: three combined control-plane/ingress nodes, one of which
#: additionally carries the block volume, the secondary private IP and the
#: reserved public address (architecture.md §3.2).
CLOUD_NODES = ('cp1', 'cp2', 'cp3')
AUGMENTED_NODE = 'cp1'

#: Designed against the conservative half of the A1 allowance (2 OCPU/12 GB),
#: so the architecture stays valid if the free tier halves again (nodes.md §3.2).
NODE_OCPUS = 1
NODE_MEMORY_GB = 8
NODE_BOOT_VOLUME_GB = 50

#: The cluster VCN. Chosen clear of everything it must coexist with: the
#: state-backend appliance's own network, the pod and service ranges, the home
#: VLANs, the ZeroTier range, and the legacy cluster's 10.42/10.43.
VCN_CIDR = IPv4Network('10.20.0.0/16')
VCN_SUBNET_CIDR = IPv4Network('10.20.0.0/24')

# ---------------------------------------------------------------------------
# Home site: the LAN, the homelab host, the gateway
# ---------------------------------------------------------------------------

#: The home subnets the gateway routes, other than the cluster's own below:
#: the untagged server LAN (the homelab host itself and the LAN's general
#: population), the IoT VLAN (Home Assistant and its devices — the LAN's
#: least-trusted population), and the container VLAN. Named here rather than
#: written out at each use because more than one package addresses them: the
#: ZeroTier managed routes below and the gateway's firewall census.
#:
#: The estate numbers every subnet the gateway serves 192.168.<VLAN id × 10>,
#: which is why a VLAN id and a third octet are one decision written twice.
VLAN_SERVER = IPv4Network('192.168.80.0/24')
VLAN_IOT = IPv4Network('192.168.90.0/24')
VLAN_CONTAINER = IPv4Network('10.0.5.0/24')

#: The site's unique-local prefix. Each subnet's /64 is numbered after the
#: third octet of its IPv4 subnet, spelled as those same digits: `:80::` is
#: the server LAN, `:90::` the IoT VLAN, `:5::` the containers, and the two
#: below follow it. Unique-local rather than global because the site's
#: delegated prefix rotates while the things that name these addresses —
#: firewall rules, resolver rewrites — have to keep matching.
SITE_ULA = IPv6Network('fd1a:665f:8bcb::/48')

#: The cluster's own VLAN: where every Talos node on this site lives, with
#: static addressing and no DHCP server (physical/gateway.md §4). A VLAN of
#: its own rather than a corner of the server LAN, because a population in
#: its own subnet is one the gateway can name in a policy and a population
#: sharing the untagged LAN is not.
CLUSTER_VLAN_ID = 7
CLUSTER_VLAN_V4 = IPv4Network('192.168.70.0/24')
CLUSTER_VLAN_V6 = IPv6Network('fd1a:665f:8bcb:70::/64')

#: The gateway's own leg on that VLAN: the nodes' default route, and the
#: address the BGP session with the worker is held at. It is a decision of
#: this program rather than a pre-existing site fact — the VLAN is declared
#: here — so the machine configuration and the gateway both read it from
#: here instead of from configuration that could disagree.
CLUSTER_VLAN_GATEWAY_V4 = IPv4Address('192.168.70.1')

#: The homelab worker: one large VM under libvirt on the homelab host, a pure
#: worker because the control plane is cloud-side (nodes.md §4.2).
HOMELAB_NODE = 'worker'

#: Its address is a constant rather than a lease: the gateway's FRR names it
#: as a BGP neighbour, the peer-port forward sends traffic to it and day 1
#: dials apid at it, so it is configured statically in machine config on one
#: side and read from here on the others (physical/homelab-host.md §2).
#: Nodes number from `.10`; `.1` is the gateway above and `.2` the homelab
#: host's own leg on the bridge, which is host preparation rather than
#: anything this program declares.
HOMELAB_NODE_IPV4 = IPv4Address('192.168.70.10')

#: Every Talos node this program declares.
ALL_NODES = (*CLOUD_NODES, HOMELAB_NODE)

#: Bootstrap sizing, deliberately below the 12–16 vCPU / 20 GiB / 100+ GB end
#: state: the legacy cluster still holds that RAM and that disk, and the VM
#: grows one wave at a time as legacy workloads stop (migration.md §0.4).
#: Growing *these two* is an edit here and a previewed apply: `vcpu` and
#: `memory` replace the domain, which is a stop, an undefine, a define and a
#: start with the disk — a separate resource — surviving, so the cost is a
#: drained window rather than a rebuild.
#:
#: The disk is not among them and has no constant here. A libvirt volume has
#: no update path — every field replaces it, `size` included — so the
#: declaration states no size at all and ignores the one it reads back; the
#: disk grows on the host instead, `truncate` plus `virsh blockresize`
#: (physical/homelab-host.md §1).
HOMELAB_VCPUS = 12
HOMELAB_MEMORY_GIB = 10

#: The host bridge the worker's tap joins, which bridges the cluster VLAN's
#: tagged subinterface. A second bridge on purpose: the existing `kvmbr0`
#: enslaves the IoT VLAN, which is where the Home Assistant domain belongs and
#: where a cluster node does not (physical/homelab-host.md §2).
HOMELAB_BRIDGE = 'kvmbr1'

#: The account the gateway is configured as. It has no other.
GW_SSH_USER = 'root'

#: The gateway's desired-state root. `/data` is the one directory that survives
#: a firmware update, which is why the whole estate lives under it
#: (architecture.md §5.2); `on_boot.d` holds the scripts that re-establish the
#: estate after one, with no expectation that Pulumi is reachable at boot.
GW_DATA_ROOT = '/data'
GW_ON_BOOT_D = f'{GW_DATA_ROOT}/on_boot.d'

#: The nspawn estate on the gateway, by unit name. ZeroTier is the member with
#: host networking — its interface has to land in the main network namespace
#: for the gateway to route through it (architecture.md §5.3).
GW_ESTATE = ('caddy', 'adguard-alice', 'adguard-bob', 'zerotier')

#: AdGuard Home's administration and API port. Three declarations meet on it:
#: the caddy vhost that proxies each instance's interface, the seed that tells
#: the instance where to listen, and the ZeroTier flow rule that admits a
#: continuous-integration member to exactly that port so the `dns` stack can
#: write its rewrites. It is a convention rather than a constant each of them
#: keeps, because the three are free to disagree and the failure is a resolver
#: that answers nothing anyone asked it.
ADGUARD_API_PORT = 3000

#: The controller site the UniFi resources are declared in. `default` is the
#: internal name whatever the site is labelled in the interface.
UNIFI_SITE = 'default'

#: The `lan` pool as controller-side address objects. Two of them for one
#: subnet because UniFi address groups are single-family, and address groups
#: at all because the pool is deliberately not a network object — it would
#: fight the BGP host routes (architecture.md §3.4, physical/gateway.md §4.1).
UNIFI_GROUP_LAN_POOL_V4 = 'kluster-lan-pool-v4'
UNIFI_GROUP_LAN_POOL_V6 = 'kluster-lan-pool-v6'

#: The cluster VLAN as controller-side objects: the network the gateway serves
#: it as, and the firewall zone that network is alone in. The contrast with the
#: pool above is the whole point — the VLAN is a network object *so that* it
#: can be named, and the pool is not one so that it cannot fight the host
#: routes. Both names are this program's, and both are what the console shows.
UNIFI_NETWORK_CLUSTER = CLUSTER_NAME
UNIFI_ZONE_CLUSTER = CLUSTER_NAME

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

#: The `lan` pool: a dedicated dual-stack subnet, deliberately not a subnet
#: the gateway serves — not the cluster VLAN the announcing node sits on and
#: not the server LAN either — BGP-announced to the UDM as host routes
#: (architecture.md §3.4). It takes the third octet one along from the cluster
#: VLAN's, and its /64 follows `SITE_ULA`'s numbering from that, so the pool
#: and the nodes that announce it read as neighbours without ever being one
#: network.
LAN_POOL_V4 = IPv4Network('192.168.71.0/24')
LAN_POOL_V6 = IPv6Network('fd1a:665f:8bcb:71::/64')

#: Fixed VIPs out of the `lan` pool. They are literals rather than
#: pool-allocated because things outside the cluster name them: the UDM's
#: IoT→media firewall allow (physical/gateway.md §4.2) and the AdGuard
#: rewrites' targets (dns.md §3).
VIP_LAN_V4 = IPv4Address('192.168.71.1')
VIP_LAN_V6 = IPv6Address('fd1a:665f:8bcb:71::1')
VIP_MEDIA_V4 = IPv4Address('192.168.71.2')
VIP_MEDIA_V6 = IPv6Address('fd1a:665f:8bcb:71::2')

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

#: The two object buckets, on two providers on purpose. The backup bucket must
#: not live with the provider whose loss it insures (storage.md §4), so it is
#: on B2; the JuiceFS chunk bucket backs a replica whose other full copy is the
#: NAS, so provider loss is survivable and it sits in-region with the cloud
#: nodes, where its traffic rides the $0 service gateway. Both names are
#: explicit because a B2 bucket name is global and both are addressed from
#: outside this program.
BUCKET_BACKUP = 'kluster-backup'
BUCKET_CHUNKS = 'kluster-chunks'

#: How long the backup bucket keeps a prior version before its lifecycle rule
#: removes it. Nothing in automation holds a delete capability, so a restic or
#: barman deletion degrades to a hide — and this is how long that hide has to
#: be recoverable before it ages out (storage.md §4).
BACKUP_VERSION_RETENTION_DAYS = 30

# ---------------------------------------------------------------------------
# DNS (declarative/dns.md)
# ---------------------------------------------------------------------------

ZONE_PRIMARY = 'unlimited-code.works'
#: Mirrors of the primary zone: the same app records, fanned out by the
#: route helpers instead of copy-pasted.
ZONE_MIRRORS = ('unlimitedcodeworks.xyz', 'peifeng.phd', 'ucw.phd')
#: Family zones — estate records only, never app fan-out targets.
ZONE_FAMILY = ('jiahui.id', 'jiahui.love')

#: Every zone an app may publish in without further thought. Membership is a
#: promise that the zone is a *full* mirror: it carries the shared estate block
#: (`dns.zones.MIRRORED_ESTATE`), so a name fanned out across the set resolves
#: in all of it. Adding a zone here means making it a mirror first.
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
#: deliberate practice. Its contents are `ZT_ROSTER` below, one record per
#: member.
ZT_LABEL = 'zt'

# ---------------------------------------------------------------------------
# ZeroTier (physical/gateway.md §2)
# ---------------------------------------------------------------------------

ZT_SUBNET = IPv4Network('10.144.0.0/16')

#: Static managed addresses. The UDM is the nexthop of every managed route;
#: the two CI identities are confined by the tag-based flow rules to exactly
#: the four targets they need. There is one identity per *stack* that joins,
#: not one per kind of run: ZeroTier maps a node to one endpoint at a time, so
#: two jobs sharing an identity would flap it (physical/gateway.md §2.6).
ZT_UDM = IPv4Address('10.144.1.1')
ZT_CI_PHYSICAL = IPv4Address('10.144.2.1')
ZT_CI_DNS = IPv4Address('10.144.2.2')

#: Role tags on the network (tag id 1000). `personal` is the permissive
#: default; membership itself is Pulumi-gated, so an undeclared member never
#: joins to receive it.
ZT_TAG_ROLE_ID = 1000
ZT_ROLE_PERSONAL = 0
ZT_ROLE_INFRA = 1
ZT_ROLE_CI = 2

#: Home subnets the UDM member routes for ZT clients. The cluster VLAN is here
#: because a run reaches the worker's machine API over the overlay, and the
#: pool because that is how a person off-site reaches a cluster service.
ZT_MANAGED_ROUTES = (
    VLAN_SERVER,
    CLUSTER_VLAN_V4,
    VLAN_IOT,
    VLAN_CONTAINER,
    LAN_POOL_V4,  # reached via the UDM's BGP-learned route
)

#: The two identities that exist only for continuous integration, one per
#: stack that joins the overlay during a run (physical/gateway.md §2.6).
ZT_CI_MEMBERS = ('ci-physical', 'ci-dns')

#: The gateway, as the roster names it. It is the only member whose identity
#: is minted by work this program does — the daemon runs on the device as a
#: container of the estate — which is why it is also the only member a caller
#: ever declares unminted (`gateway.zerotier.parse_members`).
ZT_MEMBER_UDM = 'udm'

#: The homelab host, as the roster names it. It is the one member the flow
#: rules have to look up rather than take from a constant: the libvirt session
#: a run opens reaches it member to member, at whatever overlay address it was
#: assigned before this program existed.
ZT_MEMBER_HOMELAB = 'Aetf-Arch-Homelab'


@dataclass(frozen=True)
class ZtMember:
    """A member of the overlay: what it is called, what it may do, where it sits.

    `address` is set where the address is a convention this program owns — the
    gateway's, and the two continuous-integration identities' — and left unset
    where it is a fact about a device that existed first, in which case the
    address arrives beside the node identifier as `physical` stack
    configuration and leaves again as that stack's `zerotier_addresses` output.

    `generated` marks the members whose key material this program creates.
    They have no configured identifier for the same reason they have no
    configured address: both are outputs of the resource that makes them.
    """

    #: What ZeroTier Central shows the member as. Display names are what they
    #: are — several contain spaces — and DNS normalizes rather than renames
    #: (`dns.zones.zt_label`).
    name: str
    #: One of the three `ZT_ROLE_*` values, carried as the member's role tag.
    role: int
    #: The overlay address, where this program decides it.
    address: IPv4Address | None = None
    #: Whether the identity behind the member is created in state.
    generated: bool = False
    #: Why the member is on the network, shown as its description in Central.
    note: str = ''


#: Every member of the overlay. The order is the order the design lists them
#: in: the infrastructure the overlay exists to reach, then the identities that
#: reach it unattended, then the people.
#:
#: The table is a convention rather than one stack's data because two stacks
#: decide from it and neither owns it. `physical` admits members by it — a
#: name in configuration the roster does not carry is refused, and a roster
#: entry with nothing configured for it is refused as well
#: (`gateway.zerotier.parse_members`) — and `dns` publishes the `*.zt` host
#: block from it, one A record per entry (`dns.zones.zt_records`). A member is
#: therefore admitted and named by the same declaration, so a member with no
#: record is not a state either stack can be in; a device that leaves the
#: overlay leaves this tuple, and both go with it.
#:
#: It is a census by construction. The role tag's default value is the
#: permissive one, so a member that arrived without a declared role would be
#: treated as a personal device — safe only because admission is gated by this
#: same table, so an undeclared member never reaches the default.
ZT_ROSTER: tuple[ZtMember, ...] = (
    ZtMember(
        name=ZT_MEMBER_UDM,
        role=ZT_ROLE_INFRA,
        address=ZT_UDM,
        note='the gateway: nexthop of every managed route',
    ),
    ZtMember(
        name=ZT_MEMBER_HOMELAB,
        role=ZT_ROLE_INFRA,
        note='the homelab host: a plain member and the recovery side-door, never a router',
    ),
    ZtMember(
        name='Aetf-Arch-VPS',
        role=ZT_ROLE_INFRA,
        note='the legacy deployment, retiring with its own route',
    ),
    ZtMember(
        name='haos',
        role=ZT_ROLE_INFRA,
        note='home automation, reachable while the cluster is not',
    ),
    ZtMember(
        name='ci-physical',
        role=ZT_ROLE_CI,
        address=ZT_CI_PHYSICAL,
        generated=True,
        note='the physical stack: plan, apply, and its drift check',
    ),
    ZtMember(
        name='ci-dns',
        role=ZT_ROLE_CI,
        address=ZT_CI_DNS,
        generated=True,
        note="the dns stack: previews, proofs, and the resolvers' rewrites",
    ),
    ZtMember(name='Aetf-Arch-XPS', role=ZT_ROLE_PERSONAL),
    ZtMember(name='Aetf-Win-XPS', role=ZT_ROLE_PERSONAL),
    ZtMember(name='Aetf-Handheld', role=ZT_ROLE_PERSONAL),
    ZtMember(name='PC-Homelab', role=ZT_ROLE_PERSONAL),
    ZtMember(name='OnePlus6T', role=ZT_ROLE_PERSONAL),
    ZtMember(name='Pixel 7 Pro', role=ZT_ROLE_PERSONAL),
    ZtMember(name='S26 Ultra', role=ZT_ROLE_PERSONAL),
)
