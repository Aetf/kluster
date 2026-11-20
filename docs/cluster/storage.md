# Storage Design

Objective: Define the storage classes for the next-gen cluster and the rules
for choosing between them, replacing the legacy cluster's JuiceFS-centric
layout. Requirements: declarative (Pulumi-managed), data must be *movable*
between nodes without archaeology, JuiceFS demoted from default to last
resort, object storage used directly where an app supports it.

> **Status**: Reviewed interactively 2026-08-21; decided: B2 as the backup
> bucket (§4), JuiceFS CSI **not installed** (§6), second homelab VM
> deferred with criteria (nodes.md §4.3), hath cache lives on a block
> volume on its pinned cloud node (nodes.md
> §2.1). 2026-08-22: JuiceFS root causes documented (§1), VPS
> syncthing/dav disposition decided (§6), and — economy pass —
> **Longhorn deferred out of the initial build** in favor of local-path +
> VolSync (§3), with adoption criteria on file. 2026-11-17:
> snapshot-capable local volumes deferred as well, with adoption
> criteria and the shape adoption takes (§3.4). Companion to
> [nodes.md](nodes.md); topology and pools per
> [architecture.md](architecture.md). Declared in code: the backup
> bucket and its keys (`BackupBucket`, §4) and the cloud block volumes
> (`conventions.NODE_VOLUMES`, §6), both by the `physical` stack. The
> storage classes — `local-path` and one class per node volume —
> VolSync and CNPG are the `k8s-base` stack's, and the claims, their
> backups and their moves are the `apps` stack's
> (`kluster.components.apps.storage`), as are the databases, their
> archives and their drills (`kluster.components.apps.database`). None
> of it is provisioned.

## 1. Principles

1.  **Local NVMe is the default.** Databases and latency-sensitive state get
    node-local storage; durability comes from application-level replication
    (CNPG) and backups, not from a distributed filesystem.
2.  **Mobility is a first-class requirement, durability is not** (for the
    block layer). The legacy cluster's pain was *moving* local-path data
    between nodes (claimRef surgery, tar-over-SSH, permission bits). The
    mobility mechanism is **a copy by VolSync** (§3): moves are
    rare, planned events, so they may cost a copy with the workload
    stopped — what they must not cost is archaeology. Longhorn (a standing 2–4 GiB service for the
    same affordance) is deferred until move frequency proves it out
    (§3.2).
3.  **No storage stretches the WAN synchronously.** The homelab↔cloud RTT
    (~tens of ms) goes into every write of any cross-site replica. Replicas
    stay within a site; cross-site movement is asynchronous (a copy,
    a backup and its restore, or an explicit, temporary rebuild during a
    planned migration window).
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
| `local-path` (default) | directories under `/var/mnt/storage`, a Talos user volume on each node's system disk | RWO, node-pinned | Databases (CNPG), caches, anything an app replicates itself | anything that may need to change nodes |
| `local-path` + VolSync | same local-path, plus a per-claim restic schedule to the backup bucket: `claim` under a retention class | RWO, node-pinned; **movable by copy** (§3.1) | stateful apps without built-in replication; any volume that plausibly moves | bulk media (NAS's job) |
| ~~`longhorn`~~ (deferred, §3.2) | Longhorn v1 engine | RWO | — not in the initial build — | — |
| NAS (`nas_claim`) | a static NFS PersistentVolume over a path of the host's one export, and its claim | RWX or read-only, the worker only | bulk media, large read-mostly sets | cloud-pool workloads; databases |
| `cloud-block-<row>` | `local-path` served from an OCI block volume on a cloud node, one per node (`conventions.NODE_VOLUMES`), mounted at `/var/mnt/<name>` (§6) | RWO, node-pinned | the preserved cloud-pool dataset: the hath cache | homelab-pool workloads; anything a node may have to lose |
| Object storage (direct) | S3-compatible bucket (§4) | app-native | apps with first-class S3 support; all backups | POSIX pretenders |
| JuiceFS (quarantined, no CSI — §6) | object storage + per-app metadata, mounted in-pod | RWX | last resort only, one app at a time | everything else; it is not selectable by `storageClassName` |

**Two calls claim them, and a class is named.** `claim` is the one
claim on a node, under `local-path` or a node volume's class, and its
retention is required: a retention class, or `off('<reason>')`, which
emits the claim alone. `nas_claim` is the one claim on the NAS, with no
retention to set, since the source's own regime is its safety net.
declarative/workloads.md §2 says how each is chosen.

**What local-path data outlives.** `/var/mnt/storage` is a Talos
user volume of the `directory` type (physical.md §2), which has no
disk of its own: it lives on the system disk's EPHEMERAL partition,
the one Talos mounts at `/var`, and shares that partition's capacity
with everything else under `/var`, the node's container images and logs
among it. EPHEMERAL is a partition name rather
than a lifetime, and nothing clears it on its own. The data survives
a reboot and a `talosctl upgrade`, which keeps `/var`. It is lost on
`talosctl reset`, whose default wipe takes the whole system disk; when
the node is reinstalled, or replaced by a new cloud instance or a
rebuilt worker VM; and with the system disk itself. Nothing on
it outlives its node, which is why the class holds data an application
replicates (CNPG), data VolSync backs up (§3.1), and data that can be
derived again.

Per-workload selection is a two-axis decision — (1) does the data need
to persist at all, (2) what performance does it need — then the data's
character (working state vs fixed assets) picks the backing. The full
decision framework with worked examples lives in
[declarative/workloads.md](../declarative/workloads.md) §2; the short
form:

1.  Re-derivable → a **claim with `off('<reason>')`, or an
    `emptyDir`** (the exemption declared explicitly).
2.  Working state → **local-path + VolSync** (§3.1); databases →
    **CNPG** on local-path.
3.  Fixed assets (large, append-mostly) → **NAS** (POSIX/streaming,
    homelab) or **object storage direct** (S3-speaking or archival).
4.  POSIX RWX over object capacity and NAS can't serve it → justify
    **JuiceFS** per §6, in writing, per app.

## 3. Block-layer mobility: VolSync now, Longhorn later

### 3.1 VolSync on local-path (the initial build)

Every working-state volume (rule 2: local-path, no app-level
replication) is a `claim` under a retention class, which gets a VolSync
`ReplicationSource` with the restic mover: scheduled backups to the
backup bucket (§4), retention by class, `copyMethod: Direct`, since
`local-path` has no snapshots. VolSync needs no CSI snapshots — it
mounts the claim directly; mover pods exist only while a mover runs, and
the controller idles at well under 100 Mi. This one mechanism serves as:

-   **Backup** (§5): the recurring job every stateful app gets by default.
-   **Mobility**: a copy from claim to claim by the `rsync-tls` mover
    (below), which works identically same-site and cross-site, with no
    claimRef surgery.
-   **DR**: a restore from the repository, onto a rebuilt node.

**A `Direct` copy of an embedded database can be torn.** The mover
reads the claim file by file while the application keeps writing to
it, which is what `Direct` means. So a claim whose application keeps an
embedded database in it, an engine the application links and that
writes files of its own on the claim rather than a server it connects
to, can be copied in a state that no crash leaves behind. A SQLite file
can be read beside a write-ahead log that a checkpoint has already
folded into it. A `RocksDB` manifest can name table files that a
compaction created after the copy listed the directory, or deleted
before the copy read them. Such a copy restores faithfully and matches
its checksums, yet the engine refuses to open it.

The definition decides which claims these are. Among the applications
rfc-008 §4.2 gives a backed claim, they are today:

-   matrix, whose continuwuity keeps `RocksDB`;
-   golinks, whose store is a `LevelDB` directory
    (`docker/golinks.Containerfile`);
-   syncthing's index, which is `LevelDB` before syncthing 2.0 and
    SQLite from it;
-   authelia, jellyfin's database and settings, and spoolman, each on
    SQLite.

The engines of shoko and of qbittorrent's resume data are not read, and
each of them that turns out to be embedded joins the list. seedwatch's
history is two JSON-lines files the application appends to itself
(`docker/seedwatch.Containerfile`), which the definition above leaves out.

What answers the case is a procedure, not a driver. The spot-restore of
such a claim opens the database rather than comparing checksums
(operations.md §4), and a copy that does not open is restored from the
copy before it, which is the RPO §5 states for such a claim. A
snapshot-capable driver, which would copy from the state a power cut
leaves and which every engine above is built to recover from, is
deferred with adoption criteria (§3.4). The metrics stores are not this
case: `vmbackup` copies them from a snapshot of their own, and VolSync
does not copy them at all (§5).

**The snapshot definitions are served all the same.** VolSync's
controller watches `VolumeSnapshot` objects whatever a source's
`copyMethod`, and does not start on a cluster that does not serve the
kind, so `k8s-base` installs `external-snapshotter`'s
`snapshot.storage.k8s.io` definitions ahead of it
(declarative/cluster-infra.md §1, item 5). That is all it installs of
`external-snapshotter`: VolSync creates a snapshot only for
`copyMethod: Snapshot`, and every mover here copies `Direct`, so
nothing creates one and no snapshot controller runs.

**A move is asked for, and it backs nothing up.** VolSync 0.16.0 has no
idle mover to leave standing: every source syncs on its first
reconcile, whatever its trigger, so a restic source declared on a claim
is a backup at declaration, and restic has no retention of zero, so a
move through the bucket would leave a snapshot behind it. The
`rsync-tls` mover needs no bucket, repository or credential: a source
and a destination copy claim to claim over a Service in the namespace,
on port 8000. So a claim's name and its restic source's carry a
generation, while its repository, its password and its Secret are keyed
by its base name, and `claim` takes `moving_to`, the node labels the new
generation is made on, under any retention. A move is three merges:

1.  **Copy.** The generation is raised with `moving_to` set, and the
    workload's replicas are set to zero. The claim of the generation before stays with
    its restic source, if it has one; the new claim is added, with no
    source, since one would back up a claim the copy has not filled;
    and an `rsync-tls` source and destination of one name, both
    `copyMethod: Direct` and started by hand (`trigger.manual`), copy
    the one into the other. The source depends on the workload, so it
    is applied after the apply that sets the replicas to zero, since a
    copy of a claim in use is torn; that orders the request to stop and
    not the end of the pods, which can still be writing inside their
    grace period, and the wait that closes the gap is
    [Aetf/kluster-ops#724](https://github.com/Aetf/kluster-ops/issues/724),
    ahead of the first move. The destination's mover selects `moving_to`, which is what places the
    new claim, since it waits for its first consumer. The apply waits
    for the source's `lastManualSync` through `pulumi.com/waitFor`, with
    a timeout sized to the claim, and a red run means no second merge,
    because reclaim `Delete` has no undo on an `off` claim (§3.3).
    What the copy's call changes is the new claim alone: the old claim
    keeps the class and the size it was made with, whatever the call
    states, since a changed class would replace it, and a replace of a
    named claim deletes it first. Nor does a move grow a claim in place:
    neither class expands one, and the size the new claim states is a
    request `local-path` does not enforce.
2.  **Protection off**, where the old claim is protected (§3.3).
3.  **Finish.** The same generation without `moving_to` removes the old
    claim and the movers, and the workload is scaled up on the target.
    Under a class, the new source takes its own cache on the target and
    syncs to the same repository on its first reconcile, and the old
    source's removal takes its cache.

Every mover states its pod security context, without which `restricted`
refuses it, as the user the workload runs as, so that it can read the
data, and its resources (declarative/workloads.md §1).

**A restic mover runs where the workload does.** A `local-path` claim
binds on the node its first consumer is scheduled to, and VolSync
0.16.0 places a `Direct` mover beside a pod that uses the claim and
gives it no node selection at all while no pod does. So a source that
reaches a claim before the workload's pod would bind the claim wherever
its mover landed, which may be a node the workload can never run on.
`claim` therefore takes the workload's `placement`, which the `Claim`
it returns records and the workload's pod takes as its affinity, so the
two are one value. Every restic mover of the claim carries the
placement's node affinity as `moverAffinity`, term for term: the
required terms, which decide the nodes the claim may bind on, and the
preferred ones, which weigh the scheduler's choice among them as they
weigh the workload's own pod, and the node chosen is the workload's for
the claim's whole life. Whichever of the mover and the workload's pod
is scheduled first, whether on the claim's first sync or after the
claim is made again, the claim binds on a node the placement admits,
weighed by its preferences.

**New rule:** the placement is every node constraint the workload is
under, stated as its pod's affinity. A pod that a volume already holds
to one node, as a `nas_claim` does, states that node's pool too, since
otherwise its claim's mover may bind the claim on a node the volume
never reaches. A placement stated against other pods, pod affinity or
anti-affinity, is refused for a backed claim, since the mover does not
reproduce those pods. During a copy, the placement is the workload's on
the claim being copied; it names the target in the finish.

**New rule:** a pod that mounts more than one backed claim is placed on
one node, and hands each claim that placement, the second as
`placement=<first>.placement`. Each claim's mover binds its own claim
and is scheduled apart from the others, so under a placement that
admits several nodes, two movers can bind one pod's claims on two of
them, and the pod, which needs both, then runs on neither.

Every claim with a mover carries the allow for its movers' traffic,
which reaches the claim's namespace, the cluster's DNS and, for a restic
mover, the bucket's endpoint, and nothing else. **Unproven live**: the
destination's mover placing a claim that is not yet bound, a restic
mover's affinity holding such a claim to its workload's placement, the
destination's Service resolving by its DNS name in the source's
`address`, the copy's timeout per GiB, the movers' resource figures,
restic's calls on the bucket itself under a key scoped to one prefix,
and the overlap of the source with the workload's terminating pods
(Aetf/kluster-ops#724).

The cost is planned downtime proportional to volume size per move —
acceptable because moves are rare, deliberate events (the legacy cluster
saw about one per year).

### 3.2 Longhorn: deferred (2026-08-22), with adoption criteria

Longhorn buys live detach/reattach, near-instant rebuild-to-move, and
volume snapshots — for a **standing** cost of 2–4 GiB RAM on the homelab
VM + 0.5–1 GiB on the cloud node, a 12%-of-node instance-manager CPU
request, ~256 MB RAM per TB of replica, and one more operator to run
(nodes.md §4.4 is the measured argument for refusing standing infra
costs that idle).

**Adoption criteria.** It enters the cluster when any one of these is
true. Each is a question with an answer on record, not a matter of
taste; none of them is "Longhorn would be nicer":

-   **Move rate**: volume moves run above **~1 per quarter** on volumes
    whose VolSync copy window is user-visible downtime. The count is
    of moves actually performed; the legacy baseline is about one per
    year (§3.1), so this is a factor-of-four change in behavior, not a
    bad quarter.
-   **In-cluster RWX**: an app needs RWX that the NAS cannot serve, by
    the same test the JuiceFS gate applies (§6, clauses 1–2) and
    answered the same way — in writing, per app, before the component
    is added.
-   **Same-site replica=2**: a second homelab worker VM exists
    (nodes.md §4.3). Until it does, replica=2 has nowhere to put the
    second replica, because cross-site replicas are prohibited outright
    (principle 3). Note what it would buy even then: both VMs share one
    physical host, so replica=2 is availability across VM restarts and
    drains, not durability against host loss (nodes.md §4.3) —
    durability stays Tier 0, rebuild plus backups (§5).

**Adoption-day facts**, as of 2026-08: pin **≥1.12** — dual-stack
cluster support starts there and v1.11 shipped an instance-manager
memory leak (#12668); Talos needs the
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
    retention class) is the safety net. **A claim under `off` has no
    undo**: its deletion takes the data, which is what `off` means and
    why a move's copy is confirmed before its finish removes the old
    claim (§3.1). Two kinds of volume reclaim `Retain` instead, each
    for its own reason. A NAS claim's static PV points at a path of the
    export, and deleting it never touches the data. A node volume's
    class (§6) holds a dataset preserved rather than backed up, and
    its directory is named by its claim's namespace and name, so a
    claim made again under the same name, generation included, finds
    it rather than an orphan. What `Retain` leaves is accounted for by
    hand: a removed claim's PersistentVolume stays `Released` until it
    is deleted, and a move within such a class leaves the old
    generation's directory on the volume until it is removed there.
-   **Deliberate preservation happens at the IaC layer, not the reclaim
    layer: `protect=True`.** Data-bearing and identity-bearing
    resources are Pulumi-protected so destroys/replaces fail loudly:
    the hath cache volume (a slice of the globally distributed H@H
    archive — **not** re-derivable scratch, though also not
    VolSync-backed: like NAS volumes it sits outside the cluster backup
    regime, its redundancy being the H@H network itself), all buckets,
    CNPG `Cluster`s, precious PVCs, each restic repository's password
    (the repository's only key, escrowed nowhere), `machine_secrets`,
    DNS zones, and the reserved public IP. Deleting a protected resource is a two-step,
    reviewed act: an unprotect diff, then the delete — the IaC
    equivalent of removing a safety catch, visible in preview both
    times. (Where a real resource must outlive its Pulumi entry,
    `retainOnDelete` is the tool — used case-by-case, never as a
    default, because it manufactures exactly the unaccounted-resource
    problem `Retain` had.)
-   **Orphan audit is a standing procedure.** A `just` recipe compares
    on-disk PV directories against live PVs (per node) and reports
    unaccounted entries; scheduled quarterly as an unattended check
    that alerts only on findings (operations.md §4), target zero. The legacy cluster's accumulated retained-PV folders get a
    one-time census during migration — each mapped to an app or
    deleted (migration.md).

### 3.4 Snapshot-capable local volumes: deferred (2026-11-17), with adoption criteria

A local driver with CSI snapshots buys two things. A claim under it is
copied `copyMethod: Snapshot`: VolSync takes a `VolumeSnapshot` of the
claim and gives the mover a volume made from it, so the copy is the
state a power cut would leave, which an embedded database is built to
recover from, rather than the torn copy `Direct` can make (§3.1). And
a claim gets a size it cannot exceed, where a `local-path` claim shares
EPHEMERAL with the node's images and logs and can grow until the
kubelet evicts every pod on the node.

**Why not now, on either node class.** The census that would use it is
about a dozen working-state claims holding a few GiB, and the price on
the cluster's Talos release is standing:

-   a second provisioner, because `local-path` stays for the node
    volumes, for the claims a chart makes and for `off` claims;
-   a cluster-wide snapshot controller, where `k8s-base` serves only the
    definitions today (§3.1);
-   under a ZFS or an LVM driver, a storage pool that no
    machine-configuration document declares, so a privileged Job builds
    it;
-   under a ZFS driver a guest ARC on a host whose RAM already binds, and
    under an LVM one a pool that fails every volume on it at once when
    it fills.

What it changes is narrow. Barman and `vmbackup` stay whichever driver
is chosen, and a lost node is restored the same way, from the restic
repository into a claim on the rebuilt node, since a snapshot lives on
its volume's disk and dies with it. So what the rent would insure is a
rare event, a torn latest copy meeting a restore, and the canon buys a
rare event as a procedure, by the rule of nodes.md §4.4: the copy
before it restores instead, at the RPO §5 states, and the drill that
opens the database finds the torn one (operations.md §4).

**Adoption criteria.** The driver enters on a node class when any one
of these holds there:

-   **Evidence**: a restore, drill or real, of a working-state claim
    yields an embedded database that its engine refuses while the copy
    before it opens. One such reading is enough, since it is the failure
    `Direct` allows, observed.
-   **A ruling**: the operator rules that a claim's RPO must hold at
    its class's period, ≤24 h for a daily class, through a torn copy.

Two changes in cost reopen the decision without deciding it:

-   the cluster's Talos release declares the pool in machine
    configuration, which removes the Job;
-   OpenEBS marks `rawfile-localpv` stable, which removes the disk, the
    pool and the Job on both node classes.

**The shape adoption takes**, so that adopting is a set of slices
rather than a new design.

-   **Where.** The worker first, since nearly all the census is there.
    The cloud control planes only when a criterion holds for the cloud.
-   **The worker's pool** is a second virtual disk, sparse and raw, on
    the same nodatacow subvolume as the system disk
    (physical/homelab-host.md §1), declared by `physical`. It keeps the
    pool off the path by which the worker grows, and attaching it
    redefines the domain, which is a drained window. It is replaced with
    the node, so nothing on it outlives its node (§2).
-   **The cloud's pool** is a partition of the boot volume, a
    `RawVolumeConfig` with a `VolumeConfig` capping EPHEMERAL. The cap
    applies only when EPHEMERAL is created, so each control plane takes
    it in a rebuild. Its writes land on the volume that holds etcd,
    inside nodes.md §1.2's rule 2, where every cloud claim's writes
    already land. A block volume of its own buys isolation that the
    cloud's claims do not need, and a second data disk on the node that
    carries `hath-cache` would break the node-volume selector, which
    matches one disk only.
-   **The driver is `TopoLVM` on a thin pool.** LVM holds no cache and
    needs no change to the node image, on a homelab host where RAM
    binds (nodes.md §4.1); `overprovision-ratio` keeps provisioning
    within the pool; and it is tested on the cluster's Kubernetes
    release. The OpenEBS ZFS driver is the alternative for the day RAM
    stops binding, and it is out on the cloud nodes, whose ARC would
    come out of their memory floor.
    `rawfile-localpv` becomes the first choice once OpenEBS marks it
    stable, since it needs no disk, no pool and no extension on either
    node class.

The slices, each a pull request of its own, in this order:

1.  The snapshot controller in `k8s-base`, from the pin that already
    serves the definitions; a driver's chart that bundles a controller
    has it turned off.
2.  The worker's data disk in `physical`, applied in a drained window.
3.  `TopoLVM` in `k8s-base`: the chart, the Job that builds the thin pool
    and, stricter than Talos is with a node volume (§6), refuses a disk
    carrying any signature it did not write, the `StorageClass` and the
    `VolumeSnapshotClass`.
4.  `claim` takes its copy method from its class, and a worker
    application's claim defaults to the worker's snapshot class, so no
    application chooses a copy method.
5.  The rent line in nodes.md §4.4, from a scratch install's readings,
    and the data disk's place in the rebuild runbooks (operations.md
    §3.2, §3.5).

A node class adopted before its first backed claim lands has nothing to
carry over. On one adopted after, each backed claim crosses by §3.1's
move, or by a restore from its repository into a claim of the new class.

**Adoption-day facts**, as of 2026-11:

-   `TopoLVM` v0.42.1 supports Kubernetes 1.34–1.36, restores a snapshot
    only into a thin volume on the source's node, needs the thin pool to
    exist beforehand, and takes its webhooks' certificates from
    cert-manager.
-   `TopoLVM` deletes the claims of a node that is deleted, and `apps`
    would recreate them empty on its next `up`, so adoption puts the
    restore ahead of that `up` in the rebuild runbooks.
-   Whether `dm_thin_pool` loads on demand on Talos or needs
    `machine.kernel.modules`, and what each driver's pods request, are
    read on the scratch install; no chart states requests.
-   Not Longhorn by another door: no replica, no live detach and no RWX,
    so §3.2's criteria are untouched. Longhorn, adopted by them, brings
    its own snapshots, which close this item.

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
dimension restic-style churn stresses. R2 Standard is the named alternate
if B2's 3×-stored egress allowance ever bites.

**Placement rule (2026-08-22)**: the **backup** bucket must not live
with the provider whose loss it insures — OCI tenancy termination is an
enumerated risk (nodes.md §3.1), so cluster backups stay on B2
regardless of where the cloud pool lands. The bucket, its keys and its
lifecycle rules are Pulumi-managed, declared by the `physical` stack
(`BackupBucket`).

It is not the installation's only bucket. Every other bucket is
declared by the stack it serves, beside what it serves, and one on OCI
holds only what is staged there on the way to an image import, which
is nothing the placement rule insures. Today those are:

-   the state backend's dump bucket on the same B2 account
    (`kluster-state-backend`, physical/state-backend.md §5);
-   the OCI Object Storage bucket the state backend's boot image is
    imported through;
-   the OCI Object Storage bucket in `physical`'s compartment that the
    cloud nodes' Talos image is imported through (declarative/physical.md
    §1).

The state backend's buckets are declared by the `state-backend` stack,
whose state is committed to this repository rather than kept in the
backend it creates (framework/ci.md §1).

**Backup-integrity rules (2026-08-23)** — backups are the actual HA
mechanism (§5), so backup *deletability* is a first-class threat
(architecture.md §4.1): a compromised app namespace or CI job must not
be able to destroy the safety net it lives inside.

-   The backup bucket keeps **prior file versions ≥30 days** (B2
    lifecycle: hide, then delete) — restic/barman deletions become
    recoverable hides, which is the ransomware/fat-finger floor. Costs
    a modest storage overhead on churn; that is the price of the floor.
-   Keys are **prefix-scoped per consumer**, each with the capabilities
    its consumer needs and **never `deleteFiles`**: an app's restic key
    reaches only `volsync/<its-namespace>/…`; barman's
    (`cnpg/<namespace>/…`), the monitoring stack's `vmbackup` key
    (`vm/<namespace>/…`) and the etcd snapshots' keys (`etcd/`)
    likewise. A store's writer key is `listFiles + readFiles +
    writeFiles`. One key per family of each backed namespace, from the
    census `conventions.backup.BACKED_NAMESPACES`, minted by
    `physical`.
    (restic/barman need list+read even to *back up*, and `vmbackup`
    lists the destination to upload only what it lacks — a literally
    write-only key cannot work — and their prunes still run in-cluster
    per the retention classes: without `deleteFiles`, a B2 deletion
    degrades to a **hide**, which the ≥30-day lifecycle rule purges.
    Retention semantics are preserved while nothing in automation can
    destroy a version before it ages out.) A key whose consumer keeps a
    store in the bucket — restic, barman, `vmbackup` — also carries
    `listBuckets`: each of them reads names that may not exist yet and
    takes only a 404 for absence, and B2's S3 API answers a read of a
    missing name with 403 instead unless the key holds `listBuckets`
    ([bisected over single capabilities](https://github.com/kopia/kopia/issues/5329)),
    so without it a store's first backup cannot start. On a key
    restricted to the one bucket, `listBuckets` reads that bucket's own
    settings and grants nothing on files. The etcd snapshots have two
    keys, neither with `listBuckets`. The uploader's is `writeFiles`
    alone, since `talos-backup` puts the object and reads nothing. The
    reader's is `listFiles + readFiles` and writes nothing. It is the
    key the etcd restore-verify lists the prefix and downloads the
    newest snapshot with, a reader that lands after its ciphertext
    (credentials.md §1 rule 6). Both a recovery and the restore-verify
    take the newest name, so whatever runs with the reading key must not
    be able to write one. **No delete-capable key
    exists in any automation**; the B2 account's master key is an account root in
    the operator's personal estate — deliberately not in the seed kit
    (credentials.md §2) — and is borrowed only to re-seed, account 2FA
    on. The micro's pg_dump uploader is write-only too, on the state
    backend's own bucket (`writeFiles` alone — it keeps no index,
    state-backend.md §5).
-   **The etcd snapshots' retention is the bucket's.** A second
    lifecycle rule, on `etcd/`, hides a snapshot 14 days after its
    upload (`conventions.backup.ETCD_SNAPSHOT_RETENTION_DAYS`), and
    nothing prunes the prefix: its writer key deletes nothing, and no
    code hides a snapshot. That rule's prefix overlaps the whole-bucket
    rule's, and B2 evaluates every rule whose prefix a file's name
    starts with and applies, for each property, the smallest value any
    of them sets
    ([lifecycle rules](https://www.backblaze.com/docs/cloud-storage-lifecycle-rules),
    "Overlapping Prefixes"). The `etcd/` rule sets only the hide and
    the whole-bucket rule only the purge and the cancellation of
    unfinished uploads, so a snapshot takes each from the one rule that
    sets it: listed for 14 days, then kept hidden for the 30-day floor
    above. A hide on a timer is safe on this prefix because every
    snapshot is a whole backup on its own. In a restic repository, a
    barman archive or a `vmbackup` copy an old file can be part of the
    newest backup, so their prefixes carry no hiding rule, and a timed
    hide there would delete live backups on a schedule.
-   Verification item (physical.md §6): a scoped key must fail to
    list/delete a foreign prefix.

## 5. Backup architecture (the actual HA mechanism)

Per nodes.md §5, durability = declarative rebuild + backups + drills:

1.  **etcd**: hourly snapshots from the Talos control plane, shipped to
    `b2://…/etcd/`, retained ~14 days — taken in the cluster by
    `siderolabs/talos-backup`, the CronJob `etcd-snapshot` in a
    namespace of its own, `etcd-backup`, which `k8s-base` declares
    (`components.backup.etcd`).
    -   **Placement**: on the control planes, so the snapshots go on
        through an outage of the home site, the upload leaves from the
        cloud, and the stream stays off the mesh to the worker. The run
        works in a memory-backed directory of its pod, since a control
        plane's disk is etcd's own (nodes.md §1.2).
    -   **Credentials**: it speaks Talos with a certificate Talos issues
        it for the `os:etcd:backup` role alone and renews by itself, and
        writes with the `etcd` scope's writer key, sealed strict into
        `k8s-base` as `writer-etcd`; the key writes under `etcd/` and
        cannot list, read or delete. Reading the snapshots back is the
        `etcd-reader` scope's key, `listFiles + readFiles` on `etcd/`,
        sealed strict into `k8s-base` as `reader-etcd`.
    -   **A run**: one snapshot, refused without etcd's hash trailer,
        encrypted with age to every escrowed backup generation in
        `src/kluster/lib/state_backend/machine/backup-recipients.txt`,
        the recipients the state backend's dumps are encrypted to, and
        after them to the restore-verify's own recipient,
        `src/kluster/conventions/etcd-drill-recipient.txt`, and
        uploaded. Every step's error ends the run non-zero, so a run that
        succeeded is an object uploaded.
    -   **The object**: `etcd/kluster-<RFC 3339 UTC>.snap.age`, such as
        `etcd/kluster-2026-11-17T06:05:00Z.snap.age`. The stamp is
        fixed-width, so name order is time order. The object carries
        nothing beside the snapshot, no file info, so no revision is
        recorded with it.
    -   **Liveness**: the scheduled-job rule family watches it
        (operations.md §4), and its playbook is operations.md §7.1.
    -   **The restore-verify**: the CronJob `etcd-restore-verify`, in
        the same namespace, reads the newest snapshot back daily, the way
        a recovery would. It lists `etcd/` with `reader-etcd` from the
        snapshot job's `max_age` ago, an hour and a half, so an empty
        listing is a stale bucket read independently of the snapshot
        job's own status; downloads the newest snapshot by its id;
        decrypts it with an age identity of its own, `etcd-drill`, which
        no escrow holds and which opens nothing the escrowed generations
        do not; restores it with `etcdutl`, which checks etcd's hash; and
        starts a scratch etcd on the restored member, listening on
        `127.0.0.1` inside the pod, which has to answer healthy and hold
        `/registry/namespaces/kube-system`. It runs off the control
        planes, on the worker today, in the `etcd-age` image this
        repository builds (`docker/etcd-age.conf`), with its working
        directory in memory, so the decrypted snapshot never reaches a
        disk. Its playbook is operations.md §7.2.
    -   **Status**: both run. The snapshot job's key and the
        restore-verify's two values are sealed into `k8s-base`, which
        declares each CronJob with them and stops at the read of a value
        it does not hold (physical/bring-up.md §1). Snapshots taken before the `k8s-base`
        apply that added the restore-verify's recipient carry the
        escrowed generations alone, and leave the listing within 14
        days.
    Restore path is documented Talos `--recover-from-snapshot` bootstrap, to be drilled both in-place and
    onto a substitute node (the CP cold-standby path, nodes.md §5 Tier 0);
    neither form has run.
2.  **Volumes**: VolSync restic backups on every working-state PVC
    (§3.1), same bucket, retention by class
    (declarative/workloads.md §3). Installed by the `k8s-base` stack;
    no `ReplicationSource` is declared until the first `claim` under a
    class lands, so none runs (nodes.md §5 Tier 0).
3.  **CNPG**: one instance per database, its archive in the bucket,
    and a monthly restore drill, all declared by the database shape on
    the application base (`kluster.components.apps.database`,
    declarative/workloads.md §4).
    -   **The archive**: the Barman Cloud plugin archives WAL
        continuously and takes base backups on the database's retention
        class's schedule, kept for the class's reach, to
        `conventions.barman_repo_path` of the namespace and the
        database's server name, `cnpg/<namespace>/<name>-g<generation>/`,
        under the namespace's writer key of the barman family, sealed.
        The server name carries a generation because barman archives a
        new cluster only into a server's path that holds no archive: a
        land retried after a revert raises it and archives apart.
    -   **No standby**: a second instance would share the first's site,
        node and disk, so durability is the archive, and the drill is
        what proves it.
    -   **A migrated database** recovers once, at its land, from the
        legacy store it is handed, through the legacy credential ported
        as it is, and archives to the bucket from then on.
    -   **The drill**: a CronJob beside each database recovers a
        throwaway cluster from the newest base backup in the archive
        and the WAL after it, checks that it left recovery and holds
        the application's rows and extensions, and removes it; its
        playbook is operations.md §9.1.
    -   **Status**: none runs. No application declares a database yet.
4.  **Metrics history**: the monitoring stack's VictoriaMetrics stores,
    each copied hourly and incrementally by `vmbackup` to
    `b2://…/vm/monitoring/<store>/` (`conventions.backup.metrics_repo_path`)
    under the `vm` family's writer key, which `credentials` seals into
    `k8s-base`, the stack that declares the monitoring stack, with the
    backup jobs and the restore. `vmbackup` copies from an
    instant snapshot of hard links to immutable parts, because a
    file-by-file copy such as VolSync's is not consistent for a store
    that merges and deletes its parts while the copy runs. The restore
    path is `vmrestore` into an empty data directory before the store
    starts, and a backup that cannot be read keeps the store down rather
    than letting it start empty over a history that exists.
5.  **NAS data**: stays under the NAS's own backup regime — out of
    cluster scope, but media moving onto NAS PVs must not silently drop
    out of that regime. (hath's cache is deliberately outside *every*
    backup regime — §3.3.)
6.  **Drills are part of the design**: a restore that hasn't run this
    quarter is assumed broken. By that rule every restore path the
    cluster owns on this list is assumed broken today: no drill has run,
    and nothing schedules one (operations.md §4).

RPO: ≤1 h for etcd, ~0 for CNPG (WAL), ≤1 h for the metrics history,
and for a volume the period of its retention class's schedule
(`conventions.backup.RETENTION_CLASSES`): ≤24 h under the daily
`STANDARD` and `PRECIOUS`, ≤7 days under the weekly `BULKY`. A volume
whose application keeps an embedded database (§3.1) holds that period
while its latest copy opens, and up to twice it when it does not, since
its restore then takes the copy before it (operations.md §4): ≤24 h and
up to 48 h under a daily class. RTO: rebuild either site from Pulumi +
backups in ~1–2 h hands-on.

## 6. JuiceFS containment policy

JuiceFS is not banned; it is rationed. **The quarantine is the shape of
the deployment, not a warning label**: no cluster-wide CSI driver is
installed, so no `storageClassName` reaches JuiceFS and no app can adopt
it by accident. Each admitted workload gets its own filesystem, its own
metadata store on the same node as its mount, and its own object bucket,
mounted in-pod as a sidecar with requests sized from load rather than
from idle. A failure is therefore one app's failure, and the shared
mega-filesystem of the legacy cluster cannot re-form. Admission is the
gate; the containment is what admission buys you.

A workload may use it only if all of:

1.  It needs POSIX semantics over object-storage capacity (too big for
    local NVMe, not servable from NAS — e.g. cloud-pool workload needing
    cheap bulk space).
2.  RWX/multi-node access or capacity is the actual requirement, not
    convenience.
3.  It gets its **own** filesystem and its own metadata store (per-app
    blast radius; no shared mega-filesystem like the legacy cluster), with
    automatic metadata backup to the object bucket enabled.

**Decision (2026-08-29): the census is zero.** No workload in the design
uses JuiceFS, so the policy above is a gate with nothing behind it and
the CSI driver stays uninstalled. Everything else: immich media on NAS,
the hath cache on a cloud block volume, qbittorrent on the home side,
backups to object storage directly.

The last candidate was the legacy VPS's syncthing replica (+ its dav
share, ~110 GB of the legacy cluster's 5 Ti PVC). **There is no cloud
replica** (ruled 2026-11-05, `kluster-ops#712`). Its role was an
always-reachable port for roaming clients, and every client already
holds a copy, so nothing needs the data in the cloud. The balancer's
peer port gives that reach to `syncthing-nas` on the homelab worker,
and dav moves beside it.

That leaves the hath cache as the one cloud block volume, and these
are the rules a node volume lives by:

-   **The volume reaches the cluster without a vendor component in it.**
    The `physical` stack attaches it (`conventions.NODE_VOLUMES`), and
    the machine configuration of the node it attaches to mounts it: a
    Talos `UserVolumeConfig` named for the row, one XFS partition
    filling the disk that is not the boot disk, mounted at
    `/var/mnt/<name>` and found again on every boot by the partition
    label `u-<name>` that Talos wrote to it
    ([declarative/physical.md](../declarative/physical.md) §2). The same
    configuration labels the node with the volume's name, and the
    cluster sees a `local-path` class whose pool is the mount (below).
    No OCI CSI driver, no cloud controller.
-   **The row's name is the dataset's identity**, in every place it
    appears: the OCI volume's logical name, the partition label, the
    mount path, the node label and the class,
    `conventions.node_volume_class(row)`. A renamed row is therefore a
    protected volume deleted, which Pulumi refuses; moving a dataset to
    a new name is a migration, never an edit.
-   **Talos formats a volume only where its disk probe finds nothing.**
    Provisioning happens only while no partition labelled `u-<name>` is
    found. It takes the disk the selector matches when Talos' disk probe
    recognizes nothing on it, or free GPT space of 100 mebibytes or
    more. The probe knows GPT and a fixed set of filesystems, and no other
    partition table. So a disk holding one of those filesystems on the
    bare device is refused, while an MBR-partitioned disk, or one
    holding any other filesystem, reads as empty: Talos writes a GPT
    over it and formats it. **New rule:** a dataset is seeded only
    through its volume's mount on the node, never on a disk prepared
    elsewhere, and no disk holding other data is attached to a node
    that a volume row names. Once located, a partition carrying XFS is
    mounted as it is, and one carrying another filesystem is refused
    rather than mounted. A located partition on which the probe finds
    no filesystem at all is formatted. For the hath cache both
    formatting paths cost a reseed, which is accepted because it is
    preserved rather than backed up and is recovered by reseeding
    (§3.3); the rule above is what keeps any other data off those
    paths. The dataset's own disk has free space only between an OCI
    resize and the node's next boot, when `grow` extends the partition
    into it. A disk re-attached in that window can gain a second, empty
    `u-<name>` partition, if Talos looks for the volume before the
    existing partition has been discovered, and every boot after that
    mounts whichever of the two it locates first. That sequence is read
    from Talos' source and has not been observed. **New rule:** a
    resized volume's node is rebooted before any detach of the volume
    and before any rebuild of the node. Nothing is encrypted: every key source Talos offers either
    binds the dataset to a machine that a rebuild replaces or puts the
    key in the instance's own metadata.
-   **A node volume is a `local-path` pool, and needs no mechanism of
    its own.** The provisioner's configuration keys its pools by class,
    so `k8s-base` derives one class per row, `cloud-block-<row>`, whose
    pool is the row's mount, `/var/mnt/<name>`, stated for every node
    (`DEFAULT_PATH_FOR_NON_LISTED_NODES`), which spares it a Kubernetes
    node name. Each class reclaims `Retain`, names each claim's directory
    `<namespace>/<claim>/` inside the pool (the parameter `pathPattern`),
    so that a claim made again finds its directory, and hands out
    `local` volumes (the annotation `defaultVolumeType`). An application
    declares an ordinary `claim` of the class, and the pod that consumes
    it needs no node selection of its own: the class places it.
-   **The class confines its consumers to the node carrying the
    volume.** Each class allows one topology, the node label with the
    row's name, which only the volume's node carries. The scheduler holds
    every candidate node of a pod against the allowed topologies of each
    claim it still has to provision, as a selector over the node's
    labels, and drops each node outside them from the pod's candidates.
    A consumer that selects no node therefore lands on the volume's node.
    One that selects another node, or that the volume's node cannot take
    for its other constraints or its resources, is never scheduled, and
    its claim is never handed to the provisioner. Its `FailedScheduling`
    event counts each node dropped this way under `didn't find available
    persistent volumes to bind`. The topology is the label rather than
    `kubernetes.io/hostname` so that the class names no Kubernetes node:
    a volume that moves to another node changes which node carries the
    label and leaves the class as it is. The provisioner reads no
    topology of its own; it provisions on the node the scheduler
    selected and pins the volume there by hostname.
-   **A volume that failed to mount still cannot take the data**, and
    neither half needs a step of its own. Talos leaves the mount point
    behind on the node's own disk once it has mounted a volume there, so
    what the data meets is that plain directory:
    -   **At provisioning.** The helper pod's `hostPath` asks for
        `DirectoryOrCreate`, but the kubelet's view of `/var/mnt` is
        read-only, and it can create nothing inside a plain directory
        there (declarative/physical.md §2). The helper does not start,
        the claim stays pending, and nothing lands on the node's disk.
    -   **At every later start.** A `local` volume's path has to exist,
        and with the volume unmounted the claim's directory inside it
        does not, so the kubelet refuses the volume and the pod stays
        pending until the volume is back.

**Lifting the quarantine: the criterion is not on file.** Containment
has two levers and only one of them has a written test. *Admitting
a workload* is governed — the three clauses above, per app, in
writing (declarative/workloads.md §2) — and admitting one does not by
itself change the deployment shape: each is a sidecar with a filesystem
of its own, which is what the per-app blast radius means.
*Installing the CSI driver* is the lever with no test behind it. It is
refused today because the census is zero and there is nothing for a
driver to serve, but nothing here says at what census, or under what
guarantee, a driver would become the cheaper answer — so "should we install the CSI driver?"
currently resolves to "no" by default rather than by argument.

The criterion this needs is the §3.2 shape, and writing it is a decision
someone still has to make: a **count** (at how many simultaneously
qualifying filesystems do N sidecars cost more standing RAM than one CSI
controller plus its per-node agents — the standing-rent test of
nodes.md §4.4), and a **statement of what survives the change** (how
per-app metadata isolation and per-app failure domains hold up once one
driver mediates every mount, since that sharing is precisely what root
cause (b) came from — §1). Until both exist, the census staying at zero
is the whole reason the driver stays out, and growing the census is a
reason to write the criterion, not a license to skip it.

## 7. Alternatives Considered

-   **Longhorn replica=2 across sites** ("free" DR): every write pays WAN
    RTT synchronously; measured legacy homelab↔VPS latency makes fsync-heavy
    workloads unusable. Rejected on principle 3; async backup/restore covers
    the DR case at the RPO §5 states.
-   **Rook/Ceph**: proper distributed storage, but wants ≥3 storage nodes
    per site and an operational budget this cluster doesn't have. Strictly
    overkill for 2–3 nodes.
-   **Longhorn from day one** (the 2026-08-21 plan): rejected 2026-08-22
    in the economy pass — 2–4 GiB standing RAM + CPU reservations to keep
    a mobility affordance idling for events that historically happen
    about once a year. VolSync's copy from claim to claim covers the
    requirement at
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
    where possible, local-path + VolSync otherwise".
