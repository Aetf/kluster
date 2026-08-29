# Declarative Design: Workloads (the `apps` Stack)

The per-app pattern: one Python component per application, declaring everything
the app needs — workload, storage, exposure, DNS, secrets, backups, placement —
so that "deploy an app" is one reviewable diff in one place. This is the top
stack of [README.md](README.md) §1 and the home of ~80–90% of all future
changes.

> **Status**: designed 2026-08-22. Not implemented.

## 1. The component contract

Every app is a `Component` subclass (putils, RFC-001) that owns:

| Concern | Declared as | Governed by |
| --- | --- | --- |
| Namespace | created by the component (never shared) | README.md §1 |
| Workload | Deployment/StatefulSet with **honest requests/limits** — CPU limits are mandatory on anything scheduled to the cloud pool (etcd shares those cores, nodes.md §1); memory requests sized from evidence, not idle numbers (the JuiceFS-sidecar lesson, storage.md §6) | nodes.md §§1, 4.4 |
| Storage | by the two-axis selection in §2 (performance × persistence; fixed assets → NAS/object) | §2, storage.md §2 |
| Backup | declared through the `backed_pvc` helper with a **retention class** — never ad-hoc schedules (§3) | §3, storage.md §3.1 |
| Exposure | pool label + route kind per the routing matrix, via the helpers (`public_route`/`public_port`/`lan_route`); `public_port` alone also emits the NLB listener + security rule for its port (dns.md §5); `auth=True` adds the ExternalAuth filter → Authelia for apps without native auth (cluster-infra.md §2); `iot_reachable=True` attaches `media-gw` — the explicit "IoT may reach this" decision (jellyfin; physical/gateway.md §4.2) | architecture.md §3.6, dns.md §3 |
| DNS | emitted by the same helpers (CNAME to anchor); the split-horizon rewrite the route implies is applied by the `dns` stack from the same declaration, not by this one (dns.md §3) | dns.md |
| Secrets | SealedSecret first choice, `template.data` pattern | cluster-infra.md §1.1 |
| Placement | scheduling constraints only: site pool (cloud/homelab), the augmented node for dedicated-VIP workloads, GPU resource requests | architecture.md §3.6 |
| Network policy | per-namespace default-deny + explicit allows, part of the component | architecture.md §4.1 |
| Monitoring | scrape/dashboard labels per the legacy conventions (`release`, `grafana_dashboard`) so VictoriaMetrics/grafana pick them up | cluster-infra.md §1 |

What a component may **not** do: hostPort, hostNetwork, `externalIPs`
(architecture.md §6.6); cross-namespace reach-ins; unbudgeted sidecars;
cluster-scoped resources (those belong to `k8s-base` and go through its
closed-list rule).

Namespaces are created with **Pod Security `restricted` enforced** by
default — non-root, no added capabilities, RuntimeDefault seccomp
(part of containing third-party binaries on the combined CP+ingress
nodes, architecture.md §4.1). An exception is a declared component
parameter with the reason on record — the same discipline as
`backup=None`; the JuiceFS sidecar namespace (§4) is the one current
holder.

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
    helper. Restore drills run as unattended automations — monthly
    VolSync spot-restores among them (operations.md §4): a backup
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
    UDM peer-port forward, inbound v6 via a pinhole — both declared
    in the **`physical` stack** with the rest of the UniFi resources
    (the co-location exception, architecture.md §5.1: gateway rules
    follow the gateway's credential tier; this component documents
    the flows and points there); seedwatch in
    the same namespace, talking to the qbittorrent Service and the NAS
    hardlink paths. The Web UI **keeps its public entrance** (decided
    2026-08-23): split-horizon exposure — Cloudflare-proxied public
    hostname behind Authelia forward-auth on `internet-gw`, plus the
    LAN rewrite to `lan-gw` — continuing the legacy `bt.` entry rather
    than going rewrite-only.
-   **JuiceFS-quarantined app (VPS-successor syncthing + dav)**:
    in-pod juicefs mount (sidecar, no CSI) with 0.5–1 GiB requests,
    SQLite metadata on local-path on the node's boot volume
    (`backup=None` — the auto metadata dump to the bucket plus
    syncthing reseed are the recovery, storage.md §6), its own OCI
    bucket (from physical outputs). Implementation fact: an in-pod FUSE mount needs
    `/dev/fuse` + `SYS_ADMIN` (a privileged-PSS namespace) — this app
    is the one standing exception to the unprivileged default, on
    record here rather than discovered at deploy time.
-   **CNPG-backed app (immich, splitpro, …)**: CNPG `Cluster` on
    local-path + barman to B2, monthly restore drill inherited from the
    legacy discipline (storage.md §5). **Major-version policy
    (2026-08-24)** — minors ride the normal renovate image-bump flow;
    majors use CNPG's **declarative offline in-place upgrade**
    (operator ≥1.26 — the cluster-infra.md §1 floor): bumping the
    `imageName` major shuts the cluster down, runs
    `pg_upgrade --check` then `--link`, and restarts. Constraints on
    record: our operands are self-built images (ci.md §4), so the
    new-major image carrying the *same extensions* (pg_cron, vchord)
    must be built and published **before** the deploy PR — extension
    availability for the new major is the real gate; and pg_upgrade
    requires the same distro base across the bump (a Debian-release
    change must never share a PR with a Postgres major). Major pins
    are **never automerged**: they arrive as ordinary human-reviewed
    deploy PRs, preceded by a verified-fresh barman backup — the
    drilled restore is the rollback.
-   **Static-site template (one component, two content sources)**: a
    single `StaticSite` component — stock static server, multi-vhost
    (one instance serves several hostnames), `public_route` per zone —
    parameterized by where the content comes from:
    -   **Git source** (blog: the apex/www of three zones): the blog
        repo *drives* publishing — its CI pushes the generated
        `public/` to a **built branch** (hexo's git deployer, the
        repo's original GitHub-Pages shape); the cluster side adds a
        **git-sync sidecar** pulling that branch into an emptyDir.
        Decided 2026-08-22 over a content-baked image (content is
        data, not an artifact — no image rebuild per post). Properties:
        **zero cross-system credentials** (the cluster pulls; blog CI
        holds nothing of the cluster's), push-to-live in ~a sync
        interval, rollback = git revert on the built branch, no PVC
        (truly stateless → 2 replicas across cloud nodes, ingress HA
        for the blog). A private repo would work too (git-sync + a
        read-only deploy-key SealedSecret) — noted, not currently used.
    -   **NAS source** (the "doors" instance: `door-jiahui` on the
        jiahui.love apex, `door-shiyu`, `door` — three vhosts, one
        instance): frozen low-traffic assets whose content must not
        live in a public repo (decided 2026-08-23). They are fixed
        assets, and fixed assets live on the NAS (§2): a read-only
        NAS-backed volume, pod pinned to the homelab pool, ingress
        still via `internet-gw` (cloud → KubeSpan → homelab). Editing
        = dropping files on the existing NAS share — no deploy chain
        at all, which beats both a public repo and an inconvenient
        image/hostPath arrangement.

    The **matrix `.well-known` delegation** rides the blog instance:
    `/.well-known/matrix/{server,client}` become two static files in
    the blog source repo (hexo passthrough into the built branch —
    their content is public by definition), with the CORS header added
    by an HTTPRoute `ResponseHeaderModifier` on the apex route — no
    server-config magic (the legacy shape was two nginx
    `extraConfig` fixed-responses).

    Retired with the migration: the rsync deployer + SSH pinhole, the
    hostPath webroots (blog *and* doors), kluster-code's nginx
    component, and the blog repo's rsync `deploy:` config (old-tracker
    rule).
-   **LAN-device backend (haos.ucw → HAOS)**: the public entrance to a
    device that is deliberately *not* in the cluster
    (architecture.md §6.8). Shape: a selectorless Service + manually
    declared EndpointSlice pointing at the HAOS LAN IP, fronted by an
    `internet-gw` HTTPRoute; traffic flows cloud ingress → KubeSpan →
    homelab node → LAN. This replaces the legacy VPS proxy entry and
    is the reusable pattern for any future LAN-device backend.
    Cutover note: HA's `trusted_proxies` must gain the new
    X-Forwarded-For source (the gateway pods' CIDR) at migration, or
    client IPs — and HA's login rate-limiting/ip_ban — get judged
    against the proxy address.

## 5. Porting from kluster-code

Most apps port as a rewrite of their kluster-code component onto this
contract — same images, same SealedSecrets (after the sealing-key
restore, cluster-infra.md §1), new exposure/storage declarations. The
migration order and data movement are migration.md's concern; the shape
each app lands in is this document's.

Two quiet straight-ports, on record so they aren't forgotten: **exim**
(the MTA relay behind immich/splitpro outbound mail — a plain small
app, ClusterIP SMTP; it smarthosts via smtp.gmail.com:587, so port-25
egress reachability constrains nothing and placement is free) and
**stdiscosrv** (syncthing's discovery server — raw TCP via
`public_port`; it terminates its own TLS on 8443, no gateway
involvement).
