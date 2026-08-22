# Storage Design

Objective: Define the storage classes for the next-gen cluster and the rules
for choosing between them, replacing the legacy cluster's JuiceFS-centric
layout. Requirements: declarative (Pulumi-managed), data must be *movable*
between nodes without archaeology, JuiceFS demoted from default to last
resort, object storage used directly where an app supports it.

> **Status**: Reviewed interactively 2026-08-21; decided: B2 as the backup
> bucket (§4), JuiceFS CSI **not installed** (§6), second homelab VM
> deferred so all Longhorn volumes start at replica=1 (§3), hath cache
> lives on the cloud node's local disk (nodes.md §2.1). 2026-08-22:
> JuiceFS root causes documented (§1), Longhorn resource budget added
> (§3), VPS syncthing/dav disposition decided (§6). Companion to
> [nodes.md](nodes.md); topology and pools per
> [architecture.md](architecture.md). Not implemented.

## 1. Principles

1.  **Local NVMe is the default.** Databases and latency-sensitive state get
    node-local storage; durability comes from application-level replication
    (CNPG) and backups, not from a distributed filesystem.
2.  **Mobility is a first-class requirement, durability is not** (for the
    block layer). The legacy cluster's pain was *moving* local-path data
    between nodes (claimRef surgery, tar-over-SSH, permission bits). Longhorn
    is adopted primarily as the mobility layer: detach/attach anywhere,
    replica rebuild to relocate, S3 backup/restore.
3.  **No storage stretches the WAN synchronously.** The homelab↔cloud RTT
    (~tens of ms) goes into every write of any cross-site replica. Replicas
    stay within a site; cross-site movement is asynchronous (backup/restore
    or an explicit, temporary rebuild during a planned migration window).
4.  **JuiceFS earned demotion empirically.** The legacy instability has
    two identified root causes, and both are architectural, not tuning:
    (a) the metadata server is a single Redis on one node — every client
    on the *other* node pays WAN RTT per metadata operation, and any WAN
    latency/jitter directly wedges filesystems; (b) mount pods are
    resource-hungry, and the shared-mount-pod experiment (multiple
    volumes, one mount pod) was still unstable — possibly under-resourced,
    but "give the FUSE daemon more RAM until POSIX-over-S3 stops falling
    over" is not a foundation. Where JuiceFS survives, it is quarantined
    per-app (§6) with same-site metadata.

## 2. Storage classes

| Class | Backing | Access | Use for | Not for |
| --- | --- | --- | --- | --- |
| `local-path` (default) | Talos hostPath under `/var/mnt/storage` on each node | RWO, node-pinned | Databases (CNPG), caches, anything an app replicates itself | anything that may need to change nodes |
| `longhorn` | Longhorn v1 engine, local disk per node | RWO (RWX possible but avoid) | stateful apps without built-in replication; any volume that plausibly moves | high-IOPS databases; bulk media |
| NAS (NFS PV / NodePV) | Existing NAS exports | RWO/RWX, homelab pool only | bulk media, hath cache, large read-mostly sets | cloud-pool workloads; databases |
| Object storage (direct) | S3-compatible bucket (§4) | app-native | apps with first-class S3 support; all backups | POSIX pretenders |
| JuiceFS (quarantined) | object storage + per-app metadata | RWX | last resort only (§6) | everything else |

Per-workload decision rules, in order:

1.  App speaks S3 natively (backups, media originals, artifact stores) →
    **object storage direct**.
2.  App replicates itself (PostgreSQL via CNPG, anything clustered) →
    **local-path**, ≥2 instances across nodes where HA matters, plus
    barman/object backups.
3.  Bulk media / large read-mostly data in the homelab pool → **NAS**.
4.  Everything else stateful → **Longhorn**.
5.  Genuinely needs POSIX RWX across nodes and NAS can't serve it →
    justify **JuiceFS** per §6, in writing, per app.

## 3. Longhorn specifics

-   **Version floor**: the cluster is dual-stack IPv4-primary
    (architecture.md §1.3). Longhorn supports single-stack v4/v6 clusters
    from v1.10 but *dual-stack clusters* only from **v1.12** (IPv4-family
    -first). Pin the chart at ≥1.12 and treat it as a bootstrap-order
    constraint; do not deploy an older Longhorn "temporarily".
-   **Talos prerequisites**: `siderolabs/iscsi-tools` and
    `siderolabs/util-linux-tools` system extensions in the machine config,
    a dedicated `/var/lib/longhorn` mount on the storage disk, and the
    namespace labeled for privileged pod security. These go into the Talos
    machine config in Pulumi, not into a runbook.
-   **Replica policy**: default `numberOfReplicas: 1` + `dataLocality:
    best-effort`. One replica on the node running the workload gives
    near-local performance while keeping every Longhorn affordance
    (snapshots, S3 backup, detach/reattach, rebuild-to-move).
    `replica=2` is allowed only when both replicas land in the same site —
    which requires the optional second homelab VM (nodes.md §4). Cross-site
    `replica=2` is prohibited (principle 3).
-   **Moving a volume between nodes** becomes: cordon → scale down → (same
    site: temporarily raise replica count / let rebuild land on target;
    cross-site: backup to S3, restore into the other pool) → scale up. No
    claimRef surgery.
-   **Resource budget** (the JuiceFS lesson applied in advance — VM and
    cloud-instance sizing must include the storage layer, nodes.md §4.2):
    Longhorn's official floor is 4 vCPU/4 GB per node; the
    instance-manager's *default* CPU request is 12% of node allocatable
    (tunable per node); replicas hold a read index of ~256 MB RAM per TB
    of volume. Budget **2–4 GiB on the homelab VM** (10–20 volumes) and
    **0.5–1 GiB on the 4 GB cloud node**, where Longhorn is kept to a few
    small volumes with a lowered instance-manager reservation — bulk data
    on the cloud side belongs to hath's local-path cache, not Longhorn.
    (v1.11 shipped an instance-manager memory leak, #12668 — one more
    reason the ≥1.12 pin above is a floor, not a suggestion.)
-   **Backup target**: the backup bucket (§5) configured cluster-wide;
    recurring snapshots + backups for every Longhorn volume by default.
-   **v2 engine**: not adopted — its IPv6/dual-stack support lags v1 and the
    performance win doesn't matter at this scale. Revisit ~v1.13.

## 4. Object storage provider

Needs: S3-compatible API (Longhorn, CNPG barman, restic all speak S3),
priced for ~100 GB–1 TB, and cheap *restore* traffic — restore drills are
part of the design, so egress-free matters more than the last cent on
storage.

| Provider | Storage/TB/mo | Egress | Notes |
| --- | --- | --- | --- |
| Backblaze B2 | ~$6 | free up to 3x stored/mo, then $0.01/GB | S3 API; proven with barman in legacy cluster tooling |
| Cloudflare R2 | $15 | $0 always | zero-egress; class A/B op fees instead |
| Wasabi | $6.99 (1 TB min) | $0 (fair use) | 90-day min storage duration — hostile to churny backups |
| GCS Standard | ~$20–26 | $0.12/GB out; free into GCE same region | only attractive if an app on the GCP node reads it heavily |
| AWS S3 | ~$23 | $0.09/GB | legacy incumbent (hath cache history); no reason for new data |

**Decision (2026-08-21)**: **Backblaze B2** as the single backup/object
bucket provider (cheapest storage, effectively free restores at our ratios,
S3 API works with every consumer above). R2 is the named alternate if B2's
egress fair-use ever bites. GCS only for data the GCP node itself serves
hot. Buckets, keys, and lifecycle rules are Pulumi-managed like everything
else.

## 5. Backup architecture (the actual HA mechanism)

Per nodes.md §5, durability = declarative rebuild + backups, drilled:

1.  **etcd**: hourly snapshots from the Talos control plane, shipped to
    `b2://…/etcd/`, retained ~14 days. Restore path is documented Talos
    `--recover-from-snapshot` bootstrap — drilled both in-place and onto a
    substitute node (the CP cold-standby path, nodes.md §5 Tier 0).
2.  **Longhorn**: recurring snapshot (daily) + backup (daily) jobs on all
    volumes, same bucket, retention ~30 days.
3.  **CNPG**: barman object-store backups + WAL archiving per database
    cluster (port the legacy barman-plugin setup), monthly automated
    restore drill (port the legacy drill).
4.  **NAS data**: stays under the NAS's own backup regime — out of cluster
    scope, but migration of hath/media onto NAS PVs must not silently drop
    it from that regime.
5.  **Drills are part of the design**: a restore that hasn't run this
    quarter is assumed broken.

RPO: ≤1 h for etcd, ≤24 h for volumes, ~0 for CNPG (WAL). RTO: rebuild
either site from Pulumi + backups in ~1–2 h hands-on.

## 6. JuiceFS containment policy

JuiceFS is not banned; it is rationed. A workload may use it only if all of:

1.  It needs POSIX semantics over object-storage capacity (too big for
    local NVMe, not servable from NAS — e.g. cloud-pool workload needing
    cheap bulk space).
2.  RWX/multi-node access or capacity is the actual requirement, not
    convenience.
3.  It gets its **own** filesystem and its own metadata store (per-app
    blast radius; no shared mega-filesystem like the legacy cluster), with
    automatic metadata backup to the object bucket enabled.

**Decision (2026-08-21, census re-verified 2026-08-22)**: the census is
zero — immich media is on NAS, hath cache on the cloud node's local disk,
qbittorrent on the home side, backups go to object storage directly.
**The CSI driver is not installed.** This policy stays on file for the day
a workload genuinely qualifies.

The last two *actual* JuiceFS users in the legacy cluster are the VPS-side
syncthing (5 Ti PVC, **~110 GB really used**) and the dav/webdav share
over the same data — running without visible trouble today, but note both
mount pods sit on the *same node as the Redis metadata server*, which is
exactly why they dodge root cause (a). Disposition in the new cluster:
**both move to the homelab pool onto NAS storage** — the always-on sync
replica does not need to live in the cloud; what it needs is a public
endpoint, and architecture.md §3.1 provides that (`internet`-pool
LoadBalancer on the shared VIP, `externalTrafficPolicy: Cluster`,
backends on the homelab node; syncthing cares about neither source-IP
preservation nor same-IP egress). The dav HTTP share rides `internet-gw`
the same way. Alternatives rejected: keeping them cloud-side on a block
volume (~110 GB+growth ≈ $11+/mo at typical $0.10/GB, for data that
already lives on the NAS via syncthing-nas) or keeping the JuiceFS pair
(would be the sole reason to install the CSI driver).

## 7. Alternatives Considered

-   **Longhorn replica=2 across sites** ("free" DR): every write pays WAN
    RTT synchronously; measured legacy homelab↔VPS latency makes fsync-heavy
    workloads unusable. Rejected on principle 3; async backup/restore covers
    the DR case at RPO ≤24 h.
-   **Rook/Ceph**: proper distributed storage, but wants ≥3 storage nodes
    per site and an operational budget this cluster doesn't have. Strictly
    overkill for 2–3 nodes.
-   **OpenEBS Mayastor / local ZFS replication**: less turnkey than Longhorn
    for the one feature we're buying (S3 backup + detach/attach mobility);
    Mayastor's RAM/hugepages appetite is hostile to small nodes.
-   **NFS for everything homelab-side**: simple, but couples every workload
    to NAS uptime and punts on the cloud pool entirely; kept narrowly for
    bulk media where it already excels.
-   **SeaweedFS / S3QL / s3backer** (from the 2025 vps-upgrade survey):
    unchanged verdicts — SeaweedFS's cloud modes lose either caching or
    encryption; S3QL is single-mounter; s3backer is a block device with the
    same mobility problems as local-path. None beat "object storage direct
    where possible, Longhorn otherwise".
