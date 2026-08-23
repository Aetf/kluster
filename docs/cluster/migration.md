# Migration Plan - Legacy to Next Gen Cluster

Objective: Migrate applications and data from the legacy cluster (`~/projects/kluster-code`) to the new cluster, minimizing downtime for critical services like `hath` and ensuring data integrity.

> **Status**: still the early generic draft; per-app details predate the
> 2026-08-21/22 design decisions (there is no JuiceFS in the new cluster —
> storage.md; per-app targets follow storage.md §2's decision rules). The
> sections below marked 2026-08-22 are current; the rest needs a rewrite
> once the declarative layer docs exist.

## 0. Sequencing constraints (2026-08-22)

-   **Host NVMe is nearly full** (~85 GB free) while both the legacy k3s
    and the new Talos VM coexist on the homelab host. The Talos VM starts
    at ~60 GB and grows only as legacy data is reclaimed (k3s images,
    local-path PVCs, prometheus's 30 GB) — plan per-app migration to
    interleave "migrate app → delete legacy PVC → grow VM disk" instead of
    big-bang. (nodes.md §4.2.)
-   **GPU passthrough is a migration blocker to verify first — and its
    *activation* is a scheduled cutover**: immich and jellyfin need
    `gpu.intel.com/i915` in the new VM (UHD 770 VFIO passthrough,
    nodes.md §4.1). Verify the capability early on a scratch VM, but
    binding vfio-pci on the host is one-way for the running system —
    the host loses i915 and the *legacy* cluster's transcode workloads
    degrade to CPU that instant. So the bind happens in a planned
    window, sequenced with (or just before) the immich/jellyfin
    migration itself, not at bootstrap.
-   **Every absorbed resource updates its old tracker.** Resources
    migrating under Pulumi are currently tracked elsewhere — gw-config
    (FRR, nspawn units/rootfs), yadm/aconfmgr (qbittorrent unit,
    seedwatch/thread-dashboard quadlets, state-backend compose), the
    DNSControl repo. Each migration step ends with a corresponding
    removal/pointer commit in the old tracker, so no resource is ever
    tracked twice or by nothing.

## 0.1 Host-native onboarding (new scope, 2026-08-22)

Beyond legacy-cluster apps, three host-native services join the cluster
(nodes.md §4.1): **qbittorrent-nox** (unblocked by the dual-stack v6 design,
architecture.md §3.5 — migrate its `/var/lib/qBittorrent` profile and
verify seedwatch's category paths survive the move), **seedwatch**, and
**thread-dashboard** (both currently podman quadlets; seedwatch moves
together with qbittorrent since it drives its API and reads NAS hardlink
counts). DNS (AdGuard), the state-backend Postgres, and HAOS explicitly do
NOT onboard — they stay host-side by design.

## 1. General Migration Strategy

The migration will follow a stop-copy-start approach for each application to ensure data consistency:
1.  **Scale down** the application in the legacy cluster to stop writes.
2.  **Migrate the data** (PVCs, S3 objects).
3.  **Deploy** the application in the new cluster pointing to the new storage.
4.  **Verify** and update DNS/Ingress.

## 2. Data Migration Details

### 2.1 Local Storage PVCs
Some legacy applications use local path storage (e.g., early `hath` implementation).
-   **Strategy**: Copy data from the old node's local path to the new storage system (likely a different storage class or JuiceFS in the new cluster).
-   **Tools**: `rsync`, `tar` over SSH, or a backup/restore tool like Velero.

### 2.2 JuiceFS / Object Storage (S3)
If the user decides to move the S3 bucket to a different region (as noted in legacy README), this will be the largest data migration task.
-   **Strategy**: Stop all services using JuiceFS to ensure consistency. Copy objects from the source S3 bucket to the target S3 bucket in the new region.
-   **Tools**: `aws s3 sync` or GCP equivalent if moving to Google Cloud Storage.
-   **Downtime**: This operation will cause significant downtime for all services depending on JuiceFS, proportional to the volume of data.

## 3. Application-Specific Migration Plans

### 3.1 HatH (Hentai@Home)
-   **Priority**: High. Downtime must be limited to a few hours.
-   **Storage**: Legacy code shows usage of Local Storage PVC (50Gi). If it has been moved to JuiceFS, refer to the JuiceFS migration strategy.
-   **Migration Steps**:
    1.  Identify current storage location (Local PVC host path or JuiceFS).
    2.  Prepare deployment in the new cluster (using the new Pulumi framework).
    3.  Stop `hath` in the legacy cluster.
    4.  Copy the 50Gi data to the new storage location. This should take less than an hour on a gigabit link.
    5.  Start `hath` in the new cluster.
    6.  Verify connection and operation.

### 3.2 Other Services
Other services (Authelia, Nextcloud, Syncthing, etc.) will follow the general migration strategy. Data stored in database (PostgreSQL/MariaDB) will need to be dumped and restored or migrated using replication if supported.

## 4. Rollback Plan
In case of failure during migration:
1.  Abandon the migration of the specific application.
2.  Scale up the deployment in the legacy cluster to resume service.
3.  Investigate and resolve the issue before attempting again.
