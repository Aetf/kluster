# Declarative Design: Workloads (the `apps` Stack)

The per-app pattern: one Python component per application, declaring everything
the app needs — workload, storage, exposure, DNS, secrets, backups, placement —
so that "deploy an app" is one reviewable diff in one place. This is the top
stack of [framework/pulumi.md](../framework/pulumi.md) §3 and the home of
~80–90% of all future changes.

> **Status**: designed 2026-08-22. Not implemented.

## 1. The component contract

Every app is a `Component` subclass (putils, RFC-001) that owns:

| Concern | Declared as | Governed by |
| --- | --- | --- |
| Namespace | created by the component (never shared) | pulumi.md §3 |
| Workload | Deployment/StatefulSet with **honest requests/limits** — CPU limits are mandatory on anything scheduled to the cloud pool (etcd shares those cores, nodes.md §1); memory requests sized from evidence, not idle numbers (the JuiceFS-sidecar lesson, storage.md §6) | nodes.md §§1, 4.4 |
| Storage | by the two-axis selection in §2 (performance × persistence; fixed assets → NAS/object) | §2, storage.md §2 |
| Backup | declared through the `backed_pvc` helper with a **retention class** — never ad-hoc schedules (§3) | §3, storage.md §3.1 |
| Exposure | pool label + route kind per the routing matrix, via the helpers (`public_route`/`public_port`/`lan_route`), which also emit the NLB listener + security rule for internet ports | architecture.md §3.6, dns.md §3 |
| DNS | emitted by the same helpers (CNAME to anchor; AdGuard rewrite for split-horizon) | dns.md |
| Secrets | SealedSecret first choice, `template.data` pattern | cluster-infra.md §1.1 |
| Placement | scheduling constraints only: site pool (cloud/homelab), the augmented node for dedicated-VIP workloads, GPU resource requests | architecture.md §3.6 |
| Network policy | per-namespace default-deny + explicit allows, part of the component | architecture.md §4.1 |
| Monitoring | scrape/dashboard labels per the legacy conventions (`release`, `grafana_dashboard`) so VictoriaMetrics/grafana pick them up | cluster-infra.md §1 |

What a component may **not** do: hostPort, hostNetwork, `externalIPs`
(architecture.md §6.6); cross-namespace reach-ins; unbudgeted sidecars;
cluster-scoped resources (those belong to `k8s-base` and go through its
closed-list rule).

## 2. Choosing storage: two axes, then the data's character

The storage.md §2 classes are the *menu*; this is how a workload
chooses. Ask, in order:

**Axis 1 — does it need to persist?** State that is re-derivable
(caches, transcode scratch, thumbnail stores, ML model downloads) gets
**plain local-path (or emptyDir) with no backup** — but the exemption is
explicit: the component declares `backup=None` with a one-line reason,
so "unbacked" is always a decision on record, never an omission.
Beware the false "cache": **hath's cache is not re-derivable** — it is
this client's slice of the globally distributed H@H archive, and must
be preserved (see the worked example).

**Axis 2 — what performance does it actually need?**

| Profile | Backing | Why |
| --- | --- | --- |
| Latency/fsync-sensitive: databases, app state stores, sync indexes | node-local NVMe (local-path) | NAS adds network RTT + ZFS-over-JBOD latency to every fsync; DBs on NFS is a known failure mode |
| Sequential/streaming, read-mostly | NAS is fine | throughput is what ZFS+NFS is good at; latency hidden by streaming |
| Cold, rarely read | object storage | highest latency, cheapest, durable |

**Then the data's character decides among the survivors:**

-   **Working state** (small-to-medium, hot, mutated in place) →
    **local-path + VolSync** — the default. If the app is a database,
    prefer **CNPG** (its replication + barman subsume the
    backup/mobility roles).
-   **Fixed assets** (large, append-mostly, immutable once written:
    media originals, photo masters, books, seeding payloads) → **NAS**
    when POSIX/streaming consumers live homelab-side, **object storage
    direct** when the app speaks S3 or the data is archival. Fixed
    assets never ride local-path: they'd bloat VolSync repos and pin
    huge data to a node for no latency benefit.
-   **The leftovers** — needs POSIX RWX over object capacity — is the
    JuiceFS quarantine gate (storage.md §6), per app, in writing.

Worked examples: immich = CNPG (db) + NAS (originals) + local-path
no-backup (thumbnails/ML cache); qbittorrent = NAS (payloads) +
local-path+VolSync (its config/state); syncthing-nas = NAS (data) +
local-path+VolSync (index/db); hath = **preserved but not backed**
cache (its own protected block volume, moved-never-recreated — the
"cache" is a slice of the globally distributed H@H archive, not
scratch, yet like NAS volumes its safety net is outside the cluster
backup regime: the H@H network's own replication) + VolSync'd client
state.

## 3. Backups: centrally classed, locally declared

Backup *declarations* live with the app (co-location, as everything
else), but their *policies* are central, so rotation is never an ad-hoc
per-app number:

-   **Retention classes in `conventions.py`** — e.g. `STANDARD` (daily,
    keep 30d), `PRECIOUS` (daily 30d + monthly 12), `BULKY` (weekly,
    keep 4) — each mapping to VolSync/restic `retain` settings plus a
    prune cadence. An app picks a class; nobody writes cron lines or
    retain counts inline. Changing a class is one diff that previews
    across every affected app.
-   **The `backed_pvc(name, size, retention=STANDARD)` helper** emits
    the PVC + `ReplicationSource` + its restic repo secret, with the
    repo path following one bucket-layout convention
    (`<bucket>/volsync/<namespace>/<pvc>`). Because every backup flows
    through the helper, **the program itself is the backup inventory**
    — a report of "everything backed, everything exempted (and why)"
    is derivable from the stacks, no external registry to drift.
-   **The other backup families plug into the same frame**: CNPG barman
    retention expressed from the same class constants; etcd snapshots
    (hourly, ~14d) and the state-backend pg_dump (ci.md §1) are
    physical/CI-side but follow the same bucket-layout and retention
    vocabulary (storage.md §5 stays the authority on targets/RPO).
-   **Freshness is monitored centrally, not per-app**: one vmalert rule
    family over VolSync/barman metrics — *any* backup whose last
    success is older than its class threshold alerts; a new backed_pvc
    is covered automatically because the metric labels come from the
    helper. Restore drills stay quarterly (storage.md §5): a backup
    that hasn't restored recently is assumed broken.

## 4. Shaped patterns (the non-trivial apps)

-   **Dedicated-VIP workload (hath)**: LB Service requesting the
    dedicated VIP + a `CiliumEgressGatewayPolicy` with `egressIP` = the
    secondary private IP + node affinity to the augmented node (cache
    volume locality) + strict CPU limits. All four pieces in the one
    component (architecture.md §3.2). The cache volume is
    `protect=True` and moved-never-recreated (storage.md §3.3).
-   **Split-horizon app (immich)**: one set of pods, two exposures
    (`public_route` to both gateways) — the helper emits both routes,
    the public CNAME, and the LAN rewrite. The immich LAN-direct rule
    (never via the cloud path) is thereby structural.
-   **Bulk-egress (qbittorrent + seedwatch)**: pinned to the homelab
    pool; outbound v6 via the cluster masquerade, inbound v4 via the
    existing UDM forward, inbound v6 pinhole declared as a unifi
    firewall rule *in this component* (co-location again); seedwatch in
    the same namespace, talking to the qbittorrent Service and the NAS
    hardlink paths.
-   **JuiceFS-quarantined app (VPS-successor syncthing + dav)**:
    in-pod juicefs mount (sidecar, no CSI) with 0.5–1 GiB requests,
    SQLite metadata on the cloud node's volume, its own OCI bucket
    (from physical outputs), metadata auto-backup to the bucket
    (storage.md §6).
-   **CNPG-backed app (immich, splitpro, …)**: CNPG `Cluster` on
    local-path + barman to B2, monthly restore drill inherited from the
    legacy discipline (storage.md §5).

## 5. Porting from kluster-code

Most apps port as a rewrite of their kluster-code component onto this
contract — same images, same SealedSecrets (after the sealing-key
restore, cluster-infra.md §1), new exposure/storage declarations. The
migration order and data movement are migration.md's concern; the shape
each app lands in is this document's.
