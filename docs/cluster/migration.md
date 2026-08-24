# Migration Plan: Legacy (kluster-code) → Next Gen

How the workloads and data move from the k3s legacy cluster to the new
one, in dependency-ordered waves, ending with the legacy estate
decommissioned. Target shapes per app are
[declarative/workloads.md](../declarative/workloads.md); this document
owns sequencing, data movement, and teardown.

> **Status**: rewritten 2026-08-22 against the finished design set,
> superseding the early generic draft. Each wave item gets a short
> execution checklist at migration time — this document stays at the
> plan level.

## 0. Standing rules (apply to every step)

1.  **Stop-copy-start per app**: scale down legacy → move data → deploy
    new → verify → cut DNS. Legacy stays intact and re-startable until
    verification passes; rollback is always "scale the legacy app back
    up".
2.  **DNS cutover is per-app and trivial by design**
    (declarative/dns.md §6): the app's records repoint from
    `archvps.hosts` to `kluster.hosts` (or become rewrite-only) the
    moment it verifies. No big-bang DNS day.
3.  **Every absorbed resource updates its old tracker**: gw-config
    (FRR, nspawn, on_boot.d, caddy — the whole repo retires),
    yadm/aconfmgr (qbittorrent unit, quadlets, state-backend compose,
    adguardhome-sync), DNSControl. Each step ends with a
    removal/pointer commit — nothing tracked twice or by nothing.
4.  **NVMe space *and* RAM are interleaved** (nodes.md §4.2): ~85 GB
    free and no spare RAM while both clusters coexist — legacy k3s
    still holds ~16 GiB of the host's 32, so the worker VM **cannot
    start at its 20 GiB end-state size**. It starts at ~60 GB disk /
    **~10 GiB RAM** and grows stepwise: each homelab wave first scales
    down its legacy apps (rule 1 already does this), then bumps the VM
    by the freed amount (a config change + VM reboot, scheduled at the
    head of the wave — 20 GiB must be reached by Wave C's
    immich/jellyfin move). Homelab waves alternate "migrate → delete
    legacy data → grow VM disk/RAM".
5.  **Sealing key first, then rotated last**: the legacy sealed-secrets
    key is restored into the new cluster before any SealedSecret
    manifest is ported (cluster-infra.md §1); it is regenerated and
    the legacy key deleted at decommission (§4, Wave F) —
    restore-for-continuity, rotate-for-hygiene.

## 1. Phase 0 — foundations (before any app moves)

1.  Manual preconditions: OCI tenancy on PAYG (home region choice is
    permanent), the state-backend micro + Postgres + pg_dump timer
    (ci.md §1), Talos OCI image via Image Factory, the homelab
    host-prep aconfmgr change-set (bridge, subvolume/storage pool,
    libvirt SSH identity, NFS exports — physical/homelab-host.md §4). ZeroTier
    Central config is Pulumi-managed (architecture.md §5.3); the
    legacy `10.42.0.0/24`-via-VPS managed route is deleted in Wave F.
    **ZT's home-LAN routes are net-new** (today ZT and the LANs are
    not connected at all): the UDM container deploys via an
    operator-local run over the LAN, routes are added, and only after
    the flow-rules verification does CI's per-run ZT join become
    load-bearing (physical/gateway.md §2.5).
2.  `physical` up: 3× A1 (A1 capacity confirmed at creation), worker VM
    (60 GB), NLB, UDM FRR/estate, B2. `dns` up: zones + estate records
    imported wholesale (records still pointing at `archvps.hosts`; the
    import census also drops dead weight — `abacus.hosts`, its ZT
    entry, jupyter/mc records).
3.  `k8s-base` up; sealing key restored; backup-freshness alerts
    live. (Talos-level checks that need no CNI — etcd fsync, A1
    capacity, talosctl→homelab via cloud endpoints — may run before
    this step; everything Cilium-shaped cannot.)
4.  **Verification gate** — the consolidated checklist
    (physical.md §6), run **after `k8s-base`** because most items
    exercise Cilium: LB-IPAM pool with the on-the-wire node IPs; NLB
    dual-stack + source preservation; Egress Gateway under the
    routing mode + reserved-IP NAT; MTU over KubeSpan; the security
    verifications (pod→IMDS denied, bogus-BGP rejected, ExternalAuth
    fail-closed) — alongside the physical-only items (etcd fsync;
    VFIO capability on a scratch VM). **No app migrates until this
    gate passes** — every item is cheaper to fix on an empty cluster.

## 2. Waves

Ordered by dependency and risk; cloud first (no NVMe contention, and
the VPS empties progressively):

-   **Wave A — cloud pool**: authelia (SSO gates everything else) →
    **blog/static sites** (prerequisite: blog CI publishes the built
    branch; cluster side is static server + git-sync, workloads.md §4 —
    the rsync `deploy:` config and the SSH pinhole retire with it) →
    splitpro (small CNPG — **prerequisite: the multi-arch pgcron image
    from the ported images.yml**, ci.md §4) → matrix
    (continuwuity, rsync its local-path state; its `.well-known`
    delegation rides the blog instance, workloads.md §4, so it is
    already serving by this point) → cloud syncthing/dav successor
    (**no data copy**: fresh per-app JuiceFS bucket, the replica
    reseeds itself from syncthing-nas over the syncthing protocol)
    with **stdiscosrv** alongside it (raw TCP `public_port`,
    workloads.md §5).
-   **Wave B — homelab VM, light**: monitoring (VictoriaMetrics fresh —
    no TSDB migration; legacy prometheus kept read-only until its
    retention ages out), golinks, emailproxy, spoolman, exim
    (workloads.md §5), the doors static sites (NAS-sourced,
    workloads.md §4 — their content moves from the VPS hostPath onto a
    NAS share once), the haos.ucw LAN-device backend (workloads.md §4),
    thread-dashboard (quadlet → cluster).
-   **Wave C — homelab heavy + the GPU window**: the vfio-pci cutover
    (drain → bind → hostdev → reboot, physical/homelab-host.md §3) runs at the head
    of this wave, then immich (CNPG via the drilled barman restore; NAS
    media PVs re-point in place; ML/thumbnail caches re-derive) and
    jellyfin+shoko (NAS re-point + config PVC copy; **verify the
    TVs' path — IoT → media-VIP allow — before its DNS re-points**,
    physical/gateway.md §4.2). syncthing-nas
    re-points its NAS PV.
-   **Wave D — host-native onboarding**: qbittorrent-nox
    (`/var/lib/qBittorrent` profile copy; verify seedwatch category
    paths and hardlink counts survive; outbound-v6 via masquerade
    first, inbound pinhole later) + seedwatch together.
-   **Wave E — hath, deliberately last of the apps**: hath is the
    highest-stakes workload (global-archive data, IP re-registration,
    strict downtime cap), so it moves only after the cluster has run
    everything else stably — the dedicated-VIP path, EGW, and the
    cloud pool all long proven by then. Execution: pre-provision the
    protected cache volume, rsync the 50 Gi cache warm ahead of time,
    then a short window for the final delta + client-state copy,
    dedicated VIP live, re-register.
-   **Wave F — decommission** (§4).

Explicitly **not** migrating: AdGuard alice/bob (stay on the UDM, now
Pulumi-managed), HAOS (adopted in place), the NAS role, the legacy
state backend (serves kluster-code until F), and **dmarc-check** (stays
a host timer using the host's claude credentials, nodes.md §4.1 —
in-cluster adoption deferred until the cluster is stable, and a
different approach may supersede it).

**Retired outright, never lands in the new cluster** (decided
2026-08-23): the dormant legacy apps — **mc** (Minecraft; its world/map
PVC holds real data: tidy it and archive to the NAS as fixed assets
*before* the PV is deleted in the census below), **ukulele**, the old
**bt** stack (superseded by the host qbittorrent, which *does*
migrate — Wave D), and the **genshin** daily cronjob. Their config
flags, dead DNS records (the jupyter/mc leftovers, dns.md §2 — the
`game`/`games` names stay, they are doors), and images drop during the
import census; resurrection, if ever wanted, is
git history.

## 3. Data movement, by storage kind

| Kind | Technique |
| --- | --- |
| CNPG databases | barman restore into the new cluster — the drilled path, not dump/restore reinvention |
| Legacy local-path PVCs | one-off rsync/tar over SSH into the target PVC (VolSync takes over *after* landing) |
| NAS-backed data | **no movement** — PVs re-point at the same datasets; the NAS backup regime is untouched |
| Legacy shared-JuiceFS data (VPS syncthing/dav) | **no copy** — the new replica reseeds via the syncthing protocol from syncthing-nas |
| hath cache | rsync warm + short final delta inside its downtime window (preserved, not backed — workloads.md §2) |
| Monitoring TSDB | not migrated; retention overlap instead |

**Retained-PV census** (storage.md §3.3): while touching each legacy
app, its retained-PV directories are identified and either mapped to
the migration or deleted — the one-time cleanup of the `Retain`-era
orphans, finishing with the disk-reclaim that feeds rule 0.4.

## 4. Wave F — decommission checklist

1.  Legacy VPS: after Wave E verifies (hath stable on its new IP for a
    comfortable soak — the VPS necessarily outlives every other
    migration for exactly this reason), tear down remaining k3s residue
    and **cancel the Vultr instance** (the $30/mo baseline ends). `archvps.hosts` and
    remaining DNSControl entries deleted; DNSControl repo archived with
    a pointer commit.
2.  Homelab host: k3s uninstalled after Waves C/D; freed NVMe grows the
    worker VM to its 100+ GB target; JuiceFS redis/mount residue gone
    with k3s; `lan.ucw.phd` entries emptied (dns.md §4).
3.  Trackers: gw-config repo retired (provider owns the device);
    adguardhome-sync unit removed; qbittorrent/quadlet units removed
    from yadm/aconfmgr; the legacy state backend's Postgres stops last,
    once kluster-code needs no further `pulumi` operations.
4.  **Sealing-key rotation**: with every app migrated, all
    SealedSecrets are re-sealed against a freshly generated key and
    the restored legacy key (rule 0.5) is deleted from the cluster.
    The legacy key exists in years of backups; leaving it valid would
    let any old backup copy decrypt future secrets
    (cluster/security-audit.md).
5.  Success criteria: every app serving from the new cluster with green
    backup-freshness alerts; orphan audit reports zero; legacy spend
    $0; the legacy sealing key retired; the only homelab standing
    services are the ones nodes.md §4.1 lists as staying.
