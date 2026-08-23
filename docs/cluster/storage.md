# Storage Design

Objective: Define the storage classes for the next-gen cluster and the rules
for choosing between them, replacing the legacy cluster's JuiceFS-centric
layout. Requirements: declarative (Pulumi-managed), data must be *movable*
between nodes without archaeology, JuiceFS demoted from default to last
resort, object storage used directly where an app supports it.

> **Status**: Reviewed interactively 2026-08-21; decided: B2 as the backup
> bucket (§4), JuiceFS CSI **not installed** (§6), second homelab VM
> deferred, hath cache lives on a block volume on its pinned cloud
> node (nodes.md
> §2.1). 2026-08-22: JuiceFS root causes documented (§1), VPS
> syncthing/dav disposition decided (§6), and — economy pass —
> **Longhorn deferred out of the initial build** in favor of local-path +
> VolSync (§3), with adoption criteria on file. Companion to
> [nodes.md](nodes.md); topology and pools per
> [architecture.md](architecture.md). Not implemented.

## 1. Principles

1.  **Local NVMe is the default.** Databases and latency-sensitive state get
    node-local storage; durability comes from application-level replication
    (CNPG) and backups, not from a distributed filesystem.
2.  **Mobility is a first-class requirement, durability is not** (for the
    block layer). The legacy cluster's pain was *moving* local-path data
    between nodes (claimRef surgery, tar-over-SSH, permission bits). The
    mobility mechanism is **backup/restore via VolSync** (§3): moves are
    rare, planned events, so they may cost a restore — what they must not
    cost is archaeology. Longhorn (a standing 2–4 GiB service for the
    same affordance) is deferred until move frequency proves it out
    (§3.2).
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
| `local-path` + VolSync | same local-path, plus a per-PVC restic schedule to the backup bucket | RWO, node-pinned; **movable via restore** | stateful apps without built-in replication; any volume that plausibly moves | bulk media (NAS's job) |
| ~~`longhorn`~~ (deferred, §3.2) | Longhorn v1 engine | RWO | — not in the initial build — | — |
| NAS (NFS PV / NodePV) | Existing NAS exports | RWO/RWX, homelab pool only | bulk media, large read-mostly sets | cloud-pool workloads; databases |
| Cloud block volume | OCI block volume on a cloud node (200 GB boot+block free) | RWO, node-pinned | hath cache, cloud-pool local-path backing | homelab-pool workloads |
| Object storage (direct) | S3-compatible bucket (§4) | app-native | apps with first-class S3 support; all backups | POSIX pretenders |
| JuiceFS (quarantined) | object storage + per-app metadata | RWX | last resort only (§6) | everything else |

Per-workload selection is a two-axis decision — (1) does the data need
to persist at all, (2) what performance does it need — then the data's
character (working state vs fixed assets) picks the backing. The full
decision framework with worked examples lives in
[declarative/workloads.md](../declarative/workloads.md) §2; the short
form:

1.  Re-derivable → plain **local-path/emptyDir, no backup** (exemption
    declared explicitly).
2.  Working state → **local-path + VolSync** (§3.1); databases →
    **CNPG** on local-path.
3.  Fixed assets (large, append-mostly) → **NAS** (POSIX/streaming,
    homelab) or **object storage direct** (S3-speaking or archival).
4.  POSIX RWX over object capacity and NAS can't serve it → justify
    **JuiceFS** per §6, in writing, per app.

## 3. Block-layer mobility: VolSync now, Longhorn later

### 3.1 VolSync on local-path (the initial build)

Every working-state volume (rule 2: local-path, no app-level
replication) gets a VolSync
`ReplicationSource` with the restic mover: scheduled backups to the
backup bucket (§4), retention policy per app. VolSync needs no CSI
snapshots — it mounts the PVC directly; mover pods exist only while a
backup/restore runs, and the controller idles at well under 100 Mi. This
one mechanism serves as:

-   **Backup** (§5): the recurring job every stateful app gets by default.
-   **Mobility**: moving a volume = scale down → final `ReplicationSource`
    sync → `ReplicationDestination` restore into a fresh PVC on the target
    node/pool → scale up pointing at it. Works identically same-site and
    cross-site, no claimRef surgery, and it exercises the restore path —
    every move is a restore drill for free.
-   **DR**: same restore, onto a rebuilt node.

The cost is planned downtime proportional to volume size per move —
acceptable because moves are rare, deliberate events (the legacy cluster
saw about one per year).

### 3.2 Longhorn: deferred (2026-08-22), with adoption criteria

Longhorn buys live detach/reattach, near-instant rebuild-to-move, and
volume snapshots — for a **standing** cost of 2–4 GiB RAM on the homelab
VM + 0.5–1 GiB on the cloud node, a 12%-of-node instance-manager CPU
request, ~256 MB RAM per TB of replica, and one more operator to run
(nodes.md §4.4 is the measured argument for refusing standing infra
costs that idle). It enters the cluster only when observed reality meets
one of:

-   volume moves become frequent enough that VolSync downtime measurably
    hurts (guideline: >~1 move/quarter on volumes where the downtime
    matters), or
-   a workload genuinely needs in-cluster RWX that NAS cannot serve, or
-   same-site replica=2 becomes real (requires the second homelab VM,
    nodes.md §4.2, which is itself deferred).

Adoption-day facts (verified 2026-08, so future-us doesn't re-research):
pin **≥1.12** — dual-stack cluster support starts there and v1.11 shipped
an instance-manager memory leak (#12668); Talos needs the
`siderolabs/iscsi-tools` + `util-linux-tools` extensions, a dedicated
`/var/lib/longhorn` mount, and a privileged-PSS namespace (machine-config
items in Pulumi, not a runbook); default `numberOfReplicas: 1` +
`dataLocality: best-effort`; cross-site replica=2 stays prohibited
(principle 3); v2 engine not adopted (dual-stack lags v1).

### 3.3 Volume lifecycle: reclaim, orphans, protection

Three related lessons, folded into policy:

-   **Reclaim policy: `Delete` by default, with VolSync as the undo.**
    The legacy cluster used `Retain` widely, which left orphaned PV
    directories of uncertain provenance that nobody dares delete —
    exactly the archaeology this design forbids. In the new cluster a
    deleted working-state PVC cleans its directory; the backup (and its
    retention class) is the safety net. NAS/NodePV volumes are
    inherently safe either way: the PV points at a dataset, deletion
    never touches data.
-   **Deliberate preservation happens at the IaC layer, not the reclaim
    layer: `protect=True`.** Data-bearing and identity-bearing
    resources are Pulumi-protected so destroys/replaces fail loudly:
    the hath cache volume (a slice of the globally distributed H@H
    archive — **not** re-derivable scratch, though also not
    VolSync-backed: like NAS volumes it sits outside the cluster backup
    regime, its redundancy being the H@H network itself), all buckets,
    CNPG `Cluster`s, precious PVCs, `machine_secrets`, DNS zones, and
    the reserved public IP. Deleting a protected resource is a two-step,
    reviewed act: an unprotect diff, then the delete — the IaC
    equivalent of removing a safety catch, visible in preview both
    times. (Where a real resource must outlive its Pulumi entry,
    `retainOnDelete` is the tool — used case-by-case, never as a
    default, because it manufactures exactly the unaccounted-resource
    problem `Retain` had.)
-   **Orphan audit is a standing procedure.** A `just` recipe compares
    on-disk PV directories against live PVs (per node) and reports
    unaccounted entries; run with the quarterly restore drills, target
    zero. The legacy cluster's accumulated retained-PV folders get a
    one-time census during migration — each mapped to an app or
    deleted (migration.md).

## 4. Object storage provider

Needs: S3-compatible API (restic/VolSync, CNPG barman, and Longhorn if
ever adopted all speak S3),
priced for ~100 GB–1 TB, and cheap *restore* traffic — restore drills are
part of the design, so egress-free matters more than the last cent on
storage.

| Provider | Storage/TB/mo | Egress | API ops | Notes (verified 2026-08-22, official pages) |
| --- | --- | --- | --- | --- |
| Backblaze B2 | $6.95 (was $6; 2026-05-01) | free up to 3× avg stored/mo, then $0.01/GB | **all classes free** (since 2026-05-01) | S3 API; no minimum duration; proven with barman in legacy tooling |
| OCI Object Storage | $25.50 (Standard); PAYG free: 10 GB Std + 10 IA + 10 Archive | counts in the tenancy's 10 TB/mo free pool; **$0 in-region to the OCI node** | $0.0034/10k, first 50k/mo free | only interesting co-located with an OCI cloud node; IA 31-day / Archive 90-day min retention |
| Cloudflare R2 | $15 (Std) / $10 (IA) | $0 always | A $4.50/M, B $0.36/M (1M/10M free) | IA tier is a trap for churny data (min duration + retrieval fee + 2× ops) |
| Wasabi | $7.99+ (1 TB minimum billed) | $0 (fair use: egress ≤ stored) | free (fair use) | 90-day min storage duration — structurally hostile to churny backups at our size |
| AWS S3 | $23 | 100 GB/mo free then $0.09/GB | PUT $5/M, GET $0.4/M | anchor only; a single 300 GB restore ≈ $27 of egress |

**Decision (2026-08-21, re-affirmed with verified numbers 2026-08-22)**:
**Backblaze B2 as the backup bucket**, now with a stronger case than when
chosen: since 2026-05-01 *every* API class is free, which is exactly the
dimension restic-style churn and JuiceFS chunk traffic stress. R2 Standard
is the named alternate if B2's 3×-stored egress allowance ever bites.

**Placement rule added (2026-08-22)**: the **backup** bucket must not
live with the provider whose loss it insures — OCI tenancy termination is
an enumerated risk (nodes.md §3.1), so cluster backups stay on B2
regardless of where the cloud node lands. The **JuiceFS chunk bucket**
(§6) is different: it backs a *replica* whose other full copy is the
homelab NAS, so provider-loss is survivable — with the cloud site on
OCI (nodes.md §3.1), its chunks live on same-region OCI Object Storage:
zero egress + LAN-class latency on cache misses, ~$2.6/mo at 110 GB
(minus 10 GB free). (On the Vultr fallback the bucket reverts to B2 —
free ops, 3×-stored egress covers read traffic with a warm cache.) Buckets, keys, and
lifecycle rules are Pulumi-managed like everything else.

## 5. Backup architecture (the actual HA mechanism)

Per nodes.md §5, durability = declarative rebuild + backups, drilled:

1.  **etcd**: hourly snapshots from the Talos control plane, shipped to
    `b2://…/etcd/`, retained ~14 days. Restore path is documented Talos
    `--recover-from-snapshot` bootstrap — drilled both in-place and onto a
    substitute node (the CP cold-standby path, nodes.md §5 Tier 0).
2.  **Volumes**: VolSync restic backups on every working-state PVC
    (§3.1), same bucket, retention by class
    (declarative/workloads.md §3); restores double as the volume-move
    mechanism, so the path stays exercised.
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

**Decision (2026-08-21, census revised 2026-08-22)**: the census is
**one** — the VPS-side syncthing replica (+ its dav share), which
qualifies under all three clauses (below). Everything else: immich media
on NAS, hath cache on a cloud block volume, qbittorrent on the
home side, backups to object storage directly. **The CSI driver is still
not installed** — the one qualifying workload mounts in-pod (sidecar),
which is also what keeps its blast radius per-app.

The last two *actual* JuiceFS users in the legacy cluster are the
VPS-side syncthing (5 Ti PVC, **~110 GB really used**) and the dav/webdav
share over the same data — running without visible trouble today, but
note both mount pods sit on the *same node as the Redis metadata server*,
which is exactly why they dodge root cause (a).

**Disposition (2026-08-22): the cloud-side replica stays cloud-side.**
Its role is (1) an always-reachable public sync anchor for roaming
clients and (2) an **independent second-site copy** of the data — the
homelab already has the full `syncthing-nas` replica, so pulling the VPS
instance home would collapse both copies into one site *and* route every
roaming client's sync through VIP→KubeSpan→homelab, manufacturing
cross-site traffic for no gain. What changes is only the backing store,
because ~110 GB doesn't fit a small instance disk:

-   **Preferred: per-app JuiceFS with node-local metadata** — its own
    filesystem on an object bucket chosen by the §4 placement rule
    (same-region OCI Object Storage; B2 on the Vultr fallback — §4),
    metadata in **SQLite (or a
    single-instance Redis) on its cloud node's block volume**, mounted
    in-pod (sidecar), automatic metadata backup to the bucket. This kills
    root cause (a) *by construction* (no metadata hop ever crosses the
    WAN), and root cause (b) is answered by **honest sizing, not hope**:
    the sidecar gets real requests/limits of **0.5–1 GiB** (legacy mount
    pods idle at ~130 Mi but that idle figure is exactly what starved
    them under load) — this number is in the cloud node's RAM budget
    (nodes.md §4.4), not discovered in an incident. Satisfies every
    clause of the quarantine policy above — the census becomes exactly
    **one**, and the CSI driver stays uninstalled.
-   **Alternative: s3ql** — an honest fit here since its single-mounter
    limitation is precisely the deployment shape (one syncthing writer).
    Kept as the named fallback rather than preferred: same FUSE-in-pod
    mechanics but a much smaller community/maintenance base than JuiceFS,
    for no capability we need.
-   **Rejected**: a provider block volume (~$11+/mo at $0.10/GB for data
    whose durability already comes from replication + B2); moving the
    replica to the homelab pool behind the internet VIP (see above — one
    site, more cross-site traffic).

The dav share serves the same dataset and follows the same mount.

## 7. Alternatives Considered

-   **Longhorn replica=2 across sites** ("free" DR): every write pays WAN
    RTT synchronously; measured legacy homelab↔VPS latency makes fsync-heavy
    workloads unusable. Rejected on principle 3; async backup/restore covers
    the DR case at RPO ≤24 h.
-   **Rook/Ceph**: proper distributed storage, but wants ≥3 storage nodes
    per site and an operational budget this cluster doesn't have. Strictly
    overkill for 2–3 nodes.
-   **Longhorn from day one** (the 2026-08-21 plan): rejected 2026-08-22
    in the economy pass — 2–4 GiB standing RAM + CPU reservations to keep
    a mobility affordance idling for events that historically happen
    about once a year. VolSync restore-moves cover the requirement at
    ~zero idle cost; Longhorn keeps documented adoption criteria (§3.2)
    instead of a default seat.
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
